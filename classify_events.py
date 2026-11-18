"""
Classify stored events by type and suggest Discord channel splits.

Can be run as a standalone CLI script or called from the bot via /classify.

CLI usage:
    python classify_events.py [--db PATH] [--batch-size N] [--min-count N]
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Optional

import anthropic
from dotenv import load_dotenv

import config

load_dotenv()
logger = logging.getLogger(__name__)

CATEGORIES = [
    "arts_culture",       # museums, galleries, theater, dance, film
    "music",              # concerts, live music, DJ sets
    "food_drink",         # restaurant events, food festivals, tastings
    "sports_fitness",     # games, races, fitness classes, outdoor sports
    "outdoor_parks",      # park events, nature, hiking, free outdoor gatherings
    "family_kids",        # events specifically for children or families
    "community_civic",    # neighborhood meetings, volunteer events, city programs
    "festivals_fairs",    # multi-day fests, street fairs, block parties
    "nightlife",          # bar events, comedy nights, late-night
    "education_lectures", # talks, panels, workshops, classes
    "road_closure",       # street closures, transit alerts
    "other",              # doesn't fit above
]

# Music genres — config is the single source of truth.
GENRES = config.MUSIC_GENRES

_SYSTEM_PROMPT = """You are classifying Philadelphia events into categories.
Each input line is:  <id>: <title> | venue: <venue or -> | date: <date or ->
For each event, return a category, a major flag, and (for music only) a genre.

Categories (pick exactly one):
arts_culture, music, food_drink, sports_fitness, outdoor_parks, family_kids,
community_civic, festivals_fairs, nightlife, education_lectures, road_closure, other

MAJOR — be strict. major = true ONLY for events that are genuinely city-wide or
high-profile, the kind most people in Philadelphia would have heard of:
- FIFA World Cup matches and official fan events
- Marquee professional sports: Eagles, Phillies, 76ers, Flyers, Union playoff or
  signature games; the Broad Street Run; the Philadelphia Marathon
- Stadium/arena-scale concerts (Lincoln Financial Field, Citizens Bank Park,
  Wells Fargo Center, Xfinity Mobile Arena) and major headliner tours
- Large festivals drawing tens of thousands: Made in America, Wawa Welcome America /
  July 4th on the Parkway, the Mummers Parade, the Thanksgiving Day Parade
- America250 / Semiquincentennial (250th anniversary) events
- Major road/transit closures: interstates, expressways, bridges, or full street closures
- Comparable significant city-wide civic events

major = false for prestige-but-niche or venue-scale events, EVEN at famous institutions:
a regular Philadelphia Orchestra / Opera / ballet / theater performance, a museum
exhibition or talk, a club or small-venue show, a gallery opening, a typical neighborhood
event. Prestigious or big-institution does NOT mean major. Use the venue to judge scale:
arena / stadium / the Parkway → likely major; concert hall, theater, club, gallery, museum
→ NOT major unless it is clearly a city-wide event by the rules above.

genre (ONLY when category is music; otherwise null) — pick exactly one:
- rock_indie: rock, alternative, indie, metal, punk, emo
- pop: pop, top 40, singer-songwriter pop
- hiphop_rnb: hip-hop, rap, R&B, soul, funk
- electronic_dance: electronic, EDM, house, techno, DJ sets, dance
- country_folk: country, folk, americana, bluegrass, roots
- jazz_classical: jazz, blues, classical, orchestral, opera, chamber
- other: world, latin, reggae, gospel, or genre unclear

Rules:
- music = concerts and live performances; arts_culture = theater/dance/museum/gallery
- festivals_fairs = multi-day events or street fairs (not a single concert)
- road_closure = any street closure or transit alert
- When unsure, pick the closest match; use other only as last resort

