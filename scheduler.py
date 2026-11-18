"""APScheduler setup: daily poll, Monday new-post, Tue–Sun edit."""

import asyncio
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None

# Seconds to wait between posting to consecutive Discord channels.
# Avoids hammering the API when updating 8 channels back-to-back.
_INTER_CHANNEL_DELAY = 2.0


def _monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _create_digest_channel(guild, definition: dict, category):
    """Create a digest channel; private definitions are hidden from @everyone.

    Private channels get explicit overwrites (so they don't inherit the category's
    permissions): @everyone can't view, the bot can view/post/embed/read history.
    Server admins see every channel regardless.
    """
    overwrites = None
    if definition.get("private"):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
            ),
        }
        return await guild.create_text_channel(
            definition["name"], category=category, overwrites=overwrites,
            reason="private digest channel (hidden from @everyone)",
        )
    return await guild.create_text_channel(definition["name"], category=category)


def _rows_to_event_items(rows: list[dict]):
    from scraper import EventItem
    items = []
    for row in rows:
        items.append(EventItem(
            id=row["id"],
            title=row["title"],
            date=datetime.fromisoformat(row["date"]),
            end_time=datetime.fromisoformat(row["end_time"]) if row.get("end_time") else None,
            location_name=row.get("location_name"),
            address=row.get("address"),
            lat=row.get("lat"),
            lon=row.get("lon"),
            distance_bucket=row.get("distance_bucket"),
            price=row.get("price"),
            url=row["url"],
            source=row["source"],
            channel_type=row["channel_type"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            is_new=bool(row.get("is_new", False)),
        ))
    return items


async def _post_digest_to_channel(
    bot,
    digest_key: str,
    channel_id: int,
    event_items,
    is_monday: bool,
    week_start: date,
    options=None,
) -> None:
    """Load stored message ID, decide edit vs fresh post, persist new ID."""
    from digest import post_or_edit_digest
    from store import save_digest_message, load_digest_message

    discord_channel = bot.get_channel(channel_id)
    if discord_channel is None:
        logger.error("[%s] Discord channel %d not found — skipping", digest_key, channel_id)
        return

    stored = await load_digest_message(digest_key)
    logger.debug("[%s] Stored digest message: %s", digest_key, stored)

    stored_msg_id: Optional[int] = None
    if stored is not None and not is_monday:
        msg_id, stored_week = stored
        if stored_week == week_start.isoformat():
            stored_msg_id = msg_id
            logger.info("[%s] Reusing stored message %d (week=%s)", digest_key, stored_msg_id, stored_week)
        else:
            logger.info("[%s] Stale week %s — forcing fresh post", digest_key, stored_week)
    elif is_monday:
        logger.info("[%s] Monday — forcing fresh post", digest_key)
    else:
        logger.info("[%s] No stored message — fresh post", digest_key)

    primary_id = await post_or_edit_digest(
        discord_channel, event_items, stored_msg_id, digest_key=digest_key, options=options
    )
    await save_digest_message(digest_key, primary_id, week_start.isoformat())
    logger.info("[%s] Digest complete (primary_id=%d)", digest_key, primary_id)


async def run_digest_job(bot, *, reconcile: bool = False) -> None:
    """
    Full digest pipeline:
      1. Poll + upsert each source type (events / music / closures).
         Source types are internal scraper concepts — they carry geo/lookahead
         rules — but no longer map to individual Discord channels.
      2. Classify all unclassified events via Claude Haiku.
      3. Post digests sequentially to each category channel to avoid rate limits.

    Phases 1–2.5 are identical in both modes. Phase 3 branches on `reconcile`:
      - reconcile=False (scheduled daily job): create channels for full buckets and
        EDIT the stored digest messages in place (Mon fresh / Tue–Sun edit).
      - reconcile=True (/refresh): reconcile managed channels against the fresh
        classification — create newly-needed buckets, delete no-longer-needed ones,
        keep still-needed channels as-is (mutes survive) — then update each digest via
        the same edit-in-place posting as the scheduled job (lightest on rate limits,
        no re-notify). See _reconcile_and_post_digests.
    """
    from classify_events import classify_unclassified
    from scraper import poll_all_sources
    from store import (
        upsert_events, delete_past_events,
        get_all_channels, set_channel, unlink_channel,
        bump_empty_streak, reset_empty_streak,
        get_setting, get_all_settings, set_last_run,
    )
    from digest import DigestOptions

    now = datetime.now()
    today = now.date()
    is_monday = today.weekday() == 0
    week_start = _monday_of_week(today)
    source_health: dict[str, str] = {}

    # ── Phase 1: poll + upsert all source types ────────────────────────────────
    for source_type in ("events", "music", "closures"):
        logger.info("[%s] Polling sources (monday=%s)", source_type, is_monday)
        try:
            fresh_items = await poll_all_sources(source_type)
            source_health[source_type] = f"{len(fresh_items)} items"
            logger.info("[%s] %d items fetched", source_type, len(fresh_items))

            # Prune only what ended before today (local): a date-only event (stored as
            # local midnight) must survive its own day. Rendering drops the rest via
            # event_is_active. Stored dates are UTC "+00:00" strings, so compare likewise.
            from scraper import LOCAL_TZ
            local_midnight = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = local_midnight.astimezone(timezone.utc).isoformat()
            removed = await delete_past_events(source_type, cutoff)
            if removed:
                logger.info("[%s] Pruned %d past events", source_type, removed)

            if fresh_items:
                await upsert_events(fresh_items)
            else:
                logger.warning("[%s] No fresh items — skipping upsert", source_type)

        except Exception as exc:
            logger.error("[%s] Poll/upsert failed: %s", source_type, exc, exc_info=True)
            source_health[source_type] = f"ERROR: {exc}"

    # ── Phase 2: classify all unclassified events ──────────────────────────────
    try:
        classified_count = await classify_unclassified(config.DB_PATH)
        logger.info("Classified %d new event(s)", classified_count)
    except Exception as exc:
        logger.error("Classification failed: %s", exc, exc_info=True)

    # ── Phase 2.5: web-verify the major list, demoting niche/non-citywide events ─
    try:
        from discovery import review_major_events
        changed = await review_major_events()
        logger.info("Major review: %d flag change(s)", changed)
    except Exception as exc:
        logger.error("Major review failed: %s", exc, exc_info=True)

    # ── Phase 3 (/refresh): reconcile channels + post fresh, then done ─────────
    # The scheduled daily job (reconcile=False) falls through to the original
    # create + edit-in-place phase 3 below, which is left byte-for-byte unchanged.
    if reconcile:
        await _reconcile_and_post_digests(bot, source_health, now)
        return

    # ── Phase 3: dynamic channel management + posting ─────────────────────────
    # For each definition we: create its channel if the bucket has enough events
    # (and it doesn't exist yet), post the digest, or — when the bucket is empty —
    # either delete the channel (auto_delete_empty=on, auto-created only) or just
    # log it. Disabled and manually-linked channels are never auto-deleted.
    dynamic_on = (await get_setting(config.SETTING_DYNAMIC_CHANNELS, "on")) == "on"
    auto_delete = (await get_setting(config.SETTING_AUTO_DELETE_EMPTY, "off")) == "on"
    safe_mode = (await get_setting(config.SETTING_SAFE_MODE, "on")) == "on"
    try:
        threshold = int(await get_setting(config.SETTING_CREATE_THRESHOLD, "1"))
    except (TypeError, ValueError):
        threshold = 1
    try:
        empty_runs_limit = int(await get_setting(config.SETTING_EMPTY_RUNS_BEFORE_DELETE, "3"))
    except (TypeError, ValueError):
        empty_runs_limit = 3

    guild = await _resolve_managed_guild(bot)
    can_manage = guild is not None and guild.me.guild_permissions.manage_channels
    if dynamic_on and guild is not None and not can_manage:
        logger.warning("Dynamic channels on but missing Manage Channels — create/delete disabled")
    _category_cache: dict[str, object] = {}  # category name -> discord.CategoryChannel

    # Display options for digests, built once from the current settings.
    options = DigestOptions.from_settings(await get_all_settings())
    logger.info("Digest display options: %s", options)

    existing = {c["digest_key"]: c for c in await get_all_channels()}
    if not existing and guild is None:
        logger.warning("No channels configured and no managed guild — run /setup. Skipping posting.")

    for d in config.CHANNEL_DEFINITIONS:
        digest_key = d["key"]
        if d.get("own_job"):
            continue  # rendered by its own scheduler job (run_penndot_job)
        conf = existing.get(digest_key)
        try:
            rows = await _load_for_definition(d)
            count = len(rows)

            # Disabled channels: leave entirely alone (never post/create/delete).
            if conf is not None and not conf["enabled"]:
                logger.debug("[%s] disabled — skipping", digest_key)
                continue

            live = bot.get_channel(conf["channel_id"]) if conf and conf["channel_id"] else None

            # ── Create if warranted ──
            if live is None and count >= threshold and dynamic_on and can_manage:
                category = await _ensure_category(guild, d["category_name"], _category_cache)
                channel = await _create_digest_channel(guild, d, category)
                await set_channel(digest_key, channel.id, guild.id, enabled=True, auto_created=True)
                live = channel
                logger.info("[%s] auto-created #%s under '%s' (%d events)",
                            digest_key, channel.name, d["category_name"], count)

            # ── Empty-channel handling ──
            if count == 0:
                if live is None:
                    logger.debug("[%s] no events, no channel — nothing to do", digest_key)
                    continue
                streak = await bump_empty_streak(digest_key)
                # Ownership guard is the sole gate: only delete a channel the bot
                # created and tracks in the managed guild.
                deletable = auto_delete and can_manage and await is_bot_owned_channel(bot, live)
                if deletable and streak >= empty_runs_limit and safe_mode:
                    logger.warning("[%s] SAFE MODE: would auto-delete empty channel #%s "
                                   "(empty %d run(s)); not deleting. Turn off safe_mode to apply.",
                                   digest_key, live.name, streak)
                elif deletable and streak >= empty_runs_limit:
                    name = live.name
                    await live.delete(reason=f"auto_delete_empty: empty for {streak} runs")
                    await unlink_channel(digest_key)
                    logger.info("[%s] auto-deleted empty channel #%s after %d empty run(s)",
                                digest_key, name, streak)
                elif deletable:
                    logger.info("[%s] channel #%s empty (%d/%d runs before delete)",
                                digest_key, live.name, streak, empty_runs_limit)
                else:
                    logger.info("[%s] channel #%s is empty (kept; auto_delete_empty=%s)",
                                digest_key, live.name, "on" if auto_delete else "off")
                continue

            # Has events — reset any empty streak before posting.
            await reset_empty_streak(digest_key)

            # ── Post / edit digest ──
            if live is None:
                logger.debug("[%s] %d events but no channel (dynamic=%s, can_manage=%s)",
                             digest_key, count, dynamic_on, can_manage)
                continue

            # Ownership guard at the point of action: only ever post into / edit a
            # channel the bot created and tracks. All rows are bot-owned today, so this
            # is a no-op in practice — but it makes "never touch an unmanaged channel"
            # enforced here rather than merely implied by how rows get created.
            if not await is_bot_owned_channel(bot, live):
                logger.warning("[%s] skipping post — #%s is not a bot-owned channel", digest_key, live.name)
                continue

            items = _rows_to_event_items(rows)
            logger.info("[%s] %d event(s) for digest", digest_key, count)
            await _post_digest_to_channel(bot, digest_key, live.id, items, is_monday, week_start, options=options)

        except Exception as exc:
            logger.error("[%s] Digest failed: %s", digest_key, exc, exc_info=True)
            source_health[digest_key] = f"ERROR: {exc}"

        # Brief pause between channels to stay well inside Discord rate limits
        await asyncio.sleep(_INTER_CHANNEL_DELAY)

    # Keep the bot-created categories as a block at the bottom of the server, with
    # the Important category leading the block (idempotent — no-ops when stable).
    if guild is not None and can_manage:
        await _order_managed_categories(guild)

    await set_last_run("last_digest", now.isoformat())
    await set_last_run("source_health", str(source_health))


async def _resolve_managed_guild(bot):
    """Resolve the guild the bot manages channels in (set by /setup). May be None."""
    from store import get_setting

    guild_id = await get_setting(config.SETTING_MANAGED_GUILD_ID)
    if not guild_id:
        return None
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        logger.warning("Managed guild %s not found", guild_id)
    return guild


async def is_bot_owned_channel(bot, channel) -> bool:
    """The single ownership guard. The ONLY gate any destructive op may use.

    Returns True only when `channel` is recorded in the DB `channels` table as
    bot-created (auto_created=1) in the managed guild, and actually lives in that
    guild. Otherwise returns False (caller must skip). This is what prevents the
    bot from ever deleting/purging/editing a channel it did not create and track.
    """
    from store import get_setting, get_all_channels

    if channel is None:
        return False
    guild_id_raw = await get_setting(config.SETTING_MANAGED_GUILD_ID)
    if not guild_id_raw:
        return False
    try:
        managed_guild_id = int(guild_id_raw)
    except (TypeError, ValueError):
        return False
    guild = getattr(channel, "guild", None)
    if guild is None or guild.id != managed_guild_id:
        return False
    owned = {
        c["channel_id"]
        for c in await get_all_channels()
        if c["auto_created"] and c["guild_id"] == managed_guild_id and c["channel_id"]
    }
    return channel.id in owned


async def _ensure_category(guild, name: str, cache: dict):
    """Find or create a Discord category by name, cached per run. Returns the category or None.

    Bot-created categories form a block at the BOTTOM of the server, so they don't
    dominate the channel list — users opt in to scrolling down to them. Other
    bot categories are appended at the bottom; the Important category is placed
    just above the other bot categories so it leads the managed block.

    A category the bot creates is recorded via store.add_managed_category so teardown
    can later delete it by ID. Pre-existing categories found by name are NOT recorded,
    so the bot never claims ownership of a category it did not create.
    """
    if name in cache:
        return cache[name]
    category = discord.utils.get(guild.categories, name=name)
    if category is None and guild.me.guild_permissions.manage_channels:
        from store import add_managed_category

        # Create it (Discord appends at the bottom); the exact ordering — managed
        # block at the bottom with Important first — is enforced afterwards by
        # _order_managed_categories, which is the single source of ordering truth.
        category = await guild.create_category(name)
        await add_managed_category(category.id, guild.id)
        logger.info("Created category '%s' in guild %d", name, guild.id)
    cache[name] = category
    return category


# Desired top-to-bottom order of the bot-managed category block: Important
# (#major-events + #traffic-alerts) leads, then music, then the general bucket; any other/stale managed category sorts after.
_MANAGED_CATEGORY_ORDER = [
    config.IMPORTANT_CATEGORY_NAME,
    config.GROUP_CATEGORY_NAMES["music"],
    config.SETUP_CATEGORY_NAME,
]


def _managed_category_rank(name: str) -> int:
    try:
        return _MANAGED_CATEGORY_ORDER.index(name)
    except ValueError:
        return len(_MANAGED_CATEGORY_ORDER)


async def _order_managed_categories(guild) -> None:
    """Keep bot-created categories as a contiguous block at the BOTTOM of the server,
    with the Important category leading that block.

    Idempotent: when the block is already correctly placed it makes zero API calls.
    Otherwise it moves each managed category to the bottom in the desired order so
    they stack as [Important, music, general]. Only categories the bot created
    (tracked in managed_categories) are moved — user categories are never explicitly
    repositioned; they shift up as the managed block sinks to the bottom. Fails soft.
    """
    if not guild.me.guild_permissions.manage_channels:
        return
    from store import get_managed_categories

    managed_ids = set(await get_managed_categories(guild.id))
    managed = [c for c in guild.categories if c.id in managed_ids]
    if not managed:
        return

    desired = sorted(managed, key=lambda c: (_managed_category_rank(c.name), c.name))
    start = len(guild.categories) - len(managed)
    # Already a contiguous block at the bottom in the desired order? Nothing to do.
    if all(cat.position == start + i for i, cat in enumerate(desired)):
        return

    # Move each managed category to the bottom in desired order; each "send to bottom"
    # appends below the previous one, so they end up stacked Important-first beneath the
    # user's own categories. Converges in one pass.
    bottom = len(guild.categories) - 1
    for category in desired:
        try:
            await category.edit(position=bottom, reason="bot categories: bottom block, Important first")
        except discord.HTTPException as exc:
            logger.warning("Could not reposition category '%s': %s", category.name, exc)
    logger.info("Ordered %d managed category(ies) to the bottom (Important first)", len(desired))


async def _load_for_definition(definition: dict) -> list[dict]:
    """Resolve a CHANNEL_DEFINITIONS 'load' spec to the right DB query."""
    from store import (
        load_events_for_categories, load_music_events_for_genres, load_other_events,
        load_closures_events, load_major_events,
    )
    loader_type, arg = definition["load"]
    if loader_type == "genre":
        return await load_music_events_for_genres(arg)
    if loader_type == "category":
        return await load_events_for_categories(arg)
    if loader_type == "other":
        return await load_other_events()
    if loader_type == "closures":
        return await load_closures_events()
    if loader_type == "major":
        return await load_major_events()
    logger.error("Unknown loader type %r for %s", loader_type, definition["key"])
    return []


async def _clear_bot_messages(channel, bot) -> int:
    """Delete the bot's own messages in a channel (own messages need no Manage Messages)."""
    deleted = 0
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id == bot.user.id:
                try:
                    await msg.delete()
                    deleted += 1
                except discord.HTTPException:
                    pass
    except discord.HTTPException as exc:
        logger.warning("Could not clear messages in #%s: %s", getattr(channel, "name", "?"), exc)
    return deleted


async def _reconcile_and_post_digests(bot, source_health: dict, now: datetime) -> None:
    """/refresh Phase 3: reconcile managed channels to the fresh classification, then
    POST fresh digests (never edit in place — that's what avoids the rate limiting).

    Per channel definition (all destructive ops gated by the ownership guard):
      - Disabled channel → left entirely alone.
      - No live channel + bucket >= create_threshold → create it (respects
        dynamic_channels + Manage Channels, exactly like the scheduled job).
      - Live channel that isn't bot-owned → left untouched (never clear/rename/delete
        an unmanaged channel), so its mute state and content are never disturbed.
      - Live bot-owned channel below threshold → delete it and forget the mapping
        (safe_mode makes this a dry-run log). Deletion is reserved for buckets that
        are no longer needed.
      - Live bot-owned channel still needed → KEEP the same channel object so its
        per-channel/per-category mute state survives; rename in place only if the
        definition's name changed.
      - Every kept/created channel → update its digest via _post_digest_to_channel,
        which edits the stored messages in place when the layout matches (lightest on
        rate limits, no re-notify) and only reposts on a structural change. Mutes are
        preserved by keeping the channel, not by how the messages are updated.

    Peer modules are imported locally per the project's circular-import convention.
    Fails soft per definition: one channel's error never aborts the rest.
    """
    from store import (
        get_all_channels, get_all_settings, get_setting,
        set_channel, unlink_channel, reset_empty_streak, set_last_run,
    )
    from digest import DigestOptions

    dynamic_on = (await get_setting(config.SETTING_DYNAMIC_CHANNELS, "on")) == "on"
    safe_mode = (await get_setting(config.SETTING_SAFE_MODE, "on")) == "on"
    try:
        threshold = int(await get_setting(config.SETTING_CREATE_THRESHOLD, "1"))
    except (TypeError, ValueError):
        threshold = 1

    guild = await _resolve_managed_guild(bot)
    can_manage = guild is not None and guild.me.guild_permissions.manage_channels
    if dynamic_on and guild is not None and not can_manage:
        logger.warning("Refresh reconcile: dynamic channels on but missing Manage Channels — create/delete disabled")
    _category_cache: dict[str, object] = {}

    options = DigestOptions.from_settings(await get_all_settings())
    logger.info("Refresh reconcile: display options: %s", options)
    week_start = _monday_of_week(now.date())

    existing = {c["digest_key"]: c for c in await get_all_channels()}
    if not existing and guild is None:
        logger.warning("No channels configured and no managed guild — run /setup. Skipping refresh.")

    for d in config.CHANNEL_DEFINITIONS:
        digest_key = d["key"]
        if d.get("own_job"):
            continue  # rendered by its own scheduler job; never reconciled/deleted here
        conf = existing.get(digest_key)
        try:
            rows = await _load_for_definition(d)
            count = len(rows)

            # Disabled channels: leave entirely alone (never post/create/delete).
            if conf is not None and not conf["enabled"]:
                logger.debug("[%s] disabled — skipping", digest_key)
                continue

            live = bot.get_channel(conf["channel_id"]) if conf and conf["channel_id"] else None

            if live is None:
                # ── Newly-needed bucket → create; otherwise nothing to do. ──
                if count >= threshold and dynamic_on and can_manage:
                    category = await _ensure_category(guild, d["category_name"], _category_cache)
                    live = await _create_digest_channel(guild, d, category)
                    await set_channel(digest_key, live.id, guild.id, enabled=True, auto_created=True)
                    logger.info("[%s] refresh: created #%s under '%s' (%d events)",
                                digest_key, live.name, d["category_name"], count)
                else:
                    logger.debug("[%s] refresh: no channel, %d event(s) — nothing to create", digest_key, count)
                    continue
            else:
                # Ownership guard: only ever clear/rename/delete a channel the bot
                # created and tracks. Anything else is left completely untouched.
                if not (can_manage and await is_bot_owned_channel(bot, live)):
                    logger.info("[%s] refresh: #%s is not a bot-owned channel — leaving untouched",
                                digest_key, live.name)
                    continue

                # ── No-longer-needed bucket → delete (safe_mode = dry-run log). ──
                if count < threshold:
                    if safe_mode:
                        logger.warning("[%s] refresh SAFE MODE: would delete #%s (%d < %d events); "
                                       "not deleting. Turn off safe_mode to apply.",
                                       digest_key, live.name, count, threshold)
                    else:
                        name = live.name
                        await live.delete(reason=f"refresh reconcile: bucket below threshold ({count} < {threshold})")
                        await unlink_channel(digest_key)
                        logger.info("[%s] refresh: deleted #%s (bucket below threshold, %d < %d)",
                                    digest_key, name, count, threshold)
                    continue

                # ── Still needed → keep the SAME channel object (mutes survive). ──
                # Rename in place only if the definition's name changed.
                if live.name != d["name"]:
                    try:
                        old = live.name
                        await live.edit(name=d["name"], reason="refresh reconcile: definition name changed")
                        logger.info("[%s] refresh: renamed #%s → #%s", digest_key, old, d["name"])
                    except discord.HTTPException as exc:
                        logger.warning("[%s] refresh: could not rename #%s: %s", digest_key, live.name, exc)

            # ── Update the digest, keeping the channel (mutes survive) ──
            # Edit the stored messages in place when the layout matches, only
            # reposting on a structural change. is_monday=False so /refresh never
            # forces a fresh post: editing is the lightest option on Discord's rate
            # limits and never re-notifies members. _post_digest_to_channel loads the
            # stored message id and persists the new one itself.
            await reset_empty_streak(digest_key)
            items = _rows_to_event_items(rows)
            await _post_digest_to_channel(
                bot, digest_key, live.id, items, False, week_start, options=options
            )
            logger.info("[%s] refresh: updated digest in #%s (%d events)", digest_key, live.name, count)

        except Exception as exc:
            logger.error("[%s] refresh reconcile failed: %s", digest_key, exc, exc_info=True)
            source_health[digest_key] = f"ERROR: {exc}"

        # Brief pause between channels to stay well inside Discord rate limits.
        await asyncio.sleep(_INTER_CHANNEL_DELAY)

    # Keep the bot-created categories as a block at the bottom (idempotent).
    if guild is not None and can_manage:
        await _order_managed_categories(guild)

    await set_last_run("last_digest", now.isoformat())
    await set_last_run("source_health", str(source_health))


async def rerender_digests_from_store(bot) -> int:
    """Re-render every configured channel from the stored events using current
    display settings: delete the bot's old digest messages and repost fresh.

    Does NOT poll sources or re-classify — it only reformats what's already in the
    data store. Used after a /config display change. Returns channels re-rendered.
    """
    from store import (
        get_all_settings, get_enabled_channels, save_digest_message,
    )
    from digest import DigestOptions, post_or_edit_digest

    options = DigestOptions.from_settings(await get_all_settings())
    configured = {c["digest_key"]: c for c in await get_enabled_channels()}
    week_start = _monday_of_week(datetime.now().date())

    rerendered = 0
    for d in config.CHANNEL_DEFINITIONS:
        if d.get("own_job"):
            continue  # rendered by its own scheduler job
        conf = configured.get(d["key"])
        if conf is None:
            continue
        channel = bot.get_channel(conf["channel_id"])
        if channel is None:
            continue
        # Ownership guard at the point of action: only ever clear/repost in a channel
        # the bot created and tracks. No-op today (all rows are bot-owned); makes the
        # "never touch an unmanaged channel" guarantee enforced rather than implied.
        if not await is_bot_owned_channel(bot, channel):
            logger.warning("[%s] rerender: skipping non-bot channel #%s", d["key"], channel.name)
            continue
        try:
            rows = await _load_for_definition(d)
            items = _rows_to_event_items(rows)
            # Remove old messages, then post fresh in the new format.
            await _clear_bot_messages(channel, bot)
            primary_id = await post_or_edit_digest(
                channel, items, None, digest_key=d["key"], options=options
            )
            await save_digest_message(d["key"], primary_id, week_start.isoformat())
            rerendered += 1
            logger.info("[%s] re-rendered #%s (%d events)", d["key"], channel.name, len(items))
        except Exception as exc:
            logger.error("[%s] re-render failed: %s", d["key"], exc, exc_info=True)
        await asyncio.sleep(_INTER_CHANNEL_DELAY)

    logger.info("rerender_digests_from_store: re-rendered %d channel(s)", rerendered)
    return rerendered


# last_run keys the PennDOT job writes.
PENNDOT_LAST_ATTEMPT = "penndot_last_attempt"   # ISO UTC time of the last run
PENNDOT_LAST_RESULT = "penndot_last_result"     # what that run did, or why it didn't
PENNDOT_LAST_SUCCESS = "penndot_last_success"   # ISO UTC time #traffic-alerts last updated
PENNDOT_UNHIDDEN = "penndot_unhidden"           # ISO UTC time the old private channel was made public

# Serializes scheduled runs and /refresh, so two runs never edit the channel at once
# (APScheduler's max_instances only covers the scheduled job).
_penndot_lock = asyncio.Lock()


async def run_penndot_job(bot) -> tuple[bool, str]:
    """Refresh the #traffic-alerts channel (every 15 min, and as part of /refresh).

    Separate from the daily digest: fetches only the two RCRS feeds (no DB rows, no LLM
    calls) and edits one channel in place: Live now / Ongoing / Upcoming + index. Creates
    the channel if it's missing — even when empty. Never auto-deletes and never
    fresh-posts. On a PennDOT failure the channel is left as-is; its "Updated" time shows
    how stale it is.

    Returns (updated, message) and records both in `last_run`.
    """
    from store import set_last_run

    async with _penndot_lock:
        updated, message = await _refresh_penndot(bot)
        now_iso = datetime.now(timezone.utc).isoformat()
        await set_last_run(PENNDOT_LAST_ATTEMPT, now_iso)
        await set_last_run(PENNDOT_LAST_RESULT, message)
        if updated:
            await set_last_run(PENNDOT_LAST_SUCCESS, now_iso)
    return updated, message


async def _get_or_create_penndot_channel(bot, guild, definition: dict, conf: Optional[dict]):
    """Return (channel, "") for the PennDOT definition, creating it if missing.

    Returns (None, reason) when it's missing and can't be created, or when the channel on
    record isn't bot-owned (ownership guard).
    """
    from store import get_setting, set_channel

    key = definition["key"]
    channel = bot.get_channel(conf["channel_id"]) if conf and conf["channel_id"] else None
    if channel is None:
        dynamic_on = (await get_setting(config.SETTING_DYNAMIC_CHANNELS, "on")) == "on"
        if not (dynamic_on and guild.me.guild_permissions.manage_channels):
            logger.info("[%s] no channel and can't create one (dynamic_channels=%s)", key,
                        "on" if dynamic_on else "off")
            return None, (f"No #{definition['name']} channel, and I can't create one "
                          f"(dynamic_channels={'on' if dynamic_on else 'off'}, or no Manage Channels).")
        category = await _ensure_category(guild, definition["category_name"], {})
        channel = await _create_digest_channel(guild, definition, category)
        await set_channel(key, channel.id, guild.id, enabled=True, auto_created=True)
        logger.info("[%s] created #%s under '%s'", key, channel.name, definition["category_name"])

    if not await is_bot_owned_channel(bot, channel):
        logger.warning("[%s] skipping post — #%s is not a bot-owned channel", key, channel.name)
        return None, f"Skipped: #{channel.name} isn't a channel I created."
    return channel, ""


async def _remove_retired_channels(bot, guild) -> list[str]:
    """Delete channels whose key is in config.RETIRED_CHANNEL_KEYS and forget them.

    Runs from the 15-min PennDOT job (so also from /refresh), which makes it the one
    cleanup point for every retired key, digest channels included. Same rules as every
    other deletion: only bot-owned channels (ownership guard), only with Manage Channels,
    and safe_mode makes it a logged dry run. A channel already gone from Discord just has
    its records cleared. Returns a note per channel acted on (for /refresh).
    """
    from store import get_channel, get_setting, unlink_channel, delete_digest_message

    safe_mode = (await get_setting(config.SETTING_SAFE_MODE, "on")) == "on"
    notes: list[str] = []
    for key, replacement in config.RETIRED_CHANNEL_KEYS.items():
        conf = await get_channel(key)
        if conf is None:
            continue
        channel = bot.get_channel(conf["channel_id"]) if conf["channel_id"] else None
        if channel is not None:
            if not (guild.me.guild_permissions.manage_channels and await is_bot_owned_channel(bot, channel)):
                logger.warning("[%s] retired channel #%s isn't deletable/bot-owned — leaving it", key, channel.name)
                continue
            if safe_mode:
                logger.warning("[%s] SAFE MODE: would delete old #%s (replaced by #%s); not deleting. "
                               "Turn off safe_mode to apply.", key, channel.name, replacement)
                notes.append(f"Safe mode: would delete old #{channel.name} (replaced by #{replacement}).")
                continue
            name = channel.name
            await channel.delete(reason=f"retired; replaced by #{replacement}")
            logger.info("[%s] deleted old #%s (replaced by #%s)", key, name, replacement)
            notes.append(f"Deleted old #{name} (replaced by #{replacement}).")
        await unlink_channel(key)
        await delete_digest_message(key)
    return notes


async def _unhide_penndot_channel(guild, channel) -> str:
    """One-off: make a #traffic-alerts that was created private visible to @everyone.

    #traffic-alerts used to be private (an @everyone view_channel=False overwrite). The first run
    after it went public removes just that deny, so the channel follows its category's
    permissions again. Runs once (recorded in last_run), so a later manual re-hide in
    Discord sticks. The caller has already passed the ownership guard. This is the only
    place the bot edits permissions on an existing channel.
    Returns a note for the run's result ("" when nothing was done).
    """
    from store import get_last_run, set_last_run

    if await get_last_run(PENNDOT_UNHIDDEN):
        return ""
    overwrite = channel.overwrites_for(guild.default_role)
    if overwrite.view_channel is False:
        if not channel.permissions_for(guild.me).manage_roles:
            logger.warning("[penndot] #%s is still hidden from @everyone; unhiding needs Manage Roles", channel.name)
            return "Couldn't unhide #traffic-alerts: I need the Manage Roles permission. I'll retry next run."
        overwrite.view_channel = None
        await channel.set_permissions(
            guild.default_role, overwrite=None if overwrite.is_empty() else overwrite,
            reason="#traffic-alerts is now public",
        )
        logger.info("[penndot] removed the @everyone view deny on #%s (now public)", channel.name)
        note = f"Unhid <#{channel.id}>: everyone can see it now."
    else:
        note = ""
    await set_last_run(PENNDOT_UNHIDDEN, datetime.now(timezone.utc).isoformat())
    return note


async def _refresh_penndot(bot) -> tuple[bool, str]:
    """One #traffic-alerts refresh. Returns (channel updated?, human-readable outcome)."""
    from penndot import RcrsError, load_channel_sections
    from digest import DigestOptions, post_penndot_channel
    from store import get_all_settings, get_channel, load_digest_message, save_digest_message

    d = config.CHANNEL_DEF_BY_KEY["penndot"]
    key = d["key"]
    guild = await _resolve_managed_guild(bot)
    if guild is None:
        logger.debug("[%s] no managed guild (yet) — skipping", key)
        return False, "Skipped: no managed server yet (run /setup)."

    notes: list[str] = []
    try:
        notes = await _remove_retired_channels(bot, guild)
    except Exception as exc:  # cleanup must never block the refresh
        logger.error("[%s] retired channel cleanup failed: %s", key, exc, exc_info=True)
        notes = [f"Old-channel cleanup failed: {exc}"]
    suffix = "".join(f"\n{n}" for n in notes)

    conf = await get_channel(key)
    if conf is not None and not conf["enabled"]:
        logger.debug("[%s] disabled — skipping", key)
        return False, "Skipped: the #traffic-alerts channel is disabled." + suffix

    try:
        sections = await load_channel_sections()
    except RcrsError as exc:
        logger.warning("[%s] PennDOT fetch failed; leaving the channel as-is: %s", key, exc)
        return False, f"PennDOT fetch failed; channel left as-is. {exc}" + suffix

    try:
        channel, reason = await _get_or_create_penndot_channel(bot, guild, d, conf)
        if channel is None:
            return False, reason + suffix
        if not d.get("private"):
            try:
                note = await _unhide_penndot_channel(guild, channel)
            except discord.HTTPException as exc:  # never blocks the refresh; retried next run
                logger.error("[%s] unhiding #%s failed: %s", key, channel.name, exc)
                note = f"Couldn't unhide #traffic-alerts: {exc}"
            if note:
                suffix += f"\n{note}"
        options = DigestOptions.from_settings(await get_all_settings())
        stored = await load_digest_message(key)
        primary_id = await post_penndot_channel(channel, sections, stored[0] if stored else None, d["title"], options)
        await save_digest_message(key, primary_id, _monday_of_week(datetime.now().date()).isoformat())
        counts = f"{len(sections.live)} live, {len(sections.ongoing)} ongoing, {len(sections.upcoming)} upcoming"
        logger.info("[%s] updated #%s (%s)", key, channel.name, counts)
        return True, f"Updated <#{channel.id}>: {counts}." + suffix
    except Exception as exc:
        logger.error("[%s] update failed: %s", key, exc, exc_info=True)
        return False, f"Update failed: {exc}" + suffix


def setup_scheduler(bot) -> AsyncIOScheduler:
    global _scheduler
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    scheduler.add_job(
        run_digest_job,
        CronTrigger(hour=config.DIGEST_EDIT_HOUR, minute=0, timezone=config.TIMEZONE),
        args=[bot],
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_penndot_job,
        CronTrigger(minute=config.PENNDOT_CRON_MINUTES, timezone=config.TIMEZONE),
        args=[bot],
        id="penndot",
        replace_existing=True,
        max_instances=1,        # a slow PennDOT response never stacks runs
        coalesce=True,          # missed runs collapse into one
        misfire_grace_time=600,
    )

    _scheduler = scheduler
    return scheduler


def get_scheduler() -> Optional[AsyncIOScheduler]:
    return _scheduler
