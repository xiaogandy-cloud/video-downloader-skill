from __future__ import annotations

import json
import sys
import time
from http.cookiejar import Cookie, MozillaCookieJar

from playwright.sync_api import sync_playwright
from single.common import DATA, HOME_URLS, atomic_json, cookie_domain_allowed


def login(platform, proxy):
    signal = DATA / f'login-{platform}.save'
    status = DATA / f'login-{platform}.json'
    signal.unlink(missing_ok=True)
    atomic_json(status, {'state': 'opening', 'message': '正在打开本地登录窗口'})
    try:
        with sync_playwright() as playwright:
            options = dict(channel='chrome', headless=False, no_viewport=True)
            if proxy:
                options['proxy'] = {'server': proxy, 'bypass': 'localhost,127.0.0.1'}
            else:
                options['args'] = ['--no-proxy-server']
            profile = DATA / 'profiles' / platform
            context = playwright.chromium.launch_persistent_context(str(profile), **options)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(HOME_URLS[platform], timeout=40000, wait_until='domcontentloaded')
            except Exception:
                pass
            atomic_json(status, {'state': 'waiting', 'message': '请在打开的浏览器中登录或完成验证，再点击“保存并启用登录”。'})
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not signal.exists():
                if not context.pages:
                    break
                if platform == 'wechat':
                    cookies = context.cookies(HOME_URLS[platform])
                    names = {c['name'] for c in cookies if c.get('value') and (c.get('expires', -1) <= 0 or c['expires'] > time.time())}
                    if {'hy_user', 'hy_token'} <= names:
                        signal.touch()
                        break
                context.pages[0].wait_for_timeout(300)
            if signal.exists() and context.pages:
                jar = MozillaCookieJar()
                count = 0
                for c in context.cookies():
                    domain = c['domain'].lstrip('.')
                    if not cookie_domain_allowed(platform, domain):
                        continue
                    expiry = c.get('expires', -1)
                    jar.set_cookie(Cookie(0, c['name'], c['value'], None, False, c['domain'], c['domain'].startswith('.'), c['domain'].startswith('.'), c['path'], True, c['secure'], int(expiry) if expiry > 0 else None, expiry <= 0, None, None, {'HttpOnly': ''} if c.get('httpOnly') else {}))
                    count += 1
                if platform == 'wechat' and not {'hy_user', 'hy_token'} <= {c.name for c in jar if not c.is_expired()}:
                    atomic_json(status, {'state': 'failed', 'message': '尚未检测到元宝有效登录，请重新打开窗口手动登录；匿名 Cookie 不视为登录成功。'})
                    return
                cookie_path = DATA / 'cookies' / f'{platform}.txt'
                cookie_path.parent.mkdir(parents=True, exist_ok=True)
                jar.save(str(cookie_path), ignore_discard=True)
                atomic_json(status, {'state': 'saved', 'message': '登录数据已保存在本机并启用。平台是否接受该会话，以实际下载结果为准。' if count else '没有取得该平台 Cookie，请重新登录。', 'has_cookies': count > 0, 'enable_when_saved': True})
            else:
                atomic_json(status, {'state': 'closed', 'message': '登录窗口已关闭或超时，未保存新登录数据。'})
            context.close()
    except Exception:
        atomic_json(status, {'state': 'failed', 'message': '无法启动专用登录窗口，请关闭重复窗口并检查 Chrome 安装和网络。'})
    finally:
        signal.unlink(missing_ok=True)


if __name__ == '__main__':
    payload = json.loads(sys.stdin.readline())
    login(payload['platform'], payload.get('proxy', ''))
