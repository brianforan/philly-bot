# CLAUDE.md — philly-bot

Discord bot that surfaces Philadelphia **events, live music, and road/transit closures**
as auto-updating weekly digests, sorted into themed channels. It polls public sources,
geo-filters to Philly, de-duplicates, classifies each event with Claude, and posts/edits
one digest per channel on a daily schedule. PennDOT road work runs **outside** that
pipeline, in one `#traffic-alerts` channel (see "PennDOT road work" below).

> Read this before changing code. It captures architecture, conventions, and the
> non-obvious wiring that isn't visible from any single file.

---

## Stack & runtime

- **Python 3.11**, `asyncio` throughout. Entry point: `bot.py` → `main()` → `bot.run(...)`.
- Key deps (`requirements.txt`): `discord.py`, `anthropic`, `aiohttp`, `beautifulsoup4`+`lxml`,
  `apscheduler`, `aiosqlite`, `geopy`, `python-dotenv`, `Brotli`.
- **LLM:** Claude **Haiku 4.5** (`claude-haiku-4-5-20251001`) for classification and the
  relevance filter (bulk/structured → keep on Haiku). Claude **Sonnet** (`config.DISCOVERY_MODEL`)
  for the `discovery.py` web-search agent (reasoning/extraction needs the reliability). Prompt
  caching (`cache_control: ephemeral`) is used on the static system prompts — preserve it.
- **Persistence:** single SQLite file via `aiosqlite`. Path from `DB_PATH` env (default
  `philly_bot.db`; `/data/philly_bot.db` in Docker).
- **Deploy:** Docker (`python:3.11-slim`), DB on a `/data` volume, `docker-compose.yml`,
  run under systemd (`philly-bot.service`, oneshot wrapping `docker compose up -d --build`).

## Secrets & repo state — READ FIRST

- **This is a git repository intended to be public.** `.env` contains **live** secrets
  (Discord, Ticketmaster, Google Maps, Anthropic, PennDOT RCRS). **Never** print, echo, log, or commit
  secret values; don't commit or push unless explicitly asked. `.gitignore` excludes `.env`,
  `*.db`, local settings, caches, and venvs. A local pre-commit guard (`.git/hooks/pre-commit`)
  blocks secrets and private terms; don't bypass it with `--no-verify`.
- Secrets are read via `os.environ.get(...)`: `DISCORD_BOT_TOKEN` (required, read at
  `config` import time), `ANTHROPIC_API_KEY`, `TICKETMASTER_API_KEY`, `GOOGLE_MAPS_API_KEY`,
  `PENNDOT_RCRS_USERNAME` / `PENNDOT_RCRS_PASSWORD`. Keep `.env.example` in sync (placeholders only) when adding a new var.
- **The center point is private config, not code:** `CENTER_NAME` / `CENTER_LAT` / `CENTER_LON`
  (required; read into `config.CENTER_LOCATION` at import). It's a home location — never
  hardcode it, put it in docs, or use it in tests; user-facing text says "from <CENTER_NAME>".
- **Channel IDs are NOT environment config.** They live in the SQLite `channels` table,
  created at runtime by `/setup` or dynamic channel creation. Do not reintroduce channel
  env vars.

## Module map

