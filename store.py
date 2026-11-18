"""SQLite persistence: digest message IDs, event cache, deduplication."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

import aiosqlite

from config import CHANNEL_DEFINITIONS, DB_PATH, MUSIC_GENRES

logger = logging.getLogger(__name__)

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS digest_messages (
    channel_type TEXT PRIMARY KEY,
    message_id   INTEGER NOT NULL,
    week_start   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    date            TEXT NOT NULL,
    end_time        TEXT,
    location_name   TEXT,
    address         TEXT,
    lat             REAL,
    lon             REAL,
    distance_bucket TEXT,
    price           TEXT,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL,
    channel_type    TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    is_new          INTEGER NOT NULL DEFAULT 1,
    category        TEXT,
    is_major        INTEGER NOT NULL DEFAULT 0,
    genre           TEXT
);

CREATE TABLE IF NOT EXISTS last_run (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    digest_key   TEXT PRIMARY KEY,
    channel_id   INTEGER,
    guild_id     INTEGER,
    enabled      INTEGER NOT NULL DEFAULT 1,
    auto_created INTEGER NOT NULL DEFAULT 0,
    empty_streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_categories (
    category_id INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL
);

"""

_MIGRATIONS = [
    "ALTER TABLE events ADD COLUMN category TEXT",
    "ALTER TABLE events ADD COLUMN is_major INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE events ADD COLUMN genre TEXT",
    "ALTER TABLE channels ADD COLUMN auto_created INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE channels ADD COLUMN empty_streak INTEGER NOT NULL DEFAULT 0",
]


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        # Apply migrations for existing DBs that predate these columns
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
                await db.commit()
                logger.info("Migration applied: %s", sql)
            except Exception:
                pass  # column already exists
        await _repair_local_times(db)
    logger.info("Database initialized at %s", DB_PATH)


# Sources whose Philadelphia wall-clock times used to be stored as if they were UTC
# (5 hours early in winter). Fixed at parse time by scraper.as_utc; this repairs the
# rows stored before that, once (guarded by a last_run key).
_TZ_REPAIR_KEY = "tz_repair_local_times"
_TZ_REPAIR_SOURCES = ("Major Event Discovery", "Center City District Events", "Old City District Events")


