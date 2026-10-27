"""Single Channels adapter: pinned MIT resolver + local login + existing yt-dlp."""
from __future__ import annotations

from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

import httpx
from yt_dlp import YoutubeDL

from single.common import LocalError, single_url
from single.vendor import wxsph_core as core

CDN_HOSTS = {'finder.video.qq.com', 'findermp.video.qq.com'}


def media_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in CDN_HOSTS or
                parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        raise LocalError('security_blocked', '视频号返回了未确认的媒体地址，已停止；不会访问任意主机。') from None


def cookie_header(jar: CookieJar):
    request = Request(core.YUANBAO_PARSE_URL)
    jar.add_cookie_header(request)
    value = request.get_header('Cookie', '')
    names = {part.strip().partition('=')[0] for part in value.split(';')}
    if not {'hy_user', 'hy_token'} <= names:
        raise LocalError('login_required', '视频号需要腾讯元宝登录。请在设置选择“视频号（腾讯元宝）”，打开本地登录窗口并手动登录；无需发送密码或 Cookie。')
    return value


def site_response(response, stage):
    if response.status_code == 401:
        raise LocalError('login_required', '腾讯元宝登录已过期，请重新打开视频号的本地登录窗口。')
    if response.status_code == 403:
        raise LocalError('verification_required', '腾讯要求人工验证或拒绝当前访问，请在本地登录窗口确认；工具已停止。')
    if response.status_code == 429:
        raise LocalError('rate_limited', '腾讯暂时限制请求频率，请稍后再试；不会循环重试。')
    if not 200 <= response.status_code < 300:
        raise LocalError('network_error', f'{stage}返回 HTTP {response.status_code}，请稍后重试。')
    try:
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise LocalError('site_response_changed', f'{stage}未返回可识别的数据；接口可能变化，未尝试绕过验证。') from None


class Resolver:
    def __init__(self, session, *, transport=None):
        self.cookie = cookie_header(session.jar)
        self.client = httpx.Client(proxy=session.proxy or None, trust_env=False,
            follow_redirects=False, timeout=25, transport=transport)

    def post(self, url, payload, headers):
        parsed = urlsplit(url)
        allowed = {('yuanbao.tencent.com', '/api/weixin/get_parse_result'),
                   ('channels.weixin.qq.com', '/finder-preview/api/feed/get_feed_info')}
        if parsed.scheme != 'https' or (parsed.netloc, parsed.path) not in allowed:
            raise LocalError('security_blocked', '解析器请求超出腾讯官方接口范围，已停止。')
        if parsed.hostname != 'yuanbao.tencent.com' and any(k.lower() in ('cookie', 'authorization') for k in headers):
            raise LocalError('security_blocked', '已阻止向非元宝接口发送登录数据。')
        value = site_response(self.client.post(url, json=payload, headers=headers), '腾讯元宝接口' if parsed.hostname == 'yuanbao.tencent.com' else '视频号预览接口')
        if parsed.hostname == 'yuanbao.tencent.com' and value.get('code') != 0:
            message = str(value.get('msg') or value.get('message') or '').lower()
            if any(word in message for word in ('captcha', 'verify', '验证')):
                raise LocalError('verification_required', '腾讯元宝要求人工验证，请在本地登录窗口处理后再试。')
            if any(word in message for word in ('login', 'token', '登录', '鉴权')):
                raise LocalError('login_required', '腾讯元宝登录不可用，请重新登录。')
            raise LocalError('content_unavailable', '腾讯元宝没有允许解析该视频；可能是链接失效、私密内容或不支持的内容类型。')
        return value

    def resolve(self, source):
        _, platform, _ = single_url(source)
        if platform.value != 'wechat':
            raise LocalError('platform_mismatch', '视频号适配器只接受单条视频号分享链接。')
        # Each download is a dedicated worker process; no shared resolver/auth state.
        original_transport = core.fetch_json
        core.fetch_json = self.post
        try:
            parsed = core.parse_share_url(source, self.cookie)
            preview = urlsplit(parsed.get('playable_url') or '')
            if preview.scheme != 'https' or preview.netloc != 'channels.weixin.qq.com' or preview.path != '/finder-preview/pages/feed':
                raise LocalError('site_response_changed', '腾讯没有返回有效的视频号预览地址，不能确认此视频可下载。')
            export_id, token = core.split_playable_url(parsed)
            response = core.get_feed_info(export_id, token)
            data = response.get('data')
            if not isinstance(data, dict) or (data.get('errMsg') and not isinstance(data['errMsg'], dict)):
                raise LocalError('site_response_changed', '视频号接口返回结构异常，已停止。')
            if any((data.get('errMsg') or {}).get(k) for k in ('type', 'title', 'content')):
                raise LocalError('content_unavailable', '视频号未授权预览这条视频，或视频已失效；已停止。')
            feed = data.get('feedInfo')
            if not isinstance(feed, dict) or feed.get('mediaType') != 4:
                raise LocalError('link', '该分享不是可下载的普通单条视频；不支持直播、回放或图集。')
            media_infos = [feed] + [feed[k] for k in ('h264VideoInfo', 'h265VideoInfo', 'videoInfo') if isinstance(feed.get(k), dict)]
            if any(item.get(k) for item in media_infos for k in ('has_drm', 'drm', 'isDRM', 'decodeKey', 'decryptKey')):
                raise LocalError('access', '该视频涉及加密或 DRM，已停止，不尝试解密。')
            _, candidates = core.first_video_url(feed)
            if not candidates:
                raise LocalError('content_unavailable', '腾讯预览接口未提供可下载的视频源。')
            def pixels(item):
                try:
                    return int(item.get('width') or 0) * int(item.get('height') or 0)
                except (TypeError, ValueError):
                    return 0
            chosen = max(candidates, key=pixels)
            media_url(chosen['url'])
            return {'url': chosen['url'], 'title': str(feed.get('title') or feed.get('description') or parsed.get('desc') or '视频号视频'),
                    'width': chosen.get('width'), 'height': chosen.get('height'), 'feed': feed}
        except core.ApiError:
            raise LocalError('site_response_changed', '腾讯返回结构与当前解析器不符，或未授权此内容；未将响应当作下载成功。') from None
        finally:
            core.fetch_json = original_transport

    def checked_media(self, url):
        # Resolve only official CDN redirects before handing the transfer to yt-dlp.
        for _ in range(4):
            media_url(url)
            with self.client.stream('GET', url, headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('location', ''))
                    continue
                if response.status_code in (401, 403, 404):
                    raise LocalError('media_link_expired', '腾讯视频源已过期或不允许下载，请重新解析；不会绕过权限。')
                if response.status_code not in (200, 206):
                    raise LocalError('network_error', f'腾讯视频源返回 HTTP {response.status_code}。')
                return str(response.url)
        raise LocalError('security_blocked', '腾讯视频源跳转次数异常，已停止。')