| File | Role |
|------|------|
| `bot.py` | Discord client, slash-command definitions + registration, `on_ready` per-guild sync. |
| `config.py` | **Central leaf module.** Constants, `CHANNEL_DEFINITIONS`, `SETTINGS_SPEC`, source lists, `MUSIC_GENRES`. Imports nothing internal; everything imports it. |
| `scheduler.py` | APScheduler jobs: the daily `run_digest_job` pipeline and the 15-min `run_penndot_job`; dynamic channel create/delete (all creation goes through `_create_digest_channel`); `rerender_digests_from_store`. |
| `scraper.py` | Per-source fetch/scrape handlers → `EventItem`. `_SCRAPERS` dispatch table, `fetch_source`, `poll_all_sources` (filters + dedup + validate). |
| `classify_events.py` | Claude classification (`CATEGORIES`, `GENRES`), `classify_unclassified`, report/embeds; also a standalone CLI. The model sees title+venue+date; `_refine_major` then force-promotes unambiguous city-wide signals (venue/keyword) but never demotes — borderline cases are left for the web pass (2.5) to demote. |
| `validator.py` | Haiku relevance filter (`validate_items`). **Fails open** on missing key/API error. |
| `discovery.py` | Sonnet + web-search. `discover_events` finds major/notable events the scrapers miss (registered as the "Major Event Discovery" source; **self-throttles** to `DISCOVERY_EVERY_DAYS`). `review_major_events` is the batched web-verification pass that re-checks the `is_major` list each run and demotes non-citywide ones. Both **fail soft** and reuse `ANTHROPIC_API_KEY`. |
| `ask.py` | Backend for the `/ask` command: ad-hoc Philly Q&A via Sonnet + web-search (`answer_question(question, history)` → `AskResult`). Reuses `_web_search_tool`/`_message_text` from `discovery.py` and `ANTHROPIC_API_KEY`. **Fails soft** (returns `ok=False` text, never raises). Follow-ups: `bot.py` keeps an in-memory per-user context store keyed by a short `continue_id` (text-only turns, TTL + turn-capped) and replays it via `history`. Cost guards live in `bot.py` (per-user cooldown + `store.claim_daily_quota` daily ceiling). |
| `digest.py` | Formats `EventItem`s → Discord embeds; `dedupe_events`; `_event_lines` merges series (a run of dates at one venue, or a multi-day event) into one line shown in every week it covers; `DigestOptions`; `post_or_edit_digest`; `post_penndot_channel` / `build_penndot_digest` for `#traffic-alerts` (sections + the shared index/jump-link navigation). |
| `penndot.py` | PennDOT RCRS client: fetch (HTTP Basic), Philadelphia filter (County 67), reopened-event drop, road-name tidying, direction-pair merge → `RcrsEntry`; the near-home/age split; `load_channel_sections` → `PenndotSections` (live / ongoing / upcoming). **Raises `RcrsError`** on failure (unlike scrapers) so the caller keeps the last output. Never touches the DB. |
| `store.py` | All SQLite access (events, channels, settings, digest messages, run metadata). |
| `geocoder.py` | Nominatim geocoding (1 req/s) + haversine + `distance_bucket`. Google geocoder lives in `scraper._geocode_google`. |
| `tools/scraper_agent.py` | Standalone diagnostic/dev tool, **not imported by the runtime**. Run from repo root. |
| `tools/penndot_rcrs.py` | Terminal preview of the `#traffic-alerts` sections using `penndot.py` (`--feed`, `--json`). Not imported by the runtime. |

## Data flow — `scheduler.run_digest_job` (the heart of the bot)

Runs once daily at `config.DIGEST_EDIT_HOUR` (08:00 ET). **Monday = fresh post; Tue–Sun =
edit the same messages in place** (digest message IDs are stored per `digest_key`).

1. **Poll & store** — for each *source type* (`events`, `music`, `closures`):
   `poll_all_sources(type)` → `fetch_source` dispatches by source **name** via `_SCRAPERS`
   → filter pipeline: `event_is_active` → `_apply_lookahead` → `_apply_geo_filter` →
   `dedupe_events` (the **shared** dedup from `digest.py`) → `validate_items` (Haiku) →
   `upsert_events`; `delete_past_events` prunes.
2. **Classify** — `classify_unclassified` tags each new event with `category`, `is_major`,
   and (music only) `genre` via Haiku, written back to the DB. `_refine_major` then
   force-promotes unambiguous city-wide signals (venue/keyword) but never demotes.
2.5. **Verify majors** — `discovery.review_major_events` runs a batched Sonnet+web-search
   pass over everything flagged `is_major` (incl. borderline cases left by `_refine_major`)
   and demotes niche/non-citywide events. This is the only step that demotes.
