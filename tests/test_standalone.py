"""Offline packaging/CLI checks; not a claim that every platform is available."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from single.common import LocalError, final_destination, single_url, probe_file
from single.engine import Session, install_upstream_session
from single.storage import check_storage
from scripts.download import operation_lock


class StandaloneTests(unittest.TestCase):
    def test_dependencies_load_without_aios(self):
        for name in ('single.engine', 'single.tasks', 'single.login', 'single.wechat',
                     'app.downloader', 'app.douyin', 'app.xiaohongshu', 'app.kuaishou'):
            module = importlib.import_module(name)
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT))

    def test_single_link_platforms(self):
        links = {
            'youtube': 'https://www.youtube.com/watch?v=YE7VzlLtp-4',
            'douyin': 'https://v.douyin.com/Example/',
            'kuaishou': 'https://v.kuaishou.com/Example',
            'xiaohongshu': 'https://xhslink.cn/o/Example',
            'wechat': 'https://weixin.qq.com/sph/Example123',
        }
        for expected, link in links.items():
            with self.subTest(platform=expected):
                self.assertEqual(single_url(link)[1].value, expected)

    def test_reject_batch_and_invalid_links(self):
        for link in ('', 'not-a-url', 'https://www.youtube.com/playlist?list=example',
                     'https://www.douyin.com/user/example', 'https://example.com/video',
                     'https://youtu.be/one https://youtu.be/two'):
            with self.subTest(link=link), self.assertRaises(LocalError):
                single_url(link)

    def test_no_credentials_read_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('http.cookiejar.MozillaCookieJar.load', side_effect=AssertionError('unexpected credentials read')):
                session = Session({'login': {}}, 'youtube', Path(temp))
                self.assertIsNone(session.cookiefile)

    def test_cookie_hooks_bound_to_own_session(self):
        # Patch installation happens only in this isolated test subprocess.
        code = '''
from pathlib import Path
from single.engine import Session, install_upstream_session
import app.xiaohongshu as xhs
import app.douyin as dy
import app.douyin_signing as signing
s = Session({'login': {}}, 'douyin', Path('.'))
install_upstream_session(s)
assert dy.extract_cookies_from_browser('chrome') is s.jar
assert xhs._extract_chrome_cookies() is s.jar
assert signing.extract_cookies_from_browser('chrome') is s.jar
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))

    def test_output_write_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(check_storage(temp)['ok'])
            first = final_destination(temp, 'youtube', '示例', '.mp4', '20260101_000000')
            first.write_bytes(b'original')
            second = final_destination(temp, 'youtube', '示例', '.mp4', '20260101_000000')
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b'original')

    def test_lock_blocks_duplicate_then_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            with operation_lock(Path(temp)):
                with self.assertRaises(LocalError):
                    with operation_lock(Path(temp)):
                        pass
            with operation_lock(Path(temp)):
                pass

    def test_corrupt_video_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / 'broken.mp4'
            broken.write_bytes(b'not-a-video' * 1024)
            with self.assertRaises(LocalError):
                probe_file(broken)

    def test_cli_runs_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/download.py'), 'check'],
                                    cwd=temp, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])

    def test_cli_refuses_invalid_input_without_echoing(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/download.py'), 'download'],
                                input='not-a-video', capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['ok'])
        self.assertNotIn('not-a-video', result.stdout)


if __name__ == '__main__':
    unittest.main()