Respond with a JSON object mapping each event ID to {category, major, genre}.
genre must be null when category is not music.
Example: {"abc123": {"category": "music", "major": true, "genre": "hiphop_rnb"}, "def456": {"category": "food_drink", "major": false, "genre": null}}"""


def load_events(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, source, channel_type, location_name, date "
            "FROM events ORDER BY channel_type, date ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Deterministic major-event rules (applied on top of the model's verdict) ─────
# These only ever FORCE-PROMOTE unambiguous city-wide signals (keywords/venues).
# Borderline niche-prestige cases the model flagged major are deliberately NOT
# demoted here — they are left for the web-verification pass (review_major_events)
# to confirm or demote with real-world context. Substrings match case-insensitively.

_MAJOR_VENUE_HINTS = (
    "lincoln financial field", "citizens bank park", "wells fargo center",
    "xfinity mobile arena", "subaru park", "eakins oval",
    "benjamin franklin parkway", "ben franklin parkway", "fdr park",
)
_MAJOR_TITLE_KEYWORDS = (
    "world cup", "fifa", "semiquincentennial", "250th", "america250", "america 250",
    "made in america", "welcome america", "mummers", "thanksgiving day parade",
    "broad street run", "philadelphia marathon", "playoff", "all-star", "all star",
    "papal", "nfl draft", "stanley cup", "world series",
)
_MAJOR_CLOSURE_KEYWORDS = (
    "i-95", "i95", "i-76", "i-676", "vine street expressway", "schuylkill expressway",
    "ben franklin bridge", "walt whitman bridge", "girard point",
    "full closure", "bridge closure", "highway closure",
)


def _refine_major(title: str, venue: Optional[str], category: str, model_major: bool) -> bool:
    """Refine the model's major flag. Only force-promotes; never demotes.

    Borderline niche-prestige events the model flagged major (e.g. an orchestra
    concert) are kept as major here and left for the web-verification pass
    (review_major_events) to confirm or demote with real-world context.
    """
    t = (title or "").lower()
    v = (venue or "").lower()
    # Force-promote unambiguous city-wide signals.
    if any(k in t for k in _MAJOR_TITLE_KEYWORDS):
        return True
    if v and any(h in v for h in _MAJOR_VENUE_HINTS):
        return True
    if category == "road_closure" and any(k in t for k in _MAJOR_CLOSURE_KEYWORDS):
        return True
    # Otherwise keep the model's verdict — the web pass is the only demoter.
    return model_major


async def _classify_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    """Send one batch to Claude Haiku; return {id: {category, major, genre}} mapping.

    The model sees title + venue + date; its `major` verdict is then refined by the
    deterministic rules above so every batch item gets a consistent final flag.
    """
    def _fmt(ev: dict) -> str:
        venue = (ev.get("location_name") or "-").strip() or "-"
        date = (str(ev.get("date")) or "-")[:10] if ev.get("date") else "-"
        return f'{ev["id"]}: {ev["title"]} | venue: {venue} | date: {date}'

    lines = "\n".join(_fmt(ev) for ev in batch)
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": lines}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse failed for batch: %s\nRaw: %s", exc, raw[:300])
        result = {}
    # Normalise: accept both old string format and new {category, major, genre} format
    normalised: dict[str, dict] = {}
    for k, v in result.items():
        if isinstance(v, str):
            normalised[k] = {"category": v if v in CATEGORIES else "other", "major": False, "genre": None}
        elif isinstance(v, dict):
            cat = v.get("category", "other")
            cat = cat if cat in CATEGORIES else "other"
            # genre only meaningful for music; default music with no/invalid genre to "other"
            genre = v.get("genre")
            if cat == "music":
                genre = genre if genre in GENRES else "other"
            else:
                genre = None
            normalised[k] = {
                "category": cat,
                "major": bool(v.get("major", False)),
                "genre": genre,
            }

    # Refine every batch item (even ones the model omitted) with deterministic rules,
    # using the event's title + venue, so the major flag is consistent and strict.
    out: dict[str, dict] = {}
    for ev in batch:
        info = normalised.get(ev["id"], {"category": "other", "major": False, "genre": None})
        info = {**info}
        info["major"] = _refine_major(ev.get("title"), ev.get("location_name"), info["category"], info["major"])
        out[ev["id"]] = info
    return out


async def classify_events(
    events: list[dict],
    batch_size: int = 50,
) -> dict[str, dict]:
    """Classify a list of event dicts. Returns {event_id: {category, major}}."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    classifications: dict[str, dict] = {}
    batches = [events[i : i + batch_size] for i in range(0, len(events), batch_size)]
    logger.info("Classifying %d events in %d batches", len(events), len(batches))

    for i, batch in enumerate(batches, 1):
        logger.info("Batch %d/%d (%d events)…", i, len(batches), len(batch))
        result = await _classify_batch(client, batch)
        for ev in batch:
            if ev["id"] not in result:
                logger.warning("Event %s not returned by model — marking as 'other'", ev["id"])
                result[ev["id"]] = {"category": "other", "major": False, "genre": None}
        classifications.update(result)

    return classifications


