"""Discord client, on_ready, slash command registration."""

import logging
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from classify_events import classify_events, build_discord_embeds, load_events as load_classify_events
from scheduler import (
    setup_scheduler, run_digest_job, rerender_digests_from_store, _ensure_category,
    _order_managed_categories, is_bot_owned_channel, _clear_bot_messages,
    _create_digest_channel, run_penndot_job,
)
from store import (
    init_db, get_last_run, clear_all_events,
    set_channel, get_channel, get_all_channels,
    clear_all_channels, get_setting, set_setting, get_all_settings,
    get_managed_categories, delete_managed_category, claim_daily_quota,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()  # no privileged intents: the bot is slash-command only


class PhillyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self._scheduler = None
        self._synced = False

    async def setup_hook(self) -> None:
        await init_db()
        self._scheduler = setup_scheduler(self)
        self._scheduler.start()
        logger.info("Scheduler started")

        # Register slash commands into the tree (global scope, in-memory).
        # Actual sync happens per-guild in on_ready for instant availability.
        self.tree.add_command(about_command)
        self.tree.add_command(ask_command)
        self.tree.add_command(refresh_command)
        self.tree.add_command(status_command)
        self.tree.add_command(classify_command)
        self.tree.add_command(setup_command)
        self.tree.add_command(teardown_command)
        self.tree.add_command(redraw_command)
        self.tree.add_command(config_command)
        self.tree.add_command(channels_command)

        # Lock every command to server admins: this sets each command's default
        # member permissions to Administrator, so Discord hides them from non-admins
        # in the picker and rejects non-admin invocations. Applies to all current and
        # future commands automatically. (A server admin can still re-grant a command
        # to another role via Server Settings → Integrations; the per-command runtime
        # permission checks remain as a secondary gate for that case.)
        admin_only = discord.Permissions(administrator=True)
        for command in self.tree.get_commands():
            command.default_permissions = admin_only

    async def _sync_commands(self) -> None:
        """Sync commands per-guild (instant) and clear remote globals (no dupes).

        Per-guild commands appear immediately, unlike global commands which can take
        up to ~1 hour to propagate. We also delete any remote global commands so the
        guild copies don't show up twice in the picker.
        """
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to guild %s (%d)", len(synced), guild.name, guild.id)
        # Remove remote global commands (leftover from older global syncs) without
        # touching the in-memory tree, so on_guild_join can still copy globals.
        try:
            if self.application_id:
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                logger.info("Cleared remote global commands (using per-guild)")
        except Exception as exc:
            logger.warning("Could not clear remote global commands: %s", exc)

    async def on_ready(self) -> None:
        logger.info("Ready! Logged in as %s | %d guild(s)", self.user, len(self.guilds))
        if not self._synced:
            self._synced = True
            try:
                await self._sync_commands()
            except Exception as exc:
                logger.error("Command sync failed: %s", exc, exc_info=True)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("Joined guild %s (%d) — syncing commands", guild.name, guild.id)
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as exc:
            logger.error("Per-guild sync failed for %s: %s", guild.id, exc)

    async def on_message(self, message: discord.Message) -> None:
        # Deliberately replaces commands.Bot.on_message with a no-op: the bot is
        # slash-only, so skipping process_commands keeps the default "!help" inert
        # (it would otherwise still answer in DMs, where message text is visible).
        return


bot = PhillyBot()

# Per-user cooldown for /ask, kept in memory (resets on restart). Maps user id →
# monotonic timestamp of their last accepted question. The hard cost ceiling is
# the per-day budget in the DB (store.claim_daily_quota); this just smooths bursts.
_ask_cooldowns: dict[int, float] = {}

# Resumable /ask conversations, kept in memory (resets on restart). Maps a short
# context id → {"user_id", "messages" (text-only turns), "expires" (monotonic)}.
# A follow-up replays the stored turns so the model can resolve clarifications;
# trimmed to ASK_MAX_HISTORY_TURNS pairs and expired after ASK_CONTEXT_TTL_SECONDS.
_ask_contexts: dict[str, dict] = {}


def _prune_ask_contexts(now: float) -> None:
    """Drop expired conversation contexts (cheap, called per /ask invocation)."""
    for cid in [c for c, ctx in _ask_contexts.items() if ctx["expires"] <= now]:
        del _ask_contexts[cid]


@app_commands.command(name="ask", description="Ask about Philadelphia (events, closures, parks…); I'll search the web to answer")
@app_commands.describe(
    question="Your question, e.g. 'why is FDR Park fenced off?'",
    continue_id="Optional: a context id from a previous answer, to ask a follow-up with that context",
)
@app_commands.checks.has_permissions(administrator=True)
async def ask_command(
    interaction: discord.Interaction, question: str, continue_id: Optional[str] = None
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    now = time.monotonic()
    _prune_ask_contexts(now)

    # Per-user cooldown (cheap, in-memory) — checked before any API spend.
    last = _ask_cooldowns.get(interaction.user.id)
    if last is not None and (remaining := config.ASK_USER_COOLDOWN_SECONDS - (now - last)) > 0:
        await interaction.followup.send(
            f"⏳ Please wait {int(remaining) + 1}s before asking again.", ephemeral=True
        )
        return

    # Resolve an optional follow-up context: must exist, be unexpired, and belong
    # to this user (no resuming someone else's conversation). Otherwise answer fresh.
    history: list[dict] = []
    context_id = (continue_id or "").strip().lower() or None
    resumed = False
    if context_id:
        ctx = _ask_contexts.get(context_id)
        if ctx and ctx["expires"] > now and ctx["user_id"] == interaction.user.id:
            history = ctx["messages"]
            resumed = True
        else:
            context_id = None  # stale / not found / not yours → fresh answer

    # Hard daily budget ceiling (shared across all users), enforced in the DB.
    today = datetime.now(timezone.utc).date().isoformat()
    granted, used = await claim_daily_quota("ask", today, config.ASK_DAILY_LIMIT)
    if not granted:
        await interaction.followup.send(
            f"🚧 I've hit today's question limit ({config.ASK_DAILY_LIMIT}). Try again tomorrow.",
            ephemeral=True,
        )
        return
    _ask_cooldowns[interaction.user.id] = now

    from ask import answer_question

    result = await answer_question(question, history=history)
    if not result.ok:
        await interaction.followup.send(f"⚠️ {result.text}", ephemeral=True)
        return

    # Persist the turn under a context id (reuse on a follow-up, else mint a new one),
    # trimmed to the most recent ASK_MAX_HISTORY_TURNS Q&A pairs to bound token growth.
    context_id = context_id or secrets.token_hex(3)
    messages = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": result.text},
    ]
    messages = messages[-(config.ASK_MAX_HISTORY_TURNS * 2):]
    _ask_contexts[context_id] = {
        "user_id": interaction.user.id,
        "messages": messages,
        "expires": now + config.ASK_CONTEXT_TTL_SECONDS,
    }

    embed = discord.Embed(
        title="🔎 Philly answer",
        description=result.text[:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Question", value=question[:1024], inline=False)
    if result.sources:
        lines = [f"• [{title[:80]}]({url})" for title, url in result.sources[:5]]
        embed.add_field(name="Sources", value="\n".join(lines)[:1024], inline=False)
    ttl_min = config.ASK_CONTEXT_TTL_SECONDS // 60
    note = "continued prior context • " if resumed else ""
    embed.set_footer(
        text=(
            f"AI-generated from web search — verify with official sources.\n"
            f"{note}Follow up within {ttl_min}m: /ask continue_id:{context_id}  •  "
            f"({used}/{config.ASK_DAILY_LIMIT} today)"
        )
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="refresh", description="Manually re-poll all sources and update digests, including #traffic-alerts")
@app_commands.describe(clear="If True, delete all existing messages in configured channels before posting")
@app_commands.checks.has_permissions(manage_messages=True)
async def refresh_command(interaction: discord.Interaction, clear: bool = False) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    digest_error: Optional[Exception] = None
    try:
        if clear:
            channels = await get_all_channels()
            for ch in channels:
                channel_id = ch["channel_id"]
                if channel_id is None:
                    continue
                channel = bot.get_channel(channel_id)
                if channel is None:
                    logger.warning("Channel %d not found during clear", channel_id)
                    continue
                # Ownership guard + bot-own-only deletion: never purge a channel the
                # bot didn't create, and never delete messages that aren't the bot's.
                if not await is_bot_owned_channel(bot, channel):
                    logger.warning("Refusing to clear non-bot channel %d (%s)", channel_id, ch["digest_key"])
                    continue
                deleted = await _clear_bot_messages(channel, bot)
                logger.info("Cleared %d bot message(s) from channel %d (%s)", deleted, channel_id, ch["digest_key"])
            db_deleted = await clear_all_events()
            logger.info("Cleared %d events from database", db_deleted)
        # /refresh re-polls + re-classifies, then RECONCILES managed channels to the
        # fresh results and posts fresh digests (no in-place edits — see
        # scheduler._reconcile_and_post_digests). The daily scheduled job keeps its
        # create + edit-in-place behavior; only this manual path reconciles.
        await run_digest_job(bot, reconcile=True)
    except Exception as exc:
        # Real pipeline failure (distinct from the ack below failing to deliver).
        logger.error("/refresh pipeline failed: %s", exc, exc_info=True)
        digest_error = exc

    # #traffic-alerts has its own 15-min job (the digest job skips own_job channels); /refresh runs
    # it too, even if the digest failed. It shares the job's lock, so the two never overlap.
    try:
        penndot_ok, penndot_result = await run_penndot_job(bot)
    except Exception as exc:
        logger.error("/refresh: #traffic-alerts refresh failed: %s", exc, exc_info=True)
        penndot_ok, penndot_result = False, f"Update failed: {exc}"
    penndot_line = f"{'✅' if penndot_ok else '⚠️'} PennDOT: {penndot_result}"

    # A rate-limited run can outlast Discord's 15-min interaction token, and the PennDOT
    # run can delete the channel this was run in (old #penndot-live/-planned), so deliver
    # the ack resiliently (DM fallback) rather than letting a 401/404 look like a failure.
    if digest_error is not None:
        await _resilient_send(interaction, f"❌ Refresh failed: {digest_error}\n{penndot_line}")
        return
    suffix = " (channels cleared)" if clear else ""
    await _resilient_send(interaction, f"✅ Digest refreshed for all channels{suffix}.\n{penndot_line}")


@app_commands.command(name="classify", description="Classify stored events by type and suggest channel splits")
@app_commands.describe(min_count="Min events needed for a dedicated channel (default: 20)")
@app_commands.checks.has_permissions(manage_messages=True)
async def classify_command(interaction: discord.Interaction, min_count: int = 20) -> None:
    await interaction.response.defer(thinking=True)
    try:
        events = load_classify_events(config.DB_PATH)
        if not events:
            await interaction.followup.send("⚠️ No events in the database yet.")
            return
        classifications = await classify_events(events, batch_size=50)
        embeds = build_discord_embeds(events, classifications, min_count=min_count)
        # Post first embed as the followup, then send the rest to the channel
        await interaction.followup.send(embeds=[embeds[0]])
        for embed in embeds[1:]:
            await interaction.channel.send(embeds=[embed])
    except Exception as exc:
        logger.error("/classify failed: %s", exc, exc_info=True)
        await interaction.followup.send(f"❌ Classification failed: {exc}")


@app_commands.command(name="status", description="Show last run time and source health")
async def status_command(interaction: discord.Interaction) -> None:
    last_digest = await get_last_run("last_digest")
    source_health = await get_last_run("source_health")

    if last_digest:
        try:
            dt = datetime.fromisoformat(last_digest)
            last_str = dt.strftime("%A %b %-d at %-I:%M %p")
        except ValueError:
            last_str = last_digest
    else:
        last_str = "Never"

    # Discovery agent (Claude + web search) status — it self-throttles, so show
    # when it last ran, how many events it found, and when it's next due.
    disc_last = await get_last_run("discovery_last_run")
    disc_count = await get_last_run("discovery_last_count")
    if disc_last:
        try:
            d_dt = datetime.fromisoformat(disc_last)
            d_str = d_dt.strftime("%b %-d")
            next_due = (d_dt + timedelta(days=config.DISCOVERY_EVERY_DAYS)).strftime("%b %-d")
            disc_str = f"{d_str} ({disc_count or '0'} found); next due ~{next_due}"
        except ValueError:
            disc_str = disc_last
    else:
        disc_str = "Never (runs on next daily job)"

    # Major-event web verification status.
    review_last = await get_last_run("major_review_last_run")
    review_demoted = await get_last_run("major_review_demoted")
    if review_last:
        try:
            r_str = f"{datetime.fromisoformat(review_last).strftime('%b %-d')} ({review_demoted or '0'} demoted)"
        except ValueError:
            r_str = review_last
    else:
        r_str = "Never"

    health_str = source_health or "No data"
    msg = (
        f"**Philly Bot Status**\n"
        f"Last digest run: {last_str}\n"
        f"Discovery agent: {disc_str}\n"
        f"Major-event review: {r_str}\n"
        f"Source health: `{health_str}`"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@app_commands.command(name="about", description="What this bot is, how it works, and what every command does")
async def about_command(interaction: discord.Interaction) -> None:
    n_sources = len(config.ALL_SOURCES)
    hour = config.DIGEST_EDIT_HOUR

    embed = discord.Embed(
        title="🔔 Philly Bot",
        description=(
            "I keep a live board of what's happening in Philadelphia — events, live "
            "music, and road/transit closures — as auto-updating weekly digests, sorted "
            "into themed channels so you can skim what's on without hunting across a "
            "dozen sites.\n\n"
            "**Why:** make discovering local events effortless. You skim; I do the "
            "gathering, sorting, and updating."
        ),
        color=0x0057A8,
    )

    embed.add_field(
        name="How it works",
        value=(
            f"Each morning (~{hour}:00 ET) I poll {n_sources} sources, drop past and "
            "out-of-area items, remove cross-source duplicates, and use Claude to tag "
            "each event with a category, a music genre, and whether it's a *major* "
            "event. On Monday I post a fresh weekly digest in each channel; Tue–Sun I "
            "edit those same messages in place so they stay current."
        ),
        inline=False,
    )

    embed.add_field(
        name="What I control (automatic)",
        value=(
            "• Which sources are polled, and how often\n"
            "• Filtering to Philadelphia + the lookahead window\n"
            "• De-duplication and classification (category / genre / major)\n"
            "• When digests post and refresh (daily schedule)\n"
            "• Auto-creating channels as event buckets fill up\n"
            "• Auto-deleting empty auto-created channels — only if you enable it"
        ),
        inline=False,
    )

    embed.add_field(
        name="What you control (manual)",
        value=(
            "• `/setup` to build channels and `/teardown` to start fresh\n"
            "• `/redraw` to reconcile channels to config and re-render\n"
            "• `/config` to tune display and channel-lifecycle behavior\n"
            "• `/refresh` to re-poll on demand\n"
            "• I only ever delete or clear channels **I** created and track — "
            "never one you made."
        ),
        inline=False,
    )

    embed.add_field(
        name="Commands — info (Administrator only)",
        value=(
            "🔒 **All commands require the Administrator permission.**\n"
            "`/about` — what this bot is and how it works\n"
            "`/status` — last digest run and source health\n"
            "`/channels` — list digest channels and their status"
        ),
        inline=False,
    )

    embed.add_field(
        name="Commands — content",
        value=(
            "`/refresh [clear]` — re-poll all sources and update digests now: keeps "
            "existing channels (mutes survive), updates messages in place, and adds/removes "
            "channels to match what's on (`clear` wipes stored events first). Also refreshes "
            "#traffic-alerts, which otherwise updates itself every 15 min\n"
            "`/classify [min_count]` — classify stored events and suggest channel splits\n"
            "`/ask <question>` — ask about Philly; I search the web and answer with sources"
        ),
        inline=False,
    )

    embed.add_field(
        name="Commands — channels",
        value=(
            "`/setup` — delete the bot's own channels, then poll, classify, and rebuild\n"
            "`/teardown` — delete only the channels & categories I created, and reset mappings\n"
            "`/redraw` — create missing channels, fix categories, and re-render (non-destructive)\n"
            "`/config [setting] [value]` — view or change runtime settings"
        ),
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Channel management commands ────────────────────────────────────────────────


async def _resilient_send(interaction: discord.Interaction, content: str) -> None:
    """Send an ephemeral followup; fall back to a DM if the interaction is unusable.

    Two long-job hazards make the followup target fail: the invoking channel was
    deleted (/setup/teardown → 404 Unknown Message), or the interaction token has
    expired because the job ran past Discord's 15-minute window (a rate-limited
    refresh → 401 Invalid Webhook Token, code 50027). In both cases a normal DM
    still works, since it uses the bot's auth rather than the interaction token.
    """
    text = content[:1900]
    try:
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning("Followup failed (%s) — falling back to DM", exc)
        try:
            await interaction.user.send(text)
        except discord.HTTPException:
            logger.warning("Could not deliver message (followup failed and DM failed)")


async def _teardown_owned(guild: discord.Guild, dry_run: bool = False) -> tuple[int, int]:
    """Delete ONLY the channels and categories the bot created and tracks.

    Channels: every DB-tracked channel that passes the ownership guard
    (auto_created in this managed guild). Categories: only those recorded in
    managed_categories, and only when empty — a category still holding any channel
    (e.g. one a user added) is kept, so we never orphan or touch non-bot channels.
    Clears the channel mappings afterward. Returns (channels_deleted, categories_deleted).

    When `dry_run` is True, nothing is deleted and no DB state is mutated: every
    channel/category that *would* be removed is logged instead, and the returned
    counts are the would-delete totals. This lets an operator confirm the target
    list is only bot-created channels before allowing the real thing.
    """
    deleted_channels = 0
    for conf in await get_all_channels():
        channel_id = conf["channel_id"]
        if channel_id is None:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        if not await is_bot_owned_channel(bot, channel):
            logger.warning("teardown: skipping non-bot channel %d (%s)", channel_id, conf["digest_key"])
            continue
        if dry_run:
            logger.info("teardown DRY RUN: would delete bot channel #%s", getattr(channel, "name", "?"))
            deleted_channels += 1
            continue
        try:
            await channel.delete(reason="teardown: bot-created channel")
            deleted_channels += 1
        except discord.HTTPException as exc:
            logger.warning("teardown: could not delete #%s: %s", getattr(channel, "name", "?"), exc)
    if not dry_run:
        await clear_all_channels()

    deleted_categories = 0
    for category_id in await get_managed_categories(guild.id):
        category = guild.get_channel(category_id)
        if category is None:
            if not dry_run:
                await delete_managed_category(category_id)
            continue
        if dry_run:
            # Real teardown deletes the bot channels first, then drops a category
            # only if it ends up empty. Mirror that here without deleting: the
            # category would be empty iff every channel it holds is bot-owned.
            remaining = [c for c in category.channels if not await is_bot_owned_channel(bot, c)]
            if remaining:
                logger.warning("teardown DRY RUN: would keep category '%s' — %d non-bot channel(s) remain",
                               category.name, len(remaining))
                continue
            logger.info("teardown DRY RUN: would delete bot category '%s'", category.name)
            deleted_categories += 1
            continue
        # Never delete a category that still holds channels (only ours were just
        # removed; anything left is not ours) — keep it and the tracking row.
        if category.channels:
            logger.warning("teardown: keeping category '%s' — still has %d channel(s)",
                           category.name, len(category.channels))
            continue
        try:
            await category.delete(reason="teardown: bot-created category")
            deleted_categories += 1
        except discord.HTTPException as exc:
            logger.warning("teardown: could not delete category '%s': %s", category.name, exc)
        await delete_managed_category(category_id)

    verb = "would remove" if dry_run else "removed"
    logger.info("teardown: %s %d channel(s), %d category(ies)", verb, deleted_channels, deleted_categories)
    return deleted_channels, deleted_categories


@app_commands.command(name="setup", description="(Re)bootstrap: delete the bot's own channels, then poll, classify, and rebuild")
@app_commands.checks.has_permissions(manage_channels=True)
async def setup_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Run this in a server, not a DM.", ephemeral=True)
        return
    if not guild.me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "❌ I need the **Manage Channels** permission to create channels.", ephemeral=True
        )
        return

    await set_setting(config.SETTING_MANAGED_GUILD_ID, str(guild.id))

    # Announce BEFORE teardown — the invoking channel still exists here. If it gets
    # deleted below, later messages fall back to a DM via _resilient_send.
    await _resilient_send(
        interaction,
        "⏳ Rebuilding: removing the channels I created, then polling sources, classifying "
        "events, and recreating channels. This can take a few minutes. (If I delete the "
        "channel you ran this in, I'll DM you the result.)",
    )

    # ── Teardown: delete ONLY bot-created, tracked channels/categories. ──
    await _teardown_owned(guild)

    # Seed default settings (only if not already set, so tuning survives re-setup).
    for key, value in config.DEFAULT_SETTINGS.items():
        if await get_setting(key) is None:
            await set_setting(key, value)

    # Run the full pipeline once. It dynamically recreates + populates channels.
    try:
        await run_digest_job(bot)
    except Exception as exc:
        logger.error("/setup pipeline failed: %s", exc, exc_info=True)
        await _resilient_send(interaction, f"❌ Setup pipeline failed: {exc}")
        return

    channels = await get_all_channels()
    live = [c for c in channels if c["channel_id"] and bot.get_channel(c["channel_id"])]
    lines = [f"✅ Setup complete in **{guild.name}** — {len(live)} channel(s) active."]
    for c in sorted(live, key=lambda x: x["digest_key"]):
        ch = bot.get_channel(c["channel_id"])
        lines.append(f"• {c['digest_key']} → #{ch.name}")
    lines.append("\nTune behavior with `/config`. Channels auto-create as events arrive.")
    await _resilient_send(interaction, "\n".join(lines))


@app_commands.command(name="teardown", description="Delete only the channels and categories I created, and reset mappings")
@app_commands.checks.has_permissions(manage_channels=True)
async def teardown_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Run this in a server, not a DM.", ephemeral=True)
        return
    if not guild.me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "❌ I need the **Manage Channels** permission to delete channels.", ephemeral=True
        )
        return

    safe_mode = (await get_setting(config.SETTING_SAFE_MODE, "on")) == "on"
    if safe_mode:
        await _resilient_send(
            interaction,
            "🛡️ **Safe mode is on** — this is a dry run. I'll report exactly what I *would* "
            "delete (only channels/categories I created) without removing anything.",
        )
    else:
        await _resilient_send(
            interaction,
            "🧹 Tearing down: deleting only the channels and categories I created. "
            "(If I delete the channel you ran this in, I'll DM you the result.)",
        )
    try:
        chans, cats = await _teardown_owned(guild, dry_run=safe_mode)
    except Exception as exc:
        logger.error("/teardown failed: %s", exc, exc_info=True)
        await _resilient_send(interaction, f"❌ Teardown failed: {exc}")
        return
    if safe_mode:
        await _resilient_send(
            interaction,
            f"🛡️ Dry run complete — **nothing was deleted**. I would have removed "
            f"{chans} channel(s) and {cats} category(ies) I created (see the bot logs for names). "
            "If that list looks right, run `/config safe_mode off` and re-run `/teardown`.",
        )
    else:
        await _resilient_send(
            interaction,
            f"✅ Teardown complete — removed {chans} channel(s) and {cats} category(ies) I created. "
            "Run `/setup` to rebuild.",
        )


@app_commands.command(name="redraw", description="Reconcile channels to config (create missing, fix categories) and re-render — non-destructive")
@app_commands.checks.has_permissions(manage_channels=True)
async def redraw_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Run this in a server, not a DM.", ephemeral=True)
        return
    if not guild.me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "❌ I need the **Manage Channels** permission.", ephemeral=True
        )
        return

    # Make sure this guild is the managed one and defaults are seeded.
    await set_setting(config.SETTING_MANAGED_GUILD_ID, str(guild.id))
    for k, v in config.DEFAULT_SETTINGS.items():
        if await get_setting(k) is None:
            await set_setting(k, v)

    created: list[str] = []
    moved: list[str] = []
    kept: list[str] = []
    cat_cache: dict[str, object] = {}

    for d in config.CHANNEL_DEFINITIONS:
        if not d["default"]:
            continue
        conf = await get_channel(d["key"])
        # Respect an explicit disable — don't recreate or touch it.
        if conf is not None and not conf["enabled"]:
            continue
        live = bot.get_channel(conf["channel_id"]) if conf and conf["channel_id"] else None
        category = await _ensure_category(guild, d["category_name"], cat_cache)
        if live is not None:
            # Exists already — keep it; only re-parent if it's in the wrong category.
            # Ownership guard: only ever move a channel the bot created and tracks.
            target_cat_id = category.id if category else None
            if live.category_id != target_cat_id and await is_bot_owned_channel(bot, live):
                try:
                    await live.edit(category=category, reason="/redraw category reconcile")
                    moved.append(d["key"])
                except discord.HTTPException as exc:
                    logger.warning("redraw: could not move #%s: %s", live.name, exc)
                    kept.append(d["key"])
            else:
                kept.append(d["key"])
        else:
            # Missing — create it per config.
            channel = await _create_digest_channel(guild, d, category)
            await set_channel(d["key"], channel.id, guild.id, enabled=True, auto_created=True)
            created.append(d["key"])
            logger.info("redraw: created #%s under '%s' for %s", channel.name, d["category_name"], d["key"])

    # Keep bot categories as a block at the bottom, Important first (idempotent).
    await _order_managed_categories(guild)

    # Re-render content from the data store in the current format.
    rendered = await rerender_digests_from_store(bot)

    lines = ["✅ Redraw complete."]
    if created:
        lines.append(f"**Created ({len(created)}):** {', '.join(created)}")
    if moved:
        lines.append(f"**Re-categorized ({len(moved)}):** {', '.join(moved)}")
    if kept:
        lines.append(f"**Kept ({len(kept)}):** {len(kept)} channel(s) left as-is")
    lines.append(f"**Re-rendered:** {rendered} channel(s)")
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


def _setting_hint(key: str) -> str:
    """Short allowed-values hint for a setting, from its spec."""
    spec = config.SETTINGS_SPEC[key]
    t = spec["type"]
    if t == "bool":
        return "on/off"
    if t == "choice":
        return "|".join(spec["choices"])
    if t == "int":
        return f"int>={spec.get('min', 0)}"
    return "?"


# Discord allows max 25 choices; we have well under that.
_SETTING_CHOICES = [
    app_commands.Choice(name=f"{k} ({_setting_hint(k)})", value=k)
    for k in config.SETTINGS_SPEC
]


@app_commands.command(name="config", description="View or change runtime settings")
@app_commands.describe(setting="Which setting to change (omit to view all)", value="New value")
@app_commands.choices(setting=_SETTING_CHOICES)
@app_commands.checks.has_permissions(manage_channels=True)
async def config_command(
    interaction: discord.Interaction,
    setting: Optional[app_commands.Choice[str]] = None,
    value: Optional[str] = None,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    if setting is None:
        current = {**config.DEFAULT_SETTINGS, **(await get_all_settings())}
        rows = []
        for k, spec in config.SETTINGS_SPEC.items():
            val = current.get(k, "(unset)")
            rows.append(f"{k:<26} {val:<8} [{_setting_hint(k)}]  {spec['help']}")
        body = "\n".join(rows)
        await interaction.followup.send(
            f"**Current settings** (use `/config <setting> <value>` to change)\n```\n{body}\n```"[:1950],
            ephemeral=True,
        )
        return

    if value is None:
        spec = config.SETTINGS_SPEC[setting.value]
        await interaction.followup.send(
            f"❌ Provide a `value`. `{setting.value}` expects **{_setting_hint(setting.value)}** — {spec['help']}.",
            ephemeral=True,
        )
        return

    key = setting.value
    ok, result = config.validate_setting(key, value)
    if not ok:
        await interaction.followup.send(f"❌ `{key}` {result}.", ephemeral=True)
        return

    await set_setting(key, result)
    note = ""
    if key == config.SETTING_AUTO_DELETE_EMPTY and result == "on":
        note = "\n⚠️ Empty **auto-created** channels will now be deleted without confirmation on each run. Manually linked and disabled channels are never auto-deleted."

    # Display settings change message format — re-render all channels now from the
    # data store (delete old messages, repost in the new format).
    if key.startswith("digest_"):
        await interaction.followup.send(
            f"✅ Set `{key}` = `{result}`.{note}\n🔄 Re-rendering channels in the new format…",
            ephemeral=True,
        )
        try:
            count = await rerender_digests_from_store(bot)
            await interaction.followup.send(f"✅ Re-rendered {count} channel(s).", ephemeral=True)
        except Exception as exc:
            logger.error("/config re-render failed: %s", exc, exc_info=True)
            await interaction.followup.send(f"⚠️ Setting saved, but re-render failed: {exc}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Set `{key}` = `{result}`.{note}", ephemeral=True)


@app_commands.command(name="channels", description="List all digest channels and their status")
async def channels_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    by_key = {c["digest_key"]: c for c in await get_all_channels()}
    rows = [f"{'KEY':<22} {'STATUS':<12} CHANNEL", "─" * 50]
    for d in config.CHANNEL_DEFINITIONS:
        conf = by_key.get(d["key"])
        if conf is None or conf["channel_id"] is None:
            status, chan = "not set up", "—"
        else:
            status = "enabled" if conf["enabled"] else "disabled"
            live = bot.get_channel(conf["channel_id"])
            chan = f"#{live.name}" if live else f"id:{conf['channel_id']} (missing)"
        rows.append(f"{d['key']:<22} {status:<12} {chan}")
    await interaction.followup.send("```\n" + "\n".join(rows) + "\n```", ephemeral=True)


def main() -> None:
    bot.run(config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
