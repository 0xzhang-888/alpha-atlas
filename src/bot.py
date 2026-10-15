from __future__ import annotations

import asyncio
import html
import logging
import os
import signal
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import opentwitter as ot
from .frontrun import get_kol_followers, get_many
from .grok import analyze_batch, find_devs_deep, log_provider_config, needs_research, research_account
from .parser import parse_message
from .storage import Storage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tg-summary")


CATEGORY_LABEL = {
    "project": "🏗️ 项目方",
    "crypto_dev": "🧑‍💻 加密开发者",
    "trader": "💹 交易员",
    "other": "👤 其他",
}
CATEGORIES = ("project", "crypto_dev", "trader", "other")


def alert_tier_emoji(count: int) -> str:
    """Color-coded badge. >5 red, >2 yellow."""
    if count > 5:
        return "🔴"
    if count > 2:
        return "🟡"
    if count >= 1:
        return "⚪"
    return ""


def fmt_alert_badge(count: int) -> str:
    if count <= 0:
        return ""
    return f" {alert_tier_emoji(count)}{count}"


def fmt_broadcast_badge(count: int) -> str:
    """Per-summary "Nth broadcast" badge. 1st appearance = no badge; escalates on
    repeat. >5 red, >2 yellow, 2 plain."""
    if count <= 1:
        return ""
    if count > 5:
        return f" 🔴×{count}"
    if count > 2:
        return f" 🟡×{count}"
    return f" ×{count}"


# ───────── formatting (clean / compact) ─────────


def _esc(s) -> str:
    return html.escape(str(s))


def fmt_username(handle: str) -> str:
    """Use a standalone Telegram code entity, not an account link/mention."""
    return f"<code>@{_esc(str(handle).strip().lstrip('@'))}</code>"


def _kol_code(label: str) -> str:
    # Source KOL labels may be display names, so never invent an @handle.
    return f"<code>{_esc(label)}</code>"


def _meta_line(parts: list[str]) -> str:
    return " · ".join(p for p in parts if p)


def _dev_segment(dev_infos: list[dict]) -> str:
    segs = []
    for d in dev_infos:
        k = d.get("kol")
        ktxt = f" KOL {k}" if k is not None else ""
        segs.append(f"{fmt_username(d['handle'])}{ktxt}")
    return " · ".join(segs)


def fmt_project_block(handle: str, count: int, bc: int, kol, summary: str,
                      reg_ym, is_fresh: bool, is_nft: bool,
                      dev_infos: list[dict]) -> list[str]:
    badge = fmt_broadcast_badge(bc)
    tags = ("" if not is_fresh else " 🆕") + ("" if not is_nft else " 🖼️")
    head = f"🔹 {fmt_username(handle)}{badge}{tags}"
    meta = _meta_line([
        f"本群 {count}",
        f"KOL {kol}" if kol is not None else "",
        f"注册 {reg_ym}" if reg_ym else "",
    ])
    lines = [head, meta]
    if summary:
        lines.append(_esc(summary))
    seg = _dev_segment(dev_infos)
    if seg:
        lines.append(f"🧑‍💻 {seg}")
    return lines


def fmt_dev_block(handle: str, count: int, bc: int, kol, summary: str,
                  projects: list[str]) -> list[str]:
    badge = fmt_broadcast_badge(bc)
    head = f"🔹 {fmt_username(handle)}{badge}"
    meta = _meta_line([f"本群 {count}", f"KOL {kol}" if kol is not None else ""])
    lines = [head, meta]
    if summary:
        lines.append(_esc(summary))
    if projects:
        seg = " · ".join(fmt_username(p) for p in projects)
        lines.append(f"🛠 在做 {seg}")
    return lines


