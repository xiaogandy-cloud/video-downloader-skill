"""MIT wxsph resolver core; extracted without HTTP server, cache or .env loading.
Source: https://github.com/sanmu1019/wxsph
Commit: 47104937d681a3bc36b0c6f84d6231cdc85bf5e8
Copyright (c) 2026 sanmu1019; see LICENSE.wxsph.
Transport is supplied only inside the isolated download worker.
"""
from typing import Any
import secrets
import time
from urllib.parse import parse_qs, quote, urlparse

YUANBAO_PARSE_URL = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
FEED_INFO_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"

def fetch_json(*args, **kwargs):
    raise RuntimeError("Resolver transport must be supplied by the local adapter")

class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def parse_share_url(share_url: str, cookie: str) -> dict[str, Any]:
    if not cookie:
        raise ApiError("missing WXSPH_COOKIE environment variable", status=500)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://yuanbao.tencent.com",
        "Referer": "https://yuanbao.tencent.com/",
        "User-Agent": UA,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
        "X-Source": "web",
        "Cookie": cookie,
    }
    payload = {
        "type": "video_channel_url",
        "url": share_url,
        "scene": 1,
    }
    result = fetch_json(YUANBAO_PARSE_URL, payload, headers)
    data = result.get("data")
    if not isinstance(data, dict):
        raise ApiError("parse failed: missing data", status=502, detail=result)
    if not data.get("playable_url") and not data.get("wx_export_id"):
        raise ApiError("parse failed: missing playable_url/export_id", status=502, detail=result)
    return data


def split_playable_url(parse_data: dict[str, Any]) -> tuple[str, str]:
    playable_url = parse_data.get("playable_url") or ""
    token = ""
    export_id = ""

    if playable_url:
        query = parse_qs(urlparse(playable_url).query)
        token = (query.get("token") or [""])[0]
        export_id = (query.get("eid") or [""])[0]

    export_id = export_id or parse_data.get("wx_export_id") or ""
    if not token:
        raise ApiError("parse failed: missing preview token", status=502, detail=parse_data)
    if not export_id:
        raise ApiError("parse failed: missing export id", status=502, detail=parse_data)
    return export_id, token


def generate_rid() -> str:
    return f"{int(time.time()):x}-{secrets.token_hex(4)}"


def get_feed_info(export_id: str, token: str) -> dict[str, Any]:
    rid = generate_rid()
    page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    url = f"{FEED_INFO_URL}?_rid={rid}&_pageUrl={quote(page_url, safe='')}"
    referer = (
        f"{page_url}?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={quote(token, safe='')}&entry_scene=0&eid={quote(export_id, safe='')}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": UA,
    }
    payload = {
        "baseReq": {"generalToken": token},
        "exportId": export_id,
    }
    result = fetch_json(url, payload, headers)
    if result.get("errCode") not in (None, 0):
        raise ApiError("feed info failed", status=502, detail=result)
    return result


def first_video_url(feed_info: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for key, codec in (("h264VideoInfo", "h264"), ("h265VideoInfo", "h265"), ("videoInfo", "")):
        item = feed_info.get(key)
        if not isinstance(item, dict):
            continue
        video_url = item.get("videoUrl") or item.get("url")
        if not video_url:
            continue
        width = item.get("width") or feed_info.get("width")
        height = item.get("height") or feed_info.get("height")
        quality = f"{height}p" if height else ""
        candidates.append(
            {
                "label": quality or codec or "video",
                "quality": quality,
                "url": video_url,
                "format": "mp4",
                "codec": codec,
                "width": width,
                "height": height,
            }
        )

    direct = feed_info.get("videoUrl")
    if direct and all(item["url"] != direct for item in candidates):
        candidates.append({"label": "video", "quality": "", "url": direct, "format": "mp4", "codec": ""})
    return (candidates[0]["url"] if candidates else ""), candidates
