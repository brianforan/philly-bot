# Decisions

## Bot categories at the BOTTOM, 🤖-prefixed; Major Events leads the block (audit)
Decision: Reversed the earlier "Major Events pinned to the top" rule. All bot-created
Discord categories now (1) carry a "🤖 " name prefix so it's obvious at a glance they are
bot-managed, and (2) form a contiguous block at the BOTTOM of the server, with the Major
Events category LEADING that block. `config` category names became "🤖 ⭐ Major Events",
"🤖 Music in Philly", "🤖 Happening in Philly".
Ordering is enforced by a dedicated idempotent pass, `scheduler._order_managed_categories`,
rather than by creation-time positioning (which proved fragile: Discord tie-breaks equal
positions by creation order, so a freshly-created Major could slip below an existing managed
category, and a migrated server's old top-pinned category skewed any min-position math).
`_ensure_category` now just creates the category (Discord appends at the bottom); the pass is
the single source of ordering truth. It computes the desired order (Major Events first, then
music, then general, via `_MANAGED_CATEGORY_ORDER`), and if the managed categories aren't
already a contiguous block at the bottom in that order, it moves each to the bottom in order
(each "send to bottom" stacks below the previous), converging in one pass. It is idempotent
(zero API calls when already correct), only repositions bot-tracked categories (never
explicitly moves user categories — they shift up as the block sinks), and fails soft per
category. Called at the end of `run_digest_job`'s channel phase and in `/redraw`.
Verified across four cases (mixed/wrong order, already-correct, an admin category dropped to
the very bottom, only-bot-categories): all converge in one pass to [user cats] then
🤖 Major Events / Music / Happening, and re-running makes zero edits.
Reason: User wants bot-managed categories to NOT dominate the top of the channel list —
people opt in to scrolling down to them — Major Events to lead the managed area, and the 🤖
prefix to make "this is bot-managed" unmistakable. They asked for it to hold "always
regardless," hence enforcement every run (made cheap by the idempotency guard) rather than
only at creation.
Consequence/migration: the category names changed, so on an already-set-up server the bot
creates the new "🤖 …" categories rather than reuse the old-named ones. Run `/setup`
(teardown deletes the old bot-tracked categories, then rebuilds + orders at the bottom) to
apply cleanly; `/redraw` creates the new ones and runs the ordering pass, but leaves the old
(now non-matching) categories in place until a `/setup`.
Trade-off accepted: the per-run pass will move the bot block back to the bottom if an admin
deliberately drags it elsewhere (that is the user's stated "always regardless" intent).
Channel-name (vs category-name) 🤖 prefixing was left out for now — the category marker
already makes the managed grouping clear without making 14 channel slugs noisy; easy to add
if wanted. Supersedes the "pinned to the top" note below.

## Major events get their own category, pinned to the top
Decision: The `major_events` bucket is now its own group (`major`) mapped to a dedicated
Discord category `MAJOR_CATEGORY_NAME` ("⭐ Major Events"), via `ALWAYS_OWN_CATEGORY` so it
gets a category regardless of having only one channel. `scheduler._ensure_category` creates
that category at `position=0` so it sits at the top of the server's channel list.
Reason: user wanted major events always at the top, in a separate category — they're the
headline content and should stand out above the genre/category channels.
Alternative: keep major-events in the default category but order it first within that
category — rejected; a separate top-pinned category is more visually distinct and is what
the user leaned toward.
Note: position is set on creation (fresh `/setup` after teardown recreates the bot's
categories). `/redraw` won't reposition a pre-existing category; re-run `/setup` if the
order needs resetting. Enforcing position every run was avoided to not churn/override a
server admin's manual category arrangement.

## All commands restricted to server admins
Decision: `setup_hook` sets `default_permissions = Permissions(administrator=True)` on every
registered command (via a loop over `self.tree.get_commands()`), so all current and future
commands are Administrator-only. Existing per-command `has_permissions` checks are kept as a
secondary runtime gate.
Reason: user wanted all commands exposed to admin users only. `default_member_permissions` is
Discord's intended mechanism — it hides commands from non-admins in the picker and rejects
their invocations, and the loop means new commands inherit it automatically.
Alternative: a hard global `interaction_check` requiring Administrator (not overridable by
server admins via Integrations) — not chosen by default, since `default_permissions` is
idiomatic and still lets a server owner delegate intentionally. Easy to add if a hard lock
is wanted.

## Major-event precision: three-layer flag (signal + rules + web verification)
Decision: Made the `is_major` flag strict via three layers. (1) The classifier now sees
title + venue + date (previously title only — `load_unclassified_events` and the CLI
loader were title-only), with a rewritten, Philly-anchored major rubric. (2) `_refine_major`
applies deterministic rules on top, but only ever FORCE-PROMOTES: title/venue keywords
(FIFA/World Cup, America250, Mummers, Made in America, playoffs, stadium/arena venues) and
major road-closure keywords promote to major. It never demotes — borderline niche-prestige
cases the model flagged major (e.g. an orchestra concert) are left flagged for review.
(3) `discovery.review_major_events` runs a batched Sonnet + web-search pass over the current
major list each digest run (Phase 2.5) and is the ONLY step that demotes non-citywide events,
writing back via `store.update_major_flags`.
Reason: the model was deciding "major" from the title alone, so prestigious-but-niche events
(e.g. a Philadelphia Orchestra concert) were wrongly flagged major. User wanted "major" to
mean genuinely city-wide (FIFA, major sports, stadium music, 250th anniversary, major road
closures) and prioritized accuracy/verbosity over cost.
Alternative considered: per-event web verification — rejected (~$9/mo and mostly wasted);
batched verification gives the same signal cheaply. Cheapest layers (signal + rules) run
first and fix most cases for free; the web pass is the precision backstop.
Flag-for-review (chosen): Layer 2 does not deterministically demote music/arts; instead
those borderline cases stay flagged major and the web pass (3) confirms or demotes them with
real-world context. This avoids wrongly demoting a genuine arena concert whose venue field is
missing, and lets a genuinely city-wide cultural event survive. The cost is that if the web
pass is unavailable (no key/API error), borderline cases remain major until a later run. The
venue allowlist + keyword list are single tunables in classify_events.py.

## Major-event capture: discovery agent over more hand-written scrapers
Decision: To capture major/citywide events autonomously, added `discovery.py` — a
scheduled Claude (Sonnet) + web-search agent registered as the "Major Event Discovery"
source. It returns normal EventItems that flow through the existing pipeline
(active-filter → lookahead → geo → dedup → validate → classify). It self-throttles to
`DISCOVERY_EVERY_DAYS` (default 7), caps web_search uses (`DISCOVERY_MAX_SEARCHES`) and
candidates (`DISCOVERY_MAX_EVENTS`), and fails soft. Uses the built-in Anthropic
web_search tool (no new dependency) and reuses `ANTHROPIC_API_KEY` (no new secret).
Reason: the old model was "union of ~12 hand-written scrapers"; closing each gap meant
hand-writing another scraper — the opposite of the user's goal (capture majors with
minimal human interaction). A discovery agent finds widely-reported events regardless of
whether a dedicated scraper exists. Estimated cost: well under a few $/month at weekly cadence.
Alternative: keep adding bespoke per-venue scrapers — rejected as unmaintainable and never
comprehensive. Trust: web_search grounds output in real results; candidates without a
concrete in-window date or well-formed URL are dropped, then the Haiku validator + dedup +
geo filter still apply.

## Skipped blind auto-subscribe to all City of Philadelphia calendars
Decision: Did NOT auto-enumerate and subscribe to every city Google calendar from
api.phila.gov/calendars (an earlier Phase-1 idea).
Reason: it would flood category channels with civic minutiae (every department's calendar)
— noise, not major events — working against the goal; and its schema couldn't be verified
offline, risking a fragile guess that breaks a working source. The discovery agent covers
the citywide cultural tail more precisely.
Alternative: defensive regex union with the hardcoded 5 as fallback — deferred; revisit only
if a specific high-value city calendar is identified.

## Ticketmaster: added the sports classification
Decision: `_scrape_ticketmaster` now fetches `music`, `sports`, and `arts & theatre`
(was `music`, `arts & theatre`).
Reason: World Cup matches and other stadium/pro events are Ticketmaster *Sports* and were
never queried. Routing is by classified category, so these reach sports_fitness/major_events
despite the source's `channel: "music"`.
Alternative: add a separate sports source entry with channel "events" — unnecessary, since
category-based routing already handles it.

## Events lookahead widened 14 → 30 days
Decision: `EVENTS_LOOKAHEAD_DAYS` 14 → 30; the discovery agent targets the same window.
Reason: major events are announced weeks ahead; a 14-day clip dropped them before they were
ever surfaced. 30 balances lead time against channel volume/classify cost (music stays 60).
Alternative: a separate per-major lookahead — not feasible cleanly, since `is_major` isn't
known until after classification (which runs post-poll).

## Discovery observability in /status
Decision: `/status` shows the discovery agent's last run, count found, and next-due date
(from `discovery_last_run` / `discovery_last_count`).
Reason: for hands-off operation the operator needs to see the autonomous component is
working without reading logs.
Alternative: a dedicated log channel with alerts — deferred as heavier than needed.

## Single ownership guard for all destructive channel ops
Decision: Added `scheduler.is_bot_owned_channel(bot, channel)` as the sole gate for
any destructive Discord op (delete channel, delete category, clear messages, move
channel). It returns True only when the channel is recorded in the `channels` table
with `auto_created=1`, `guild_id` == the managed guild, and the channel actually lives
in that guild. Every destructive call site now routes through it: scheduler auto-delete,
/setup teardown, /teardown, /redraw move, /refresh clear.
Reason: Prior audit found paths that could touch channels the bot never created
(name-based teardown, channel_link binding arbitrary channels, purge of all messages).
A single DB-backed guard makes "only touch what I created" verifiable in one place.
Alternative: per-call ad-hoc checks (the old `conf["auto_created"]` inline test) —
rejected as easy to forget at a new call site and it skipped the guild check.

## Track bot-created categories in DB (managed_categories table)
Decision: Added a `managed_categories(category_id, guild_id)` table (created via
`CREATE TABLE IF NOT EXISTS` in init_db — no ALTER migration needed). `_ensure_category`
records a category ONLY when it creates one; categories merely found by name are never
recorded. Teardown deletes only tracked category IDs, and only when the category is
empty (any remaining channel — e.g. one a user added — means we keep it).
Reason: Replaces name-based category deletion (which could delete a user's category that
happened to share a managed name). Schema change approved by the user (Option A).
Alternative: don't delete categories at all (Option C, no schema) — rejected by user
who wanted "start fresh" to remove the bot's own categories too.

## Name-based teardown and purge removed
Decision: /setup teardown no longer iterates categories by name; it deletes only
DB-tracked, bot-created channels via the ownership guard, then tracked empty categories.
/refresh clear no longer calls `channel.purge(limit=None)` (which deletes ALL messages);
it uses `_clear_bot_messages` (bot-own messages only) and only on owned channels.
Removed the now-unused `config.MANAGED_CATEGORY_NAMES`.
Reason: purge and name-based deletion are the two ways the bot could affect content it
did not create. Both are gone.
Alternative: keep purge gated behind the guard — rejected; purge still deletes non-bot
messages within an owned channel, which violates "only touch what I created."

## /teardown command added; channel_* commands removed
Decision: Removed /channel_link, /channel_unlink, /channel_enable, /channel_disable,
/channel_delete (and the `_key_autocomplete` helper). Added a standalone /teardown that
deletes only the bot's tracked channels/categories and resets mappings. Kept the manual
admin tooling the user asked for (/refresh, /config, /classify, /channels, /setup, /redraw).
Reason: User wanted the bot's core purpose fully automatic but with admin tooling to
refresh / status-check / start-fresh on demand. channel_link was the entry point for
binding non-bot channels; channel_delete could delete a linked non-bot channel — both
incompatible with the ownership guarantee. Restart behavior is unchanged (setup_hook does
no destructive work).
Alternative: also remove /refresh, /config, /classify, /channels (my initial proposal) —
rejected by the user, who wants that admin surface to exist.

## Music genre channels
Decision: Split music into 6 broad genre channels (rock_indie, pop, hiphop_rnb,
electronic_dance, country_folk, jazz_classical) plus a "More Music" catch-all
("other"). No single combined music channel — genre channels replace it.
Reason: Music is by far the highest-volume category; one channel was unmanageable.
Alternative: Ticketmaster's native genre field, or full TM genre list (~15 channels).
User chose LLM classification for all music and broad 6-genre granularity.

## Genre via LLM, not Ticketmaster API
Decision: Classify genre with Claude Haiku in the same call as category + major,
rather than reading Ticketmaster's classifications[].genre field.
Reason: User chose LLM-for-everything for consistency across all music sources.
Alternative: Use TM native genre (free, no tokens) with LLM fallback for other
sources — rejected to avoid label drift between TM and non-TM events.

## Re-classify music missing a genre
Decision: load_unclassified_events also returns music rows where genre IS NULL
(category already set). So music classified before genre support gets a genre on
the next run without a full clear.
Reason: Avoids stranding all pre-existing music in the "More Music" catch-all.
Alternative: Require /refresh clear:true to rebuild — rejected as heavier-handed.

## Events must be current/future unless over a period
Decision: An event is shown only if its start is now/future, OR it's a multi-day
period whose end_time is still now/future (ongoing). Fully-past single events and
ended periods are dropped. Implemented via scraper.event_is_active and applied in
three places: ingestion (poll_all_sources drops past events before storage), DB
prune (delete_past_events keeps rows where end_time >= cutoff even if start < cutoff),
and display (build_grouped_digest / format_closures_digest filter at render time).
Reason: User wanted dates always current/future unless the event spans a period.
The previous prune deleted by start date only, which wrongly removed ongoing
multi-day events.

## Cross-source dedup by normalized title + date
Decision: poll_all_sources dedups by (normalized_title, calendar_date), keeping
the most complete record; digest formatters run a second dedup pass as a safety net.
Reason: Same event from multiple sources was duplicating and wasting classify tokens.
Alternative: exact-id (title+date) dedup only — missed punctuation/casing variants.

## Runtime channel management (no env channel IDs)
Decision: Channel config moved out of env vars into a DB `channels` table. The
canonical set lives in config.CHANNEL_DEFINITIONS (defaults from code); the bot
creates real Discord channels and stores their IDs via the /setup command.
Maintenance via /channels, /channel_enable, /channel_disable, /channel_link,
/channel_unlink. Setup is command-triggered (not auto-on-join), per user choice.
Reason: User wants the bot to self-manage channels with minimal redeploys.
Alternative: auto-create on guild join — rejected; user preferred explicit /setup.

## Command sync: per-guild + clear remote globals
Decision: Commands are synced per-guild in on_ready (and on_guild_join), and any
remote GLOBAL commands are deleted via http.bulk_upsert_global_commands(app_id, []).
Per-guild commands appear instantly (global commands can take ~1 hour to propagate,
which is why a newly added command like /redraw didn't show up). Clearing remote
globals prevents the global+guild duplicates seen earlier. The in-memory tree keeps
the global commands so on_guild_join can copy them to new guilds.
Reason: New commands must appear immediately without duplicates.

## /redraw — non-destructive config reconcile
Decision: /redraw reconciles the live server to CHANNEL_DEFINITIONS without tearing
anything down: for each default definition it creates the channel only if one does
not already exist, re-parents an existing channel if it's in the wrong category,
and otherwise leaves it untouched. Then it re-renders content from the data store
(rerender_digests_from_store). Disabled channels are skipped. Contrast with /setup,
which deletes everything in the managed categories and rebuilds from scratch.
Reason: User wanted a command to apply config (e.g. new categories/channels) and
re-render, "unless one exists already" — i.e. idempotent, non-destructive.

## Channel grouping into Discord categories + setup rebuild
Decision: Channels are grouped; any group with MORE THAN 3 channels gets its own
Discord category, smaller groups fall into the default "Happening in Philly".
Music (7 genre channels) → its own "Music in Philly" category; general channels
(8) → "Happening in Philly". Group + category_name are derived in config from a
key prefix (music_*) and GROUP_OWN_CATEGORY_THRESHOLD=3.
Reason: User wanted music in its own subcategory and a general >3 grouping rule.

/setup is now a clean REBUILD: it deletes every channel inside the bot's managed
categories (config.MANAGED_CATEGORY_NAMES), deletes those categories, clears the
channels table, then reruns the pipeline to recreate + populate. This wipes even
hand-added channels inside those categories — intended per user request.
Category resolution moved from a single stored category_id to by-name lookup
(_ensure_category), since channels can now live in multiple categories.

## Dynamic channels + configurable empty-channel policy
Decision: Channels are managed dynamically by the digest pipeline, not a one-off
/setup. Each run, for every channel definition: if its bucket has >= create_threshold
events and no channel exists, auto-create it; if a channel's bucket is empty, apply
the auto_delete_empty policy. /setup now just registers the managed guild + category,
seeds default settings, and triggers one full pipeline run to bootstrap.
Reason: User wanted setup to run once automatically and channels to manage themselves.

Settings (DB `settings` table, tuned via /config, no redeploy):
- dynamic_channels (on/off, default on) — auto-create channels with events
- auto_delete_empty (on/off, default off) — when off, empty channels are only LOGGED;
  when on, empty channels are DELETED without confirmation ("reckless" mode)
- create_threshold (int, default 1) — events needed before a channel is auto-created
- empty_runs_before_delete (int, default 3) — consecutive empty runs before an empty
  channel is deleted (tracked per-channel via channels.empty_streak; reset when events
  reappear). Prevents thrash from a bucket that's empty one run and populated the next.

Safety rails (apply even in reckless mode):
- Only auto-created channels (channels.auto_created=1) can be auto-deleted.
  Manually /channel_link'd channels (auto_created=0) are never auto-deleted.
- Disabled channels are left entirely alone — disabling is how an admin says
  "keep this channel even when empty."
Reason: User accepted thrash/recklessness for empties but we guard hand-made and
explicitly-kept channels.
Empty channels are deleted only after empty_runs_before_delete consecutive empty
runs (default 3), tracked via channels.empty_streak, to avoid delete/recreate thrash.

## Channel deletion requires typed confirmation
Decision: /channel_unlink only forgets the DB mapping (non-destructive).
/channel_delete deletes the actual Discord channel but requires the user to type
the channel's exact name in a `confirm` parameter; mismatch aborts with the name
shown. Both require Manage Channels.
Reason: User wants deletion available but guarded by explicit approval.
Alternative: unlink-only (no deletion) — superseded by this request.

## Local dev pinned to Python 3.11
Decision: Local venv rebuilt on Python 3.11 to match the container (python:3.11-slim).
Reason: User directive — everything uses 3.11. Some modules use 3.10+ syntax
(e.g. `dict | list`) that fails on 3.9.

## DB pinned to persistent volume
Decision: docker-compose sets DB_PATH=/data/philly_bot.db (the mounted volume).
Reason: Channel mappings + events must survive rebuilds, else /setup would be
required after every deploy. Previously DB_PATH defaulted to /app (ephemeral).

## Closures restored as a default channel
Decision: Re-added a "closures" channel (road-closures). Earlier the road_closure
category folded into "other", but load_events_for_category("other") excludes
channel_type='closures', so closures were routing nowhere. New load_closures_events
pulls channel_type='closures' items into the closures channel.
Reason: Bug fix — road closure events were silently dropped from all digests.

## Description-based digest layout
Decision: events/music digests use embed descriptions (markdown), not fields.
Reason: Discord does not render markdown links in embed field names — hyperlinks
were showing as raw text. Descriptions render links and are more compact.

## Re-render on display-setting change
Decision: When a `digest_*` display setting changes via /config, the bot
immediately re-renders all configured channels FROM THE DATA STORE — deletes its
old digest messages and reposts in the new format — via
scheduler.rerender_digests_from_store. It does NOT re-poll sources or re-classify;
it only reformats stored events. Non-display settings (channel lifecycle) do not
trigger a re-render.
Reason: User wanted format changes to take effect immediately, not next run.
Old messages are deleted (bot's own messages only — no Manage Messages needed) and
reposted fresh rather than edited, because format changes alter embed counts.

## Spec-driven, granular digest configuration
Decision: All runtime settings live in a single config.SETTINGS_SPEC (type +
default + allowed values + help). DEFAULT_SETTINGS, the /config option list, and
validation (config.validate_setting) are all derived from it — adding a setting is
one spec entry, no other code. Digest display is controlled by a DigestOptions
object built from settings (DigestOptions.from_settings) and threaded through the
formatters and post_or_edit_digest.
Reason: User wanted many granular display options ("more is better, can be ignored").

New digest settings: digest_group_by (week/day/distance/flat), digest_sort
(date/distance), digest_show_index, digest_show_distance, digest_show_price,
digest_show_location, digest_max_per_channel (0 = unlimited).
Defaults reproduce the prior behavior (week grouping, index on, all fields shown).

## Index at bottom (default), non-obtrusive
Decision: The index defaults to the BOTTOM of the channel (last message), so it's
the always-visible message without scrolling. Setting digest_index_position
(top|bottom, default bottom) controls it. Section back-links point toward the index
(↓ when bottom, ↑ when top). Non-obtrusiveness comes from the existing edit-in-place
behavior: Tue–Sat runs edit messages in place (silent, no notification/bump); only
Monday (or a structural count change) does a fresh post. So week-to-week the index
just updates in place; ~weekly full rewrites are acceptable per the user.
Reason: User wanted the index pinned at the bottom without constant new messages.

## Jump-link navigation between index and section messages
Decision: Grouped digests post ONE MESSAGE PER SECTION (week/day/etc.). The index
embed's section lines are clickable jump links to each section's message, and each
section message has a "↑ Back to index" link. Implemented as a two-pass post
(_reconcile_messages then _apply_navigation): messages are sent/edited first to get
their IDs, then the index + section-start messages are edited with the cross-links
(URL form https://discord.com/channels/{guild}/{channel}/{message}).
Toggle: digest_jump_links (default on). Closures layout is exempt (no index).
build_grouped_digest returns a RenderedDigest (embeds + section→embed mapping) so
the orchestrator knows which message each section maps to.
Reason: User asked for clickable week navigation with a link back to the index.

## Week-grouped digests with an index embed
Decision: events and music digests now group by WEEK ("This Week", "Next Week",
"Week of <Mon D>"), chronologically, and lead with an index/table-of-contents
embed listing total + per-week counts. Distance bucket moved from the primary
grouping axis to an inline tag on each event line.
Reason: With 14-day (events) / 60-day (music) windows the digests were too long to
scan; weekly sections + an index make them navigable. User asked to split by
week/date and index them.
Alternative: keep distance-bucket grouping for events — dropped as primary axis
(kept as inline tag) since time is the more useful navigation dimension.

## .gitignore (cleanup scan)
Decision: Add a .gitignore at repo root covering .env, *.db, __pycache__/, *.pyc,
venv/, .venv/.
Reason: Repo is not yet under git and .env holds live secrets; ignore secrets,
the SQLite DB, caches, and virtualenvs before the first commit.
Alternative: none — minimal ignore set only.

## .env.example sync (cleanup scan)
Decision: Add placeholder-only TICKETMASTER_API_KEY and GOOGLE_MAPS_API_KEY entries
(with source URLs) to .env.example; kept the existing 511PA_API_KEY entry.
Reason: scraper.py reads both keys (lines ~91, ~1164) but they were undocumented in
the example file.
Alternative: leave undocumented — rejected; example should mirror what code reads.

## config.DIGEST_POST_HOUR removed (cleanup scan)
Decision: Delete the unused DIGEST_POST_HOUR constant; DIGEST_EDIT_HOUR is the only
scheduling hour referenced (scheduler.py:355).
Reason: Verified zero references repo-wide before deletion.
Alternative: keep for symmetry — rejected as dead code.

## config.MESSAGE_CHAR_LIMIT removed (cleanup scan)
Decision: Delete the unused MESSAGE_CHAR_LIMIT constant.
Reason: Verified zero references; digest.py uses its own limits.
Alternative: keep — rejected as dead code.

## requirements.txt duplicate anthropic removed (cleanup scan)
Decision: Remove the conflicting "anthropic>=0.28.0" line; keep "anthropic>=0.105.0".
Reason: Two pins for the same package; the lower one is stale and conflicting.
Alternative: keep both — rejected; pip would resolve to the higher pin anyway.

## scraper.py inline os imports (cleanup scan)
Decision: Add top-level "import os" and replace three inline __import__("os").environ.get(...)
calls (lines ~91, ~957, ~1164) with os.environ.get(...).
Reason: Behavior-identical readability cleanup; one canonical import.
Alternative: leave inline imports — rejected as non-idiomatic.

## .env CHANNEL_*_ID lines (cleanup scan)
Decision: No change made. The cleanup item expected 8 dead CHANNEL_*_ID lines and a
"Discord channel IDs" comment header in .env, but .env contains only the 4 real
secret key/value lines — no channel lines or that header exist.
Reason: Nothing to remove; flagged the discrepancy rather than editing real secrets.
Alternative: n/a.

## Genres consolidated to one source (cleanup scan, item A)
Decision: Made config.MUSIC_GENRES the single source of truth. classify_events.GENRES
is now `config.MUSIC_GENRES`, and store.py derives its known-genre set as
`[g for g in MUSIC_GENRES if g != "other"]`. The genre list is a hardcoded code
constant — no env/user input (per the "code is the source of truth, no user input
unless an explicit override" directive).
Reason: The same 7-genre list was duplicated in three places (config unused, classifier,
store) and could drift. config is the natural leaf module (imported everywhere, imports
nothing internal), so it owns the canonical list.
Alternative: Make classify_events.GENRES canonical and have config import it — rejected;
that pulls `anthropic` into every importer of config and inverts the dependency direction.
Consequence: classify_events.py now imports config, so running it standalone requires
.env to provide DISCORD_BOT_TOKEN (config reads it at import). Acceptable: .env is
already required for any real run.

## scraper_agent.py moved to tools/ (cleanup scan, item B)
Decision: Moved scraper_agent.py (standalone dev/diagnostic tool, not imported by the
runtime) to tools/scraper_agent.py. Added a sys.path shim so it can still import
repo-root modules, repointed SCRAPER_PY to `Path(__file__).resolve().parent.parent /
"scraper.py"`, updated its usage docstring to `python tools/scraper_agent.py`, and
updated the docstring reference in scraper.py to tools/scraper_agent.py.
Reason: User confirmed it is a debugging tool; relocating keeps it without cluttering the
package root. The path fixes are required for it to keep working from the new location.
Alternative: delete it (rejected — user wants to keep it) or run via `python -m`
(rejected — would need an __init__.py and change the documented invocation).

## Songkick Philly duplicate removed (cleanup scan, item C)
Decision: Removed the duplicate "Songkick Philly" entry from EVENT_SOURCES; the
identical entry in MUSIC_SOURCES (channel "music") is the canonical one.
Reason: It was listed in both EVENT_SOURCES and MUSIC_SOURCES, so it appeared twice in
ALL_SOURCES and was fetched twice for the music channel. Only ALL_SOURCES consumes these
lists, so removing the EVENT_SOURCES copy is behavior-neutral (now fetched once).
Alternative: keep both — rejected; duplicates only create future drift/work.

## Cross-source dedup consolidated (cleanup scan, item D)
Decision: scraper.poll_all_sources now calls digest.dedupe_events instead of its own
inline copy. The two implementations were byte-for-byte equivalent (same title
normalization, same completeness scoring, same keep-best-preserve-order loop). Imported
locally inside the function (matching the existing `from validator import validate_items`
pattern) to avoid a scraper<->digest module-level import cycle.
Reason: Eliminate duplicated dedup logic that could drift between scraper and digest.
Alternative: extract a shared helper into a third module — rejected as unnecessary; the
function already lives in digest and the local import is cycle-safe.
Consequence: The dedup log line is now "dedupe_events: removed N..." (from digest)
rather than the channel-typed "%s: deduplication removed N..." — minor observability change.

## /about command
Decision: Added an /about slash command (bot.py) that explains what the bot is, how it
works, its intention, the bot-controlled vs. user-controlled split, and every command
grouped by required permission. Rendered as a single Discord embed, ephemeral, with no
permission gate (available to everyone, like /status and /channels). Source count and
the daily refresh hour are pulled from config (len(ALL_SOURCES), DIGEST_EDIT_HOUR) so the
text can't drift from the actual configuration.
Reason: User requested a self-documenting overview command.
Alternative: plain-text message (rejected — embed fields read better for grouped content);
public/non-ephemeral (rejected — matches the bot's ephemeral info-command convention and
avoids channel clutter).

## scraper.py import hygiene (audit, Phase 1)
Decision: Behavior-neutral cleanups in scraper.py: (1) dropped the unused `field` from
`from dataclasses import dataclass, field` (EventItem uses no `field(...)`); (2) removed
the redundant `import asyncio` inside `poll_all_sources` (asyncio is already imported at
module top, line 3); (3) replaced the two inline `from geocoder import distance_bucket as _db`
(in `_fetch_511pa` and `_scrape_ticketmaster`) with the module-top `distance_bucket` that
is already imported at line 15.
Reason: Same canonical-import cleanup the prior audit applied to inline `os` imports — one
import per name, no shadow aliases. No scraper logic, source definition, or output changes;
verified each `_db(...)` call site uses the identical function.
Alternative: leave the inline imports/alias — rejected as redundant and drift-prone.

## digest.py dead formatter wrappers removed (audit, Phase 1)
Decision: Removed `format_events_digest` and `format_music_digest` from digest.py. Both were
thin wrappers over `build_grouped_digest(...).embeds` with zero callers anywhere in the repo
(runtime path is `post_or_edit_digest` → `build_grouped_digest`; `tools/` does not reference
them). `format_closures_digest` is kept (still called by `post_or_edit_digest`), and
`_DEFAULT_OPTIONS` is kept (still used by `post_or_edit_digest`).
Reason: Dead code superseded by the grouped-digest orchestration; removal is behavior-neutral.
Alternative: keep as a convenience public API — rejected; nothing consumes them and they
duplicate logic already reachable via build_grouped_digest.

## Closures channel normalized onto the shared grouped layout (audit, Phase 2)
Decision: The closures channel now renders through the same `build_grouped_digest` path as
events and music instead of the bespoke `format_closures_digest`. `post_or_edit_digest` no
longer special-cases `layout == "closures"`; layout now only selects an accent colour and
default title via two new maps (`_LAYOUT_COLORS`, `_LAYOUT_DEFAULT_TITLES`) covering events /
music / closures. Removed the now-dead `format_closures_digest` (and its nested `_closure_line`)
and the helpers only it used (`_price_str`, `_EMBED_TOTAL_LIMIT`). The empty-state title was
changed from "{label} — No items this week" to "{label} — Nothing scheduled right now" so it
is correct for every channel and every `digest_group_by` mode.
Reason: User directive — normalize ALL channels for consistency, provided (a) no events are
lost, (b) it normalizes channels, and (c) overall functionality isn't reduced. Closures was
the lone channel bypassing the configurable layout: it had no index, no jump-links, and ignored
every `digest_*` setting. Routing it through `build_grouped_digest` makes all three channel
families honor the same grouping/sort/visibility/index/navigation options.
No event loss (a): `build_grouped_digest` applies the same `dedupe_events` + `event_is_active`
filter the closures formatter did, the per-channel cap defaults to unlimited, and over-long
sections split into continuation embeds — verified 5/5 sample closures placed, 0 dropped.
No functionality reduction (c): the change is additive — closures GAIN the index, jump-links,
and configurable grouping/sort; the bot still polls, classifies, and posts closures unchanged.
The only visible change is the per-line format (now the standard event line with an inline
timestamp/venue/distance) instead of the old `└`-prefixed closure line.
Alternative: keep closures on a distinct compact layout — rejected; it was the inconsistency
the user asked to remove. Supersedes the earlier "Closures layout is exempt (no index)" note.

## Single-week-lookahead channels always group by DAY (audit, Phase 2)
Decision: A channel whose lookahead window is a single week or less (≤ 7 days) is always
grouped by day rather than week, overriding the global `digest_group_by` for those channels
only. Implemented in `post_or_edit_digest`: a layout→lookahead map (`_LAYOUT_LOOKAHEAD_DAYS`,
mirroring config's per-source-type windows) drives the rule; when the window is ≤
`_SINGLE_WEEK_DAYS` (7) and the option isn't already `day`, the per-render options are copied
with `dataclasses.replace(options, group_by="day")` (never mutated, so the shared options
object the scheduler builds once per run is not affected across channels). Today this applies
to the closures channel (`CLOSURES_LOOKAHEAD_DAYS = 7`); events (30) and music (60) keep week
grouping. The rule is lookahead-driven, not hardcoded to closures, so it tracks any future
change to the window constants.
Reason: User directive ("always make single week lookahead a day index"). Week grouping a
7-day window collapses everything into one "This Week" section, so the week index is useless;
a per-day index is the meaningful navigation for a one-week horizon.
No event loss: only the grouping axis changes; verified 5/5 sample closures placed across day
sections with 0 dropped. The override sits at the single `post_or_edit_digest` chokepoint, so
both the daily job and `/config` re-render honor it.
Alternative: respect the global setting and let admins pick — rejected per the explicit
"always"; week-on-a-week-window is never the useful view. Alternative: hardcode closures to
day — rejected; deriving from the lookahead constant generalizes correctly.

## safe_mode dry-run setting for deletions (default on)
Decision: Added a `safe_mode` runtime setting (config.SETTING_SAFE_MODE, default "on").
When on, the two channel/category-deleting paths only LOG what they would delete and
remove nothing: `/teardown` runs `_teardown_owned(dry_run=True)` and the scheduler's
auto-delete-empty branch logs a "would delete" warning instead of deleting. `/setup`'s
internal teardown intentionally always runs for real (dry_run defaults False) — it only
ever touches bot-created channels (same ownership guard) and must clean its own channels
to rebuild, so gating it would break re-setup.
Reason: User is dropping the bot into an existing populated server and wants assurance it
can't delete their channels. The ownership guard already makes that impossible; safe_mode
adds a verifiable preview so the operator can confirm the target list is only bot-created
channels before allowing real deletions. Defaulting on is the conservative posture for the
first-drop-in case; auto-delete is also already off by default.
Alternative: (1) Default off — rejected; the whole point is safety for the anxious first
run. (2) Make safe_mode also gate /setup's teardown — rejected; would leave duplicate bot
channels on re-setup. (3) A single delete-chokepoint helper enforcing the guard — deferred;
larger refactor, the per-path dry_run is sufficient for the stated need.
Note on dry-run category accuracy: a real teardown deletes channels first, then drops a
category if it became empty. In dry-run nothing is deleted, so category emptiness is
computed as "every child channel is bot-owned (would be deleted)" to keep the preview count
honest.

## /ask command — ad-hoc Philly Q&A via Claude + web search
Decision: Added an `/ask <question>` slash command (backend in new `ask.py`,
`answer_question` → `AskResult`) that answers free-text Philadelphia questions using
Sonnet + the built-in web_search tool. Reuses discovery.py's `_web_search_tool` (Philly
location bias) and `_message_text`, plus `ANTHROPIC_API_KEY` (no new secret/dep). Kept it
Administrator-only by inheriting the existing setup_hook default_permissions loop (user
chose "admin-only first"). Replies are ephemeral, as an embed with the answer, the cited
source links, and an "AI-generated — verify" footer.
Reason: User wanted users to ask things like "why is this park fenced off?" and get a
web-grounded answer, but was worried about (a) accuracy and (b) cost.
Accuracy mitigations: system prompt forbids guessing ("if you can't find a specific answer,
say so") and steers to official/local sources; cited URLs are surfaced for verification;
embed footer disclaims AI generation. Cost mitigations: web_search capped at
ASK_MAX_SEARCHES=3, answer at ASK_MAX_TOKENS=1024, an in-memory per-user cooldown
(ASK_USER_COOLDOWN_SECONDS=30), and a hard per-day call ceiling (ASK_DAILY_LIMIT=50)
enforced atomically via new store.claim_daily_quota (counter stored in the settings table
as "<day>|<count>" under internal key "_daily:ask"; resets on day rollover; not in
SETTINGS_SPEC so it never shows in /config).
Alternatives: (1) Public + rate limits — offered, user chose admin-only first; easy to open
later by excluding ask_command from the setup_hook admin loop and relying on the cooldown +
daily cap. (2) Haiku for synthesis to cut cost — deferred; Sonnet is more reliable at
multi-step search reasoning and per-call cost is already cents. (3) Refund the daily-quota
unit on API failure — skipped for atomicity simplicity; the cap is a safety ceiling, so a
rare over-count on errors is acceptable.
Note: ask.py imported locally inside the bot.py command (not at module top) per the
project's circular-import convention.

## /ask follow-ups via optional continue_id (in-memory context)
Decision: Added resumable conversations to /ask. Each answer mints (or reuses) a short
context id (secrets.token_hex(3)) shown in the embed footer; passing it back via the new
optional `continue_id` parameter replays the prior turns so the model can resolve
clarifying questions. Context lives in an in-memory dict `_ask_contexts` in bot.py
(user_id, text-only messages, monotonic expiry), pruned each invocation. `answer_question`
gained a `history` param prepended to the new question. User chose the explicit-id UX over
auto-continue-last or Discord threads.
Reason: User asked for a way to ask follow-up/clarifying questions with the same context.
Explicit id is unambiguous and works with the existing ephemeral replies (threads would
force answers public; auto-continue is ambiguous across multiple questions).
Scope/cost guards: history capped to ASK_MAX_HISTORY_TURNS=6 Q&A pairs (bounds token
growth), context expires after ASK_CONTEXT_TTL_SECONDS=1800 (30 min), and a follow-up is
still subject to the per-user cooldown + daily quota (it's another web-search API call).
A continue_id that is unknown, expired, or belongs to another user is ignored and the
question is answered fresh (no error, no cross-user context leak).
Alternatives: (1) Auto-continue last / (2) Discord threads — offered, not chosen. (3) Persist
context in SQLite for restart survival — deferred; in-memory matches the existing cooldown
store and is sufficient for short clarifications (downside: context lost on restart). (4)
Replay full web_search tool-use/result blocks instead of text-only history — rejected as
bulky/fiddly; text turns are enough and the model can re-search.

## /refresh reconciles managed channels instead of editing in place
Decision: `/refresh` now re-polls + re-classifies (phases 1–2.5, unchanged) and then
RECONCILES managed channels to the fresh classification rather than editing digest
messages in place. Implemented as a new keyword-only param on the shared pipeline —
`run_digest_job(bot, *, reconcile=False)` — with a new phase-3 helper
`scheduler._reconcile_and_post_digests`. The scheduled daily job calls
`run_digest_job(bot)` (default reconcile=False) so its create + edit-in-place behavior
(Mon fresh / Tue–Sun edit) is byte-for-byte unchanged; `/refresh` calls
`run_digest_job(bot, reconcile=True)`.
Reason: editing messages in place on every refresh caused Discord rate limiting, and the
delete-whole-category/channel alternative destroyed per-channel/per-category mute state.
Reconcile keeps existing channel objects (so mutes survive), only deleting buckets that are
no longer needed and only creating newly-needed ones. Chose the shared-helper + parameter
branch (per the task) over a second top-level job, since phases 1–2.5 are identical.
Behavior of the reconcile phase 3, per definition (all destructive ops gated by
`is_bot_owned_channel`): disabled → untouched; no live channel + count >= create_threshold
→ create; live channel not bot-owned → untouched (never clear/rename/delete unmanaged);
live bot-owned + count < threshold → delete + unlink; live bot-owned + still needed → keep
the same object (rename in place only if the definition name changed); every kept/created
channel → clear the bot's own prior messages and post fresh (`stored_message_id=None`).

Ambiguities resolved:
- Delete condition is `count < create_threshold` (covers both empty and below-threshold, per
  the task's "empty / below threshold"). Note this differs from the scheduled job, which only
  removes a channel once it hits count==0 for N consecutive runs — intentional, since the task
  scopes below-threshold deletion to the /refresh path.
- Reconcile deletion honors `safe_mode` (dry-run log when on) — consistent with the repo-wide
  convention that safe_mode gates all destructive deletes (teardown, auto-delete). It does NOT
  gate on `auto_delete_empty`: that setting governs the scheduled streak-based empty deletion,
  whereas removing no-longer-needed buckets is the explicit purpose of the reconcile.
- Create still respects `dynamic_channels` + Manage Channels, matching the scheduled job, so
  the operator's dynamic-channels toggle behaves the same on both paths.
- Live channels that aren't bot-owned are skipped entirely (not even our own messages cleared),
  satisfying "unmanaged channels are never modified, cleared, renamed, or deleted."
- The existing `clear=True` branch is kept as-is (clears bot messages + wipes stored events as
  a hard reset before the pipeline re-polls); its per-channel message clearing is now partly
  redundant with the reconcile's per-channel clear, but harmless.
Alternatives: (1) a separate `refresh_job` function — rejected, would duplicate phases 1–2.5.
(2) gating reconcile deletion on auto_delete_empty — rejected, would make /refresh never
remove stale buckets in the default config, contradicting the acceptance criteria.

## /refresh reverted to edit-in-place (keep channel); ownership guard added at post/clear sites
Decision: Following a rate-limit + safety audit, changed two things (user chose "best overall"
for the message strategy and "yes" to the guard):
1. The /refresh reconcile now UPDATES digests via `_post_digest_to_channel` (edit the stored
   messages in place when the layout matches, repost only on a structural change) instead of
   always clearing + reposting fresh. This SUPERSEDES the earlier "delete old messages, then
   POST fresh; do NOT edit in place" decision above. The channel-level reconcile (create /
   delete / keep / rename managed channels) is unchanged.
2. Added `is_bot_owned_channel` checks at the remaining post/clear sites — the scheduled
   `run_digest_job` Phase 3 (before `_post_digest_to_channel`) and `rerender_digests_from_store`
   (before `_clear_bot_messages` + repost). Destructive ops were already guarded; posting/
   clearing were not.
Reason: (1) Mutes are preserved by keeping the CHANNEL, not by how messages are updated —
editing a message doesn't touch mute state, doesn't ping, and doesn't bump unread. Mechanically,
delete+repost is ~1.5x the Discord API calls of edit-in-place AND re-notifies members every
refresh, so the original "fresh post" design worked against the stated rate-limit goal. Edit-in-
place keeps mutes, minimizes calls, and is silent. `_reconcile_messages` already falls back to
delete+repost only when the message count changed, so structure changes still render correctly.
(2) Previously "never touch an unmanaged channel" for posting/clearing held only because every
`set_channel` call passes `auto_created=True` (no manual-link path exists) — an invariant in
another module, not an enforced check at the point of action. The guard makes it enforced: a
non-owned row (future manual-link feature, manual DB edit, bug) can no longer be posted into or
cleared. No behavior change today (all rows are bot-owned). Fails closed: if ownership can't be
proven (e.g. managed_guild_id unset), the bot skips posting rather than risk touching a channel
it doesn't own.
Scope: is_monday=False in the reconcile so /refresh never force-posts fresh (that Monday-fresh
behavior stays exclusive to the scheduled job). `_clear_bot_messages` is no longer called by the
reconcile (still used by `/refresh clear` and `rerender_digests_from_store`).
Alternatives: (1) embedding the guard inside `_post_digest_to_channel`/`_clear_bot_messages`
themselves (covers all callers in one place) — deferred; explicit per-call-site checks keep the
security-critical guard's own logic untouched (per the "stop and ask before changing the
ownership guard" rule) and read clearly. (2) Keeping always-fresh-post — rejected as strictly
worse for the user's rate-limit priority with no offsetting benefit (mutes already safe via
channel-keep).

## Auto-threads in approved channels (threads.py)
Decision: Approved channels live in a new `thread_channels` table, managed with the admin-only
`/threads add|remove|list`. Threads are public, named from the message's first non-empty line
(author's name for image-only posts), and auto-archive after 7 days. Every member message gets
one, including replies; bot, webhook, and system messages don't.
Reason: Follows the rule that channel IDs are DB state, not env config. A separate table keeps
user channels out of `channels`, so the ownership guard and teardown can never act on them.
Message Content is now enabled because thread names need the text; `on_message` skips
`process_commands` so enabling it doesn't activate the default `!help` prefix command.
Alternative: Generic titles without the privileged intent (rejected: the request is for threads
based on the message); LLM-written titles (not requested, adds cost per message); deleting
non-thread chatter in approved channels (not requested).

## PennDOT RCRS feed starts as a terminal-only preview (tools/penndot_rcrs.py)
Decision: The PennDOT RCRS Event Data service (`https://eventsdata.dot.pa.gov/` `liveEvents`,
`plannedEvents`, `winterConditions`; HTTP Basic auth via `PENNDOT_RCRS_USERNAME` /
`PENNDOT_RCRS_PASSWORD`) is integrated first as a standalone CLI in `tools/`, not imported by the
runtime, that prints a digest-style preview of Philadelphia records. Philadelphia = County code 67
on any of `County`/`CountyFrom`/`CountyTo`/`CountyInc` (keeps events crossing in from Bucks/
Delaware/Montgomery). The feed is statewide with no server-side filters, so filtering is client-side.
Reason: The bot only runs live, so the user wants to tune the output in the terminal before
anything reaches Discord. A tools/ script changes nothing in the deployed bot.
Open forks for the Discord step: (1) route into the existing 1-mile `road-closures` bucket vs. a
new county-wide "Highways & State Roads" bucket (the 1-mile `CLOSURES_MAX_MILES` filter would drop
most state-route items); (2) whether RCRS replaces the never-configured `511PA` source (RCRS is
the data 511PA is built on, so running both would duplicate events); (3) which `LaneStatus`/
`EventType` values the daily digest keeps (transient crashes are stale by the 08:00 run).
Alternative: Writing the parser into `scraper.py` now, unregistered, which is less to move later
but ships unused code to the live bot (rejected for now; move it at the Discord step).

## PennDOT RCRS: private live board + private planned digest, fetched at render time
Decision: PennDOT RCRS data goes to two bot-created, PRIVATE channels (hidden from @everyone;
visible to the bot and server admins), and nowhere else:
- `#penndot-live` — one board message edited in place every 30 minutes (at :15/:45, 24/7) by its
  own scheduler job. Shows incidents plus any work that started in the last 24 hours. Never
  fresh-posts, never auto-deleted; on a fetch failure the board is left as-is (its "updated"
  time shows staleness).
- `#penndot-planned` — part of the daily 08:00 digest. Shows ongoing work (started >24h ago) plus
  upcoming planned-feed events in the next 7 days, with recurring schedules.
Split is by AGE, not by event type. RCRS rows are NEVER written to the events table: both
channels fetch from the API when they render. Parsing lives in a new root module `penndot.py`
(shared by `tools/penndot_rcrs.py`). The never-configured 511PA source is removed.
Reason: User wants no PennDOT output in public channels while it's evaluated, and live traffic
needs a faster cadence than the once-daily digest. Fetch-on-render removes every route to a
public channel (classification → Major Events; the `#other-events` catch-all loads any non-
`closures` row with no category) instead of patching each one, and avoids a synthetic end_time
(the pipeline's `event_is_active` drops past-start items with no end, i.e. all live roadwork).
The planned feed alone is too thin for its own digest (9 statewide, 0 Philadelphia in the first
real run), so long-running work goes to the planned digest rather than the live board.
Alternatives: split by event type (rejected: work starting after 08:00 wouldn't appear until the
next day); everything on the live board + planned feed only in the digest (rejected: digest
would usually be empty); posting a new message per incident (rejected for now: notifies and is
noisy — possible later for severe events only); storing rows in the events table (rejected:
needs leak fixes in classification and loaders plus an expiry hack).
Supersedes the "Open forks" in the terminal-preview entry above.

## Auto-threads feature removed (threads.py, /threads, Message Content intent)
Decision: Removed the auto-thread feature entirely: `threads.py`, the `/threads` command and its
`/about` line, the thread call in `on_message`, the `store` thread helpers, the `THREAD_*` config,
and `intents.message_content`. The bot now requests no privileged intents. `on_message` stays as
a deliberate no-op so discord.py's default `!` prefix handling (e.g. `!help` in DMs) stays off.
The `thread_channels` table is no longer created for new databases; an existing DB keeps its
(now unused) table — dropping it would delete data, so it's left alone.
Reason: The bot crashed at login with `PrivilegedIntentsRequired` because Message Content
wasn't enabled in the Developer Portal. The user chose to drop the feature rather than grant
the intent.
Alternative: Enable Message Content in the portal and keep the feature (rejected by the user);
keep threads without the intent (rejected: thread names come from message text, which the bot
can't read without it). Supersedes "Auto-threads in approved channels (threads.py)" above.

## PennDOT channels show near-home work + nearby incidents only (radius filters)
Decision: Both PennDOT channels now filter by distance from `CENTER_LOCATION` (the configured center point):
road work within 3 mi (`PENNDOT_WORK_MAX_MILES`), live incidents within 5 mi
(`PENNDOT_INCIDENT_MAX_MILES`). Live board = nearby incidents + near-home work that started in
the last 24h; planned digest = older near-home work + near-home planned events (7 days).
Everything else is dropped — including major highway closures outside 3 mi — and so is anything
without coordinates. Board/entry times are absolute (`<t:…:f>`) instead of relative "… ago".
Reason: User: GPS already routes around distant planned work; residents care about work
around the center point and live incidents that hit before GPS reacts. The first real run showed
the county-wide filter surfacing items 6–10 mi away. Relative "Updated N seconds ago" counted
up live, which the user didn't want.
Alternatives (user chose): work radius 1 mi (too quiet in the first run) or
2 mi; incidents citywide or same radius as work; keeping citywide highway full closures;
keeping new near-home work out of the live board (it would wait for the 08:00 digest).
Ungeocoded entries dropped rather than shown as "unknown distance" — matches the road-closures
channel's rule.

## PennDOT condensed into one private #penndot channel with an index; center moved to .env
Decision: `#penndot-live` + `#penndot-planned` → one private, own_job channel `#penndot`, refreshed
every 30 min (:15/:45) and by `/penndot`. Sections: Live now / Ongoing / Upcoming, always present,
plus the same index + jump-link embed the other digests use (`_index_embed` / `_apply_navigation`,
honoring `/config` index settings). Both feeds are fetched each run; if either fails the whole
channel is left as-is. The daily job no longer touches PennDOT. The old two channels are removed
by the job (ownership guard + Manage Channels; dry run under safe_mode).
Separately: `CENTER_LOCATION` (a private location name + coordinates) moved from `config.py` to required
`.env` vars `CENTER_NAME` / `CENTER_LAT` / `CENTER_LON`, so the repo can be committed without it.
All user-facing distances say "from <CENTER_NAME>", including the public digests' distance buckets.
Reason: User wanted one PennDOT channel with a table of contents like the others, and no address
in git. One cadence is simpler than a daily section inside a 30-min channel; the planned feed costs
one extra request per run.
Alternatives: keep the planned section daily inside the live channel (two cadences, more code);
show a failed feed's section as "unavailable" while updating the other (more states); leave the
old channels for the user to delete by hand (chosen against: the bot created them, so it cleans
them up, under the usual deletion guards); default CENTER_* to a public landmark when missing
(rejected: would silently change every distance).
Supersedes the two-channel layout in "PennDOT RCRS: private live board + private planned digest".

## #penndot refresh every 15 minutes; no extra traffic source
Decision: `PENNDOT_CRON_MINUTES` "15,45" → "5,20,35,50" (every 15 min, 24/7). Still RCRS only.
Reason: User wanted fresher incidents. RCRS has no published rate limit we found; each run is
two requests (live + planned). Minutes stay off :00 so a run never lands on the 08:00 digest job.
Alternatives: "0,15,30,45" (collides with the digest's start minute); adding a commercial
incident feed for city streets (HERE: terms forbid sharing results; Azure Maps ~$9/mo at this
rate; TomTom evaluation-only; Waze for agencies only) — user declined adding any new source,
and the research notes (docs/traffic-incident-sources.md) were deleted.

## #penndot public, refreshed by /refresh; /penndot removed
Decision: `#penndot` drops `private: True`. On its first run after the change, the bot removes the
old @everyone "can't view" overwrite once (`_unhide_penndot_channel`): ownership guard already
passed, needs Manage Roles, done-flag `penndot_unhidden` in `last_run`. `/penndot` is gone. `/refresh`
runs the digest pass (which still skips `own_job` channels) and then `run_penndot_job`, even if
the digest failed, and adds the PennDOT result to its reply. The 15-min schedule is unchanged.
Reason: User wanted the channel visible to everyone and one refresh command for everything.
Alternatives: flip `private` and unhide by hand in Discord (no bot permission edits); keep it
private and only link it from /refresh. Unhiding every run rather than once was rejected: it
would undo a deliberate manual re-hide every 15 min. The PennDOT last-run keys are still
written, though nothing shows them now that /penndot is gone.

## Major Events + PennDOT share one "🤖 ❗ Important" category; #penndot → #traffic-alerts
Decision: `major_events` and `penndot` form the "important" group, which always gets its own
category, `IMPORTANT_CATEGORY_NAME = "🤖 ❗ Important"` (replaces `MAJOR_CATEGORY_NAME`). It
leads the bot block at the bottom of the server, as Major Events did. The PennDOT channel is
now `#traffic-alerts`, titled "🚧 Traffic Alerts"; its key stays `penndot` (DB rows, last-run
keys, code names) so no data migration is needed. Config only: the live server is moved by hand.
Reason: User wanted the two "check this first" channels grouped together under one name.
Alternatives: name it exactly "! Important" (drops the 🤖 bot-managed prefix; the "!" doesn't
sort anything, since Discord orders by position); put it at the top of the server (breaks the
bot-stays-at-the-bottom rule); auto-migrate in the PennDOT job (rename the old category in
place, move and rename the channel; keeps mutes and IDs, ~25 more lines). Renaming the key
was rejected: it would orphan the stored channel row and digest message id.

## Removed the Philadelphia Inquirer source (2026-11-10)
Decision: Deleted "Philadelphia Inquirer Events" from `EVENT_SOURCES`, its `_scrape_inquirer`
handler and `_SCRAPERS` key, and its diagnostic (`_diag_inquirer`, `_make_inquirer_patch`) in
`tools/scraper_agent.py`.
Reason: It contributed nothing. Each run fetched ~41 items, but zero reached the events table:
the HTML path dated items from each card's `time[datetime]`, which on a news listing is the
article publish date, so they were dropped as past events (and they were article headlines,
not events). It was also the source with the most ToS risk: a paywalled commercial news site,
fetched with a rotated browser User-Agent.
Alternative not taken: marking it `skip_js` (keeps dead code, and that flag means "needs JS").

## Digest channels merged 14 → 9; names capped at 14 characters (2026-11-12)
Decision: Music goes from 7 channels to 4: `#rock-indie`, `#hiphop-pop` (hip-hop/R&B + pop),
`#electronic`, `#more-music` (jazz/classical, country/folk, other). Events: food + festivals
merge into `#food-fests`, and sports folds into `#other-events`. Arts, nightlife, major,
closures and traffic-alerts are unchanged. `load` specs now take a tuple of genres or
categories, and `#other-events` takes any category that no `category` load covers (derived
from `CHANNEL_DEFINITIONS`, not a hardcoded set). Surviving channels keep their old keys
(`music_hiphop_rnb` → `#hiphop-pop`, `music_other` → `#more-music`, `food_drink` →
`#food-fests`, `music_electronic_dance` → `#electronic`), so channel ids, mutes and digest
message ids carry over; `/refresh` renames them. Dropped keys go in `RETIRED_CHANNEL_KEYS`,
which generalizes the old PennDOT legacy-channel cleanup; the 15-min job deletes them.
Classification is untouched: Haiku still tags all genres and categories.
Reason: The live data (2026-11-12) had 6 channels with 6 or fewer distinct shows; genre
boundaries were fuzzy (the same show was tagged pop on one listing and rock on the other).
Names had to stay short enough that the sidebar doesn't truncate them, so the full scope
lives in the embed title.
Alternatives: new keys for merged channels (cleaner names, but deletes and recreates every
merged channel, losing mutes); one all-music channel (~155 shows, too long); a three-way
`#food-fests-sports` (21 characters, truncates); renaming in the daily job too (not needed
for a one-time change).

## Philly-time fix; plain-text dates and ranges instead of Discord timestamps (2026-11-12)
Decision: Naive times from sources are Philadelphia wall-clock time (`scraper.as_utc`), not
UTC. This applies to Discovery (its prompt now asks for local time, or a bare date), Center
City District, phila.gov iCal without "Z", and Ticketmaster's date-only fallback. A bare date
is stored as local midnight and means "date only": shown without a time, kept active and
unpruned through that day (pruning now cuts at local midnight). A one-off startup repair
(`store._repair_local_times`, guarded by `last_run` key `tz_repair_local_times`)
reinterprets stored Discovery + CCD rows and recomputes their ids. Digests show dates as
plain text in Philly time, as ranges when an event spans days ("Fri, Nov 13 – Sat, Nov 14",
"Through Sun, Jul 5, 2027"), and the PennDOT channel does too. Week/day grouping and the
dedup day key use Philly time.
Reason: Discord showed these sources 5 hours early (Victoria Monét at 3 PM, a date-only
two-night show as "Thu 7:00 PM – 7:00 PM"). The container runs in UTC, so week sections
split at Sunday 7 PM and the dedup day key missed same-night cross-source copies. The user
chose plain text over `<t:…>` tokens: shorter, readable as ranges, and Discord doesn't render
tokens in embed titles (the day-grouping labels were broken).
Alternatives: keep `<t:…:D>` pills for ranges (long, always shows the year); an `all_day`
column instead of the midnight convention (a real midnight start now shows without a time);
plain text in the embed footer too (kept Discord's native "Today at …").
Old City District: checked against the live page on 2026-11-12. Its `<time datetime>` is
unreliable: 75 of 80 were the shown Philly wall-clock time mislabeled "Z", and 5 were real
UTC. The scraper now parses the visible text ("11/14/2026 - 1:00pm") as Philly time and
only falls back to the attribute's wall clock. Its stored rows are in the repair too.
The upsert now also updates `date` on an id conflict, so a re-scraped event takes the
source's current time (this also heals the few real-UTC Old City rows the repair shifts).

## Series shown once per week with their full range; Old City end dates (2026-11-12)
Decision: At render time (`digest._event_lines`), a run (same normalized title + venue on
several dates) becomes one line, "Sat, Nov 14 – Sun, Dec 6 · 12:00 PM · 11 dates", or
"Thursdays, Nov 12 – Dec 17 · 9:00 PM" when every date is the same weekday. The time
shows only when all dates share it. A multi-day event shows its actual start, "Sat, Jul 18 –
Mon, Jul 5, 2027" (no more "Through …"). With week grouping, each series appears in every
week it covers: a run in each week it has a date, a multi-day event in each week from now
to its end, but never past the channel's last week of other events. Day/distance/flat
grouping shows a series once. The index counts each series once. Nothing changes in the DB.
Old City is now parsed card by card. The old cross-card regex dropped the first card and
invented an "Upcoming Events" item. It also reads the card's end <time>, so its running
series (exhibitions, markets) stay active and show as ranges: 55 cards parse, up from 28
stored rows.
Reason: The user wants a multi-week series visible in each week, with its real start.
Answered forks: long series repeat every week rather than in one "Ongoing" section or a
4-week cap; each week's line shows the full run range rather than that week's dates.
Cost: #arts-culture still has ~125 lines over 9 weeks (6 Discovery exhibitions repeat in
every week), so collapsing alone doesn't shorten it much. Packing week sections into
shared messages is what would.

## Repo prepared for public GitHub (2026-11-18)
Decision: The repo is public, with an MIT license. DECISIONS.md and CLAUDE.md are published,
with the home-location hints reworded. `.gitignore` now also covers `.env.*`, key files, `*.local.*`,
`.private/`, `CLAUDE.local.md` and OS/editor junk. `.gitattributes` normalizes line endings to LF.
A local pre-commit guard blocks sensitive file names, high-confidence secrets and private terms.
Reason: The design reasoning is the most useful part to publish. The center point stays in `.env` only.
Alternatives: keep DECISIONS.md private (loses the reasoning); split it into per-decision ADR files
(churn, with no gain for a single-maintainer project).