def fmt_simple_block(handle: str, count: int, bc: int, kol, summary: str) -> list[str]:
    badge = fmt_broadcast_badge(bc)
    head = f"🔹 {fmt_username(handle)}{badge}"
    meta = _meta_line([f"本群 {count}", f"KOL {kol}" if kol is not None else ""])
    lines = [head, meta]
    if summary:
        lines.append(_esc(summary))
    return lines


def fmt_summary(window_minutes: int, sections: dict[str, list[list[str]]]) -> str:
    counts = {k: len(v) for k, v in sections.items()}
    total = sum(counts.values())
    head = [f"📊 <b>过去 {window_minutes} 分钟关注汇总</b>"]
    tagline = "  ".join(
        f"{CATEGORY_LABEL[k].split(' ')[0]} {counts[k]}"
        for k in CATEGORIES if counts[k]
    )
    if tagline:
        head.append(f"<i>共 {total} 个 · {tagline}</i>")
    out = list(head)
    for cat in CATEGORIES:
        blocks = sections.get(cat) or []
        if not blocks:
            continue
        out.append("")
        out.append(f"━━━ {CATEGORY_LABEL[cat]} ━━━")
        for block in blocks:
            out.append("")
            out.extend(block)
    return "\n".join(out)


# ───────── ingestion ─────────


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    chat_id = msg.chat_id
    source_id = context.application.bot_data["source_chat_id"]
    text = msg.text or msg.caption or ""
    log.info("RX msg: chat_id=%s (expect=%s) chat_type=%s len=%d preview=%r",
             chat_id, source_id, msg.chat.type if msg.chat else "?", len(text), text[:80])
    if chat_id != source_id:
        return
    events = parse_message(text)
    if not events:
        return
    storage: Storage = context.application.bot_data["storage"]
    added_handles: set[str] = set()
    for ev in events:
        if storage.add_event(ev.kol, ev.project_handle, msg.message_id,
                             event_ts=int(msg.date.timestamp()), bio=ev.bio, followers=ev.followers,
                             follower_handle=ev.follower_handle):
            added_handles.add(ev.project_handle)
    log.info("parsed message %s: %d events (%d new)",
             msg.message_id, len(events), len(added_handles))
    if added_handles:
        await check_alert(context.application, added_handles)


# ───────── real-time hot alert ─────────


async def check_alert(app: Application, handles: set[str]) -> None:
    # Only local reads and Telegram delivery are awaited here. Slow enrichment
    # runs in background so it cannot hold up ingestion of the next follow.
    lock = app.bot_data.setdefault("alert_lock", asyncio.Lock())
    async with lock:
        await _check_alerts_locked(app, handles)


