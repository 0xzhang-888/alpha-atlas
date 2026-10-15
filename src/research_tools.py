"""Bounded, cached evidence tools. No model is allowed to invent tool results."""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from . import grok, opentwitter as ot
from .storage import Storage
from .chain_evidence import own_x_source


def public_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme in {"http", "https"} and bool(p.hostname) and not p.username
                and not p.password and p.port in {None, 80, 443})
    except ValueError:
        return False


async def validate_url(url: str) -> None:
    if not public_url(url):
        raise ValueError("invalid public URL")
    host = urlsplit(url).hostname
    addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=10)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("private network URL is not allowed")


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


async def read_page(url: str, storage: Storage) -> dict:
    key = "page:v1:" + hashlib.sha256(url.encode()).hexdigest()
    cached = storage.cache_get(key, 24 * 3600)
    if cached:
        return cached
    original = url
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for _ in range(5):
                await validate_url(url)
                async with client.stream("GET", url, headers={"User-Agent": "TGResearchBot/1.0"}) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers["location"])
                        continue
                    r.raise_for_status()
                    chunks = []
                    size = 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > 4_000_000:
                            raise ValueError("document exceeds 4 MB limit")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    content_type = r.headers.get("content-type", "")
                    break
            else:
                raise ValueError("too many redirects")
        links = []
        partial = False
        if raw.startswith(b"%PDF") or "application/pdf" in content_type:
            from pypdf import PdfReader
            def extract_pdf():
                reader = PdfReader(io.BytesIO(raw))
                return "\n".join((p.extract_text() or "") for p in reader.pages[:15]), len(reader.pages) > 15
            text, partial = await asyncio.to_thread(extract_pdf)
        elif "html" in content_type or raw.lstrip().startswith(b"<"):
            parser = PageText()
            parser.feed(raw.decode("utf-8", errors="replace"))
            text = "\n".join(parser.parts)
            links = list(dict.fromkeys(urljoin(url, x) for x in parser.links))
            links = [x for x in links if public_url(x)][:40]
        else:
            text = raw.decode("utf-8", errors="replace")
        if len(text.strip()) < 80:
            raise ValueError("insufficient readable text")
        result = {"url": url, "requested_url": original, "text": text[:14000],
                  "links": links, "fetched_at": int(time.time()), "read": True,
                  "truncated": partial or len(text) > 14000}
        storage.cache_set(key, result)
        return result
    except Exception as exc:
        return {"url": original, "read": False, "error": type(exc).__name__}


async def profile(handle: str, storage: Storage) -> dict:
    # Channel messages already contain metered data: reuse it instead of paying twice.
    source = storage.get_source_followers([handle]).get(handle) or {}
    bio = storage.get_source_bios([handle]).get(handle, "")
    if source and bio:
        return {**source, "bio": bio, "handle": handle}
    key = "profile:v1:" + handle.lower()
    cached = storage.cache_get(key, 6 * 3600)
    if cached:
        return {**cached, **source, "bio": bio or cached.get("bio", "")}
    info = await ot.get_user_info(handle)
    if info:
        result = {**info, "source": "6551", "observed_at": int(time.time())}
        storage.cache_set(key, result)
        return {**result, **source, "bio": info.get("bio") or bio}
    return {"handle": handle, "bio": bio, **source}


