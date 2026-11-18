"""Autonomous major-event discovery via Claude + web search.

Finds notable/major Philadelphia events the structured scrapers miss — free
museum programming, fan fests, tournaments, festivals, civic happenings — and
returns them as normal EventItems so the existing pipeline (active-filter →
lookahead → geo → dedup → validate → classify) handles them unchanged.

Runs as a registered source ("Major Event Discovery") but self-throttles to
config.DISCOVERY_EVERY_DAYS so cost stays at a few cents/month. Reuses
ANTHROPIC_API_KEY (no new secret). Fails soft: any problem → returns [].

Trust model: the server-side web_search tool grounds output in real search
results (not model memory), and every candidate still passes the Haiku
relevance validator, cross-source dedup, and geo filter downstream. Items
without a concrete future date or a well-formed URL are dropped here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import anthropic

import config
from scraper import EventItem, _make_id, _geocode_event, as_utc

logger = logging.getLogger(__name__)

_LAST_RUN_KEY = "discovery_last_run"
_LAST_COUNT_KEY = "discovery_last_count"

_SYSTEM_PROMPT = """You find notable and major upcoming events in Philadelphia for a city Discord bot.

Use the web_search tool to find CURRENT, real events. Focus on things that are city-wide, high-profile, or otherwise notable, including:
- Tournaments and major sporting events (e.g. World Cup matches and related fan events)
- Festivals, street fairs, parades, and citywide celebrations
- Major concerts and performing-arts runs
- Museum and cultural programming and exhibitions (e.g. Philadelphia Museum of Art, Franklin Institute), including recurring/daily series
- Food & drink festivals, civic events, and large free public gatherings

Rules:
- Only events physically in Philadelphia or its immediate metro area.
- Only events whose date falls within the requested window.
- Every event MUST have a concrete date and a working source URL (prefer the official venue/organizer page).
- Do NOT invent events, dates, or URLs. If you are unsure an event is real, omit it.
- Prefer fewer, higher-confidence, genuinely notable events over a long noisy list.

Respond with ONLY a JSON array (no prose, no markdown), each element:
{"title": str, "date": ISO-8601, "end_date": ISO-8601 or null, "venue": str or null, "address": str or null, "url": str}
Give dates and times in Philadelphia local time with no UTC offset: "YYYY-MM-DDTHH:MM" when the start time is known, or just "YYYY-MM-DD" when it isn't. For a multi-day event, end_date is its last day.
"""


_REVIEW_SYSTEM_PROMPT = """You verify whether Philadelphia events are genuinely MAJOR (city-wide / high-profile).

You receive a JSON array of events already flagged major, each with: id, title, venue, date, url.
Use the web_search tool to check the ones you're unsure about. Decide, for each, whether it is a
genuinely major Philadelphia event — the kind most residents would have heard of:
- FIFA World Cup matches and official fan events
- Marquee pro sports (Eagles/Phillies/76ers/Flyers/Union playoff or signature games), marathons, the Broad Street Run
- Stadium/arena-scale concerts and major headliner tours
- Large festivals drawing tens of thousands (Made in America, Wawa Welcome America / July 4th on the Parkway, Mummers, Thanksgiving Day Parade)
- America250 / Semiquincentennial (250th anniversary) events
- Major road/transit closures (interstates, expressways, bridges, full street closures)

NOT major: a regular orchestra/opera/ballet/theater performance, a museum exhibition or talk,
a club or small-venue show, a gallery opening, a typical neighborhood event — even at famous
institutions. Prestigious or big-institution does NOT mean major.

Respond with ONLY a JSON object mapping each id to true (genuinely major) or false (demote).
Example: {"abc123": true, "def456": false}
"""


def _web_search_tool(max_uses: int) -> dict:
    """The built-in Anthropic web-search server tool, biased to Philadelphia."""
    return {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
        "user_location": {
            "type": "approximate",
            "city": "Philadelphia",
            "region": "Pennsylvania",
            "country": "US",
            "timezone": config.TIMEZONE,
        },
    }


def _extract_json_object(text: str) -> dict:
    """Pull a JSON object out of the model's final text, tolerating fences/prose."""
    if not text:
        return {}
    cleaned = text.strip()
    if "```" in cleaned:
        for part in cleaned.split("```"):
            p = part[4:] if part.startswith("json") else part
            if "{" in p and "}" in p:
                cleaned = p
                break
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        logger.warning("major review: could not parse JSON object: %s", exc)
        return {}


def _message_text(message) -> str:
    """Concatenate the text content blocks of an Anthropic message."""
    return "".join(
        block.text for block in message.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    )


def _extract_json_array(text: str) -> list:
    """Pull the JSON array out of the model's final text, tolerating fences/prose."""
    if not text:
        return []
    cleaned = text.strip()
    if "```" in cleaned:
        # Take the content of the first fenced block if present.
        parts = cleaned.split("```")
        for part in parts:
            p = part[4:] if part.startswith("json") else part
            if "[" in p and "]" in p:
                cleaned = p
                break
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as exc:
        logger.warning("discovery: could not parse JSON array: %s", exc)
        return []


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The prompt asks for Philadelphia local time (a bare date when no time is known),
    # so a naive value is local wall-clock time, and a bare date becomes date-only.
    return as_utc(dt)