async def _check_alerts_locked(app: Application, handles: set[str]) -> None:
    storage: Storage = app.bot_data["storage"]
    threshold: int = app.bot_data["alert_threshold"]
    cooldown: int = app.bot_data["alert_cooldown_seconds"]
    target_chat_id: int = app.bot_data["target_chat_id"]
    fast_window = app.bot_data.get("fast_alert_window_seconds", 180)
    fast_threshold = app.bot_data.get("fast_alert_threshold", 2)
    now = int(time.time())
    by_handle = {h.lower(): (h, n, k) for h, n, k in storage.aggregate_window(now - 3600, now)}
    fast = {h.lower(): (h, n, k) for h, n, k in storage.aggregate_window(now - fast_window, now)}
    pending = app.bot_data.setdefault("pending_hourly_alerts", set())

    for incoming_handle in handles:
        info = by_handle.get(incoming_handle.lower())
        if not info:
            continue
        handle, count, kols = info
        key = handle.lower()
        fast_info = fast.get(key)
        _, last_fast = storage.get_fast_alert_info(handle)
        if (fast_info and fast_info[1] >= fast_threshold
                and (not last_fast or now - last_fast >= fast_window)):
            _, fast_count, fast_kols = fast_info
            bio = storage.get_source_bios([handle]).get(handle, "")
            followers = storage.get_source_followers([handle]).get(handle, {}).get("followers")
            text_msg = (
                f"⚡ <b>3 分钟快速预警</b>\n"
                f"3 分钟内 {fast_count} 位不同 KOL 关注 "
                f"{fmt_username(handle)}\n"
                + (f"粉丝 {followers:,}（频道）\n" if followers is not None else "粉丝：待核实\n")
                + (f"频道简介：{_esc(bio[:240])}\n" if bio else "")
                + "\n关注的 KOL：\n" + "\n".join(f"  • {_kol_code(k)}" for k in fast_kols[:50])
            )
            try:
                await app.bot.send_message(chat_id=target_chat_id, text=text_msg,
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as exc:
                log.warning("fast alert delivery failed @%s: %s", handle, type(exc).__name__)
                continue  # no cooldown is recorded for a failed delivery
            storage.record_fast_alert(handle)
            storage.record_alert(handle, count)
            storage.mark_announced(handle, fast_kols)
            log.info("fast alert sent: @%s distinct_kols=%d window=%ds", handle, fast_count, fast_window)
            continue
        if count < threshold:
            continue
        _, last_ts, last_size = storage.get_alert_info(handle)
        if count <= last_size:
            continue
        if now - last_ts < cooldown:
            continue
        if key in pending:
            continue
        pending.add(key)
        app.create_task(_send_hourly_alert(app, handle, count, kols))


async def _send_hourly_alert(app: Application, handle: str, count: int, kols: list[str]) -> None:
    storage: Storage = app.bot_data["storage"]
    db_path = app.bot_data["db_path"]
    try:
        # Light enrichment only (real-time path stays cheap): Frontrun KOL count
        # + 6551 profile + one cheap batched categorize. No web search.
        kol_count, prof = await asyncio.gather(
            get_kol_followers(handle), ot.get_user_info(handle)
        )
        bio = (prof or {}).get("bio", "") or storage.get_source_bios([handle]).get(handle, "")
        metas = await analyze_batch([{"handle": handle, "bio": bio, "tweets": []}], db_path)
        m = metas.get(handle.lower(), {})
        cat = "project" if m.get("is_project") else m.get("category", "other")
        summary = m.get("summary", "")

        badge = fmt_alert_badge(storage.get_alert_info(handle)[0] + 1)
        kol_lines = "\n".join(f"  • {_kol_code(k)}" for k in kols)
        kol_part = f"\nKOL 关注：{kol_count}" if kol_count is not None else ""
        summary_part = f"\n{_esc(summary)}" if summary else ""
        followers = (prof or {}).get("followers")
        if followers is None:
            followers = storage.get_source_followers([handle]).get(handle, {}).get("followers")
        summary_part += f"\n粉丝 {followers:,}（资料快照）" if followers is not None else "\n粉丝：暂未查到"
        cat_part = f"\n类型：{CATEGORY_LABEL.get(cat, CATEGORY_LABEL['other'])}"

        text_msg = (
            f"🚨 <b>热点预警</b>{badge}\n"
            f"1 小时内 {count} 人关注 {fmt_username(handle)}"
            f"{cat_part}{kol_part}{summary_part}\n\n关注的 KOL：\n{kol_lines}"
        )
        async with app.bot_data["alert_lock"]:
            # A fast alert may have been delivered while the API calls were running.
            _, last_ts, last_size = storage.get_alert_info(handle)
            if (count <= last_size or int(time.time()) - last_ts
                    < app.bot_data["alert_cooldown_seconds"]):
                return
            await app.bot.send_message(
                chat_id=app.bot_data["target_chat_id"], text=text_msg,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            new_alert_count = storage.record_alert(handle, count)
            storage.mark_announced(handle, kols)
            log.info("alert sent: @%s count=%d (alert#%d)", handle, count, new_alert_count)
    except Exception as e:  # noqa: BLE001
        log.exception("failed to send alert: %s", e)
    finally:
        app.bot_data["pending_hourly_alerts"].discard(handle.lower())


# ───────── commands ─────────


async def cmd_summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    await push_summary(context.application)


async def cmd_summary_12h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    await push_summary(context.application, window_override=12 * 60)


async def cmd_summary_24h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    await push_summary(context.application, window_override=24 * 60)


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat is None:
        return False
    return update.effective_chat.id == context.application.bot_data["target_chat_id"]


# ───────── summary (hybrid A→B dev finding, full coverage) ─────────


async def push_summary(app: Application, window_override: int | None = None) -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        from .pipeline import push_deepseek_summary
        await push_deepseek_summary(app, window_override)
        return
    log.info("push_summary triggered")
    storage: Storage = app.bot_data["storage"]
    window_minutes: int = window_override or app.bot_data["window_minutes"]
    target_chat_id: int = app.bot_data["target_chat_id"]
    db_path: str = app.bot_data["db_path"]
    # No per-round quota: queued research is bounded only by in-flight concurrency.
    research_slots = app.bot_data.setdefault("grok_research_slots", asyncio.Semaphore(3))

    since = int(time.time()) - window_minutes * 60
    rows = storage.aggregate_window(since)
    if not rows:
        await _send(app, target_chat_id,
                    f"📊 过去 {window_minutes} 分钟关注汇总\n\n暂无新关注动态。")
        return

    handles = [r[0] for r in rows]
    row_count = {r[0]: r[1] for r in rows}

    # 1) Concurrent: Frontrun KOL counts (main handles) + 6551 profiles (bio/reg/tweets)
    kol_counts, infos = await asyncio.gather(
        get_many(handles), ot.get_many_user_info(handles)
    )

    # 2) Prefer fetched profiles, then the bio already present in source messages.
    source_bios = storage.get_source_bios(handles)
    items = [{"handle": h, "bio": (infos.get(h) or {}).get("bio", "") or source_bios.get(h, ""), "tweets": []}
             for h in handles]
    metas = await analyze_batch(items, db_path)

    def _rank_base(h: str):
        return (-row_count.get(h, 0), -(kol_counts.get(h) or 0))

    # Unknown accounts must be researched before the project-only dev filter.
    research_candidates = [it["handle"] for it in items
                           if needs_research(it, metas.get(it["handle"].lower(), {}))]
    research_candidates.sort(key=_rank_base)
    research_targets = research_candidates
    log.info("account research: %d targets (limit=unlimited, insufficient=%d)",
             len(research_targets), len(research_candidates))
    if research_targets:
        async def bounded_research(h):
            async with research_slots:
                return await research_account(h, db_path)
        researched = await asyncio.gather(*(bounded_research(h) for h in research_targets))
        for h, meta in zip(research_targets, researched):
            metas[h.lower()] = meta

    broadcast_counts = storage.bump_broadcast_counts(handles)

    # 3) Effective category — is_project lifts hidden projects out of "other"
    def eff_cat(h: str) -> str:
        m = metas.get(h.lower(), {})
        if m.get("is_project"):
            return "project"
        c = m.get("category", "other")
        return c if c in CATEGORIES else "other"
    decided = {h: eff_cat(h) for h in handles}

    # Ordering rule (user spec): 1) 本群关注人数  2) 项目自身/其 dev 的 KOL 关注数。
    # _rank_base is used before devs are resolved (B-target selection) so it can
    # only see the handle's own KOL count as the secondary key.
    # 4) B-escalation targets: project + A found no dev + not NFT (traders excluded
    #    inherently). Every eligible account is queued, hottest first.
    b_candidates = [
        h for h in handles
        if decided[h] == "project"
        and not metas.get(h.lower(), {}).get("devs")
        and not metas.get(h.lower(), {}).get("is_nft")
        and h not in research_targets
    ]
    b_candidates.sort(key=_rank_base)
    b_targets = set(b_candidates)
    log.info("B-search: %d targets (limit=unlimited, candidates=%d)",
             len(b_targets), len(b_candidates))

    # 5) Resolve devs per project: A devs, else B (if target); then Frontrun KOL per dev
    async def resolve_devs(h: str) -> tuple[str, list[dict]]:
        m = metas.get(h.lower(), {})
        devs = list(m.get("devs") or [])
        if not devs and h in b_targets:
            async with research_slots:
                devs = await find_devs_deep(h, db_path)
        kols = await asyncio.gather(*(get_kol_followers(d) for d in devs[:3]))
        return h, [{"handle": d, "kol": k} for d, k in zip(devs[:3], kols)]

    project_handles = sorted((h for h in handles if decided[h] == "project"), key=_rank_base)
    dev_map: dict[str, list[dict]] = {}
    if project_handles:
        for item in await asyncio.gather(
            *(resolve_devs(h) for h in project_handles), return_exceptions=True
        ):
            if isinstance(item, Exception):
                log.exception("resolve_devs failed: %s", item)
                continue
            dev_map[item[0]] = item[1]

    # 6) Compose sections. Final ordering: 1) 本群关注人数  2) 项目自身或其 dev
    #    的 KOL 关注数（取较大者）—— 让自身还小但 dev 有大量 KOL 的项目也能排上来。
    def _eff_kol(h: str) -> int:
        own = kol_counts.get(h) or 0
        devs = dev_map.get(h) or []
        dev_max = max((d.get("kol") or 0) for d in devs) if devs else 0
        return max(own, dev_max)

    def _rank_final(h: str):
        return (-row_count.get(h, 0), -_eff_kol(h))

    sections: dict[str, list[list[str]]] = {c: [] for c in CATEGORIES}
    groups: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for h in handles:
        groups[decided[h]].append(h)
    for hs in groups.values():
        hs.sort(key=_rank_final)

    for cat in CATEGORIES:
        for h in groups[cat]:
            bc = broadcast_counts.get(h, 1)
            kol = kol_counts.get(h)
            m = metas.get(h.lower(), {})
            summary = m.get("summary", "")
            info = infos.get(h) or {}
            cnt = row_count.get(h, 0)
            if cat == "project":
                sections[cat].append(fmt_project_block(
                    h, cnt, bc, kol, summary,
                    info.get("reg_ym"), ot.is_fresh(info), bool(m.get("is_nft")),
                    dev_map.get(h, []),
                ))
            elif cat == "crypto_dev":
                sections[cat].append(fmt_dev_block(
                    h, cnt, bc, kol, summary, m.get("dev_projects") or [],
                ))
            else:
                sections[cat].append(fmt_simple_block(h, cnt, bc, kol, summary))
            sources = m.get("_sources") or []
            if sources:
                sections[cat][-1].append(f'🔎 <a href="{_esc(sources[0])}">核查来源</a>')

    await _send(app, target_chat_id, fmt_summary(window_minutes, sections))

    purge_before = int(time.time()) - max(window_minutes * 4, 60 * 48) * 60
    deleted = storage.purge_older_than(purge_before)
    if deleted:
        log.info("purged %d old events", deleted)


async def _send(app: Application, chat_id: int, text: str) -> bool:
    # Telegram message limit ~4096 chars; chunk on newline boundaries.
    MAX = 3800
    chunks: list[str] = []
    s = text
    while len(s) > MAX:
        cut = s.rfind("\n", 0, MAX)
        if cut <= 0:
            cut = MAX
        chunks.append(s[:cut])
        s = s[cut:].lstrip("\n")
    chunks.append(s)
    delivered = True
    for c in chunks:
        try:
            await app.bot.send_message(
                chat_id=chat_id, text=c,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception as e:  # noqa: BLE001
            delivered = False
            log.exception("send failed: %s", e)
    return delivered


def build_app() -> Application:
    token = os.environ["BOT_TOKEN"]
    source_chat_id = int(os.environ["SOURCE_CHAT_ID"])
    target_chat_id = int(os.environ["TARGET_CHAT_ID"])
    window_minutes = int(os.environ.get("SUMMARY_INTERVAL_MINUTES", "60"))
    db_path = os.environ.get("DB_PATH", "/data/messages.db")
    alert_threshold = int(os.environ.get("ALERT_THRESHOLD", "3"))
    alert_cooldown_seconds = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "3600"))

    storage = Storage(db_path)
    app = Application.builder().token(token).build()
    app.bot_data.update(
        storage=storage,
        source_chat_id=source_chat_id,
        target_chat_id=target_chat_id,
        window_minutes=window_minutes,
        alert_threshold=alert_threshold,
        alert_cooldown_seconds=alert_cooldown_seconds,
        db_path=db_path,
        fast_alert_window_seconds=180,
        fast_alert_threshold=2,
    )

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_message))
    app.add_handler(CommandHandler("summary_now", cmd_summary_now))
    app.add_handler(CommandHandler("summary_12h", cmd_summary_12h))
    app.add_handler(CommandHandler("summary_24h", cmd_summary_24h))
    return app