async def _repair_local_times(db: aiosqlite.Connection) -> None:
    """Reinterpret affected rows' stored UTC times as local wall-clock times.

    The id is recomputed from the corrected date (as the fixed scraper would), so the
    next poll updates these rows instead of adding a second copy. If the corrected id
    already belongs to another row, this one is the duplicate and is deleted.
    """
    from scraper import LOCAL_TZ, _make_id  # local import: scraper → digest → store cycle

    async with db.execute("SELECT value FROM last_run WHERE key = ?", (_TZ_REPAIR_KEY,)) as cur:
        if await cur.fetchone():
            return

    def fix(value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        wall = datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)
        return wall.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()

    placeholders = ",".join("?" * len(_TZ_REPAIR_SOURCES))
    async with db.execute(
        f"SELECT id, title, date, end_time FROM events WHERE source IN ({placeholders})",
        _TZ_REPAIR_SOURCES,
    ) as cur:
        rows = await cur.fetchall()
    fixed = dropped = 0
    for old_id, title, date_s, end_s in rows:
        new_date = fix(date_s)
        new_id = _make_id(title, datetime.fromisoformat(new_date))
        try:
            await db.execute(
                "UPDATE events SET id = ?, date = ?, end_time = ? WHERE id = ?",
                (new_id, new_date, fix(end_s), old_id),
            )
            fixed += 1
        except aiosqlite.IntegrityError:
            await db.execute("DELETE FROM events WHERE id = ?", (old_id,))
            dropped += 1
    await db.execute(
        "INSERT INTO last_run (key, value) VALUES (?, ?)",
        (_TZ_REPAIR_KEY, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()
    logger.info("Local-time repair: corrected %d row(s), removed %d duplicate(s)", fixed, dropped)


async def save_digest_message(channel_type: str, message_id: int, week_start: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO digest_messages (channel_type, message_id, week_start)
               VALUES (?, ?, ?)
               ON CONFLICT(channel_type) DO UPDATE SET message_id=excluded.message_id,
               week_start=excluded.week_start""",
            (channel_type, message_id, week_start),
        )
        await db.commit()


async def load_digest_message(channel_type: str) -> Optional[tuple[int, str]]:
    """Return (message_id, week_start) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT message_id, week_start FROM digest_messages WHERE channel_type = ?",
            (channel_type,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None


async def delete_digest_message(channel_type: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM digest_messages WHERE channel_type = ?", (channel_type,)
        )
        await db.commit()


async def upsert_events(events: list) -> None:
    """Insert new events; mark existing ones as not-new; remove passed events."""
    from scraper import EventItem  # local import to avoid circular

    if not events:
        logger.warning("upsert_events called with empty list")
        return

    channel_type = events[0].channel_type
    logger.info("upsert_events: %d events for channel_type=%s", len(events), channel_type)
    now_iso = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Mark all existing as not-new before inserting fresh batch
        cur = await db.execute(
            "UPDATE events SET is_new = 0 WHERE channel_type = ?",
            (channel_type,),
        )
        logger.debug("upsert_events: marked %d existing events as not-new for %s", cur.rowcount, channel_type)
        for ev in events:
            await db.execute(
                """INSERT INTO events
                   (id, title, date, end_time, location_name, address, lat, lon,
                    distance_bucket, price, url, source, channel_type, first_seen, is_new)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title,
                       date=excluded.date,
                       end_time=excluded.end_time,
                       location_name=excluded.location_name,
                       address=excluded.address,
                       lat=excluded.lat,
                       lon=excluded.lon,
                       distance_bucket=excluded.distance_bucket,
                       price=excluded.price,
                       url=excluded.url,
                       source=excluded.source
                """,
                (
                    ev.id,
                    ev.title,
                    ev.date.isoformat(),
                    ev.end_time.isoformat() if ev.end_time else None,
                    ev.location_name,
                    ev.address,
                    ev.lat,
                    ev.lon,
                    ev.distance_bucket,
                    ev.price,
                    ev.url,
                    ev.source,
                    ev.channel_type,
                    ev.first_seen.isoformat(),
                ),
            )
        await db.commit()
    logger.info("upsert_events: commit complete for %s", channel_type)


async def load_events(channel_type: str) -> list:
    """Return list of dicts for all stored events of a given channel type."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE channel_type = ? ORDER BY date ASC",
            (channel_type,),
        ) as cursor:
            rows = await cursor.fetchall()
            result = [dict(r) for r in rows]
    logger.info("load_events: %d rows for channel_type=%s", len(result), channel_type)
    return result


async def clear_all_events() -> int:
    """Delete all rows from the events and digest_messages tables. Returns deleted count."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM events")
        deleted = cursor.rowcount
        await db.execute("DELETE FROM digest_messages")
        await db.commit()
    logger.info("clear_all_events: deleted %d event(s) and all digest message records", deleted)
    return deleted


async def update_event_classifications(classifications: dict[str, dict]) -> None:
    """Write category + is_major + genre onto events rows. {id: {category, major, genre}}."""
    if not classifications:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for event_id, info in classifications.items():
            await db.execute(
                "UPDATE events SET category = ?, is_major = ?, genre = ? WHERE id = ?",
                (info.get("category"), 1 if info.get("major") else 0, info.get("genre"), event_id),
            )
        await db.commit()
    logger.info("update_event_classifications: wrote %d classification(s)", len(classifications))


async def load_unclassified_events() -> list[dict]:
    """Return events needing classification.

    This is events with no category yet, plus music events that were classified
    before genre support existed (category='music' but genre IS NULL) so they get
    a genre assigned on the next run without requiring a full re-classify.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, source, channel_type, location_name, date FROM events "
            "WHERE category IS NULL OR (category = 'music' AND genre IS NULL) "
            "ORDER BY date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = [dict(r) for r in rows]
    logger.info("load_unclassified_events: %d event(s) needing classification", len(result))
    return result


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" * len(values))


async def _select_events(where: str, params: Sequence[str]) -> list[dict]:
    """Rows matching a WHERE clause (built from constants + placeholders), ordered by date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM events WHERE {where} ORDER BY date ASC", list(params)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def load_events_for_categories(categories: Sequence[str]) -> list[dict]:
    """Return non-major events in any of the given categories, ordered by date."""
    result = await _select_events(
        f"is_major = 0 AND category IN ({_placeholders(categories)})", categories
    )
    logger.info("load_events_for_categories(%s): %d event(s)", ",".join(categories), len(result))
    return result


async def load_other_events() -> list[dict]:
    """Return non-major, non-closure events whose category has no channel of its own.

    "Has a channel" means music, or any category a CHANNEL_DEFINITIONS "category"
    load covers — so folding a category into #other-events is a config-only change.
    NULL categories (not yet classified) land here too.
    """
    dedicated = sorted({"music"} | {
        c for d in CHANNEL_DEFINITIONS if d["load"][0] == "category" for c in d["load"][1]
    })
    result = await _select_events(
        f"is_major = 0 AND channel_type != 'closures' "
        f"AND (category NOT IN ({_placeholders(dedicated)}) OR category IS NULL)",
        dedicated,
    )
    logger.info("load_other_events: %d event(s)", len(result))
    return result


async def load_music_events_for_genres(genres: Sequence[str]) -> list[dict]:
    """Return non-major music events in any of the given genres, ordered by date.

    Genre "other" also matches rows whose genre is NULL or not in MUSIC_GENRES.
    """
    clauses: list[str] = []
    params: list[str] = []
    named = [g for g in genres if g != "other"]
    if named:
        clauses.append(f"genre IN ({_placeholders(named)})")
        params += named
    if "other" in genres:
        known = [g for g in MUSIC_GENRES if g != "other"]
        clauses.append(f"genre IS NULL OR genre NOT IN ({_placeholders(known)})")
        params += known
    result = await _select_events(
        f"category = 'music' AND is_major = 0 AND ({' OR '.join(clauses)})", params
    )
    logger.info("load_music_events_for_genres(%s): %d event(s)", ",".join(genres), len(result))
    return result


async def load_closures_events() -> list[dict]:
    """Return non-major road closure / transit items, ordered by date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE channel_type = 'closures' AND is_major = 0 ORDER BY date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = [dict(r) for r in rows]
    logger.info("load_closures_events: %d event(s)", len(result))
    return result


async def update_major_flags(flags: dict[str, bool]) -> int:
    """Set is_major for the given event ids. Returns the number of rows changed.

    Used by the web-verification pass to demote (or keep) events flagged major.
    """
    if not flags:
        return 0
    changed = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for event_id, is_major in flags.items():
            cursor = await db.execute(
                "UPDATE events SET is_major = ? WHERE id = ? AND is_major != ?",
                (1 if is_major else 0, event_id, 1 if is_major else 0),
            )
            changed += cursor.rowcount
        await db.commit()
    logger.info("update_major_flags: changed %d row(s)", changed)
    return changed


async def load_major_events() -> list[dict]:
    """Return all events flagged is_major=1, ordered by date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE is_major = 1 ORDER BY date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = [dict(r) for r in rows]
    logger.info("load_major_events: %d event(s)", len(result))
    return result


# ── Channel configuration (runtime, stored in DB) ──────────────────────────────


async def set_channel(
    digest_key: str,
    channel_id: int,
    guild_id: int,
    enabled: bool = True,
    auto_created: bool = False,
) -> None:
    """Bind a digest key to a Discord channel id (upsert).

    auto_created marks channels the bot made itself — only these are eligible for
    automatic empty-channel deletion. Manually linked channels stay auto_created=0.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO channels (digest_key, channel_id, guild_id, enabled, auto_created)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(digest_key) DO UPDATE SET
                   channel_id=excluded.channel_id,
                   guild_id=excluded.guild_id,
                   enabled=excluded.enabled,
                   auto_created=excluded.auto_created""",
            (digest_key, channel_id, guild_id, 1 if enabled else 0, 1 if auto_created else 0),
        )
        await db.commit()
    logger.info(
        "set_channel: %s -> %d (guild=%d, enabled=%s, auto_created=%s)",
        digest_key, channel_id, guild_id, enabled, auto_created,
    )


# ── Runtime settings (key/value, stored in DB) ─────────────────────────────────


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()
    logger.info("set_setting: %s = %s", key, value)


async def claim_daily_quota(name: str, day: str, limit: int) -> tuple[bool, int]:
    """Atomically consume one unit of a per-day quota; return (granted, used_after).

    Tracks a counter keyed by (name, day) in the settings table (value stored as
    "<day>|<count>"). When the stored day differs from `day`, the counter resets.
    A unit is granted only while the prior count is below `limit`; a denied call
    does not increment. Single source for the /ask daily budget ceiling. Internal
    key (not in SETTINGS_SPEC), so it never surfaces in /config.
    """
    key = f"_daily:{name}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        used = 0
        if row and row[0]:
            stored_day, _, stored_count = row[0].partition("|")
            if stored_day == day:
                try:
                    used = int(stored_count)
                except ValueError:
                    used = 0
        if used >= limit:
            return False, used
        used += 1
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, f"{day}|{used}"),
        )
        await db.commit()
    return True, used


