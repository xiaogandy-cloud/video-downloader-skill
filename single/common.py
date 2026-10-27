from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.platforms import extract_url, identify_url, UnsupportedUrlError
from app.models import Platform, SourceKind

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / '.local'
NAMES = {'youtube': 'YouTube', 'douyin': '抖音', 'kuaishou': '快手', 'xiaohongshu': '小红书', 'wechat': '视频号'}
HOME_URLS = {'youtube': 'https://www.youtube.com/', 'douyin': 'https://www.douyin.com/', 'kuaishou': 'https://www.kuaishou.com/', 'xiaohongshu': 'https://www.xiaohongshu.com/', 'wechat': 'https://yuanbao.tencent.com/'}
DOMAINS = {'youtube': ('youtube.com', 'google.com'), 'douyin': ('douyin.com', 'amemv.com'), 'kuaishou': ('kuaishou.com', 'gifshow.com'), 'xiaohongshu': ('xiaohongshu.com', 'xhslink.com', 'xhslink.cn'), 'wechat': ('yuanbao.tencent.com',)}
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class LocalError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def cookie_domain_allowed(platform, domain):
    domain = domain.lstrip('.').lower()
    if platform == 'wechat':
        return domain in ('yuanbao.tencent.com', 'tencent.com') or domain.endswith('.yuanbao.tencent.com')
    return any(domain == d or domain.endswith('.' + d) for d in DOMAINS[platform])


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def single_url(text):
    if len(text) > 12000:
        raise LocalError('link', '分享文字过长，请只保留一条视频。')
    candidates = re.findall(r'https?://[^\s<>\"\u3000]+', html.unescape(text), re.I)
    if not candidates:
        raise LocalError('link', '未找到完整链接，请粘贴视频链接或带链接的分享文字。')
    if len(set(candidates)) != 1:
        raise LocalError('link', '一次只下载一条视频，请只保留一个链接。')
    try:
        url = extract_url(html.unescape(text))
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise LocalError('link', '视频链接不能包含账号密码。')
        if parsed.port not in (None, 80, 443):
            raise LocalError('link', '不支持自定义端口的视频链接。')
        host = parsed.hostname or ''
        if host in ('weixin.qq.com', 'mp.weixin.qq.com') and re.fullmatch(r'/sph/[A-Za-z0-9_-]{6,128}/?', parsed.path):
            if parsed.scheme != 'https':
                raise LocalError('link', '视频号分享链接必须使用 HTTPS。')
            return url, Platform.WECHAT, SourceKind.ITEM
        if host in ('channels.weixin.qq.com', 'finder.video.qq.com', 'weixin.qq.com') or host.endswith('.weixin.qq.com'):
            raise LocalError('wechat', '请从微信复制单条视频的完整 https://weixin.qq.com/sph/… 分享链接；不支持主页、直播、图集或任意视频直链。')
        if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
            query = parse_qs(parsed.query)
            valid = (parsed.path == '/watch' and bool(query.get('v'))) or bool(re.fullmatch(r'/(shorts|embed)/[\w-]+/?', parsed.path)) or (host == 'youtu.be' and bool(re.fullmatch(r'/[\w-]+/?', parsed.path)))
            if not valid:
                raise LocalError('link', '只支持单条视频链接，不支持频道、播放列表或直播链接。')
            # The list parameter remains intact; this edition never expands playlists.
            return url, Platform.YOUTUBE, SourceKind.ITEM
        info = identify_url(url)
        if info.platform not in (Platform.DOUYIN, Platform.KUAISHOU, Platform.XIAOHONGSHU):
            raise LocalError('link', '本版仅接入 YouTube、抖音、快手、小红书。')
        if info.kind not in (SourceKind.ITEM, SourceKind.SHORT_LINK):
            raise LocalError('link', '请使用单条作品链接，不支持主页或批量采集。')
        return info.url, info.platform, info.kind
    except (UnsupportedUrlError, ValueError) as exc:
        raise LocalError('link', '链接失效或类型不支持，请重新复制单条视频的完整分享链接。') from exc


def safe_title(text, max_chars=65):
    value = re.sub(r'chatgpt', '', str(text), flags=re.I)
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' ._')
    return (value[:max_chars].rstrip(' .') or '未命名视频')


def final_destination(folder, platform, title, suffix, stamp=None):
    folder = Path(folder).resolve()
    if suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.m4v'):
        raise LocalError('integrity', '下载结果不是受支持的视频文件，未标记为成功。')
    stamp = stamp or datetime.now().strftime('%Y%m%d_%H%M%S')
    budget = min(65, 220 - len(str(folder)) - len(stamp) - len(NAMES[platform]) - 12)
    if budget < 8:
        raise LocalError('local', '保存目录路径过长，请选择更短的目录。')
    stem = f'{stamp}_{NAMES[platform]}_{safe_title(title, budget)}'
    path = folder / (stem + suffix.lower())
    index = 2
    while path.exists():
        path = folder / f'{stem}_{index}{suffix.lower()}'
        index += 1
    return path