async def run() -> None:
    app = build_app()
    log_provider_config()
    if os.environ.get("DEEPSEEK_API_KEY"):
        from .deepseek_config import messages_url
        log.info("DeepSeek research enabled: model=%s endpoint=%s; native web search + page reader; original Grok fallback retained",
                 os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"), messages_url())
    log.info("fast alerts enabled: 2 distinct KOLs within 180s; cooldown=180s")
    window_minutes: int = app.bot_data["window_minutes"]
    tz = os.environ.get("TIMEZONE", "Asia/Shanghai")
    scheduler = AsyncIOScheduler(timezone=tz)

    # Quiet hours [QUIET_START, QUIET_END): no normal hourly pushes. At QUIET_END
    # the morning job consolidates the whole quiet window. Hot alerts run 24/7
    # (message-triggered, not cron).
    quiet_start = int(os.environ.get("QUIET_START_HOUR", "2"))
    quiet_end = int(os.environ.get("QUIET_END_HOUR", "9"))
    active_hours = [h for h in range(24) if h < quiet_start or h > quiet_end]
    hour_spec = ",".join(str(h) for h in active_hours)
    morning_window_minutes = (quiet_end - quiet_start + 1) * 60

    job_hourly = scheduler.add_job(
        push_summary, trigger="cron", hour=hour_spec, minute=0, args=[app],
        misfire_grace_time=600, coalesce=True, id="hourly",
    )
    job_morning = scheduler.add_job(
        push_summary, trigger="cron", hour=quiet_end, minute=0,
        args=[app], kwargs={"window_override": morning_window_minutes},
        misfire_grace_time=600, coalesce=True, id="morning",
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            registered_signals.append(sig)
        except NotImplementedError:
            pass
    api_runner = None
    initialized = False
    try:
        await app.initialize()
        initialized = True
        await app.start()
        scheduler.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        api_token = os.environ.get("ALPHA_API_TOKEN", "")
        if api_token:
            from .alpha_api import start_api
            api_runner = await start_api(app.bot_data["db_path"], api_token,
                                         int(os.environ.get("PORT", "8080")))
        log.info(
            "bot started; tz=%s; default_window=%d min; research_limit=unlimited; "
            "quiet=%02d:00-%02d:00; morning_window=%d min; next hourly=%s; next morning=%s",
            tz, window_minutes,
            quiet_start, quiet_end, morning_window_minutes,
            job_hourly.next_run_time, job_morning.next_run_time,
        )
        await stop_event.wait()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if api_runner:
            await api_runner.cleanup()
        if app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        if initialized:
            await app.shutdown()
        for sig in registered_signals:
            loop.remove_signal_handler(sig)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
