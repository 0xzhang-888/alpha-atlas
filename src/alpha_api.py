"""Read-only Alpha dashboard API. Never invokes research or Telegram delivery."""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from aiohttp import web
from .chain_evidence import text_evidence, structured_evidence, follow_evidence, merge_evidence, evidence_status, own_x_source

log = logging.getLogger("tg-summary.alpha")
WINDOW_SECONDS = 48 * 3600
MAX_ACCOUNTS = 400
MAX_EVENTS = 100_000
CACHE_SECONDS = 10
PUBLIC_CACHE_SECONDS = 30
PUBLIC_FAILURE_SECONDS = 5
PUBLIC_READ_TIMEOUT = 8
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
CHAIN_ALIASES = {
    "eth": "ethereum", "ethereum": "ethereum", "以太坊": "ethereum",
    "bsc": "bsc", "bnb": "bsc", "bnb chain": "bsc", "bnb smart chain": "bsc",
    "robinhood": "robinhood", "robinhood chain": "robinhood", "arc": "arc",
    "near": "near", "near protocol": "near", "sol": "solana", "solana": "solana",
    "base": "base", "arb": "arbitrum", "arbitrum": "arbitrum", "arbitrum one": "arbitrum",
    "op": "optimism", "optimism": "optimism", "op mainnet": "optimism",
    "btc": "bitcoin", "bitcoin": "bitcoin", "polygon": "polygon", "polygon pos": "polygon",
    "avax": "avalanche-c", "avalanche": "avalanche-c", "avalanche c-chain": "avalanche-c",
    "sui": "sui", "aptos": "aptos", "ton": "ton", "tron": "tron", "starknet": "starknet",
    "cosmos": "cosmos-hub", "cosmos hub": "cosmos-hub", "osmosis": "osmosis", "celo": "celo",
    "megaeth": "megaeth", "scroll": "scroll", "linea": "linea", "monad": "monad",
    "zksync": "zksync", "zksync era": "zksync", "berachain": "berachain", "sonic": "sonic",
    "hyperliquid": "hyperliquid", "hyperevm": "hyperevm", "mantle": "mantle", "opbnb": "opbnb",
}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


def text(value, limit=1400):
    return value[:limit] if isinstance(value, str) else ""


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def observed_ts(value, fallback, now):
    return value if number(value) is not None and 0 < value <= now else fallback


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        u = urlsplit(value)
        host = (u.hostname or "").lower()
        if u.scheme not in {"https", "http"} or not host or u.username or u.password:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        params = unquote(unquote(u.query + "&" + u.fragment))
        if re.search(r"(?:^|[?&#;/])(?:api[_-]?key|access[_-]?token|token|authorization|secret|password|signature|sig|auth|key|jwt|code)=", params, re.I):
            return None
        return value
    except ValueError:
        return None


def urls(values):
    return list(dict.fromkeys(u for v in values[:10] if (u := public_url(v)))) if isinstance(values, list) else []