async def classify_unclassified(db_path: str, batch_size: int = 50) -> int:
    """Classify all events in the DB that have no category yet. Returns count classified."""
    from store import load_unclassified_events, update_event_classifications
    events = await load_unclassified_events()
    if not events:
        logger.info("classify_unclassified: no unclassified events")
        return 0
    classifications = await classify_events(events, batch_size=batch_size)
    await update_event_classifications(classifications)
    logger.info("classify_unclassified: classified %d event(s)", len(classifications))
    return len(classifications)


def _build_category_stats(
    events: list[dict],
    classifications: dict[str, dict],
) -> list[dict]:
    """Return sorted list of per-category stat dicts."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        info = classifications.get(ev["id"], {})
        cat = info.get("category", "other") if isinstance(info, dict) else info
        by_category[cat].append(ev)

    total = len(events)
    stats = []
    for cat, items in sorted(by_category.items(), key=lambda x: -len(x[1])):
        sources = Counter(ev["source"] for ev in items)
        stats.append({
            "category": cat,
            "count": len(items),
            "pct": len(items) / total * 100,
            "top_sources": sources.most_common(3),
            "examples": [ev["title"] for ev in items[:3]],
        })
    return stats


def build_report(
    events: list[dict],
    classifications: dict[str, dict],
    min_count: int = 20,
) -> str:
    """Build a plain-text report string (used by CLI)."""
    stats = _build_category_stats(events, classifications)
    total = len(events)

    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"EVENT CLASSIFICATION REPORT  ({total} total events)")
    lines.append("=" * 70)

    for s in stats:
        top_sources = ", ".join(f"{src} ({n})" for src, n in s["top_sources"])
        lines.append("")
        lines.append("─" * 70)
        lines.append(f"  {s['category'].upper().replace('_', ' '):<30}  {s['count']:>4} events  ({s['pct']:.1f}%)")
        lines.append(f"  Sources: {top_sources}")
        lines.append("  Examples:")
        for title in s["examples"]:
            lines.append(f"    • {title[:80]}")

    lines.append("")
    lines.append("=" * 70)
    lines.append(f"CHANNEL RECOMMENDATIONS  (min_count threshold: {min_count})")
    lines.append("=" * 70)
    lines.append("\nWould warrant their own channel:")
    for s in stats:
        if s["count"] >= min_count:
            lines.append(f"  ✓  #{s['category'].replace('_', '-'):<30}  ({s['count']} events)")
    lines.append("\nToo few to split out:")
    for s in stats:
        if s["count"] < min_count:
            lines.append(f"  –  {s['category']:<35}  ({s['count']} events)")
    lines.append("")
    return "\n".join(lines)


def build_discord_embeds(
    events: list[dict],
    classifications: dict[str, dict],
    min_count: int = 20,
) -> list:
    """Build Discord embed objects for posting to a channel."""
    try:
        import discord as _discord
    except ImportError:
        raise RuntimeError("discord.py not available")

    stats = _build_category_stats(events, classifications)
    total = len(events)

    # ── Embed 1: summary table ─────────────────────────────────────────────
    col_cat = max(len(s["category"]) for s in stats)
    header  = f"{'CATEGORY':<{col_cat}}  {'#':>5}  {'%':>5}  TOP SOURCE"
    divider = "─" * len(header)
    rows = [header, divider]
    for s in stats:
        top_src = s["top_sources"][0][0] if s["top_sources"] else "—"
        # Shorten common source names so the table stays narrow
        top_src = (top_src
                   .replace("Ticketmaster Philly", "Ticketmaster")
                   .replace("City of Philadelphia Calendars", "PHL Calendars")
                   .replace("Center City District Events", "CCD Events")
                   .replace("Old City District Events", "OCD Events")
                   .replace("Office of Special Events Closures", "OSE Closures"))
        marker = "✓" if s["count"] >= min_count else " "
        rows.append(f"{marker} {s['category']:<{col_cat}}  {s['count']:>5}  {s['pct']:>4.1f}%  {top_src}")

    table_text = "```\n" + "\n".join(rows) + "\n```"

    summary_embed = _discord.Embed(
        title=f"📊 Event Classification — {total} events",
        description=table_text,
        color=0x5865F2,
    )
    summary_embed.set_footer(text=f"✓ = ≥{min_count} events (recommend own channel)")

    # ── Embed 2: recommendations ───────────────────────────────────────────
    recommend = [s for s in stats if s["count"] >= min_count]
    skip      = [s for s in stats if s["count"] < min_count]

    rec_lines = [f"✅ `#{s['category'].replace('_', '-')}` — **{s['count']}** events" for s in recommend]
    skip_lines = [f"➖ `{s['category']}` — {s['count']} events" for s in skip]

    major_count = sum(1 for info in classifications.values() if isinstance(info, dict) and info.get("major"))
    rec_embed = _discord.Embed(
        title="💡 Channel Recommendations",
        color=0x57F287,
    )
    rec_embed.add_field(
        name=f"Would warrant their own channel (≥{min_count} events)",
        value="\n".join(rec_lines) or "None",
        inline=False,
    )
    if skip_lines:
        rec_embed.add_field(
            name="Too few — fold into a general channel",
            value="\n".join(skip_lines),
            inline=False,
        )
    rec_embed.add_field(
        name="⭐ Major events (→ #major-events only)",
        value=f"{major_count} event(s) flagged as major — will not appear in category channels",
        inline=False,
    )

    # ── Embed 3: examples per category ────────────────────────────────────
    example_lines: list[str] = []
    for s in stats:
        if not s["examples"]:
            continue
        cat_label = s["category"].replace("_", " ").title()
        example_lines.append(f"**{cat_label}**")
        for title in s["examples"]:
            example_lines.append(f"  • {title[:72]}")

    # Split into ≤4000-char chunks (embed description limit)
    example_embeds: list = []
    chunk: list[str] = []
    chunk_len = 0
    for line in example_lines:
        if chunk_len + len(line) + 1 > 3800:
            example_embeds.append("\n".join(chunk))
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        example_embeds.append("\n".join(chunk))

    embeds = [summary_embed, rec_embed]
    for i, text in enumerate(example_embeds):
        title = "🔍 Example Events" if i == 0 else "🔍 Example Events (cont.)"
        embeds.append(_discord.Embed(title=title, description=text, color=0xFEE75C))

    return embeds


async def run(db_path: str, batch_size: int, min_count: int) -> str:
    """Entry point used by both CLI and the bot slash command."""
    events = load_events(db_path)
    if not events:
        raise RuntimeError(f"No events found in DB: {db_path}")
    logger.info("Loaded %d events from %s", len(events), db_path)
    classifications = await classify_events(events, batch_size=batch_size)
    return build_report(events, classifications, min_count=min_count)


# ── CLI entry point ────────────────────────────────────────────────────────────

def _cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Classify stored events by type")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "philly_bot.db"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--min-count", type=int, default=20)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    report = asyncio.run(run(args.db, args.batch_size, args.min_count))
    print(report)


if __name__ == "__main__":
    _cli()
