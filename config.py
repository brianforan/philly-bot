import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_BOT_TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]

# Channel configuration is no longer read from env. The canonical set of digest
# channels is defined below as CHANNEL_DEFINITIONS; actual Discord channel IDs are
# created/stored at runtime in the SQLite `channels` table via the /setup command.
#
# Each definition:
#   key     — stable digest key (DB key + classifier routing)
#   name    — Discord channel name to create (slug, no emoji)
#   title   — embed title shown in the digest
#   layout  — "music" (by-day) | "events" (distance buckets) | "closures"
#             | "penndot" (PennDOT channel; rendered by scheduler.run_penndot_job)
#   load    — (loader_type, arg) telling the scheduler which events to pull:
#               ("genre", <genre>)      music events of that genre
#               ("category", <cat>)     non-major events of that category
#               ("other", None)         catch-all non-major events
#               ("closures", None)      road closure / transit items
#               ("major", None)         events flagged is_major
#               ("penndot", None)       not a DB loader: the PennDOT channel is own_job and
#                                       fetches from RCRS itself (never from the DB)
#   default — created by /setup when no explicit subset is requested
#   private — optional; True creates the channel hidden from @everyone (only the bot
#             and server admins can see it). Never changes an existing channel's
#             permissions — make it public by hand in Discord.
#   own_job — optional; True means the channel is created and rendered only by its own
#             scheduler job (the PennDOT channel). The daily digest, /refresh and
#             /redraw skip it entirely.
#
# Two fields are derived below (not set per-row):
#   group         — logical grouping (music genres, "important", everything else)
#   category_name — the Discord category the channel lives under. A group with
#                   MORE THAN 3 channels gets its own category named after the
#                   group; smaller groups fall into the default category.
# Bot-managed Discord categories are prefixed with 🤖 so it's clear at a glance that
# they are bot-managed. They are positioned at the BOTTOM of the server (see
# scheduler._ensure_category): users opt in to scrolling down to them rather than
# having them dominate the top of the channel list.
SETUP_CATEGORY_NAME = "🤖 Happening in Philly"
# The Important category (#major-events + #traffic-alerts) leads the bot-managed
# block — placed just above the other bot categories — but the whole managed block
# still sits at the bottom of the server.
IMPORTANT_CATEGORY_NAME = "🤖 ❗ Important"

# Discord category name for each group when it earns its own (>3 channels, or always).
GROUP_CATEGORY_NAMES: dict[str, str] = {
    "music":   "🤖 Music in Philly",
    "general": SETUP_CATEGORY_NAME,
    "important": IMPORTANT_CATEGORY_NAME,
}
# Groups larger than this get their own Discord category.
GROUP_OWN_CATEGORY_THRESHOLD = 3
# Groups that always get their own category regardless of size.
ALWAYS_OWN_CATEGORY: set[str] = {"important"}