class WeChatAdapter:
    def download(self, url, stage, session):
        from single.engine import QuietLogger, emit
        resolver = Resolver(session)
        try:
            emit(status='parsing', message='正在通过腾讯官方接口解析视频号分享链接')
            info = resolver.resolve(url)
            watermark_info = {**info['feed'], 'url': info['url'], 'requested_formats': [
                item for key in ('h264VideoInfo', 'h265VideoInfo', 'videoInfo')
                if isinstance((item := info['feed'].get(key)), dict)]}
            session.confirm_watermark(watermark_info)
            source = resolver.checked_media(info['url'])
            emit(status='downloading', message='已取得腾讯视频源，开始下载接口提供的画质', title=info['title'], watermark=session.watermark)
            output = stage / 'wechat.mp4'
            def progress(value):
                if value.get('status') == 'downloading':
                    done = value.get('downloaded_bytes', 0)
                    total = value.get('total_bytes') or value.get('total_bytes_estimate')
                    emit(status='downloading', message='正在下载视频号视频', downloaded=done, total=total,
                         percent=min(100, done * 100 / total) if total else None,
                         speed=value.get('speed'), eta=value.get('eta'))
            options = {**session.options(), 'outtmpl': str(output), 'logger': QuietLogger(),
                       'progress_hooks': [progress], 'continuedl': False, 'overwrites': False}
            options.pop('cookiefile', None)
            with YoutubeDL(options) as ydl:
                ydl.process_info({'id': urlsplit(url).path.rstrip('/').split('/')[-1], 'title': info['title'],
                    'url': source, 'ext': 'mp4', 'protocol': 'https', 'format_id': 'tencent-preview',
                    'extractor': 'local-wechat', 'webpage_url': url})
            if not output.is_file():
                raise LocalError('integrity', '视频号下载引擎未生成完整文件。')
            return output, info['title'], None
        except httpx.TimeoutException:
            raise LocalError('network_error', '腾讯接口或视频源连接超时，请检查网络后重试。') from None
        except httpx.HTTPError:
            raise LocalError('network_error', '腾讯接口或视频源网络连接失败，请检查网络设置。') from None
        finally:
            resolver.client.close()
