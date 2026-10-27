"""Read identity-bound metadata from the normal official detail HTML."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from app import kuaishou as ks
from app.errors import DiscoveryError


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.in_script = True
            self.parts = []

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.in_script:
            self.scripts.append(''.join(self.parts))
            self.in_script = False


def parse_html(html, expected_id):
    parser = Scripts()
    parser.feed(html)
    for script in parser.scripts:
        match = re.match(r'\s*window\.(?:__APOLLO_STATE__|APOLLO_STATE)\s*=\s*', script)
        if not match:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(script[match.end():])
        except ValueError:
            continue
        for _, payload in ks.apollo_operations(state, 'visionVideoDetail', 'photoId', expected_id):
            ks.response_error(payload, f'https://www.kuaishou.com/short-video/{expected_id}')
            video = ks.parse_video(payload, expected_id=expected_id)
            if video.assets:
                return video
    return None


def discover(url, session):
    kind, identity = ks.source_identity(url)
    if kind not in ('item', 'short_link'):
        raise DiscoveryError('Kuaishou single-video URL required')

    class CheckedRedirect(HTTPRedirectHandler):
        max_redirections = 5
        bound_id = identity if kind == 'item' else None

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urlsplit(newurl)
            # The official short link uses this exact HTTPS handoff host.
            intermediate = re.fullmatch(r'/fw/(?:long-video|photo)/([A-Za-z0-9_-]{1,80})', target.path)
            if target.scheme == 'https' and target.netloc == 'v.m.chenzhongtech.com' and intermediate:
                next_kind, next_id = 'item', intermediate[1]
            elif ks.is_page_url(newurl):
                next_kind, next_id = ks.source_identity(newurl)
            else:
                raise DiscoveryError('Kuaishou redirected outside trusted pages')
            if self.bound_id and (next_kind != 'item' or next_id != self.bound_id):
                raise DiscoveryError('Kuaishou video identity changed during redirect')
            if next_kind == 'item':
                self.bound_id = next_id
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    proxies = {'http':session.proxy,'https':session.proxy} if session.proxy else {}
    redirects = CheckedRedirect()
    opener = build_opener(ProxyHandler(proxies), redirects, HTTPCookieProcessor(session.jar))
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml',
    })
    with opener.open(request, timeout=20) as response:
        final_kind, final_id = ks.source_identity(response.url)
        if final_kind != 'item' or (redirects.bound_id and final_id != redirects.bound_id):
            raise DiscoveryError('Kuaishou share did not resolve to the requested single video')
        raw = response.read(ks.MAX_RESPONSE_BYTES + 1)
        if len(raw) > ks.MAX_RESPONSE_BYTES:
            raise DiscoveryError('Kuaishou response exceeded safe size limit')
        text = raw.decode('utf-8')
        if text.lstrip().startswith('{'):
            ks.response_error(json.loads(text), url)
            return None
        video = parse_html(text, final_id)
        return ks.Result([video], 'item', final_id) if video else None