# Channel names stay at 14 characters or fewer so the Discord sidebar doesn't truncate
# them; the full scope goes in "title". A "genre"/"category" load covers a tuple of
# MUSIC_GENRES / classify_events.CATEGORIES. Genre "other" also matches NULL/unknown
# genres; categories no "category" load covers fall into #other-events.
# Keys are DB keys (channel row + digest message ids), so a merged channel keeps the key
# of the channel it grew out of even when its name changes (as "penndot" did).
CHANNEL_DEFINITIONS: list[dict] = [
    # ── Music genre channels ──
    {"key": "music_rock_indie",       "name": "rock-indie",   "title": "🎸 Rock & Indie",       "layout": "music",  "load": ("genre", ("rock_indie",)),                              "default": True},
    {"key": "music_hiphop_rnb",       "name": "hiphop-pop",   "title": "🎧 Hip-Hop, R&B & Pop", "layout": "music",  "load": ("genre", ("hiphop_rnb", "pop")),                        "default": True},
    {"key": "music_electronic_dance", "name": "electronic",   "title": "🔊 Electronic & Dance", "layout": "music",  "load": ("genre", ("electronic_dance",)),                        "default": True},
    {"key": "music_other",            "name": "more-music",   "title": "🎵 Jazz, Classical, Country & More", "layout": "music", "load": ("genre", ("jazz_classical", "country_folk", "other")), "default": True},
    # ── Category channels ──
    {"key": "arts_culture",    "name": "arts-culture",  "title": "Arts & Culture",     "layout": "events", "load": ("category", ("arts_culture",)),                 "default": True},
    {"key": "nightlife",       "name": "nightlife",     "title": "Nightlife",          "layout": "events", "load": ("category", ("nightlife",)),                    "default": True},
    {"key": "food_drink",      "name": "food-fests",    "title": "Food & Festivals",   "layout": "events", "load": ("category", ("food_drink", "festivals_fairs")), "default": True},
    {"key": "other",           "name": "other-events",  "title": "Other Events",       "layout": "events", "load": ("other", None),                                 "default": True},
    # ── Special channels ──
    {"key": "closures",        "name": "road-closures",   "title": "Road Closures & Transit Alerts", "layout": "closures", "load": ("closures", None), "default": True},
    {"key": "major_events",    "name": "major-events",    "title": "⭐ Major Events",     "layout": "events", "load": ("major", None),                 "default": True},
    # ── PennDOT (#traffic-alerts; public; fetched live from RCRS, never stored; own 15-min
    #    job + /refresh). The key stays "penndot": it's the DB key for the channel row. ──
    {"key": "penndot",         "name": "traffic-alerts",  "title": "🚧 Traffic Alerts", "layout": "penndot", "load": ("penndot", None), "default": False, "own_job": True},
]

# ── Derive group + category_name for each definition ──────────────────────────
def _group_of(key: str) -> str:
    if key in ("major_events", "penndot"):
        return "important"
    return "music" if key.startswith("music_") else "general"

_group_sizes: dict[str, int] = {}
for _d in CHANNEL_DEFINITIONS:
    g = _group_of(_d["key"])
    _group_sizes[g] = _group_sizes.get(g, 0) + 1

for _d in CHANNEL_DEFINITIONS:
    g = _group_of(_d["key"])
    _d["group"] = g
    # A group earns its own category if it's in ALWAYS_OWN_CATEGORY (e.g. important)
    # or has more than the threshold; otherwise its channels go in the default category.
    if g in ALWAYS_OWN_CATEGORY or _group_sizes[g] > GROUP_OWN_CATEGORY_THRESHOLD:
        _d["category_name"] = GROUP_CATEGORY_NAMES.get(g, SETUP_CATEGORY_NAME)
    else:
        _d["category_name"] = SETUP_CATEGORY_NAME

CHANNEL_DEF_BY_KEY: dict[str, dict] = {d["key"]: d for d in CHANNEL_DEFINITIONS}

# Channel keys that no longer have a definition → the channel name that replaced them.
# scheduler._remove_retired_channels deletes any still recorded (ownership guard +
# Manage Channels; a dry run while safe_mode is on) and forgets their rows.
RETIRED_CHANNEL_KEYS: dict[str, str] = {
    "penndot_live": "traffic-alerts",
    "penndot_planned": "traffic-alerts",
    "music_pop": "hiphop-pop",
    "music_jazz_classical": "more-music",
    "music_country_folk": "more-music",
    "festivals_fairs": "food-fests",
    "sports_fitness": "other-events",
}

# ── Runtime settings (stored in the DB `settings` table, tunable via /config) ──
# Setting keys
SETTING_DYNAMIC_CHANNELS = "dynamic_channels"
SETTING_SAFE_MODE = "safe_mode"
SETTING_AUTO_DELETE_EMPTY = "auto_delete_empty"
SETTING_CREATE_THRESHOLD = "create_threshold"
SETTING_EMPTY_RUNS_BEFORE_DELETE = "empty_runs_before_delete"
SETTING_DIGEST_GROUP_BY = "digest_group_by"
SETTING_DIGEST_SORT = "digest_sort"
SETTING_DIGEST_SHOW_INDEX = "digest_show_index"
SETTING_DIGEST_SHOW_DISTANCE = "digest_show_distance"
SETTING_DIGEST_SHOW_PRICE = "digest_show_price"
SETTING_DIGEST_SHOW_LOCATION = "digest_show_location"
SETTING_DIGEST_MAX_PER_CHANNEL = "digest_max_per_channel"
SETTING_DIGEST_JUMP_LINKS = "digest_jump_links"
SETTING_DIGEST_INDEX_POSITION = "digest_index_position"
SETTING_MANAGED_GUILD_ID = "managed_guild_id"  # internal, not user-tunable via /config

