from __future__ import annotations

import contextlib
import copy
import json
import os
import subprocess
import sys
import time
from http.cookiejar import MozillaCookieJar, LoadError
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from single.common import DATA, DOMAINS, NO_WINDOW, LocalError, error_message, probe_file, single_url, cookie_domain_allowed


def emit(**payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


class QuietLogger:
    def debug(self, *_): pass
    def info(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def watermark_required(info):
    candidates = [info, *(info.get('requested_formats') or [])]
    return any(
        item.get('has_watermark') in (True, 1) or item.get('is_watermark') in (True, 1)
        or '/playwm/' in str(item.get('url', ''))
        for item in candidates if isinstance(item, dict)
    )


class Session:
    def __init__(self, config, platform, stage, session_root=None):
        self.proxy = config.get('proxy', '')
        mode = config.get('network_mode', 'auto')
        if mode == 'direct' or (mode == 'auto' and platform != 'youtube'):
            self.proxy = ''
        self.cookiefile = None
        self.watermark = '水印情况待确认'
        self.consent = False
        self.jar = MozillaCookieJar()
        cookie_path = DATA / 'cookies' / f'{platform}.txt'
        if config.get('login', {}).get(platform):
            if not cookie_path.is_file():
                raise LocalError('cookie_unavailable', '尚未保存该平台的本机登录数据，请先打开本地登录窗口并保存。')
            original = MozillaCookieJar(str(cookie_path))
            try:
                original.load(ignore_discard=True)
            except (OSError, LoadError) as exc:
                raise LocalError('cookie_unavailable', '本机登录文件无法读取，请重新登录并保存。') from exc
            for cookie in original:
                domain = cookie.domain.lstrip('.')
                if cookie_domain_allowed(platform, domain):
                    self.jar.set_cookie(copy.copy(cookie))
            if list(self.jar):
                session_dir = Path(session_root or DATA / 'sessions') / stage.name
                session_dir.mkdir(parents=True, exist_ok=True)
                self.cookiefile = str(session_dir / 'cookies.txt')
                self.jar.save(self.cookiefile, ignore_discard=True)
            else:
                raise LocalError('login_required', '保存的登录数据已过期或没有该平台的有效 Cookie，请重新登录并保存。')

    def confirm_watermark(self, info):
        if not watermark_required(info):
            return
        self.watermark = '平台标记为有水印源（用户已同意）'
        if not self.consent:
            emit(status='awaiting', message='当前选中的视频源带平台水印，是否继续下载？', watermark='有平台水印')
            if sys.stdin.readline().strip() != 'continue':
                raise LocalError('cancelled', '已取消有水印源下载。')
            self.consent = True

    def options(self):
        options = {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
                   'socket_timeout': 20, 'retries': 1, 'fragment_retries': 1,
                   'extractor_retries': 1, 'file_access_retries': 1,
                   'noplaylist': True, 'skip_unavailable_fragments': False,
                   'js_runtimes': {'deno': {}}, 'proxy': self.proxy,
                   'concurrent_fragment_downloads': 8}
        if self.cookiefile:
            options['cookiefile'] = self.cookiefile
        return options


class YouTubeAdapter:
    def download(self, url, stage, session):
        outputs = []
        def progress(data):
            if data.get('status') == 'downloading':
                total = data.get('total_bytes') or data.get('total_bytes_estimate')
                done = data.get('downloaded_bytes', 0)
                emit(status='downloading', message='正在下载视频或音频流', downloaded=done,
                     total=total, percent=min(100, done * 100 / total) if total else None,
                     speed=data.get('speed'), eta=data.get('eta'))
        def post(data):
            if data.get('status') == 'started':
                emit(status='merging', message='正在合并音画或封装，未重新编码', percent=None)
        options = {**session.options(), 'format': 'bestvideo*+bestaudio/best',
                   'merge_output_format': 'mp4/mkv', 'outtmpl': str(stage / 'video.%(ext)s'),
                   'windowsfilenames': True, 'overwrites': False,
                   'progress_hooks': [progress], 'postprocessor_hooks': [post],
                   'post_hooks': [outputs.append]}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info or info.get('_type') in ('playlist', 'multi_video'):
                raise LocalError('link', '链接没有返回唯一视频，已停止批量展开。')
            if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
                raise LocalError('link', '本版不下载直播或预告。')
            if info.get('has_drm'):
                raise LocalError('access', '视频受 DRM 保护，已停止。')
            session.confirm_watermark(info)
            emit(status='downloading', message='已解析，开始下载最高可获取画质', title=info.get('title'), watermark=session.watermark, percent=None)
            ydl.process_ie_result(info, download=True)
        paths = [Path(p) for p in outputs if Path(p).suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov') and Path(p).is_file()]
        if len(paths) != 1:
            raise LocalError('integrity', '下载引擎未返回唯一完整视频文件。')
        return paths[0], info.get('title') or '未命名视频', info.get('duration')


def install_upstream_session(session):
    """Limit upstream browser-cookie hooks to this worker's explicit local session."""
    import app.downloader as downloader
    import app.xiaohongshu as xhs
    import app.douyin as dy
    import app.douyin_signing as signing
    import app.kuaishou as ks
    from playwright.sync_api import BrowserType

    def local_cookies(*args, **kwargs):
        return session.jar

    class LocalYoutubeDL(YoutubeDL):
        def __init__(self, params=None, *args, **kwargs):
            params = dict(params or {})
            use_cookies = bool(params.pop('cookiesfrombrowser', None))
            params.update(proxy=session.proxy, logger=QuietLogger(), quiet=True, no_warnings=True,
                          retries=1, fragment_retries=1, extractor_retries=1,
                          skip_unavailable_fragments=False, noplaylist=True,
                          concurrent_fragment_downloads=8)
            if use_cookies and session.cookiefile:
                params['cookiefile'] = session.cookiefile
            super().__init__(params, *args, **kwargs)

        def urlopen(self, request):
            session.confirm_watermark({'url': str(getattr(request, 'url', request))})
            return super().urlopen(request)

        def process_info(self, info):
            if info.get('has_drm') or info.get('is_live'):
                raise LocalError('access', '不支持 DRM 视频或直播。')
            session.confirm_watermark(info)
            return super().process_info(info)

    for module in (xhs, dy, signing):
        if hasattr(module, 'extract_cookies_from_browser'):
            module.extract_cookies_from_browser = local_cookies
    original_cookie_converter = signing._cookie_jar_to_playwright
    def session_cookies(jar):
        if jar is not session.jar:
            raise LocalError('local', '抖音解析器请求了非本次任务的会话，已停止。')
        return original_cookie_converter(jar) if list(jar) else []
    signing._cookie_jar_to_playwright = session_cookies
    # Match the upstream anonymous smoke adapter, without weakening response checks.
    from urllib.request import ProxyHandler
    original_opener = signing.build_opener
    def local_opener(*handlers):
        proxies = {'http': session.proxy, 'https': session.proxy} if session.proxy else {}
        return original_opener(ProxyHandler(proxies), *handlers)
    signing.build_opener = local_opener
    xhs._extract_chrome_cookies = local_cookies
    ks._extract_chrome_cookies = local_cookies
    original_kuaishou = downloader.discover_kuaishou
    cached = {}
    def kuaishou_once(url, **kwargs):
        entry = cached.get(url)
        if entry and time.monotonic() - entry[0] < 60:
            return entry[1]
        from single.kuaishou_page import discover
        result = discover(url, session)
        if result is None:
            result = original_kuaishou(url, **kwargs)
        cached[url] = (time.monotonic(), result)
        return result
    downloader.discover_kuaishou = kuaishou_once
    downloader.YoutubeDL = LocalYoutubeDL
    xhs.YoutubeDL = LocalYoutubeDL
    original_launch = BrowserType.launch

    def launch(browser_type, *args, **kwargs):
        if session.proxy:
            kwargs['proxy'] = {'server': session.proxy, 'bypass': 'localhost,127.0.0.1'}
        else:
            kwargs['args'] = [*kwargs.get('args', []), '--no-proxy-server']
        return original_launch(browser_type, *args, **kwargs)
    BrowserType.launch = launch

    original_parse = downloader.parse_xhs_note
    def parse_video(*args, **kwargs):
        result = original_parse(*args, **kwargs)
        if not result[0].videos:
            raise LocalError('link', '这条小红书作品没有可确认的视频源；本版不下载图集或封面。')
        return result
    downloader.parse_xhs_note = parse_video


class UpstreamAdapter:
    def download(self, url, stage, session):
        install_upstream_session(session)
        from app.downloader import MediaDownloader, DownloaderConfig
        from app.models import SourceKind, MediaType

        class SingleDownloader(MediaDownloader):
            def discover(self, url, platform, kind, **kwargs):
                if platform.value == 'kuaishou' and kind in (SourceKind.ITEM, SourceKind.SHORT_LINK):
                    return self._discover_kuaishou(url, kwargs.get('should_cancel') or (lambda: False))
                if kind == SourceKind.SHORT_LINK:
                    resolved, _ = self._resolve_short_url(url)
                    checked_url, checked_platform, checked_kind = single_url(resolved)
                    if checked_kind != SourceKind.ITEM or checked_platform != platform:
                        raise LocalError('link', '分享短链接未指向同平台的单条视频，已停止。')
                    return self.discover(checked_url, checked_platform, checked_kind, **kwargs)
                if kind != SourceKind.ITEM:
                    raise LocalError('link', '本版不采集主页或播放列表。')
                return super().discover(url, platform, kind, **kwargs)

        def progress(event):
            if event.media_type == MediaType.IMAGE:
                raise LocalError('link', '这是图文作品，本版只下载单条视频。')
            state = 'parsing' if event.event == 'probing' else 'downloading'
            if event.event == 'postprocessing':
                state = 'merging'
            fields = {'status': state, 'message': {'parsing': '正在解析和核验平台视频源', 'downloading': '正在下载视频', 'merging': '正在合并音画'}[state]}
            if event.progress:
                p = event.progress
                # Upstream also transfers quality-probe bytes under "probing".
                if p.downloaded_bytes:
                    fields.update(status='downloading', message='正在下载并核验视频源')
                fields.update(downloaded=p.downloaded_bytes, total=p.total_bytes, percent=p.percent, speed=p.speed_bytes_per_second, eta=p.eta_seconds)
            if event.title:
                fields['title'] = event.title
            emit(**fields)

        engine = SingleDownloader(DownloaderConfig(cookie_browser='chrome', cookie_profile='LocalVideo',
                                   socket_timeout_seconds=20, retries=1, fragment_retries=1, filename_limit=64), discovery_callback=progress)
        url, platform, kind = single_url(url)
        discovered = engine.discover(url, platform, kind)
        if len(discovered.items) != 1:
            raise LocalError('link', '平台未返回唯一作品，本版不执行批量任务。')
        item = discovered.items[0]
        if item.media_type == MediaType.IMAGE:
            raise LocalError('link', '这是图文作品，本版仅支持单条视频。')
        outcome = engine.download_item(item, platform, stage, callback=progress)
        paths = [Path(p) for p in outcome.output_paths]
        if len(paths) != 1 or paths[0].suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov'):
            raise LocalError('integrity', '平台未返回唯一视频文件，不能标记为成功。')
        metadata = item.metadata.get('douyin_item_media') or {}
        duration = metadata.get('duration_ms')
        return paths[0], outcome.title or item.title, duration / 1000 if duration else None


class DouyinAdapter(UpstreamAdapter): pass
class KuaishouAdapter(UpstreamAdapter): pass
class XiaohongshuAdapter(UpstreamAdapter): pass
from single.wechat import WeChatAdapter
ADAPTERS = {'youtube': YouTubeAdapter, 'douyin': DouyinAdapter, 'kuaishou': KuaishouAdapter, 'xiaohongshu': XiaohongshuAdapter, 'wechat': WeChatAdapter}


def run():
    request = json.loads(sys.stdin.readline())
    stage = Path(request['stage']).resolve()
    try:
        if not stage.is_dir():
            raise LocalError('local', '下载临时目录不可用。')
        url, platform, _ = single_url(request['url'])
        session = Session(request['config'], platform.value, stage, request.get('session_root'))
        emit(status='parsing', message='正在解析视频', watermark=session.watermark)
        path, title, duration = ADAPTERS[platform.value]().download(url, stage, session)
        path = path.resolve()
        if not path.is_relative_to(stage) or not path.is_file():
            raise LocalError('integrity', '下载文件路径不在本次任务目录中。')
        emit(status='verifying', message='正在检查音画、时长和完整解码', percent=None)
        facts = probe_file(path, duration)
        from single.verification import decode_file
        validation = decode_file(path, facts, emit)
        # Remux only; preserve unsupported containers without lowering video quality.
        if path.suffix.lower() != '.mp4':
            emit(status='merging', message='尝试无转码封装为 MP4', percent=None)
            remux = stage / 'remux.mp4'
            result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', str(remux)], capture_output=True, timeout=300, creationflags=NO_WINDOW)
            if result.returncode == 0:
                facts = probe_file(remux, facts['duration'])
                path = remux
            else:
                remux.unlink(missing_ok=True)
        emit(status='ready', path=str(path), title=title, facts=facts, validation=validation, watermark=session.watermark)
    except Exception as exc:
        code, message = error_message(exc)
        emit(status='failed', code=code, message=message, exception_type=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    raise SystemExit(run())