def probe_file(path, expected_duration=None):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], capture_output=True, timeout=40, creationflags=NO_WINDOW)
        if result.returncode:
            raise ValueError('probe')
        payload = json.loads(result.stdout)
        videos = [s for s in payload['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
        audios = [s for s in payload['streams'] if s.get('codec_type') == 'audio']
        duration = float(payload['format'].get('duration') or 0)
        if not videos or not audios or duration <= 0 or Path(path).stat().st_size < 1024:
            raise ValueError('missing streams')
        if expected_duration and abs(duration - expected_duration) > max(3, expected_duration * .03):
            raise ValueError('duration mismatch')
        video = videos[0]
        return {'duration': round(duration, 2), 'width': video.get('width'), 'height': video.get('height'), 'video_codec': video.get('codec_name'), 'audio_codec': audios[0].get('codec_name'), 'bytes': Path(path).stat().st_size}
    except FileNotFoundError as exc:
        raise LocalError('local', '本机缺少 FFprobe，请运行 setup.bat 修复依赖。') from exc
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        raise LocalError('integrity', '文件完整性校验失败：未确认完整音画、有效时长或文件可读性，未标记为成功。') from exc


def error_message(exc):
    from app.errors import classify_site_issue
    if isinstance(exc, LocalError):
        return exc.code, str(exc)
    if isinstance(exc, OSError) and (getattr(exc, 'errno', None) == 28 or getattr(exc, 'winerror', None) in (39, 112)):
        from single.storage import storage_error
        error = storage_error(exc)
        return error.code, str(error)
    text = str(exc).lower()
    if 'confirm' in text and 'bot' in text:
        return 'verification', '平台要求确认身份。请在设置中打开该平台的本地登录窗口，手动完成验证后保存登录；验证未通过前停止下载。'
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return 'local', '本地依赖缺失或保存权限不足，请检查保存目录并运行 setup.bat。'
    code = classify_site_issue(exc).value
    messages = {
        'verification_required': '平台要求验证码或安全验证，请在本地登录窗口手动完成并保存登录后重试。',
        'login_required': '需要登录或登录状态已过期，请在设置中打开该平台的本地登录窗口并保存登录。',
        'cookie_unavailable': '本地登录数据不可用，请重新登录并保存；本工具不读取日常浏览器数据。',
        'network_error': '网络无法访问或连接超时，请确认浏览器能打开原视频，并检查设置中的代理地址。',
        'rate_limited': '平台限制了当前请求频率（HTTP 429），请稍后重试；不会连续重试或切换第三方解析。',
        'request_rejected': '平台拒绝访问（可能是 HTTP 403）。请在浏览器确认访问权限；工具已停止，不能保证登录后可解决。',
        'content_unavailable': '链接失效、内容已删除或无权访问，请确认原视频仍可正常播放。',
        'region_restricted': '平台限制了当前地区或账号访问，工具已停止。',
        'site_response_changed': '平台接口或返回结构发生变化，当前适配器无法确认视频，请检查上游更新。',
        'media_link_expired': '平台返回的视频地址已过期，请重新粘贴原始分享链接解析。',
        'site_unavailable': '平台服务暂时不可用，请稍后重试。',
        'security_blocked': '视频地址或身份校验不通过，已停止下载。',
        'local_configuration': '本地依赖或配置异常，请运行 setup.bat 并检查保存目录。',
    }
    if code in ('site_response_changed', 'unknown') and 'xiaohongshu' in text and ('no note data' in text or 'could not parse' in text):
        return 'link_parameters', '小红书没有返回这条作品的数据。请复制 App 的完整分享文字或含 xsec_token 的网页分享链接；裸作品地址可能缺少访问参数。若原链接已不能播放，则可能已失效。'
    if code == 'unknown' and 'xiaohongshu' in text and 'note redirect was blocked' in text:
        return 'link_parameters', '小红书未返回同一作品的有效页面，已停止不匹配的跳转。请重新复制 App 的完整分享文字，保留访问参数；原页面明确要求登录时请在本地登录后重试。'
    if code in messages:
        return code, messages[code]
    if any(x in text for x in ('timed out', 'timeout', 'connection', 'resolve host', 'proxy')):
        return 'network', messages['network_error']
    if 'drm' in text:
        return 'access', '视频受 DRM 保护，工具已停止，不支持解密下载。'
    return 'unknown', '下载引擎未能确认视频，原因尚未判定。请检查原链接能否播放；必要时更新引擎。不会把此结果标记为成功。'
