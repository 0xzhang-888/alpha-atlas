from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("tg-summary.frontrun")

_followers_cache: dict[str, tuple[int, int | None]] = {}
_history_cache: dict[str, tuple[int, list]] = {}
FOLLOWERS_TTL = 600  # 10 minutes
HISTORY_TTL = 24 * 3600  # 24 hours; username changes are rare
TIMEOUT = 10.0
_billing_blocked_until = 0.0
_billing_lock = asyncio.Lock()


def _headers() -> dict[str, str]:
    api_key = os.environ.get("FRONTRUN_API_KEY", "")
    return {
        "accept": "application/json",
        "X-Copilot-Client-Language": "en",
        "X-Copilot-Client-Platform": "CHROME_EXTENSION",
        "X-Copilot-Client-Version": "1.0.0",
        "Authorization": f"Bearer {api_key}",
    }


async def get_kol_followers(handle: str) -> int | None:
    global _billing_blocked_until
    if not os.environ.get("FRONTRUN_API_KEY"):
        return None
    key = handle.lower()
    cached = _followers_cache.get(key)
    if cached and time.time() - cached[0] < FOLLOWERS_TTL:
        return cached[1]
    url = f"https://api.frontrun.pro/api/v1/pro/twitter/{handle}/smart-followers"
    try:
        async with _billing_lock, httpx.AsyncClient(timeout=TIMEOUT) as client:
            if time.time() < _billing_blocked_until:
                return None
            r = await client.get(url, headers=_headers())
            if r.status_code == 402:
                _billing_blocked_until = time.time() + 1800
                log.warning("Frontrun billing unavailable; pausing requests for 30 minutes")
            r.raise_for_status()
            data = r.json()
            count = data.get("data", {}).get("totalCount")
            _followers_cache[key] = (int(time.time()), count)
            return count
    except Exception as e:
        log.warning("smart-followers failed for @%s: %s", handle, e)
        _followers_cache[key] = (int(time.time()), None)
        return None


async def get_username_history(handle: str) -> list[dict]:
    """Return list of {old_username, changed_at} entries, oldest-to-newest."""
    if not os.environ.get("FRONTRUN_API_KEY"):
        return []
    key = handle.lower()
    cached = _history_cache.get(key)
    if cached and time.time() - cached[0] < HISTORY_TTL:
        return cached[1]
    url = f"https://api.frontrun.pro/api/v1/pro/twitter/{handle}/username-history"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
            data = r.json().get("data", {})
            raw = data.get("usernameHistory", []) or []
            history = [
                {"old": item.get("oldTwitterUsername"), "changed_at": item.get("changedAt")}
                for item in raw
                if item.get("oldTwitterUsername")
            ]
            _history_cache[key] = (int(time.time()), history)
            return history
    except Exception as e:
        log.warning("username-history failed for @%s: %s", handle, e)
        _history_cache[key] = (int(time.time()), [])
        return []


async def get_many(handles: list[str]) -> dict[str, int | None]:
    results = await asyncio.gather(*(get_kol_followers(h) for h in handles), return_exceptions=True)
    out: dict[str, int | None] = {}
    for h, r in zip(handles, results):
        out[h] = r if isinstance(r, (int, type(None))) else None
    return out
