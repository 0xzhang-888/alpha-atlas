"""6551 (ai.6551.io) Twitter data client.

Thin async wrapper over the 6551 REST API. Used to offload cheap structured
lookups (profile / registration date / tweet count / bio / KOL-follower count /
recent tweets) that would otherwise cost expensive Grok web-search calls.

Auth: TWITTER_TOKEN env var (bearer). Base overridable via TWITTER_API_BASE.

NOTE: every call is credit-metered on 6551's side (response carries a `usage`
field). We log the cost at DEBUG so credit burn can be monitored.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("tg-summary.6551")

BASE = os.environ.get("TWITTER_API_BASE", "https://ai.6551.io").rstrip("/")
TIMEOUT = 30.0

# Twitter/X snowflake epoch (ms). User IDs minted after the ~2013 snowflake
# migration encode their creation time; sequential pre-2013 IDs do not.
SNOWFLAKE_EPOCH = 1288834974657
# Pre-snowflake sequential user IDs topped out around ~1.5e9. Anything above
# this threshold is a snowflake ID we can decode; below it is an old account.
SNOWFLAKE_MIN_ID = 10_000_000_000

HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")

# Mentions that show up in bios but are never the user's own project.
BIO_HANDLE_STOPWORDS = {
    "ethereum", "bitcoin", "solana", "base", "arbitrum", "optimism", "binance",
    "coinbase", "ycombinator", "a16z", "paradigm", "youtube", "github", "discord",
    "telegram", "gmail", "substack", "medium",
}

USER_TTL = 6 * 3600        # profile rarely changes
KOL_TTL = 10 * 60          # KOL-follower count, keep fresh-ish
TWEETS_TTL = 30 * 60

_user_cache: dict[str, tuple[float, "dict | None"]] = {}
_kol_cache: dict[str, tuple[float, "int | None"]] = {}
_tweets_cache: dict[str, tuple[float, list[str]]] = {}
_billing_blocked_until = 0.0
_billing_lock = asyncio.Lock()


def _token() -> str | None:
    return os.environ.get("TWITTER_TOKEN")


def snowflake_to_ym(user_id) -> str | None:
    """Return 'YYYY-MM' registration month from a snowflake user id, or None for
    pre-snowflake (old) accounts / unparseable input."""
    try:
        i = int(user_id)
    except (TypeError, ValueError):
        return None
    if i < SNOWFLAKE_MIN_ID:
        return None  # old sequential id → not a fresh account
    ms = (i >> 22) + SNOWFLAKE_EPOCH
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return None


def is_fresh(info: "dict | None", *, max_age_months: int = 2,
             max_tweets: int = 10) -> bool:
    """Heuristic: account registered within max_age_months OR very few tweets."""
    if not info:
        return False
    reg_ym = info.get("reg_ym")
    statuses = info.get("statuses")
    if statuses is not None and statuses <= max_tweets:
        return True
    if reg_ym:
        try:
            y, m = (int(x) for x in reg_ym.split("-"))
            reg = datetime(y, m, 1, tzinfo=timezone.utc)
            months = (datetime.now(timezone.utc) - reg).days / 30.0
            return months <= max_age_months
        except (ValueError, TypeError):
            return False
    return False


def bio_project_handles(bio: str, exclude: str = "") -> list[str]:
    """Extract candidate @project handles from a bio string, dropping the user's
    own handle and well-known non-project mentions. Preserves casing & order."""
    if not bio:
        return []
    ex = exclude.lower().lstrip("@")
    seen: set[str] = set()
    out: list[str] = []
    for h in HANDLE_RE.findall(bio):
        low = h.lower()
        if low == ex or low in BIO_HANDLE_STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(h)
    return out


async def _post(client: httpx.AsyncClient, path: str, payload: dict) -> "dict | None":
    global _billing_blocked_until
    token = _token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with _billing_lock:
        if time.time() < _billing_blocked_until:
            return None
        r = await client.post(f"{BASE}{path}", headers=headers, json=payload)
        if r.status_code == 402:
            _billing_blocked_until = time.time() + 1800
            log.warning("6551 billing unavailable; pausing requests for 30 minutes")
        r.raise_for_status()
    data = r.json()
    usage = data.get("usage") if isinstance(data, dict) else None
    if usage:
        log.debug("6551 %s usage=%s", path, usage)
    return data


async def get_user_info(handle: str) -> "dict | None":
    """Return normalized profile dict or None.

    keys: handle, name, bio, followers, statuses, user_id, reg_ym, verified
    """
    key = handle.lower()
    now = time.time()
    hit = _user_cache.get(key)
    if hit and now - hit[0] < USER_TTL:
        return hit[1]
    if not _token():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            data = await _post(c, "/open/twitter_user_info", {"username": handle.lstrip("@")})
        if data is None:
            return None  # billing pause is not a six-hour "missing profile" cache
        d = (data or {}).get("data") or {}
        if not d or d.get("success") is False:
            _user_cache[key] = (now, None)
            return None
        uid = d.get("userId")
        info = {
            "handle": d.get("screenName") or handle,
            "name": d.get("name", "") or "",
            "bio": d.get("description", "") or "",
            "followers": d.get("followersCount"),
            "statuses": d.get("statusesCount"),
            "user_id": uid,
            "reg_ym": snowflake_to_ym(uid),
            "verified": bool(d.get("verified")),
        }
        _user_cache[key] = (now, info)
        return info
    except Exception as e:  # noqa: BLE001
        log.warning("6551 user_info @%s failed: %s", handle, e)
        return None


async def get_kol_count(handle: str) -> "int | None":
    """Return number of KOLs following this handle (6551 totalCount), or None."""
    key = handle.lower()
    now = time.time()
    hit = _kol_cache.get(key)
    if hit and now - hit[0] < KOL_TTL:
        return hit[1]
    if not _token():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            data = await _post(c, "/open/twitter_kol_followers", {"username": handle.lstrip("@")})
        d = (data or {}).get("data") or {}
        cnt = d.get("totalCount")
        cnt = int(cnt) if cnt is not None else None
        _kol_cache[key] = (now, cnt)
        return cnt
    except Exception as e:  # noqa: BLE001
        log.warning("6551 kol_followers @%s failed: %s", handle, e)
        return None


async def get_user_tweets(handle: str, n: int = 5) -> list[str]:
    """Return up to n recent tweet texts (for bio-fallback dev extraction)."""
    key = f"{handle.lower()}:{n}"
    now = time.time()
    hit = _tweets_cache.get(key)
    if hit and now - hit[0] < TWEETS_TTL:
        return hit[1]
    if not _token():
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            data = await _post(
                c, "/open/twitter_user_tweets",
                {"username": handle.lstrip("@"), "maxResults": n},
            )
        rows = (data or {}).get("data") or []
        texts = [str(r.get("text", "")).strip() for r in rows if r.get("text")]
        _tweets_cache[key] = (now, texts)
        return texts
    except Exception as e:  # noqa: BLE001
        log.warning("6551 user_tweets @%s failed: %s", handle, e)
        return []


async def get_many_user_info(handles: list[str]) -> dict[str, "dict | None"]:
    """Concurrent profile fetch. Returns {original_handle: info|None}."""
    if not handles:
        return {}
    results = await asyncio.gather(
        *(get_user_info(h) for h in handles), return_exceptions=True
    )
    out: dict[str, "dict | None"] = {}
    for h, r in zip(handles, results):
        out[h] = None if isinstance(r, Exception) else r
    return out


async def get_many_kol_counts(handles: list[str]) -> dict[str, "int | None"]:
    if not handles:
        return {}
    results = await asyncio.gather(
        *(get_kol_count(h) for h in handles), return_exceptions=True
    )
    return {h: (None if isinstance(r, Exception) else r) for h, r in zip(handles, results)}