# Spec drives validation, defaults, and the /config option list. Add a setting
# here and it's automatically validated and selectable — no other code changes.
# type: "bool" (on/off) | "int" (with optional min) | "choice" (with choices)
SETTINGS_SPEC: dict[str, dict] = {
    # Channel lifecycle
    SETTING_SAFE_MODE:                {"type": "bool",   "default": "on",   "help": "Dry-run deletions: /teardown and auto-delete only LOG what they would delete; nothing is removed. Turn off to allow deletions. (/setup still cleans its own channels.)"},
    SETTING_DYNAMIC_CHANNELS:         {"type": "bool",   "default": "on",   "help": "Auto-create channels for buckets that have events"},
    SETTING_AUTO_DELETE_EMPTY:        {"type": "bool",   "default": "off",  "help": "Delete empty auto-created channels (no confirmation)"},
    SETTING_CREATE_THRESHOLD:         {"type": "int",    "default": "1",    "min": 1, "help": "Min events in a bucket before its channel is auto-created"},
    SETTING_EMPTY_RUNS_BEFORE_DELETE: {"type": "int",    "default": "3",    "min": 1, "help": "Consecutive empty runs before an empty channel is deleted"},
    # Digest display
    SETTING_DIGEST_GROUP_BY:          {"type": "choice", "default": "week", "choices": ["week", "day", "distance", "flat"], "help": "How to group events within a digest"},
    SETTING_DIGEST_SORT:              {"type": "choice", "default": "date", "choices": ["date", "distance"], "help": "Sort order within each group"},
    SETTING_DIGEST_SHOW_INDEX:        {"type": "bool",   "default": "on",   "help": "Show the index/table-of-contents embed"},
    SETTING_DIGEST_SHOW_DISTANCE:     {"type": "bool",   "default": "on",   "help": "Show distance tag on each event line"},
    SETTING_DIGEST_SHOW_PRICE:        {"type": "bool",   "default": "on",   "help": "Show price on each event line"},
    SETTING_DIGEST_SHOW_LOCATION:     {"type": "bool",   "default": "on",   "help": "Show venue/location on each event line"},
    SETTING_DIGEST_MAX_PER_CHANNEL:   {"type": "int",    "default": "0",    "min": 0, "help": "Max events per channel (0 = unlimited)"},
    SETTING_DIGEST_JUMP_LINKS:        {"type": "bool",   "default": "on",   "help": "Index links jump to each section; sections link back to the index (one message per section)"},
    SETTING_DIGEST_INDEX_POSITION:    {"type": "choice", "default": "bottom", "choices": ["top", "bottom"], "help": "Put the index at the top or bottom of the channel"},
}

DEFAULT_SETTINGS: dict[str, str] = {k: v["default"] for k, v in SETTINGS_SPEC.items()}


def validate_setting(key: str, value: str) -> tuple[bool, str]:
    """Validate a setting value against SETTINGS_SPEC. Returns (ok, normalized_or_error)."""
    spec = SETTINGS_SPEC.get(key)
    if spec is None:
        return False, f"unknown setting '{key}'"
    v = value.strip().lower()
    t = spec["type"]
    if t == "bool":
        if v not in ("on", "off"):
            return False, "must be `on` or `off`"
        return True, v
    if t == "int":
        if not v.isdigit():
            return False, "must be a non-negative integer"
        if int(v) < spec.get("min", 0):
            return False, f"must be >= {spec.get('min', 0)}"
        return True, str(int(v))
    if t == "choice":
        if v not in spec["choices"]:
            return False, "must be one of: " + ", ".join(spec["choices"])
        return True, v
    return False, "unsupported setting type"

# Valid music genre tokens — single source of truth. classify_events.GENRES and
# store's known-genre set both derive from this list.
MUSIC_GENRES = ["rock_indie", "pop", "hiphop_rnb", "electronic_dance", "country_folk", "jazz_classical", "other"]