def payload(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def normalized_chain(value):
    value = text(value, 60).strip().lower()
    if value in {"", "unknown", "unknown chain", "待核实", "链待核实", "未确认", "evm", "multi-chain", "multichain"}:
        return None
    # Keep genuinely new chain names extensible, never infer Ethereum from an address.
    return CHAIN_ALIASES.get(value) or (value if re.fullmatch(r"[a-z][a-z0-9-]{1,35}", value) else None)


def document(value):
    value = value if isinstance(value, dict) else {}
    status = value.get("status") if value.get("status") in {"read", "not_found", "unavailable", "skipped"} else "unavailable"
    return {"status": status, "url": public_url(value.get("url")),
            "finding": text(value.get("finding")) or "当前数据库暂无可用研究记录。",
            "partial": bool(value.get("partial"))}


def read_snapshot(db_path: str, now: int | None = None, *, private_terms: set[str] | None = None) -> dict:
    """Create and close the read-only connection inside the calling worker thread."""
    now = int(time.time()) if now is None else now
    cutoff = now - WINDOW_SECONDS
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "follow_events" not in tables:
            raise sqlite3.OperationalError("not ready")
        if private_terms is not None:
            # This side channel never enters the JSON snapshot. Names may also occur
            # in research prose, even though the event API already omits identities.
            private_terms.update(r[0].strip() for r in conn.execute(
                "SELECT DISTINCT kol FROM follow_events WHERE trim(kol) != ''"
            ) if isinstance(r[0], str))
            if "account_watch" in tables:
                for row in conn.execute("SELECT announced_kols FROM account_watch"):
                    try:
                        announced = json.loads(row[0])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(announced, list):
                        private_terms.update(v.strip() for v in announced if isinstance(v, str) and v.strip())
        events = conn.execute(
            "SELECT project_handle, kol, ts FROM follow_events WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
            (cutoff, now, MAX_EVENTS + 1),
        ).fetchall()
        event_limited = len(events) > MAX_EVENTS
        grouped = {}
        for row in events[:MAX_EVENTS]:
            handle = row["project_handle"]
            if not HANDLE.fullmatch(handle or ""):
                continue
            key = handle.lower()
            item = grouped.setdefault(key, {"handle": handle, "kols": set(), "fast": set(), "last": row["ts"]})
            if item["handle"].islower() and not handle.islower():
                item["handle"] = handle
            kol = (row["kol"] or "").strip().casefold()
            if kol:
                item["kols"].add(kol)
                if row["ts"] >= now - 180:
                    item["fast"].add(kol)

        handles = list(grouped)
        # Bounded batches keep parameter limits safe without scanning page/tweet caches.
        def rows_for(table, columns, keys, key_column="handle"):
            if table not in tables:
                return []
            out = []
            for start in range(0, len(keys), 300):
                batch = keys[start:start + 300]
                marks = ",".join("?" for _ in batch)
                out.extend(conn.execute(f"SELECT {columns} FROM {table} WHERE {key_column} IN ({marks})", batch).fetchall())
            return out

        metas = {r["handle"]: (payload(r["payload"]), r["ts"]) for r in rows_for("handle_meta", "handle,payload,ts", handles)}
        source_bios = {r['handle']: r for r in rows_for('source_profiles', 'handle,bio,ts', handles)}
        edges = rows_for('following_edges', 'follower,followed,ts', handles, 'follower')
        edges_by_handle = {}
        for edge in edges:
            if now - 30 * 86400 <= edge['ts'] <= now:
                edges_by_handle.setdefault(edge['follower'], []).append(dict(edge))
        followers = {r["handle"]: r for r in rows_for("profile_followers", "handle,followers,ts", handles)}
        watches = {r["handle"]: bool(r["promoted"]) for r in rows_for("account_watch", "handle,promoted", handles)}
        history = {r["handle"]: r for r in rows_for("summary_history", "handle,broadcast_count,last_broadcast_ts", handles)}
        keys = [f"profile:v1:{h}" for h in handles]
        keys.extend(f'x-account-evidence:v1:{h}' for h in handles)
        keys.extend(f'chain-check:v2:{h}' for h in handles)
        keys.extend(f"deepseek:v3:{h}:{mode}" for h in handles for mode in ("project", "crypto_dev", "overview", "profile"))
        cache = {r["cache_key"]: (payload(r["payload"]), r["ts"]) for r in rows_for("research_cache", "cache_key,payload,ts", keys, "cache_key")}
        all_reports = [value[0] for key, value in cache.items() if key.startswith("deepseek:")]
        all_reports.extend(value[0] for value in metas.values())
        page_keys = list({'page:v1:' + hashlib.sha256(doc['url'].encode()).hexdigest()
                          for report in all_reports for name in ('website','whitepaper')
                          if isinstance(doc := report.get(name), dict) and doc.get('status') == 'read' and public_url(doc.get('url'))})
        pages = {r['cache_key']: (payload(r['payload']), r['ts']) for r in rows_for('research_cache', 'cache_key,payload,ts', page_keys, 'cache_key')}
        ca_keys = list({"market:v1:" + c["address"] for report in all_reports
                        for c in (report.get("official_ca") or []) if isinstance(c, dict) and isinstance(c.get("address"), str)})
        markets = {r["cache_key"]: (payload(r["payload"]), r["ts"]) for r in rows_for("research_cache", "cache_key,payload,ts", ca_keys, "cache_key")}
        # Release the read lock before JSON shaping; no changes are committed.
        conn.rollback()
    finally:
        conn.close()

    records = []
    excluded = {"unclassified": 0, "other": 0, "trader": 0}
    for key, event in grouped.items():
        meta, meta_row_ts = metas.get(key, ({}, 0))
        meta_ts = observed_ts(meta.get("_researched_at"), meta_row_ts, now)
        if now - meta_ts > 7 * 86400:
            meta = {}
        candidates = [(cache[k][0], observed_ts(cache[k][0].get("_researched_at"), cache[k][1], now))
                      for mode in ("project", "crypto_dev", "overview") if (k := f"deepseek:v3:{key}:{mode}") in cache]
        if meta.get("_mode") != "profile" and meta.get("_researched_at"):
            candidates.append((meta, meta_ts))
        report, report_ts = max(candidates, key=lambda pair: pair[1], default=({}, 0))
        if report and now - report_ts <= 7 * 86400:
            identity = {**meta, **report} if report_ts > meta_ts else {**report, **meta}
        else:
            identity = meta
        category = "project" if identity.get("is_project") else identity.get("category", "unclassified")
        if category not in {"project", "crypto_dev", "other", "trader"}:
            category = "unclassified"
        promoted = watches.get(key, False)
        if category not in {"project", "crypto_dev"} and not promoted:
            excluded[category] += 1
            continue
        view_category = category if category in {"project", "crypto_dev"} else "project"

        profile, profile_ts = cache.get(f"profile:v1:{key}", ({}, 0))
        profile_ts = observed_ts(profile.get("observed_at"), profile_ts, now)
        follower_count, follower_at, follower_source = None, None, None
        channel = followers.get(key)
        if channel and now - channel["ts"] <= 86400 and number(channel["followers"]) is not None:
            follower_count, follower_at, follower_source = channel["followers"], channel["ts"], "监控消息中的资料快照"
        elif now - profile_ts <= 21600 and number(profile.get("followers")) is not None:
            follower_count, follower_at, follower_source = profile["followers"], profile_ts, "X 资料缓存"
        else:
            reports_for_followers = candidates + ([cache[f"deepseek:v3:{key}:profile"]] if f"deepseek:v3:{key}:profile" in cache else [])
            reports_for_followers = [(r, observed_ts(r.get("_researched_at"), ts, now)) for r, ts in reports_for_followers]
            for r, ts in sorted(reports_for_followers, key=lambda pair: pair[1], reverse=True):
                if now - ts <= 21600 and number(r.get("followers")) is not None and public_url(r.get("followers_source")):
                    follower_count, follower_at, follower_source = r["followers"], ts, "有来源的研究快照"
                    break

        website, whitepaper = document(report.get("website")), document(report.get("whitepaper"))
        cas, chain_evidence = [], []
        for ca in (report.get("official_ca") or [])[:10]:
            if not isinstance(ca, dict) or not public_url(ca.get("source")) or not text(ca.get("address"), 200):
                continue
            cas.append({"address": text(ca["address"], 200), "chain": text(ca.get("chain"), 60),
                        "source": public_url(ca["source"]), "narrative": text(ca.get("narrative"), 500)})
            chain = normalized_chain(ca.get("chain"))
            if chain:
                chain_evidence.append({"chain": chain, "relation": "reported_token_chain", "source": ca["source"],
                                       "finding": "官方 CA 研究记录标注的链；链字段尚未经独立部署核验。"})
            market, market_ts = markets.get("market:v1:" + ca["address"], ({}, 0))
            if now - market_ts <= 86400:
                for pair in (market.get("pairs") or [])[:10]:
                    if isinstance(pair, dict) and (chain := normalized_chain(pair.get("chainId"))) and public_url(pair.get("url")):
                        chain_evidence.append({"chain": chain, "relation": "token_market", "source": pair["url"],
                                               "finding": "该官方 CA 的交易对链证据，不代表项目全部部署链。"})
        # Early-stage affiliation is independent of a token or CA.
        bio_candidates = [(profile.get('bio', ''), profile_ts)]
        if key in source_bios:
            bio_candidates.append((source_bios[key]['bio'], source_bios[key]['ts']))
        bio, bio_ts = max(((b, ts) for b, ts in bio_candidates if b and now-7*86400 <= ts <= now), default=('', 0), key=lambda p:p[1])
        chain_evidence = merge_evidence(text_evidence(bio, f'https://x.com/{event["handle"]}', 'bio', bio_ts), chain_evidence)
        report_category = 'project' if report.get('is_project') else report.get('category')
        if report and report_ts and now-report_ts <= 7*86400 and report_category == category:
            if report.get('_chain_evidence_version') == 1:
                chain_evidence = merge_evidence(chain_evidence, structured_evidence(report.get('chain_evidence')))
            for doc in (website, whitepaper):
                if doc['status'] != 'read' or not doc['url']:
                    continue
                page, page_ts = pages.get('page:v1:' + hashlib.sha256(doc['url'].encode()).hexdigest(), ({}, 0))
                # Saved findings explicitly labelled as research, not live deployment verification.
                chain_evidence = merge_evidence(chain_evidence, text_evidence(doc['finding'], doc['url'], 'document', report_ts))
                if page.get('read') and now-7*86400 <= page_ts <= now:
                    chain_evidence = merge_evidence(chain_evidence, text_evidence(page.get('text', ''), doc['url'], 'document', page.get('fetched_at') or page_ts))
        posts, posts_ts = cache.get(f'x-account-evidence:v1:{key}', ({}, 0))
        if now - 7*86400 <= posts_ts <= now:
            for post in (posts.get('posts') or [])[:20]:
                if (isinstance(post, dict) and own_x_source(post.get('url'), key, post=True)
                        and str(post.get('author', '')).lower() == key and not post.get('is_retweet')):
                    chain_evidence = merge_evidence(chain_evidence, text_evidence(post.get('text', ''), post['url'], 'official_post', posts.get('searched_at') or posts_ts))
        check, check_ts = cache.get(f'chain-check:v2:{key}', ({}, 0))
        check_at = number(check.get('checked_at')) or 0
        check_status = check.get('status')
        check_ttl = {'complete': 21600, 'unknown': 3600, 'failed': 300}.get(check_status, 0)
        check_valid = (check.get('version') == 2 and check.get('handle') == key
                       and 0 < check_at <= now and 0 <= now-check_at < check_ttl)
        if check_valid and check_status == 'complete' and check.get('ai_checked') is True:
            # Preflight has already checked quotes against raw evidence. Recheck its safe public shape.
            for item in (check.get('chain_evidence') or [])[:6]:
                if not isinstance(item, dict) or item.get('kind') not in {'bio','official_post'}:
                    continue
                source = public_url(item.get('source'))
                chain = normalized_chain(item.get('chain'))
                if not chain or not source or not own_x_source(source, key, post=item['kind']=='official_post'):
                    continue
                relation = 'planned_launch' if item.get('relation') == 'planned_launch' else item['kind']
                chain_evidence = merge_evidence(chain_evidence, [{'chain':chain, 'relation':relation,
                    'source':source, 'finding':text(item.get('quote'),450), 'observedAt':check_at,
                    'confidence':'planned' if relation=='planned_launch' else 'declared'}])
        # Weak follows are a fallback, not a reason to expand a clear official declaration to every chain.
        if not chain_evidence:
            chain_evidence = follow_evidence(key, edges_by_handle.get(key, []))
        chain_evidence = [e for e in chain_evidence if e.get('relation') == 'follows_official' or public_url(e.get('source'))]
        for evidence_row in chain_evidence:
            evidence_row['observedAt'] = iso(observed_ts(evidence_row.get('observedAt'), report_ts, now))
        chain_ids = list(dict.fromkeys(e["chain"] for e in chain_evidence))
        catalyst = report.get("catalyst") if isinstance(report.get("catalyst"), dict) else {}
        third = report.get("third_party") if isinstance(report.get("third_party"), dict) else {}
        age = now - report_ts if report_ts else None
        ttl = 3600 if report.get("_mode") == "crypto_dev" else 86400
        status = "unavailable" if not report else "stale" if age > ttl else "snapshot"
        if report and not report.get("_verified"):
            status = "insufficient"
        labels = ["NFT"] if identity.get("is_nft") else ["发射台"] if identity.get("is_launchpad") else []
        labels += ["规则纳入观察"] if promoted else []
        bcast = history.get(key)
        records.append({
            "id": key, "name": text(profile.get("name"), 80) or event["handle"], "handle": event["handle"],
            "category": view_category, "originalCategory": category, "watchlisted": promoted, "demo": False,
            "chains": chain_ids, "chainStatus": evidence_status(chain_evidence), "chainEvidence": chain_evidence,
            "chainCheck": {"status": check_status if check_valid else 'not_checked',
                           "checkedAt": iso(check_at) if check_valid else None,
                           "aiChecked": bool(check_valid and check.get('ai_checked') is True),
                           "model": text(check.get('model'), 80) if check_valid else None},
            "narrative": " · ".join(labels) or ("加密开发者" if view_category == "crypto_dev" else "项目观察"),
            "summary": text(identity.get("summary")) or "尚无可用研究摘要；关注信号不代表项目身份已核实。",
            "followers": follower_count, "followersObservedAt": iso(follower_at), "followersSource": follower_source,
            "distinctKols": len(event["kols"]), "distinctKols3m": len(event["fast"]), "signal3m": len(event["fast"]) >= 2,
            "lastEventAt": iso(event["last"]), "researchStatus": status, "researchedAt": iso(report_ts),
            "researchContext": "已保存研究快照，网页刷新不重新调用模型，也不代表重新核验。",
            "website": website, "whitepaper": whitepaper, "officialCa": cas,
            "thirdParty": {"finding": text(third.get("finding")), "sources": urls(third.get("sources"))},
            "catalyst": text(catalyst.get("finding")) or "当前暂无有证据的关注原因记录。",
            "catalystSources": urls(catalyst.get("sources")),
            "catalystEvents": [{"date": text(e.get("date"), 40), "finding": text(e.get("finding"), 500), "source": public_url(e.get("source"))}
                               for e in (catalyst.get("events") or [])[:8] if isinstance(e, dict)],
            "sources": urls(report.get("_sources") or identity.get("_sources")),
            "devs": [h for h in (identity.get("devs") or [])[:5] if isinstance(h, str) and HANDLE.fullmatch(h)],
            "devProjects": [h for h in (identity.get("dev_projects") or [])[:5] if isinstance(h, str) and HANDLE.fullmatch(h)],
            "broadcastCount": bcast["broadcast_count"] if bcast else 0,
        })
    records.sort(key=lambda r: (-int(r["signal3m"]), -r["distinctKols"], r["id"]))
    return {"schemaVersion": 1, "source": "railway-monitor", "generatedAt": iso(now),
            "windowHours": 48, "windowStart": iso(cutoff), "windowEnd": iso(now),
            "lastEventAt": iso(max((e["last"] for e in grouped.values()), default=0)),
            "refreshSeconds": 30, "records": records[:MAX_ACCOUNTS],
            "totals": {"observed": len(grouped), "visible": len(records), **excluded},
            "truncated": event_limited or len(records) > MAX_ACCOUNTS}


class PublicProjection:
    """A second, explicit boundary between saved research and anonymous visitors."""
    credential = re.compile(
        r"\bsk-[A-Za-z0-9_-]{12,}|(?<!\d)\d{6,}:[A-Za-z0-9_-]{20,}|"
        r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*|"
        r"\b(?:api[_-]?key|access[_-]?token|authorization|password|client[_-]?secret|session[_-]?secret|token|secret|signature)"
        r"\s*[=:]\s*[\"']?[^\s,;\"'<>]{4,}", re.I)
    url_pattern = re.compile(r"https?://[^\s<>\"']+", re.I)
    sensitive_parameter = re.compile(
        r"(?:token|secret|password|signature|credential|authorization|session|apikey|privatekey)|"
        r"^(?:auth|key|jwt|code|sig|pass|bearer)$", re.I)

    def __init__(self, private_terms=(), secrets=()):
        terms = {v.strip().lstrip("@") for v in private_terms if isinstance(v, str) and v.strip()}
        terms.update(h for term in tuple(terms) for h in re.findall(r"@([A-Za-z0-9_]{1,15})", term))
        self.private_terms = {v.casefold() for v in terms}
        self.term_patterns = []
        for term in sorted(terms, key=len, reverse=True):
            if re.fullmatch(r"[A-Za-z0-9_ ]+", term):
                # "AI", "K" and other short labels must not erase ordinary prose.
                prefix = "@" if len(term) < 3 else "@?"
                pattern = r"(?<![A-Za-z0-9_])" + prefix + re.escape(term) + r"(?![A-Za-z0-9_])"
            elif len(term) >= 2:
                pattern = re.escape(term)
            else:
                continue
            self.term_patterns.append(re.compile(pattern, re.I))
        self.secrets = sorted({v for v in secrets if isinstance(v, str) and len(v) >= 4}, key=len, reverse=True)

    def url(self, value):
        value = public_url(value)
        if not value:
            return None
        decoded = value
        for _ in range(3):
            decoded = unquote(decoded)
        if (self.credential.search(decoded) or any(s in decoded for s in self.secrets)
                or any(p.search(decoded) for p in self.term_patterns)
                or any(ord(c) < 32 for c in decoded)):
            return None
        parsed = urlsplit(decoded)
        if parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            account = parsed.path.strip("/").split("/")[0].casefold()
            if account in self.private_terms:
                return None
        for key, _ in parse_qsl(parsed.query + "&" + parsed.fragment, keep_blank_values=True):
            if self.sensitive_parameter.search(re.sub(r"[^a-z0-9]", "", key.lower())):
                return None
        return value

    def text(self, value, limit=1400):
        value = text(value, limit * 2)
        for secret in self.secrets:
            value = value.replace(secret, "[已隐藏]")
        value = self.credential.sub("[凭据已隐藏]", value)
        value = self.url_pattern.sub(lambda m: m[0] if self.url(m[0]) else "[链接已隐藏]", value)
        for pattern in self.term_patterns:
            value = pattern.sub("[观察者]", value)
        return value[:limit]

    def handle(self, value, *, project=False):
        return value if (isinstance(value, str) and HANDLE.fullmatch(value)
                         and (project or value.casefold() not in self.private_terms)) else None

    def urls(self, value):
        return list(dict.fromkeys(u for v in value[:10] if (u := self.url(v)))) if isinstance(value, list) else []

    @staticmethod
    def timestamp(value):
        if not isinstance(value, str) or len(value) > 40:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if parsed.tzinfo else None
        except ValueError:
            return None

    def document(self, value):
        value = value if isinstance(value, dict) else {}
        return {"status": value.get("status") if value.get("status") in {"read", "not_found", "unavailable", "skipped"} else "unavailable",
                "url": self.url(value.get("url")), "finding": self.text(value.get("finding")),
                "partial": value.get("partial") is True}

    def evidence(self, values):
        out = []
        for item in values[:30] if isinstance(values, list) else []:
            if not isinstance(item, dict) or not (chain := normalized_chain(item.get("chain"))):
                continue
            # Extensible chain identifiers are text too. Normalization lowercases
            # them, so compare known credentials without losing that boundary.
            if self.credential.search(chain) or any(secret.casefold() in chain for secret in self.secrets):
                continue
            relation = item.get("relation")
            if relation not in {"bio", "official_post", "research_document", "planned_launch", "follows_official", "reported_token_chain", "token_market"}:
                continue
            source = self.url(item.get("source"))
            if relation != "follows_official" and not source:
                continue
            out.append({"chain": chain, "relation": relation,
                        "source": None if relation == "follows_official" else source,
                        "finding": "根据已保存的公开关注关系推定所属生态，尚未获得项目方部署声明。" if relation == "follows_official" else self.text(item.get("finding"), 500),
                        "observedAt": self.timestamp(item.get("observedAt")),
                        "confidence": item.get("confidence") if item.get("confidence") in {"declared", "planned", "documented", "inferred"} else "inferred"})
        return out

    def snapshot(self, raw):
        records = []
        for item in raw.get("records", [])[:MAX_ACCOUNTS]:
            if not isinstance(item, dict) or not (handle := self.handle(item.get("handle"), project=True)):
                continue
            if item.get("category") not in {"project", "crypto_dev"}:
                continue
            evidence = self.evidence(item.get("chainEvidence"))
            check = item.get("chainCheck") if isinstance(item.get("chainCheck"), dict) else {}
            third = item.get("thirdParty") if isinstance(item.get("thirdParty"), dict) else {}
            cas = []
            for ca in item.get("officialCa", [])[:10] if isinstance(item.get("officialCa"), list) else []:
                if not isinstance(ca, dict) or not (source := self.url(ca.get("source"))):
                    continue
                address = ca.get("address")
                if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{8,200}", address) or self.credential.search(address) or any(s in address for s in self.secrets):
                    continue
                cas.append({"address": address, "chain": self.text(ca.get("chain"), 60),
                            "source": source, "narrative": self.text(ca.get("narrative"), 500)})
            events = []
            for event in item.get("catalystEvents", [])[:8] if isinstance(item.get("catalystEvents"), list) else []:
                if isinstance(event, dict) and (source := self.url(event.get("source"))):
                    events.append({"date": self.text(event.get("date"), 40), "finding": self.text(event.get("finding"), 500), "source": source})
            count, fast = number(item.get("distinctKols")) or 0, number(item.get("distinctKols3m")) or 0
            records.append({
                "id": handle.lower(), "name": self.text(item.get("name"), 80) or handle, "handle": handle,
                "category": item["category"], "demo": False,
                "chains": list(dict.fromkeys(e["chain"] for e in evidence)), "chainStatus": evidence_status(evidence), "chainEvidence": evidence,
                "chainCheck": {"status": check.get("status") if check.get("status") in {"complete", "unknown", "failed", "not_checked"} else "not_checked",
                               "checkedAt": self.timestamp(check.get("checkedAt")), "aiChecked": check.get("aiChecked") is True},
                "narrative": self.text(item.get("narrative"), 100), "summary": self.text(item.get("summary")),
                "followers": number(item.get("followers")), "followersObservedAt": self.timestamp(item.get("followersObservedAt")),
                "followersSource": self.text(item.get("followersSource"), 80),
                "distinctKols": count, "distinctKols3m": min(fast, count), "signal3m": min(fast, count) >= 2,
                "lastEventAt": self.timestamp(item.get("lastEventAt")),
                "researchStatus": item.get("researchStatus") if item.get("researchStatus") in {"unavailable", "stale", "snapshot", "insufficient"} else "unavailable",
                "researchedAt": self.timestamp(item.get("researchedAt")),
                "researchContext": "已保存研究快照；页面刷新只读取数据，后台研究独立更新。",
                "website": self.document(item.get("website")), "whitepaper": self.document(item.get("whitepaper")), "officialCa": cas,
                "thirdParty": {"finding": self.text(third.get("finding")), "sources": self.urls(third.get("sources"))},
                "catalyst": self.text(item.get("catalyst")), "catalystSources": self.urls(item.get("catalystSources")),
                "catalystEvents": events, "sources": self.urls(item.get("sources")),
                "devs": [h for v in item.get("devs", [])[:5] if (h := self.handle(v))] if isinstance(item.get("devs"), list) else [],
                "devProjects": [h for v in item.get("devProjects", [])[:5] if (h := self.handle(v))] if isinstance(item.get("devProjects"), list) else [],
            })
        totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
        return {"schemaVersion": 1, "source": "alpha-atlas-public", "scope": "public",
                "generatedAt": self.timestamp(raw.get("generatedAt")), "windowHours": 48,
                "windowStart": self.timestamp(raw.get("windowStart")), "windowEnd": self.timestamp(raw.get("windowEnd")),
                "lastEventAt": self.timestamp(raw.get("lastEventAt")), "refreshSeconds": PUBLIC_CACHE_SECONDS,
                "records": records, "totals": {"observed": number(totals.get("observed")) or 0, "visible": len(records),
                    "projects": sum(r["category"] == "project" for r in records), "developers": sum(r["category"] == "crypto_dev" for r in records)},
                "truncated": raw.get("truncated") is True}


def _public_secrets(token):
    values = {token}
    for name, value in os.environ.items():
        if re.search(r"TOKEN|SECRET|PASSWORD|API_?KEY|AUTH|(?:CHAT|CHANNEL|GROUP)_?ID", name, re.I):
            values.add(value)
            if value.startswith("{"):
                values.update(v for v in payload(value).values() if isinstance(v, str))
    return values


class PublicSnapshotCache:
    def __init__(self, db_path, secrets):
        self.db_path, self.secrets = db_path, secrets
        self.cached, self.cache_at, self.retry_at, self.task = None, 0.0, 0.0, None

    async def refresh(self):
        private_terms = set()
        try:
            raw = await asyncio.to_thread(read_snapshot, self.db_path, private_terms=private_terms)
            self.cached = PublicProjection(private_terms, self.secrets).snapshot(raw)
            self.cache_at, self.retry_at = time.monotonic(), 0.0
            return self.cached
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
            self.retry_at = time.monotonic() + PUBLIC_FAILURE_SECONDS
            log.warning("public dashboard snapshot unavailable")
            return None

    async def get(self):
        now = time.monotonic()
        if now < self.retry_at:
            return None
        if self.cached is not None and now - self.cache_at < PUBLIC_CACHE_SECONDS:
            return self.cached
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.refresh())
        try:
            return await asyncio.wait_for(asyncio.shield(self.task), PUBLIC_READ_TIMEOUT)
        except TimeoutError:
            # The worker can still finish. Keep its task shared across requests;
            # cancelling a to_thread waiter would leave an untracked DB read.
            self.retry_at = time.monotonic() + PUBLIC_FAILURE_SECONDS
            return None

    async def close(self, app):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