async def get_all_settings() -> dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}


async def get_channel(digest_key: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT digest_key, channel_id, guild_id, enabled, auto_created, empty_streak FROM channels WHERE digest_key = ?",
            (digest_key,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_channels() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT digest_key, channel_id, guild_id, enabled, auto_created, empty_streak FROM channels"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_enabled_channels() -> list[dict]:
    """Channels that are enabled and have a channel_id bound."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT digest_key, channel_id, guild_id, enabled, auto_created, empty_streak FROM channels "
            "WHERE enabled = 1 AND channel_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def set_channel_enabled(digest_key: str, enabled: bool) -> bool:
    """Toggle posting for a digest key. Returns True if a row was updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE channels SET enabled = ? WHERE digest_key = ?",
            (1 if enabled else 0, digest_key),
        )
        await db.commit()
        updated = cursor.rowcount > 0
    logger.info("set_channel_enabled: %s -> %s (updated=%s)", digest_key, enabled, updated)
    return updated


async def bump_empty_streak(digest_key: str) -> int:
    """Increment the consecutive-empty-run counter for a channel; return the new value."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE channels SET empty_streak = empty_streak + 1 WHERE digest_key = ?",
            (digest_key,),
        )
        await db.commit()
        async with db.execute(
            "SELECT empty_streak FROM channels WHERE digest_key = ?", (digest_key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def reset_empty_streak(digest_key: str) -> None:
    """Reset the consecutive-empty-run counter (called when a channel has events)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE channels SET empty_streak = 0 WHERE digest_key = ? AND empty_streak != 0",
            (digest_key,),
        )
        await db.commit()


