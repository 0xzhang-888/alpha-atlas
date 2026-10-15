"""Rule-based hourly research, separate from the latency-sensitive instant alert."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chain_preflight, deepseek, grok
from .frontrun import get_many
from .research_tools import profile

log = logging.getLogger("tg-summary.pipeline")


def category(meta: dict) -> str:
    if meta.get("is_project"):
        return "project"
    return meta.get("category") if meta.get("category") in grok.VALID_CATEGORIES else "other"


def research_mode(meta: dict, count: int, promoted: bool, followers_missing: bool, has_context: bool) -> str | None:
    if meta.get("is_nft") or meta.get("is_launchpad"):
        return "profile" if followers_missing else None
    if promoted:
        return "project"
    if category(meta) == "project" and not (meta.get("is_nft") or meta.get("is_launchpad")):
        return "project"
    if category(meta) == "crypto_dev" and count >= 2:
        return "crypto_dev"
    if grok._is_uninformative(meta) or not has_context:
        return "overview"
    if followers_missing:
        return "profile"
    return None


def report_lines(report: dict, mode: str, esc) -> list[str]:
    if report.get("_research_status") == "fallback_brief":
        return ["⚠️ DeepSeek 深研失败，暂用备用接口基础核查"]
    if mode == "crypto_dev":
        c = report.get("catalyst") or {}
        return ["🔍 可能催化：" + esc(c.get("finding", "尚未找到明确催化")), "（近期事件不等于关注的确定原因）"]
    if mode == "profile" or report.get("is_launchpad") or report.get("is_nft"):
        return []
    if mode not in {"project", "overview"} or (mode == "overview" and category(report) != "project"):
        return []
    out = []
    labels = {"read": "已读", "not_found": "未找到", "unavailable": "未能核实正文", "skipped": "未展开"}
    for field, label in (("website", "官网"), ("whitepaper", "白皮书/文档")):
        p = report.get(field) or {}
        url = p.get("url")
        link = f'<a href="{esc(url)}">{label}</a>' if url else label
        line = f"📄 {link}：{labels.get(p.get('status'), '待核查')}"
        if p.get("status") == "read" and p.get("partial"):
            line += "（正文摘录）"
        if p.get("finding"):
            line += " · " + esc(p["finding"])
        out.append(line)
    third = report.get("third_party") or {}
    if third.get("finding"):
        out.append("🗣 第三方介绍：" + esc(third["finding"]))
    for ca in report.get("official_ca") or []:
        out.append(f"🪙 官方 CA（{esc(ca.get('chain') or '链待核实')}）：<code>{esc(ca['address'])}</code>")
        out.append("叙事：" + esc(ca.get("narrative") or "证据不足，暂不判断"))
        out.append(f'<a href="{esc(ca["source"])}">官方发布/认领证据</a>')
    if not report.get("official_ca"):
        out.append("🪙 尚未查到可核实的官方 CA")
    return out


async def push_deepseek_summary(app, window_override: int | None = None) -> None:
    async with app.bot_data.setdefault("research_summary_lock", asyncio.Lock()):
        await _push(app, window_override)


async def research_after_chain_check(handle, mode, context, storage):
    """Ordering is per account: await its chain AI check before starting deep research."""
    if mode != "profile":
        try:
            context["chain_preflight"] = await chain_preflight.check(handle, context, storage)
        except Exception as exc:
            # A preflight outage must not disable the established deep research/fallback path.
            log.warning("chain preflight wrapper @%s failed error=%s", handle, type(exc).__name__)
            context["chain_preflight"] = chain_preflight.failure(handle, error_type=type(exc).__name__)
    return await deepseek.research(handle, mode, context, storage)


async def _push(app, window_override):
    # Import formatting lazily to keep the ordinary Grok-only path compatible.
    from . import bot
    storage = app.bot_data["storage"]
    window = window_override or app.bot_data["window_minutes"]
    rows = storage.aggregate_window(int(time.time()) - window * 60)
    if not rows:
        await bot._send(app, app.bot_data["target_chat_id"], f"📊 过去 {window} 分钟关注汇总\n\n暂无新关注动态。")
        return
    handles = [r[0] for r in rows]
    counts = {h: n for h, n, _ in rows}
    kols = {h: k for h, _, k in rows}
    fetched, kol_counts = await asyncio.gather(
        asyncio.gather(*(profile(h, storage) for h in handles)), get_many(handles))
    infos = dict(zip(handles, fetched))
    items = [{"handle": h, "bio": infos[h].get("bio", ""), "tweets": []} for h in handles]
    metas = {}
    for offset in range(0, len(items), 20):
        metas.update(await grok.analyze_batch(items[offset:offset + 20], storage.db_path))
    promoted = {h: storage.promote_other(h, category(metas.get(h.lower(), {})), kols[h]) for h in handles}
    modes = {h: research_mode(metas.get(h.lower(), {}), counts[h], promoted[h],
                             infos[h].get("followers") is None, bool(infos[h].get("bio"))) for h in handles}
    contexts = {h: {"bio": infos[h].get("bio", ""), "profile": infos[h],
                    "initial_category": category(metas.get(h.lower(), {})), "distinct_kols_in_window": counts[h],
                    "known_kols": storage.known_kols(h, kols[h]), "rule_promoted": promoted[h]} for h in handles}
    reports = {}
    candidates = []
    for h in handles:
        if modes[h]:
            hit = deepseek.cached_research(h, modes[h], contexts[h], storage)
            if hit:
                reports[h] = hit
            else:
                candidates.append(h)
    # Reuse cached reports; research every remaining eligible account, hottest first.
    # Per-account budgets and provider concurrency limits remain in their own layers.
    candidates.sort(key=lambda h: (-(counts[h] >= 2 or promoted[h]), -counts[h], -(kol_counts.get(h) or 0)))
    targets = candidates
    log.info("DeepSeek summary accounts=%d profiles_known=%d research=%d cached=%d limit=unlimited",
             len(handles), sum(i.get("followers") is not None for i in infos.values()), len(targets), len(reports))
    fresh = await asyncio.gather(*(research_after_chain_check(h, modes[h], contexts[h], storage) for h in targets))
    reports.update(zip(targets, fresh))
    for h, r in reports.items():
        # A profile-only query must not overwrite a known classification.
        if modes[h] != "profile":
            metas[h.lower()] = {**metas.get(h.lower(), {}), **r}
            if r.get("_verified") and r.get("_research_status") == "complete":
                # Preserve discovered identity: a developer found from an empty
                # profile must not return to "other" at the next hourly run.
                grok._ensure_tables(storage.db_path)
                grok._write_meta_cache(storage.db_path, {h.lower(): {**r, "_classifier_version": 2}})
        if (infos[h].get("followers") is None and r.get("followers") is not None
                and int(time.time()) - r.get("_researched_at", 0) <= 6 * 3600):
            infos[h].update(followers=r["followers"], source="搜索证据", observed_at=r.get("_researched_at"))
    for h in handles:
        if promoted[h]:
            metas[h.lower()] = {**metas.get(h.lower(), {}), "category": "project", "is_project": True}
    # Every additional X account actually shown (devs and their projects) gets a profile lookup too.
    related = set()
    for m in metas.values():
        related.update(m.get("devs") or [])
        related.update(m.get("dev_projects") or [])
    related = {h for h in related if h.lower() not in {x.lower() for x in handles}}
    extra = await asyncio.gather(*(profile(h, storage) for h in sorted(related)))
    related_kols = await get_many(sorted(related)) if related else {}
    all_kols = {h.lower(): n for h, n in {**kol_counts, **related_kols}.items()}
    related_infos = {h.lower(): p for h, p in zip(sorted(related), extra)}
    related_infos.update({h.lower(): infos[h] for h in handles})
    broadcasts = storage.next_broadcast_counts(handles)
    sections = {c: [] for c in bot.CATEGORIES}
    def rank(h):
        team = metas.get(h.lower(), {}).get("devs") or []
        return (-counts[h], -max([kol_counts.get(h) or 0] + [all_kols.get(d.lower()) or 0 for d in team]))
    for h in sorted(handles, key=rank):
        m = metas.get(h.lower(), {})
        cat = category(m)
        info = infos[h]
        followers = info.get("followers")
        ftext = f"粉丝 {followers:,}（{info.get('source', '资料')}快照）" if followers is not None else "粉丝：暂未查到"
        if followers is not None and info.get("observed_at"):
            ftext += datetime.fromtimestamp(info["observed_at"], ZoneInfo("Asia/Shanghai")).strftime(" %m-%d %H:%M")
        head = f'🔹 {bot.fmt_username(h)}{bot.fmt_broadcast_badge(broadcasts[h])}'
        if m.get("is_launchpad"):
            head += " 🚀发射台"
        if m.get("is_nft"):
            head += " 🖼NFT"
        block = [head, bot._meta_line([f"本群 {counts[h]}", bot._esc(ftext), f"全网 KOL {kol_counts[h]}" if kol_counts.get(h) is not None else ""])]
        if promoted[h]:
            block.append("📌 按新增关注规则纳入项目观察，身份仍以核查证据为准")
        if m.get("summary"):
            block.append(bot._esc(m["summary"]))
        for field, label in (("devs", "团队"), ("dev_projects", "在做")):
            names = []
            for d in (m.get(field) or [])[:3]:
                n = (related_infos.get(d.lower()) or {}).get("followers")
                names.append(bot.fmt_username(d) + (f'（粉丝 {n:,}）' if n is not None
                             else '（粉丝待核实）'))
            if names:
                block.append(label + "：" + " · ".join(names))
        if h in reports:
            block.extend(report_lines(reports[h], modes[h], bot._esc))
            sources = reports[h].get("_sources") or []
            if sources:
                block.append("🔎 " + " · ".join(f'<a href="{bot._esc(u)}">来源{i+1}</a>' for i, u in enumerate(sources[:3])))
        sections[cat].append(block)
    delivered = await bot._send(app, app.bot_data["target_chat_id"], bot.fmt_summary(window, sections))
    if delivered:
        storage.bump_broadcast_counts(handles)
        for h in handles:
            storage.mark_announced(h, kols[h])
        storage.purge_older_than(int(time.time()) - max(window * 4, 60 * 48) * 60)