def create_api(db_path: str, token: str, *, dashboard_config: str = "", dashboard_dir: str = "",
               public_enabled: bool = False, public_dir: str = "/app/showcase") -> web.Application:
    if not token or len(token) < 32:
        raise ValueError("dashboard token must contain at least 32 characters")
    expected = ("Bearer " + token).encode()
    access = None
    if dashboard_config:
        from .dashboard_access import DashboardAccess
        access = DashboardAccess(dashboard_config, dashboard_dir)
    cached, cache_at = None, 0.0
    lock = asyncio.Lock()
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}

    async def health(request):
        return web.json_response({"status": "ok"}, headers=headers)

    async def alpha(request):
        nonlocal cached, cache_at
        provided = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(provided, expected) and not (access and access.authorized(request)):
            return web.json_response({"error": "unauthorized"}, status=401, headers=headers)
        if request.query_string:
            return web.json_response({"error": "unsupported_parameters"}, status=400, headers=headers)
        try:
            async with asyncio.timeout(8):
                async with lock:
                    if cached is None or time.monotonic() - cache_at >= CACHE_SECONDS:
                        cached = await asyncio.to_thread(read_snapshot, db_path)
                        cache_at = time.monotonic()
            return web.json_response(cached, headers=headers)
        except (sqlite3.Error, OSError, ValueError, TypeError, TimeoutError) as exc:
            log.warning("dashboard read unavailable: %s", type(exc).__name__)
            return web.json_response({"error": "data_unavailable"}, status=503, headers=headers)

    app = web.Application(client_max_size=4096)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/alpha", alpha, allow_head=False)
    if access:
        access.install(app)
    if public_enabled:
        from .dashboard_access import PublicShowcase
        showcase = PublicShowcase(public_dir)
        public_cache = PublicSnapshotCache(db_path, _public_secrets(token))
        public_headers = {"Cache-Control": "public, max-age=30", "X-Content-Type-Options": "nosniff",
                          "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}

        async def public_alpha(request):
            if request.query_string:
                return web.json_response({"error": "unsupported_parameters"}, status=400, headers=headers)
            data = await public_cache.get()
            if data is None:
                return web.json_response({"error": "data_unavailable"}, status=503,
                                         headers={**headers, "Retry-After": str(PUBLIC_FAILURE_SECONDS)})
            return web.json_response(data, headers=public_headers)

        app.router.add_get("/api/public/alpha", public_alpha, allow_head=False)
        showcase.install(app)
        app.on_cleanup.append(public_cache.close)
    return app


async def start_api(db_path: str, token: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_api(db_path, token,
                          dashboard_config=os.environ.get("ALPHA_WEB_AUTH", ""),
                          dashboard_dir=os.environ.get("ALPHA_WEB_DIR", "/app/dashboard"),
                          public_enabled=os.environ.get("ALPHA_PUBLIC_ENABLED") == "1",
                          public_dir=os.environ.get("ALPHA_PUBLIC_DIR", "/app/showcase")),
                          access_log=None, shutdown_timeout=10)
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info("read-only Alpha API ready; port=%d; refresh cache=%ds", port, CACHE_SECONDS)
    return runner