# Scheduling (24-hour ET)
DIGEST_EDIT_HOUR = 8    # Tue–Sun: edit existing
TIMEZONE = "America/New_York"

# Geographic center for distance bucketing, near-home filters, and "N mi from <name>" labels.
# It's a private location, so it lives in .env (never in code): CENTER_NAME is the label
# users see (an address or a name like "home"), CENTER_LAT / CENTER_LON its coordinates.
def _center_from_env() -> dict:
    missing = [k for k in ("CENTER_NAME", "CENTER_LAT", "CENTER_LON") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} in .env (see .env.example)")
    try:
        return {
            "name": os.environ["CENTER_NAME"],
            "lat": float(os.environ["CENTER_LAT"]),
            "lon": float(os.environ["CENTER_LON"]),
        }
    except ValueError as exc:
        raise RuntimeError(f"CENTER_LAT / CENTER_LON must be decimal degrees: {exc}") from exc


CENTER_LOCATION = _center_from_env()
DISTANCE_BUCKETS = ["< 1 mi", "1–3 mi", "3–7 mi", "7+ mi"]
DISTANCE_THRESHOLDS_MI = [1.0, 3.0, 7.0]  # upper bounds for first three buckets

# Geocoding
NOMINATIM_USER_AGENT = "philly-discord-bot/1.0"
NOMINATIM_RATE_LIMIT_SEC = 1.0

# SQLite database file
DB_PATH = os.environ.get("DB_PATH", "philly_bot.db")

# Lookahead windows (days)
# Events widened from 14 → 30 so major events announced weeks ahead (festivals,
# tournaments, citywide programming) aren't clipped before they're surfaced.
EVENTS_LOOKAHEAD_DAYS = 30
CLOSURES_LOOKAHEAD_DAYS = 7

# ── PennDOT RCRS (penndot.py) ──────────────────────────────────────────────────
# Fetched at render time for the #traffic-alerts channel; never stored in the events table.
# Credentials: PENNDOT_RCRS_USERNAME / PENNDOT_RCRS_PASSWORD (HTTP Basic auth).
PENNDOT_LIVE_NEW_HOURS = 24          # near-home work that started within this many hours is "Live now"
# Near-home filters, measured from CENTER_LOCATION. Road work farther out is dropped (GPS
# routes around it); incidents get a wider radius because GPS is slow to react to them.
# Entries without coordinates are dropped (distance unknown).
PENNDOT_WORK_MAX_MILES = 3.0         # road work (live + planned) shown within this distance
PENNDOT_INCIDENT_MAX_MILES = 5.0     # live incidents (crashes, debris, police, …) within this distance
PENNDOT_PLANNED_LOOKAHEAD_DAYS = 7   # planned-feed events starting within this window are "Upcoming"
# #traffic-alerts refresh: cron minutes (every 15 min, offset from the 08:00 digest job).
PENNDOT_CRON_MINUTES = "5,20,35,50"
MUSIC_LOOKAHEAD_DAYS = 60

# ── Major-event discovery agent (discovery.py) ─────────────────────────────────
# A scheduled Claude + web-search pass that finds notable/major Philadelphia events
# the structured scrapers miss (free museum programming, fan fests, civic happenings,
# tournaments). It runs as a normal source but self-throttles to keep cost to a few
# cents/month. Reuses ANTHROPIC_API_KEY — no new secret. Sonnet for reliable
# search/extraction; downstream validate + classify + dedup still apply.
DISCOVERY_MODEL = "claude-sonnet-4-6"
DISCOVERY_EVERY_DAYS = 7          # run at most once a week (cadence gate)
DISCOVERY_MAX_SEARCHES = 5        # cap web_search tool uses per run (cost ceiling)
DISCOVERY_MAX_EVENTS = 30         # cap candidates returned per run

# Major-event web verification (discovery.review_major_events): a batched Sonnet +
# web-search pass that re-checks everything flagged is_major each digest run and
# demotes niche/prestige-but-not-citywide events. Reuses DISCOVERY_MODEL.
MAJOR_REVIEW_MAX_SEARCHES = 8     # cap web_search uses for the batched verification

