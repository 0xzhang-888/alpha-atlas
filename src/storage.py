from __future__ import annotations

import sqlite3
import json
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS follow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kol TEXT NOT NULL,
    project_handle TEXT NOT NULL,
    message_id INTEGER,
    UNIQUE(message_id, kol, project_handle)
);
CREATE INDEX IF NOT EXISTS idx_ts ON follow_events(ts);

CREATE TABLE IF NOT EXISTS following_edges (
    follower TEXT NOT NULL,
    followed TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY(follower, followed)
);

CREATE TABLE IF NOT EXISTS alert_history (
    handle TEXT PRIMARY KEY,
    alert_count INTEGER NOT NULL DEFAULT 0,
    last_alert_ts INTEGER NOT NULL DEFAULT 0,
    last_alert_size INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS summary_history (
    handle TEXT PRIMARY KEY,
    broadcast_count INTEGER NOT NULL DEFAULT 0,
    last_broadcast_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_profiles (
    handle TEXT PRIMARY KEY,
    bio TEXT NOT NULL,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fast_alert_history (
    handle TEXT PRIMARY KEY,
    alert_count INTEGER NOT NULL DEFAULT 0,
    last_alert_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile_followers (
    handle TEXT PRIMARY KEY,
    followers INTEGER NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_watch (
    handle TEXT PRIMARY KEY,
    promoted INTEGER NOT NULL DEFAULT 0,
    announced_kols TEXT NOT NULL DEFAULT '[]'
);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_event(self, kol: str, project_handle: str, message_id,
                  event_ts: int | None = None, bio: str = "", followers: int | None = None,
                  follower_handle: str | None = None) -> bool:
        ts = int(time.time()) if event_ts is None else int(event_ts)
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO follow_events (ts, kol, project_handle, message_id) VALUES (?, ?, ?, ?)",
                    (ts, kol, project_handle, message_id),
                )
                if follower_handle:
                    import re
                    if re.fullmatch(r'[A-Za-z0-9_]{1,15}', follower_handle):
                        c.execute("INSERT INTO following_edges VALUES (?,?,?) ON CONFLICT(follower,followed) "
                                  "DO UPDATE SET ts=MAX(following_edges.ts,excluded.ts)",
                                  (follower_handle.lower(), project_handle.lower(), ts))
                if bio.strip():
                    c.execute(
                        "INSERT INTO source_profiles VALUES (?, ?, ?) "
                        "ON CONFLICT(handle) DO UPDATE SET bio=excluded.bio, ts=excluded.ts "
                        "WHERE excluded.ts >= source_profiles.ts",
                        (project_handle.lower(), bio.strip()[:1000], ts),
                    )
                if isinstance(followers, int) and not isinstance(followers, bool) and followers >= 0:
                    c.execute(
                        "INSERT INTO profile_followers VALUES (?, ?, ?) "
                        "ON CONFLICT(handle) DO UPDATE SET followers=excluded.followers, ts=excluded.ts "
                        "WHERE excluded.ts >= profile_followers.ts",
                        (project_handle.lower(), followers, ts),
                    )
            return True
        except sqlite3.IntegrityError:
            return False

    def aggregate_window(self, since_ts: int, until_ts: int | None = None) -> list[tuple[str, int, list[str]]]:
        """Aggregate case-insensitively; return display handle preserving the
        most informative casing seen (prefer mixed-case over all-lowercase)."""
        with self._conn() as c:
            cur = c.execute(
                """
                SELECT project_handle, kol, ts
                FROM follow_events
                WHERE ts >= ? AND ts <= ?
                ORDER BY ts ASC
                """,
                (since_ts, int(time.time()) if until_ts is None else until_ts),
            )
            # key=lower; value=[display_handle, set(kols)]
            buckets: dict[str, list] = {}
            for handle, kol, _ts in cur.fetchall():
                key = handle.lower()
                if key not in buckets:
                    buckets[key] = [handle, {}]
                else:
                    # Prefer variant with uppercase letters
                    existing = buckets[key][0]
                    if existing == existing.lower() and handle != handle.lower():
                        buckets[key][0] = handle
                buckets[key][1][kol.strip().casefold()] = kol
        result = [(disp, len(kols), sorted(kols.values())) for disp, kols in buckets.values()]
        result.sort(key=lambda x: (-x[1], x[0].lower()))
        return result

    def get_source_bios(self, handles: list[str]) -> dict[str, str]:
        cutoff = int(time.time()) - 7 * 24 * 3600
        with self._conn() as c:
            return {h: row[0] for h in handles
                    if (row := c.execute(
                        "SELECT bio FROM source_profiles WHERE handle=? AND ts>=?",
                        (h.lower(), cutoff),
                    ).fetchone())}

    def get_source_followers(self, handles: list[str]) -> dict[str, dict]:
        with self._conn() as c:
            return {h: {"followers": row[0], "observed_at": row[1], "source": "频道"}
                    for h in handles if (row := c.execute(
                        "SELECT followers, ts FROM profile_followers WHERE handle=? AND ts>=?",
                        (h.lower(), int(time.time()) - 24 * 3600),
                    ).fetchone())}

    def cache_get(self, key: str, ttl: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT payload FROM research_cache WHERE cache_key=? AND ts>?",
                            (key, int(time.time()) - ttl)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (ValueError, TypeError):
                pass
        return None

    def cache_set(self, key: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO research_cache VALUES (?, ?, ?)",
                      (key, json.dumps(payload, ensure_ascii=False), int(time.time())))

    def promote_other(self, handle: str, category: str, kols: list[str]) -> bool:
        """Remember rule-based promotion; new people means not previously broadcast."""
        with self._conn() as c:
            row = c.execute("SELECT promoted, announced_kols FROM account_watch WHERE handle=?",
                            (handle.lower(),)).fetchone()
            announced = set(json.loads(row[1])) if row else set()
            current = {k.strip().casefold() for k in kols}
            promoted = bool(row and row[0]) or (category == "other" and (
                len(current) >= 2 or (bool(announced) and bool(current - announced))))
            if promoted:
                c.execute("INSERT INTO account_watch (handle, promoted) VALUES (?, 1) "
                          "ON CONFLICT(handle) DO UPDATE SET promoted=1", (handle.lower(),))
        return promoted

    def mark_announced(self, handle: str, kols: list[str]) -> None:
        with self._conn() as c:
            row = c.execute("SELECT announced_kols FROM account_watch WHERE handle=?",
                            (handle.lower(),)).fetchone()
            seen = set(json.loads(row[0])) if row else set()
            seen.update(k.strip().casefold() for k in kols)
            c.execute("INSERT INTO account_watch (handle, announced_kols) VALUES (?, ?) "
                      "ON CONFLICT(handle) DO UPDATE SET announced_kols=excluded.announced_kols",
                      (handle.lower(), json.dumps(sorted(seen), ensure_ascii=False)))

    def known_kols(self, handle: str, current: list[str]) -> list[str]:
        with self._conn() as c:
            row = c.execute("SELECT announced_kols FROM account_watch WHERE handle=?",
                            (handle.lower(),)).fetchone()
        return sorted(set(json.loads(row[0]) if row else []) | {k.strip().casefold() for k in current})

    def get_fast_alert_info(self, handle: str) -> tuple[int, int]:
        with self._conn() as c:
            row = c.execute(
                "SELECT alert_count, last_alert_ts FROM fast_alert_history WHERE handle=?",
                (handle.lower(),),
            ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def record_fast_alert(self, handle: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO fast_alert_history VALUES (?, 1, ?) "
                "ON CONFLICT(handle) DO UPDATE SET "
                "alert_count=alert_count+1, last_alert_ts=excluded.last_alert_ts",
                (handle.lower(), int(time.time())),
            )

    def purge_older_than(self, ts: int) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM follow_events WHERE ts < ?", (ts,))
            return cur.rowcount

    def get_alert_info(self, handle: str) -> tuple[int, int, int]:
        """Return (alert_count, last_alert_ts, last_alert_size)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT alert_count, last_alert_ts, last_alert_size FROM alert_history WHERE handle=?",
                (handle.lower(),),
            ).fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

    def record_alert(self, handle: str, size: int) -> int:
        """Increment alert_count for handle, return new count."""
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT alert_count FROM alert_history WHERE handle=?",
                (handle.lower(),),
            ).fetchone()
            new_count = (row[0] if row else 0) + 1
            c.execute(
                """
                INSERT INTO alert_history (handle, alert_count, last_alert_ts, last_alert_size)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(handle) DO UPDATE SET
                  alert_count=excluded.alert_count,
                  last_alert_ts=excluded.last_alert_ts,
                  last_alert_size=excluded.last_alert_size
                """,
                (handle.lower(), new_count, now, size),
            )
        return new_count

    def bump_broadcast_counts(self, handles: list[str]) -> dict[str, int]:
        """Increment broadcast_count for each handle (per summary push); return
        post-bump counts keyed by the caller's display casing."""
        if not handles:
            return {}
        now = int(time.time())
        out: dict[str, int] = {}
        with self._conn() as c:
            for h in handles:
                key = h.lower()
                c.execute(
                    """
                    INSERT INTO summary_history (handle, broadcast_count, last_broadcast_ts)
                    VALUES (?, 1, ?)
                    ON CONFLICT(handle) DO UPDATE SET
                      broadcast_count = broadcast_count + 1,
                      last_broadcast_ts = excluded.last_broadcast_ts
                    """,
                    (key, now),
                )
                row = c.execute(
                    "SELECT broadcast_count FROM summary_history WHERE handle=?",
                    (key,),
                ).fetchone()
                out[h] = row[0] if row else 1
        return out

    def next_broadcast_counts(self, handles: list[str]) -> dict[str, int]:
        with self._conn() as c:
            return {h: (row[0] if (row := c.execute(
                "SELECT broadcast_count FROM summary_history WHERE handle=?", (h.lower(),)
            ).fetchone()) else 0) + 1 for h in handles}

    def get_alert_counts(self, handles: list[str]) -> dict[str, int]:
        if not handles:
            return {}
        # Map lower→original-case so we return the keys exactly as caller passed
        lower_to_disp = {h.lower(): h for h in handles}
        with self._conn() as c:
            placeholders = ",".join("?" * len(handles))
            rows = c.execute(
                f"SELECT handle, alert_count FROM alert_history WHERE handle IN ({placeholders})",
                tuple(lower_to_disp.keys()),
            ).fetchall()
        return {lower_to_disp[h]: cnt for h, cnt in rows if h in lower_to_disp}