async def _cadence_due() -> bool:
    """True if at least DISCOVERY_EVERY_DAYS have passed since the last run."""
    from store import get_last_run

    last = await get_last_run(_LAST_RUN_KEY)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_dt >= timedelta(days=config.DISCOVERY_EVERY_DAYS)


def _build_items(raw: list, source: dict, window_end: datetime) -> list[EventItem]:
    now = datetime.now(timezone.utc)
    items: list[EventItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        url = (entry.get("url") or "").strip()
        event_date = _parse_dt(entry.get("date"))
        # Require a real title, a well-formed URL, and a concrete in-window date.
        if not title or not url.startswith(("http://", "https://")):
            continue
        if event_date is None or event_date > window_end:
            continue
        items.append(EventItem(
            id=_make_id(title, event_date),
            title=title,
            date=event_date,
            end_time=_parse_dt(entry.get("end_date")),
            location_name=(entry.get("venue") or None),
            address=(entry.get("address") or None),
            lat=None,
            lon=None,
            distance_bucket=None,
            price=None,
            url=url,
            source=source["name"],
            channel_type=source["channel"],
            first_seen=now,
        ))
        if len(items) >= config.DISCOVERY_MAX_EVENTS:
            break
    return items


async def discover_events(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Discovery source handler. Returns [] when not due, on missing key, or on any error."""
    from store import set_last_run

    if not await _cadence_due():
        logger.info("discovery: skipped (within %d-day cadence)", config.DISCOVERY_EVERY_DAYS)
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("discovery: ANTHROPIC_API_KEY not set — skipping")
        return []

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=config.EVENTS_LOOKAHEAD_DAYS)
    user_msg = (
        f"Find notable and major events happening in Philadelphia between "
        f"{now.date().isoformat()} and {window_end.date().isoformat()} (inclusive). "
        f"Today is {now.date().isoformat()}."
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        message = await client.messages.create(
            model=config.DISCOVERY_MODEL,
            max_tokens=4096,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
            tools=[_web_search_tool(config.DISCOVERY_MAX_SEARCHES)],
        )
    except anthropic.APIError as exc:
        # No cadence stamp on a failed call — allowed to retry on the next daily run.
        logger.warning("discovery: API error, skipping this run: %s", exc)
        return []
    except Exception as exc:
        logger.error("discovery: unexpected error: %s", exc, exc_info=True)
        return []

    # Cost has been incurred — stamp the run now so we honor the cadence even if
    # parsing yields nothing this week.
    await set_last_run(_LAST_RUN_KEY, now.isoformat())

    raw = _extract_json_array(_message_text(message))
    items = _build_items(raw, source, window_end)

    # Geocode anything with an address so distance tags render (events keep
    # ungeocoded items regardless).
    for ev in items:
        if ev.address:
            try:
                await _geocode_event(ev, session)
            except Exception as exc:
                logger.debug("discovery: geocode failed for %r: %s", ev.title, exc)

    await set_last_run(_LAST_COUNT_KEY, str(len(items)))
    logger.info("discovery: returned %d candidate event(s)", len(items))
    return items


_REVIEW_LAST_RUN_KEY = "major_review_last_run"
_REVIEW_DEMOTED_KEY = "major_review_demoted"


async def review_major_events() -> int:
    """Batched web-verification of the major list: demote niche/non-citywide events.

    Loads every event currently flagged is_major, asks Sonnet (with web search) which
    are genuinely major, and writes back the verdicts. Returns the number of events
    whose flag changed. Fails soft: on missing key, no majors, or any error, changes
    nothing and returns 0.
    """
    from store import load_major_events, update_major_flags, set_last_run

    majors = await load_major_events()
    if not majors:
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("major review: ANTHROPIC_API_KEY not set — skipping")
        return 0

    payload = json.dumps([
        {
            "id": m["id"],
            "title": m["title"],
            "venue": m.get("location_name"),
            "date": (str(m.get("date")) or "")[:10],
            "url": m.get("url"),
        }
        for m in majors
    ], ensure_ascii=False)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        message = await client.messages.create(
            model=config.DISCOVERY_MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": _REVIEW_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": payload}],
            tools=[_web_search_tool(config.MAJOR_REVIEW_MAX_SEARCHES)],
        )
    except anthropic.APIError as exc:
        logger.warning("major review: API error, skipping: %s", exc)
        return 0
    except Exception as exc:
        logger.error("major review: unexpected error: %s", exc, exc_info=True)
        return 0

    verdicts = _extract_json_object(_message_text(message))
    # Only act on ids we recognize, coercing values to bool. Omitted ids are left as-is.
    known = {m["id"] for m in majors}
    flags = {k: bool(v) for k, v in verdicts.items() if k in known}
    changed = await update_major_flags(flags)

    demoted = sum(1 for v in flags.values() if not v)
    await set_last_run(_REVIEW_LAST_RUN_KEY, datetime.now(timezone.utc).isoformat())
    await set_last_run(_REVIEW_DEMOTED_KEY, str(demoted))
    logger.info("major review: %d flag change(s), %d demoted of %d major event(s)",
                changed, demoted, len(majors))
    return changed