# /ask command — ad-hoc Philadelphia Q&A via Claude + web search (see ask.py).
# Sonnet for reliable multi-step search reasoning; reuses ANTHROPIC_API_KEY.
# Cost is bounded three ways: searches/answer, answer length, an in-memory
# per-user cooldown, and a hard per-day call ceiling enforced in bot.py.
ASK_MODEL = DISCOVERY_MODEL          # reuse the discovery model (Sonnet 4.6)
ASK_MAX_SEARCHES = 3                 # cap web_search tool uses per question
ASK_MAX_TOKENS = 1024                # cap answer length
ASK_USER_COOLDOWN_SECONDS = 30       # min seconds between questions per user
ASK_DAILY_LIMIT = 50                 # hard ceiling on /ask calls per day (budget guard)
ASK_CONTEXT_TTL_SECONDS = 1800       # how long a continue_id stays resumable (30 min)
ASK_MAX_HISTORY_TURNS = 6            # prior Q&A pairs replayed on a follow-up (caps token growth)


# Geographic filters
# Events: must geocode within Philadelphia city limits (or have no coordinates — source is Philly-specific)
# Closures: must be within CLOSURES_MAX_MILES of CENTER_LOCATION
# Music: must be within MUSIC_MAX_MILES of CENTER_LOCATION (Philadelphia metro area)
CLOSURES_MAX_MILES = 1.0
MUSIC_MAX_MILES = 25.0

EVENT_SOURCES = [
    {
        "name": "Visit Philadelphia",
        "url": "https://www.visitphilly.com/events/",
        "type": "scrape",
        "channel": "events",
    },
    {
        "name": "Eventbrite Philly",
        "url": "https://www.eventbrite.com/d/pa--philadelphia/events/",
        "type": "skip_js",  # requires JS rendering, blocks scrapers
        "channel": "events",
    },
    {
        "name": "Do215",
        "url": "https://do215.com/events/",
        "type": "scrape",
        "channel": "events",
    },
    {
        "name": "Ticketmaster Philly",
        "url": "https://app.ticketmaster.com/discovery/v2/events.json",
        "type": "scrape",
        "channel": "music",
    },
    {
        "name": "City of Philadelphia Calendars",
        "url": "https://api.phila.gov/phila/calendars/v1",
        "type": "scrape",
        "channel": "events",
    },
    {
        "name": "Center City District Events",
        "url": "https://centercityphila.org/explore/upcoming-events/",
        "type": "scrape",
        "channel": "events",
    },
    {
        "name": "Old City District Events",
        "url": "https://www.oldcitydistrict.org/things-do/upcoming-events",
        "type": "scrape",
        "channel": "events",
    },
    {
        # Autonomous discovery agent (Claude + web search). Self-throttles to
        # DISCOVERY_EVERY_DAYS; handler lives in discovery.py. Not a real URL.
        "name": "Major Event Discovery",
        "url": "https://www.google.com/search?q=major+events+in+philadelphia",
        "type": "scrape",
        "channel": "events",
    },
]

ROAD_CLOSURE_SOURCES = [
    {
        "name": "PHL Streets Dept",
        "url": "https://phl.carto.com/api/v2/sql",
        "type": "api",
        "channel": "closures",
    },
    {
        "name": "Office of Special Events Closures",
        "url": "https://www.phila.gov/departments/office-of-special-events/events/upcoming-special-event-road-closures/",
        "type": "scrape",
        "channel": "closures",
    },
]

MUSIC_SOURCES = [
    {
        "name": "Songkick Philly",
        "url": "https://www.songkick.com/metro-areas/28563-us-philadelphia",
        "type": "skip_js",  # JSON API blocked; iCal redirects to HTML; no static data surface
        "channel": "music",
    },
    {
        "name": "Bandsintown Philly",
        "url": "https://www.bandsintown.com/c/philadelphia-pa",
        "type": "skip_js",  # returns 403 to non-browser clients
        "channel": "music",
    },
    {
        "name": "The Key (WXPN)",
        "url": "https://thekey.xpn.org/category/show-previews/",
        "type": "scrape",
        "channel": "music",
    },
]

ALL_SOURCES = EVENT_SOURCES + ROAD_CLOSURE_SOURCES + MUSIC_SOURCES