3. **Render** — for each entry in `CHANNEL_DEFINITIONS`, load rows via
   `scheduler._load_for_definition`, dynamically create the channel if its bucket has
   `>= create_threshold` events, post/edit the digest, and optionally auto-delete channels
   that stay empty. Auto-delete only ever fires on channels that pass the ownership guard
   (`is_bot_owned_channel`: bot-created, in the managed guild); disabled channels are skipped.

*Source types* (`events`/`music`/`closures`) are internal — they carry geo/lookahead rules.
They are distinct from *digest channels* (the `CHANNEL_DEFINITIONS` buckets).

One "source" is special: **Major Event Discovery** (`discovery.py`) is an LLM + web-search
agent, not a scraper. It runs inside `poll_all_sources("events")` like any source but
self-throttles to `DISCOVERY_EVERY_DAYS` (cost control) and emits the same `EventItem`s,
so the rest of the pipeline treats it identically. Ticketmaster fetches `music`, `sports`,
and `arts & theatre` classifications; final routing is always by classified `category`/`is_major`,
not by a source's `channel`.

## PennDOT road work — outside the pipeline

PennDOT RCRS data (`penndot.py`) is **not a source** and is **never written to the events
table**. It's fetched at render time for one bot-created, public channel, `#traffic-alerts` (key
`penndot`, `own_job: True`; it was named `#penndot` until it moved into the 🤖 ❗ Important
category next to `#major-events`). Code and DB keys still say "penndot".

- `run_penndot_job` refreshes it every 15 min (`PENNDOT_CRON_MINUTES`, :05/:20/:35/:50, 24/7,
  `max_instances=1`) and as part of `/refresh` (after the digests, even if they failed); an
  `asyncio.Lock` serializes the two. There's no PennDOT-only command.
  It fetches both feeds (`load_channel_sections`) and edits the channel in place:
  **Live now** (incidents within `PENNDOT_INCIDENT_MAX_MILES` + near-home work started within
  `PENNDOT_LIVE_NEW_HOURS`), **Ongoing** (older near-home work), **Upcoming** (near-home
  planned events within `PENNDOT_PLANNED_LOOKAHEAD_DAYS`), plus the same index/jump-link
  embed the other digests use (`_index_embed` / `_apply_navigation`, honoring `/config`).
- Every section is always present, so refreshes edit in place. Never auto-deleted, never
  fresh-posted. The daily job and `/redraw` skip `own_job` channels; `/refresh`'s digest pass
  skips them too, then calls `run_penndot_job` itself.
  Each run records `penndot_last_attempt/_result/_success` in `last_run`.
- It replaced `#penndot-live` / `#penndot-planned`. Each run removes every channel still
  recorded under a `config.RETIRED_CHANNEL_KEYS` key, those two and the merged-away digest
  channels alike (`_remove_retired_channels`: ownership guard + Manage Channels; a dry run
  while `safe_mode` is on).
- It used to be private. `_unhide_penndot_channel` removes the old @everyone view deny once
  (needs Manage Roles; recorded as `penndot_unhidden` in `last_run`, so a manual re-hide sticks).
- `/teardown` deletes `#traffic-alerts` like any bot channel, but the job recreates it within 15 min
  because the managed guild setting survives teardown.

"Near home" = within `PENNDOT_WORK_MAX_MILES` (3 mi) of `CENTER_LOCATION`; incidents use the
wider `PENNDOT_INCIDENT_MAX_MILES` (5 mi). Everything farther out, and anything without
coordinates, is dropped (`penndot.split_live_entries` / `upcoming_planned`) — GPS already
routes around distant planned work. Times are absolute plain text in Philly time, not live-counting.