async def search_x(handle: str, query: str, storage: Storage) -> dict:
    key = "x-evidence:v1:" + hashlib.sha256(f"{handle.lower()}:{query}".encode()).hexdigest()
    hit = storage.cache_get(key, 3600)
    if hit:
        _index_own_posts(handle, hit, storage)
        return hit
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            data = await ot._post(client, "/open/twitter_search", {
                "keywords": query[:200], "maxResults": 8, "product": "Latest"})
        rows = (data or {}).get("data") or []
        if isinstance(rows, list) and rows:
            posts = [{"text": str(p.get("text", ""))[:1400],
                      "author": p.get("userScreenName", ""),
                      "url": f"https://x.com/{p.get('userScreenName', 'i')}/status/{p.get('id', '')}",
                      "published_at": p.get("createdAt", "")} for p in rows if p.get("text") and p.get("id")]
            if posts:
                result = {"posts": posts, "source": "6551", "searched_at": int(time.time())}
                storage.cache_set(key, result)
                _index_own_posts(handle, result, storage)
                return result
    except Exception:
        pass

    def parse(data):
        output = data.get("output") or []
        if data.get("status") not in (None, "completed"):
            raise ValueError("incomplete X evidence")
        if not any(x.get("status") == "completed" and (
            x.get("type") == "web_search_call" or (x.get("type") == "custom_tool_call"
            and str(x.get("name", "")).startswith("x_"))) for x in output):
            raise ValueError("no actual X search")
        sources = []
        for x in output:
            sources.extend(s.get("url", "") for s in (x.get("action") or {}).get("sources", []))
            for p in x.get("content", []):
                sources.extend(a.get("url", "") for a in p.get("annotations", []))
        for x in reversed(output):
            if x.get("type") == "message":
                text = "".join(p.get("text", "") for p in x.get("content", []) if p.get("type") == "output_text")
                body = grok._extract_json(text)
                posts = body.get("posts")
                if not isinstance(posts, list):
                    raise ValueError("missing posts")
                # Accept only cited X URLs, never a model-only claim about a post.
                posts = [p for p in posts[:8] if isinstance(p, dict) and p.get("url") in sources
                         and urlsplit(p["url"]).hostname in {"x.com", "twitter.com"}]
                return {"posts": posts, "source": "grok_x_search", "searched_at": int(time.time())}
        raise ValueError("no X evidence output")

    try:
        result = await grok._request_with_fallback("deep", "/responses", {
            "input": f"实际搜索 X，主题账号 @{handle}，查询：{query[:200]}。只提供相关原始推文证据（最多6条），不要推测原因或身份。"
                     '引用真实推文URL，返回JSON {"posts":[{"author":"handle","url":"https://x.com/.../status/...","text":"推文内容","published_at":"日期"}]}。搜不到返回空列表。',
            "tools": [{"type": "x_search"}], "tool_choice": "required", "max_tool_calls": 1,
            "max_output_tokens": 3500,
        }, parse)
        storage.cache_set(key, result)
        _index_own_posts(handle, result, storage)
        return result
    except Exception as exc:
        return {"posts": [], "error": type(exc).__name__}


def _index_own_posts(handle: str, result: dict, storage: Storage) -> None:
    # Reuse searches the bot already paid for. Dashboard reads never trigger a new search.
    posts = [p for p in result.get('posts', []) if isinstance(p, dict)
             and str(p.get('author', '')).lower() == handle.lower()
             and own_x_source(p.get('url'), handle, post=True)
             and not str(p.get('text', '')).startswith('RT @')]
    if posts:
        storage.cache_set('x-account-evidence:v1:' + handle.lower(),
                          {'posts':posts[:20], 'searched_at':result.get('searched_at') or int(time.time())})


CA_RE = re.compile(r"(?:0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\Z")


async def token_market(ca: str, storage: Storage) -> dict:
    if not CA_RE.fullmatch(ca):
        return {"error": "invalid contract address"}
    key = "market:v1:" + ca
    hit = storage.cache_get(key, 600)
    if hit:
        return hit
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.dexscreener.com/latest/dex/search", params={"q": ca})
            r.raise_for_status()
        rows = [p for p in r.json().get("pairs", [])
                if (p.get("baseToken") or {}).get("address") == ca
                or (ca.startswith("0x") and (p.get("baseToken") or {}).get("address", "").lower() == ca.lower())]
        rows.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        result = {"ca": ca, "observed_at": int(time.time()), "pairs": [
            {k: p.get(k) for k in ("chainId", "url", "baseToken", "priceUsd", "priceChange", "liquidity", "marketCap", "info")}
            for p in rows[:3]]}
        storage.cache_set(key, result)
        return result
    except Exception as exc:
        return {"ca": ca, "error": type(exc).__name__}
