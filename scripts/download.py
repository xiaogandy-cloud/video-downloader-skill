"""Thin standalone CLI over the unchanged, previously deployed download engine."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PLATFORMS = ('youtube', 'douyin', 'kuaishou', 'xiaohongshu', 'wechat')


def output(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


@contextlib.contextmanager
def operation_lock(folder):
    """OS lock released on exit/crash; never touch another installation's task."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / 'operation.lock').open('a+b')
    locked = False
    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            from single.common import LocalError
            raise LocalError('busy', '此 Skill 已有下载或登录操作进行中，请等待完成。') from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def check():
    modules = ('yt_dlp', 'playwright', 'pydantic', 'httpx')
    dependencies = {name: importlib.util.find_spec(name) is not None for name in modules}
    binaries = {name: bool(shutil.which(name)) for name in ('ffmpeg', 'ffprobe')}
    # The engine also adds the interpreter directory to PATH for subprocesses.
    binaries['deno'] = bool(shutil.which('deno') or (Path(sys.executable).parent / ('deno.exe' if os.name == 'nt' else 'deno')).is_file())
    ok = sys.version_info >= (3, 12) and all(dependencies.values()) and all(binaries.values())
    output({'ok': ok, 'python': sys.version.split()[0], 'dependencies': dependencies,
            'executables': binaries, 'message': '依赖就绪；这不是平台下载成功验证。' if ok else '请按 README 安装缺少的依赖。'})
    return 0 if ok else 1


def login(args):
    from single.common import DATA, NO_WINDOW, read_json
    with operation_lock(DATA):
        (DATA / f'login-{args.platform}.json').unlink(missing_ok=True)
        child = subprocess.Popen([sys.executable, '-m', 'single.login'], cwd=ROOT,
                                 stdin=subprocess.PIPE, creationflags=NO_WINDOW)
        try:
            child.stdin.write(json.dumps({'platform': args.platform, 'proxy': args.proxy}).encode('utf-8'))
            child.stdin.close()
            print('请在专用 Chrome 窗口手动登录。完成后回到此终端按回车保存；不要发送密码或 Cookie。', file=sys.stderr)
            input()
            # Avoid a premature save signal: the existing login worker clears it on startup.
            deadline = time.monotonic() + 45
            while child.poll() is None and time.monotonic() < deadline:
                state = read_json(DATA / f'login-{args.platform}.json', {}).get('state')
                if state in ('saved', 'failed', 'closed'):
                    break
                if state == 'waiting':
                    (DATA / f'login-{args.platform}.save').touch()
                    break
                time.sleep(.2)
            child.wait(timeout=620)
            state = read_json(DATA / f'login-{args.platform}.json', {})
            ok = state.get('state') == 'saved' and state.get('has_cookies') is True
            output({'ok': ok, 'status': state.get('state', 'failed'), 'message': state.get('message', '未保存登录。')})
            return 0 if ok else 1
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def download(args):
    from single.common import DATA, LocalError, single_url
    from single.tasks import TaskManager
    from single.windows_context import restricted_process

    if restricted_process() is not False:
        raise LocalError('restricted_environment', '当前执行权限受限或无法核实，请在正常用户终端运行，不自动提权。')
    text = args.url
    if text is None:
        if sys.stdin.isatty():
            text = input('视频链接或分享文字：')
        else:
            text = sys.stdin.read(12001)
    _, platform, _ = single_url(text)
    if platform.value == 'wechat' and not args.allow_wechat_resolver:
        raise LocalError('confirmation_required', '视频号链接需发送到腾讯官方元宝解析。确认后使用 --allow-wechat-resolver；首次需 login wechat。')
    folder = Path(args.output).expanduser().resolve()
    config = {'download_dir': str(folder), 'folder_confirmed': True,
              'network_mode': 'auto', 'proxy': args.proxy,
              'login': {platform.value: args.use_login}}
    with operation_lock(DATA):
        manager = TaskManager()
        try:
            manager.start(text, config)
            previous = None
            while manager.thread.is_alive():
                state = manager.snapshot()
                key = (state.get('status'), state.get('message'), round(state.get('percent') or 0))
                if key != previous:
                    print(json.dumps({'status': key[0], 'message': key[1], 'percent': state.get('percent')}, ensure_ascii=False), file=sys.stderr, flush=True)
                    previous = key
                if state.get('status') == 'awaiting':
                    if args.accept_watermark:
                        manager.proceed()
                    else:
                        manager.shutdown()
                        output({'ok': False, 'status': 'confirmation_required', 'code': 'watermark_consent',
                                'message': '来源带平台水印，尚未同意下载。确认后使用 --accept-watermark。'})
                        return 2
                time.sleep(.4)
            manager.thread.join()
            result = manager.snapshot()
            valid = (result.get('status') == 'success' and Path(result.get('path') or '').is_file()
                     and result.get('validation', {}).get('full_decode') is True)
            result['ok'] = bool(valid)
            output(result)
            return 0 if valid else 1
        finally:
            manager.shutdown()


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='单条视频下载 Skill；不依赖 AIOS 服务。')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', help='仅检查依赖，不联网下载')
    item = sub.add_parser('download', help='下载一条视频，优先通过标准输入传入分享文字')
    item.add_argument('--url', help='可选；带签名的私有链接请改用标准输入')
    item.add_argument('--output', default=str(Path.cwd() / 'downloads'))
    item.add_argument('--proxy', default='', help='可选代理；不自动继承原 AIOS 配置')
    item.add_argument('--use-login', action='store_true', help='仅使用本 Skill 手动登录保存的会话')
    item.add_argument('--accept-watermark', action='store_true', help='用户明确接受来源水印')
    item.add_argument('--allow-wechat-resolver', action='store_true')
    auth = sub.add_parser('login', help='可选，打开专用 Chrome 手动登录')
    auth.add_argument('platform', choices=PLATFORMS)
    auth.add_argument('--proxy', default='')
    args = parser.parse_args()
    try:
        return check() if args.command == 'check' else login(args) if args.command == 'login' else download(args)
    except KeyboardInterrupt:
        output({'ok': False, 'status': 'cancelled', 'message': '已取消本次操作。'})
        return 130
    except ModuleNotFoundError:
        output({'ok': False, 'status': 'failed', 'message': '依赖缺失，请运行 setup.bat 或安装 requirements.txt。'})
        return 1
    except Exception as exc:
        from single.common import error_message
        code, message = error_message(exc)
        output({'ok': False, 'status': 'failed', 'code': code, 'message': message})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