On an RCRS failure the channel is left as-is (its "Updated" time shows staleness). No LLM calls. Why it's kept out of the DB: anything stored there can reach
public channels — classification can flag it `is_major` (→ Major Events) and the
`#other-events` loader takes any non-`closures` row with no category. **Keep it that way**
unless you also close those paths. Real-feed quirks (camelCase keys, months-old "reopened"
events) are documented at the top of `penndot.py`. Settled design: `DECISIONS.md`.

## Two source-of-truth rules (do not duplicate)

- **Music genres:** `config.MUSIC_GENRES` is canonical. `classify_events.GENRES` is
  `config.MUSIC_GENRES`; `store.py` derives its known-genre set from it. Add/rename a genre
  in **one** place. (Event `CATEGORIES` live in `classify_events.py`.)
- **Cross-source dedup:** `digest.dedupe_events` is the single implementation;
  `scraper.poll_all_sources` calls it (imported locally — see circular-import note).

## Circular imports — important pattern

`digest.py` imports from `scraper` **at module level** (`EventItem`, `event_is_active`).
Therefore `scraper.py` and `scheduler.py` import `digest`/`store`/`validator`/`classify_events`
**locally inside functions**, never at module top, to avoid an import cycle. Follow this:
heavy/peer modules are imported inside the function that uses them. `config` is the only
module safe to import at the top everywhere (it's the leaf).

## How to extend

**Add a data source**
1. Add an entry to `EVENT_SOURCES` / `ROAD_CLOSURE_SOURCES` / `MUSIC_SOURCES` in `config.py`
   (`name`, `url`, `type`, `channel`). **Never list the same source in two lists** — they
   concatenate into `ALL_SOURCES`. Use `type: "skip_js"` to register-but-skip a source.
2. Write an `async def _scrape_x(source, session) -> list[EventItem]` (or `_fetch_x`).
3. Register it in `scraper._SCRAPERS` keyed by the **exact** source `name`.
4. Per-source failures must return `[]`, not raise (the pipeline silences them).

**Add a digest channel/bucket**
1. Add a row to `config.CHANNEL_DEFINITIONS` (`key`, `name`, `title`, `layout`, `load`,
   `default`). `group`/`category_name` are derived automatically (music genres → "Music in
   Philly"; `major_events` and `penndot` → the "important" group, `IMPORTANT_CATEGORY_NAME`; everything
   else → the default category). Groups in `ALWAYS_OWN_CATEGORY` get a dedicated category
   regardless of size. Bot categories are 🤖-prefixed and kept as a block at the **bottom**
   of the server with Important leading it — `scheduler._order_managed_categories`
   enforces that order each run (`_ensure_category` only creates; it doesn't position).
   Keep `name` to 14 characters or fewer (the Discord sidebar truncates longer names); put
   the full scope in `title`. `("genre", (...))` / `("category", (...))` loads take a tuple,
   so one channel can cover several genres/categories; any category no `category` load
   covers falls into `#other-events` (`store.load_other_events`).
2. If you introduce a new `load` loader type, handle it in `scheduler._load_for_definition`
   and add the matching `load_*` query in `store.py`.
   **Merging or removing a channel:** keep the surviving channel's `key` (it's the DB key
   for the channel row and digest messages) and change only `name`/`title`/`load`;
   `/refresh` renames it in place (the daily job doesn't rename). Add each dropped key to
   `config.RETIRED_CHANNEL_KEYS` so the 15-min job deletes its channel.
3. Optional fields: `private: True` creates the channel hidden from @everyone; `own_job: True`
   takes it out of the daily job, `/refresh`'s digest pass and `/redraw` (it needs its own
   scheduler job; `/refresh` calls it separately).
   Always create channels through `scheduler._create_digest_channel` so `private` is honored.

**Add a runtime setting**
Add a `SETTING_*` constant and a `SETTINGS_SPEC` entry in `config.py` — it's then
auto-validated (`validate_setting`) and selectable in `/config` with no other changes.
Display settings (`digest_*`) are consumed via `DigestOptions.from_settings`.

**Add a slash command**
Define a module-level `@app_commands.command(...)` function in `bot.py`, gate it with
`@app_commands.checks.has_permissions(...)` if it mutates, register it in
`PhillyBot.setup_hook` via `self.tree.add_command(...)`. The `setup_hook` loop then sets
`default_permissions = administrator` on **every** registered command, so new commands are
admin-only automatically — no extra step. Commands sync **per-guild** for instant
availability. Info/admin replies are **ephemeral** by convention.

## Commands (current)

**All commands are Administrator-only.** `setup_hook` sets every command's
`default_permissions` to `administrator`, so Discord hides them from non-admins and
rejects non-admin invocations (a server admin can still re-grant via Integrations; the
per-command `has_permissions` checks remain as a secondary gate). Surface:
`/about`, `/status`, `/channels`, `/refresh [clear]`, `/classify [min_count]`,
`/ask <question>`, `/setup`, `/teardown`, `/redraw`, `/config [setting] [value]`.
`/refresh` also runs `run_penndot_job` and adds its result to the reply.

**Ownership guard (security-critical):** every destructive op (delete channel,
delete category, clear messages, move channel) MUST pass `scheduler.is_bot_owned_channel`,
which confirms the target is recorded in the `channels` table as `auto_created=1` in
the managed guild. There is no name-based channel/category deletion, no `purge`, and
no command to bind/delete an arbitrary user channel — so the bot can only ever touch
channels it created itself. Categories the bot creates are tracked in the
`managed_categories` table (`store.add_managed_category`) and are the only categories
teardown will delete (and only when empty).

**Private channels:** `_create_digest_channel` gives `private` definitions explicit overwrites
(@everyone can't view; the bot can view/send/embed/read history). No definition is private
right now. The bot never sets or syncs permissions on an existing channel (category moves use
discord.py's default `sync_permissions=False`), with one exception: the one-off
`_unhide_penndot_channel`. Making any other private channel public is a manual step in Discord
(channel settings → Permissions → sync with category) plus flipping `private` to False.

## Conventions

- **Times:** store aware UTC. A naive time from a source is Philadelphia wall-clock time:
  convert it with `scraper.as_utc`, never `.replace(tzinfo=timezone.utc)`. A date with no
  time is stored as local midnight (`scraper.is_date_only`), shown without a time, and kept
  through its whole day. Never use bare `.astimezone()`: the container runs in UTC; use
  `scraper.LOCAL_TZ`. Output is plain text in Philly time (`digest._fmt_when`), not
  Discord `<t:…>` tokens; only the embed footer uses Discord's native timestamp.
- Type hints on public functions; `logging` (module logger), never `print`, in non-CLI code.
- Handle failure explicitly. Scrapers and LLM filters **fail open / fail soft** (return `[]`
  or pass items through) so one bad source never breaks a run.
- snake_case, 4-space indent, double-quoted strings, f-strings for messages.
- Discord embed limits when building output: field value ≤ 1024, description ≤ 4096,
  total ≤ 6000 chars. Long content is sent across multiple messages.
- Per the global instructions: build only what's asked; log non-obvious or ambiguous
  decisions to `DECISIONS.md` (repo root) rather than pausing.

## Dev / run

- A `venv/` exists. Quick checks: `python3 -m py_compile <files>`; import-level smoke tests
  with `./venv/bin/python -c "..."` (importing `config` requires `.env` with
  `DISCORD_BOT_TOKEN`). **There is no test suite.**
- Run the bot locally: `./venv/bin/python bot.py` (needs a populated `.env`).
- Run the diagnostic tool: `./venv/bin/python tools/scraper_agent.py` from the repo root.
- Preview the `#traffic-alerts` channel: `./venv/bin/python tools/penndot_rcrs.py` (needs the RCRS
  credentials in `.env`; calls the live PennDOT API).
- Do not install/upgrade dependencies or run `git` unless explicitly asked.
