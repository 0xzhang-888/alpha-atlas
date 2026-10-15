from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("tg-summary.grok")

DEEP_TTL = 24 * 3600
META_TTL = 7 * 24 * 3600
TIMEOUT = 180.0

VALID_CATEGORIES = {"project", "crypto_dev", "trader", "other"}
T = TypeVar("T")


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str


def _providers(kind: str) -> list[Provider]:
    """Prefer TeamoRouter; retain the original xAI credentials and models."""
    providers = []
    if kind == "basic" and os.environ.get("DEEPSEEK_API_KEY"):
        providers.append(Provider("deepseek", os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
                                  os.environ["DEEPSEEK_API_KEY"], os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")))
    for name, prefix, default_url, default_model in (
        ("teamorouter", "TEAMOROUTER", "https://api.teamorouter.com/v1", "grok-4.6"),
        ("xai", "XAI", "https://api.x.ai/v1", "grok-4.3"),
    ):
        key = os.environ.get(f"{prefix}_API_KEY", "").strip()
        if key:
            providers.append(Provider(
                name, os.environ.get(f"{prefix}_BASE_URL", default_url).rstrip("/"),
                key, os.environ.get(f"{prefix}_MODEL_{kind.upper()}", default_model),
            ))
    return providers


def log_provider_config() -> None:
    for kind in ("basic", "deep"):
        chain = " -> ".join(f"{p.name}/{p.model}" for p in _providers(kind))
        log.info("grok routing [%s]: %s", kind, chain or "disabled")


async def _request_with_fallback(kind: str, path: str, payload: dict,
                                 parse: Callable[[dict], T]) -> T:
    for provider in _providers(kind):
        log.info("grok request [%s] provider=%s model=%s",
                 kind, provider.name, provider.model)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(
                    f"{provider.base_url}{path}",
                    headers={"Authorization": f"Bearer {provider.api_key}",
                             "Content-Type": "application/json"},
                    json={**payload, "model": provider.model, **(
                        {"thinking": {"type": "disabled"}, "max_tokens": 4096,
                         "response_format": {"type": "json_object"}} if provider.name == "deepseek" else {})},
                )
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict):
                raise ValueError("expected JSON response object")
            _log_usage(f"{kind}/{provider.name}", data)
            result = parse(data)
            log.info("grok request succeeded [%s] provider=%s", kind, provider.name)
            return result
        except Exception as exc:  # malformed responses also trigger the fallback
            status = (exc.response.status_code
                      if isinstance(exc, httpx.HTTPStatusError) else None)
            # Never include response bodies, headers or keys in failure logs.
            log.warning("grok request failed [%s] provider=%s error=%s status=%s; "
                        "trying next configured provider if available",
                        kind, provider.name, type(exc).__name__, status)
    raise RuntimeError(f"all configured Grok providers failed for {kind}")


def _log_usage(tag: str, data: dict) -> None:
    """Log Grok token consumption. chat/completions uses prompt/completion_tokens;
    the responses API uses input/output_tokens — handle both."""
    u = (data or {}).get("usage") or {}
    pt = u.get("prompt_tokens", u.get("input_tokens"))
    ct = u.get("completion_tokens", u.get("output_tokens"))
    tt = u.get("total_tokens", (pt or 0) + (ct or 0) if (pt or ct) else None)
    log.info("grok usage [%s] prompt=%s completion=%s total=%s cache_hit=%s cache_miss=%s returned_model=%s", tag, pt, ct, tt,
             u.get("prompt_cache_hit_tokens"), u.get("prompt_cache_miss_tokens"), data.get("model"))


# ───────── cache layer ─────────


def _ensure_tables(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS handle_analysis (
                handle TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS handle_deep (
                handle TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                payload TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        # A-method (cheap, no-search) enrichment result: category + is_project +
        # is_nft + devs + dev_projects + summary, derived from 6551 bio/tweets.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS handle_meta (
                handle TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        # One-time migration: drop legacy crypto_dev cache entries that lack
        # the new schema (max_mcap field). Idempotent — only removes rows
        # matching the old shape; new entries always contain "max_mcap".
        c.execute(
            "DELETE FROM handle_deep WHERE category='crypto_dev' "
            "AND payload NOT LIKE '%max_mcap%'"
        )
        c.commit()


def read_deep_cache(db_path: str, handle: str) -> dict | None:
    cutoff = int(time.time()) - DEEP_TTL
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT category, payload FROM handle_deep WHERE handle=? AND ts>=?",
            (handle.lower(), cutoff),
        ).fetchone()
    if not row:
        return None
    try:
        return {"category": row[0], **json.loads(row[1])}
    except Exception:
        return None


def write_deep_cache(db_path: str, handle: str, category: str, payload: dict) -> None:
    now = int(time.time())
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO handle_deep VALUES (?, ?, ?, ?)",
            (handle.lower(), category, json.dumps(payload, ensure_ascii=False), now),
        )
        c.commit()


# ───────── JSON extraction ─────────


def _extract_json(content: str) -> dict:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        if s.startswith("json\n"):
            s = s[5:]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


# ───────── deep agent analysis (Responses API + tools) ─────────


def _parse_devs_response(data: dict) -> list[str]:
    if data.get("error") or data.get("status") not in (None, "completed"):
        raise ValueError("incomplete or failed search response")
    for item in reversed(data.get("output", [])):
        if item.get("type") == "message":
            content = "".join(p.get("text", "") for p in item.get("content", [])
                              if p.get("type") == "output_text")
            if not content.strip():
                continue
            parsed = _extract_json(content)
            devs = parsed.get("devs")
            if not isinstance(devs, list) or not all(isinstance(d, str) for d in devs):
                raise ValueError("missing or invalid devs list")
            return _norm_handle_list(devs)
    raise ValueError("search returned no final message")


async def _call_responses(prompt: str, max_tool_calls: int = 3) -> list[str]:
    payload = {
        "input": prompt,
        "tools": [{"type": "x_search"}, {"type": "web_search"}],
        "max_tool_calls": max_tool_calls,
        "tool_choice": "auto",
    }
    return await _request_with_fallback("deep", "/responses", payload, _parse_devs_response)


# ───────── A-method: cheap batched enrichment (no web search) ─────────
#
# Takes 6551-sourced bio + recent tweets and, in ONE cheap chat/completions
# call (no tools), decides category / is_project / is_nft / devs / dev_projects
# / summary. This replaces expensive per-handle web searches for everything
# except "who are a project's devs when the bio doesn't say" (that escalates to
# find_devs_deep / B-method).


def _read_meta_cache(db_path: str, handles: list[str]) -> dict[str, dict]:
    cutoff = int(time.time()) - META_TTL
    out: dict[str, dict] = {}
    with sqlite3.connect(db_path) as c:
        for h in handles:
            row = c.execute(
                "SELECT payload, ts FROM handle_meta WHERE handle=? AND ts>=?",
                (h.lower(), cutoff),
            ).fetchone()
            if row:
                try:
                    meta = json.loads(row[0])
                    if os.environ.get("DEEPSEEK_API_KEY") and meta.get("_classifier_version") != 2:
                        continue
                    if meta.get("_evidence") == "search":
                        ttl = DEEP_TTL if meta.get("_verified") else 30 * 60
                        if int(time.time()) - row[1] >= ttl:
                            continue
                    elif _is_uninformative(meta):
                        # Ignore old seven-day entries created from empty inputs.
                        continue
                    out[h.lower()] = meta
                except Exception:  # noqa: BLE001
                    pass
    return out


def _write_meta_cache(db_path: str, results: dict[str, dict]) -> None:
    now = int(time.time())
    with sqlite3.connect(db_path) as c:
        for h, r in results.items():
            c.execute(
                "INSERT OR REPLACE INTO handle_meta VALUES (?, ?, ?)",
                (h.lower(), json.dumps(r, ensure_ascii=False), now),
            )
        c.commit()


ANALYZE_SYSTEM = (
    "你是加密 Twitter 分析助手。严格只输出符合要求的 JSON，"
    "不要任何额外文字、不要 markdown 代码块。"
)

ANALYZE_USER_TEMPLATE = """对下列每个账号，结合它的 bio 和近期推文判断（信息不足就保守判断）：
- category：project / crypto_dev / trader / other 之一
- is_project：这个账号本身是不是某个项目/产品/协议（true/false）。注意有些看起来像个人的账号其实是项目方。
- is_nft：是否主要是 NFT / PFP / 数字藏品类项目（true/false）
- is_launchpad：是否为发射台/代币发行平台（true/false）
- devs：若它是项目，列出能从 bio/推文判断出的「开发者/创始人」X handle（最多 3 个）。
        务必区分「Building @X / Founder of @X」(团队，算) 和「Backed by @VC / 投资方」(不算)。判断不出给空数组 []。
- dev_projects：若它本身是个人开发者(crypto_dev)，列出 ta 最近在做的项目 handle（最多 3 个），同样排除投资方。
- summary：≤ 18 字中文简介

账号数据：
{blocks}

只输出 JSON：
{{"results":[{{"handle":"vitalikbuterin","category":"crypto_dev","is_project":false,"is_nft":false,"devs":[],"dev_projects":["ethereum"],"summary":"以太坊联合创始人"}}]}}"""


def _norm_handle_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in v[:3]:
        h = str(x).strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def _default_meta() -> dict:
    return {
        "category": "other", "is_project": False, "is_nft": False,
        "devs": [], "dev_projects": [], "summary": "",
    }


def _is_uninformative(meta: dict) -> bool:
    summary = meta.get("summary") or ""
    return not summary.strip() or bool(re.search(
        r"信息不足|资料不足|无法(?:推断|判断)|未提供|缺乏.*(?:信息|资料)|暂无.*(?:信息|资料)|待联网核查",
        summary,
    ))


def _has_context(item: dict) -> bool:
    return bool((item.get("bio") or "").strip() or any(item.get("tweets") or []))


def _context_fingerprint(item: dict) -> str:
    data = json.dumps([item.get("bio") or "", item.get("tweets") or []], ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def needs_research(item: dict, meta: dict) -> bool:
    if meta.get("_evidence") == "search":
        return False  # including an explicit recent "not found" result
    return not _has_context(item) or _is_uninformative(meta)


def _parse_meta_response(data: dict, expected: set[str]) -> dict[str, dict]:
    choice = data["choices"][0]
    if choice.get("finish_reason") not in (None, "stop"):
        raise ValueError("incomplete classification response")
    parsed = _extract_json(choice["message"]["content"])
    results = parsed.get("results")
    if not isinstance(results, list):
        raise ValueError("missing results list")
    fresh = {}
    for item in results:
        h = str(item.get("handle", "")).lstrip("@").lower()
        if h not in expected:
            continue
        if (item.get("category") not in VALID_CATEGORIES
                or not isinstance(item.get("is_project"), bool)
                or not isinstance(item.get("is_nft"), bool)
                or not isinstance(item.get("summary"), str)):
            raise ValueError("invalid classification fields")
        for key in ("devs", "dev_projects"):
            value = item.get(key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"invalid {key} list")
        fresh[h] = {
            "category": item["category"],
            "is_project": item["is_project"], "is_nft": item["is_nft"],
            "is_launchpad": item.get("is_launchpad") is True,
            "devs": _norm_handle_list(item["devs"]),
            "dev_projects": _norm_handle_list(item["dev_projects"]),
            # The model's length instruction is not a reason to silently cut
            # an English name or a factual qualification in half.
            "summary": _short_summary(item["summary"]),
        }
    if expected - fresh.keys():
        raise ValueError("classification omitted requested handles")
    return fresh


async def analyze_batch(items: list[dict], db_path: str) -> dict[str, dict]:
    """A-method. items: [{handle, bio, tweets:[...]}]. Returns {handle_lower: meta}.

    meta keys: category, is_project, is_nft, devs[], dev_projects[], summary.
    Cached 7 days. On any failure, returns conservative defaults so callers can
    always proceed.
    """
    if not items:
        return {}
    _ensure_tables(db_path)
    handles = [it["handle"] for it in items]
    cached = _read_meta_cache(db_path, handles)
    for it in items:
        h = it["handle"].lower()
        m = cached.get(h)
        if (m and m.get("_evidence") != "search" and _has_context(it)
                and m.get("_context_fingerprint") != _context_fingerprint(it)):
            cached.pop(h)
    missing = [it for it in items if it["handle"].lower() not in cached]
    empty = [it for it in missing if not _has_context(it)]
    for it in empty:
        cached[it["handle"].lower()] = {**_default_meta(), "summary": "待联网核查"}
    missing = [it for it in missing if _has_context(it)]
    if empty:
        log.info("grok input missing for %d handles; queued for summary research", len(empty))
    if not missing:
        return cached
    if not _providers("basic"):
        for it in missing:
            cached[it["handle"].lower()] = _default_meta()
        return cached

    blocks = []
    for it in missing:
        bio = (it.get("bio") or "").replace("\n", " ").strip()[:240]
        tweets = it.get("tweets") or []
        tw = " / ".join(t.replace("\n", " ")[:120] for t in tweets[:3])[:360]
        blocks.append(f"@{it['handle']}\n  bio: {bio or '(无)'}\n  近期推文: {tw or '(无)'}")
    payload = {
        "messages": [
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": ANALYZE_USER_TEMPLATE.format(blocks="\n\n".join(blocks))},
        ],
        "temperature": 0.2,
    }
    try:
        missing_lower = {it["handle"].lower() for it in missing}
        fresh = await _request_with_fallback(
            "basic", "/chat/completions", payload,
            lambda data: _parse_meta_response(data, missing_lower),
        )
        for it in missing:
            m = fresh[it["handle"].lower()]
            m["_evidence"] = "provided"
            m["_classifier_version"] = 2
            m["_context_fingerprint"] = _context_fingerprint(it)
        _write_meta_cache(db_path, {h: m for h, m in fresh.items() if not _is_uninformative(m)})
        cached.update(fresh)
        log.info("grok analyze_batch enriched %d handles", len(fresh))
    except Exception as e:  # noqa: BLE001
        log.exception("grok analyze_batch failed: %s", e)
        for it in missing:
            cached[it["handle"].lower()] = _default_meta()
    return cached


def _short_summary(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 160 else text[:159].rstrip() + "…"


RESEARCH_PROMPT = """请实际联网核查 X 账号 @{handle}。不能仅根据 handle 猜测。
先搜索/打开该账号的资料与近期推文，再核对其官网或公开团队资料。
确认账号属于项目、加密开发者、交易员还是其他；如是项目，核查开发者/创始人（最多3个X handle），排除投资方。
找不到可核实资料时 information_found=false，并如实说明；不要杜撰账号简介、团队或KOL数量。
只输出 JSON，不要 markdown。简介为最多60字的完整中文句子，不要照抄英文简介；未经独立证实的宣传标注“自述”，不要混淆同名账号。
{{"results":[{{"handle":"{handle}","information_found":true,"category":"project","is_project":true,"is_nft":false,"devs":[],"dev_projects":[],"summary":"根据实际检索资料填写"}}]}}"""


def _parse_research_response(data: dict, handle: str) -> dict:
    if data.get("error") or data.get("status") not in (None, "completed"):
        raise ValueError("incomplete research response")
    output = data.get("output") or []
    searched = any(
        item.get("status") == "completed" and (
            item.get("type") == "web_search_call"
            or (item.get("type") == "custom_tool_call"
                and str(item.get("name", "")).startswith("x_"))
        ) for item in output
    )
    if not searched:
        raise ValueError("research returned no completed search tool call")
    for item in reversed(output):
        if item.get("type") != "message":
            continue
        parts = [p for p in item.get("content", []) if p.get("type") == "output_text"]
        text = "".join(p.get("text", "") for p in parts)
        parsed = _extract_json(text)
        meta = _parse_meta_response({"choices": [{"message": {"content": text}}]},
                                    {handle.lower()})[handle.lower()]
        row = next(r for r in parsed["results"] if r["handle"].lstrip("@").lower() == handle.lower())
        found = row.get("information_found")
        if not isinstance(found, bool):
            raise ValueError("missing verification result")
        sources = [a["url"] for p in parts for a in p.get("annotations", [])
                   if a.get("type") == "url_citation" and str(a.get("url", "")).startswith("https://")]
        for call in output:
            sources.extend(s["url"] for s in (call.get("action") or {}).get("sources", [])
                           if str(s.get("url", "")).startswith("https://"))
        if found and not sources:
            raise ValueError("research claims success but has no source URLs")
        if found and _is_uninformative(meta):
            raise ValueError("research claims success but lacks a usable description")
        if not found:
            meta = {**_default_meta(), "summary": "联网检索未找到可核实资料"}
        # Prefer the actual account, then cited sources in model order. An
        # alphabetical sort of every search hit can surface an unrelated site.
        sources = list(dict.fromkeys(sources))
        def source_priority(url: str) -> int:
            parsed_url = urlsplit(url)
            account = parsed_url.path.strip("/").split("/")[0].lower()
            return 0 if (parsed_url.hostname in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
                         and account == handle.lower()) else 1
        sources.sort(key=source_priority)
        return {**meta, "_evidence": "search", "_verified": found,
                "_classifier_version": 2,
                "_sources": sources[:10]}
    raise ValueError("research returned no final JSON")


async def research_account(handle: str, db_path: str) -> dict:
    """Use live search when upstream profiles/ordinary classification are insufficient."""
    _ensure_tables(db_path)
    cached = _read_meta_cache(db_path, [handle]).get(handle.lower())
    if cached and cached.get("_evidence") == "search":
        return cached
    try:
        meta = await _request_with_fallback("deep", "/responses", {
            "input": RESEARCH_PROMPT.format(handle=handle),
            "tools": [{"type": "x_search"}, {"type": "web_search"}],
            "tool_choice": "required", "max_tool_calls": 1,
        }, lambda data: _parse_research_response(data, handle))
        _write_meta_cache(db_path, {handle.lower(): meta})
        # Reuse team findings so the same account does not incur a second deep search.
        write_deep_cache(db_path, handle, "devsearch", {"devs": meta["devs"]})
        log.info("account research @%s verified=%s category=%s sources=%d",
                 handle, meta["_verified"], meta["category"], len(meta["_sources"]))
        return meta
    except Exception as exc:
        log.warning("account research @%s failed: %s", handle, type(exc).__name__)
        return {**_default_meta(), "summary": "联网核查失败，稍后重试"}


# ───────── B-method: slim web-search dev lookup (escalation only) ─────────


DEVFIND_PROMPT = """查 Twitter 加密项目 @{handle} 的「开发者 / 创始人 / 核心团队」是谁。
只要他们的 X handle（最多 3 个最核心的）。务必区分团队成员和投资机构——投资方不算。
只输出 JSON，不要 markdown：
{{"devs":["handle1","handle2"]}}"""


async def find_devs_deep(handle: str, db_path: str) -> list[str]:
    """B-method. Live web/x search to find a project's dev handles. Used only when
    A-method found none and the project is not NFT. Returns list of handles
    (possibly empty). Cached 24h under category 'devsearch'."""
    if not _providers("deep"):
        return []
    _ensure_tables(db_path)
    cached = read_deep_cache(db_path, handle)
    if cached and cached.get("category") == "devsearch":
        return _norm_handle_list(cached.get("devs"))
    prompt = DEVFIND_PROMPT.format(handle=handle)
    try:
        devs = await _call_responses(prompt, max_tool_calls=1)
        write_deep_cache(db_path, handle, "devsearch", {"devs": devs})
        log.info("find_devs_deep @%s -> %d devs", handle, len(devs))
        return devs
    except Exception as e:  # noqa: BLE001
        log.exception("find_devs_deep @%s failed: %s", handle, e)
        return []