async def unlink_channel(digest_key: str) -> bool:
    """Forget a channel mapping (does NOT delete the Discord channel). Returns True if removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM channels WHERE digest_key = ?", (digest_key,))
        await db.commit()
        removed = cursor.rowcount > 0
    logger.info("unlink_channel: %s (removed=%s)", digest_key, removed)
    return removed


async def clear_all_channels() -> int:
    """Forget all channel mappings (does NOT delete Discord channels). Returns count removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM channels")
        await db.commit()
        removed = cursor.rowcount
    logger.info("clear_all_channels: removed %d mapping(s)", removed)
    return removed


# ── Managed categories (Discord categories the bot itself created) ──────────────
# Only categories recorded here are eligible for teardown deletion. Pre-existing
# categories the bot merely reused (found by name) are never recorded, so teardown
# can never delete a category the bot did not create.


async def add_managed_category(category_id: int, guild_id: int) -> None:
    """Record a category the bot created so teardown can later delete it by ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO managed_categories (category_id, guild_id) VALUES (?, ?)",
            (category_id, guild_id),
        )
        await db.commit()
    logger.info("add_managed_category: %d (guild=%d)", category_id, guild_id)


async def get_managed_categories(guild_id: int) -> list[int]:
    """Return the IDs of categories the bot created in the given guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT category_id FROM managed_categories WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def delete_managed_category(category_id: int) -> None:
    """Forget a tracked category (after it has been deleted on Discord, or is gone)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM managed_categories WHERE category_id = ?", (category_id,)
        )
        await db.commit()


async def delete_past_events(channel_type: str, before_iso: str) -> int:
    """Delete fully-past events. Keeps still-ongoing multi-day events whose
    end_time is at/after the cutoff (event over a period)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM events WHERE channel_type = ? AND date < ? "
            "AND (end_time IS NULL OR end_time < ?)",
            (channel_type, before_iso, before_iso),
        )
        await db.commit()
        return cursor.rowcount


async def set_last_run(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO last_run (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_last_run(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM last_run WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
