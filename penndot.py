"""PennDOT RCRS Event Data — fetch, filter to Philadelphia, and shape into display entries.

Docs: https://www.pa.gov/agencies/penndot/programs-and-doing-business/online-services/developer-resources-documentation-api

RCRS rows are never stored in the events table: callers fetch and render on demand.
`fetch_feed` raises `RcrsError` on any failure so callers can tell "PennDOT is down"
from "nothing is happening" (unlike scrapers, which fail soft to []).

Real-feed quirks found against the live service (the docs are out of date):
- the envelope key is `values`, not `Values`, so keys are normalized to the documented names;
- `LaneStatus` casing varies ("Closed" vs "open");
- reopened events linger for months, not the documented 30 minutes.
"""

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

import config
from geocoder import haversine_miles

logger = logging.getLogger(__name__)

BASE_URL = "https://eventsdata.dot.pa.gov"
FEEDS = {"live": "liveEvents", "planned": "plannedEvents", "winter": "winterConditions"}
PHILADELPHIA_COUNTY = 67  # RCRS County code table
TIMEOUT = aiohttp.ClientTimeout(total=60)  # statewide payloads, no server-side filtering
FEED_TZ = ZoneInfo(config.TIMEZONE)  # the docs don't state a timezone; Eastern fits the data

# Most disruptive first; anything unlisted sorts after these.
LANE_STATUS_ORDER = [
    "closed", "ramp closure", "no entry access", "lane restriction", "ramp restriction",
    "vehicle restriction", "speed restriction", "shoulder closed", "traffic disruption", "open",
]
# Planned/maintenance work (docs: EventClass 3) and special events (class 14). Every other
# EventType is an incident (crash, disabled vehicle, debris, flooding, police activity, ...).
WORK_TYPES = {"roadwork", "moving roadwork", "utility work", "damaged roadway", "special event"}
DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M %p")
# Opposite-direction events whose midpoints are this close are shown as one entry.
MERGE_MAX_MILES = 0.25
# All-caps words tidy_text leaves alone (route prefixes and agencies), e.g. "US 13", "PSP".
KEEP_UPPER = {"US", "PA", "SR", "NJ", "PSP", "SEPTA", "PHL"}


class RcrsError(Exception):
    """A feed request failed or returned something other than the expected shape."""


@dataclass
class RcrsEntry:
    """One displayable item: a single RCRS event, or an opposite-direction pair merged."""
    feed: str                      # "live" | "planned"
    status: str                    # LaneStatus as reported, e.g. "Closed"
    road: str                      # tidied facility + direction(s), e.g. "Market St westbound + eastbound"
    descriptions: list[str]        # tidied, one per merged event
    event_type: str
    event_ids: list[str]
    start: Optional[datetime]      # aware, Eastern
    updated: Optional[datetime]    # aware, Eastern
    miles: Optional[float]         # from config.CENTER_LOCATION
    lat: Optional[float]
    lon: Optional[float]
    detour: bool = False
    lanes: list[str] = field(default_factory=list)
    speeds: list[str] = field(default_factory=list)
    schedules: list[str] = field(default_factory=list)

    @property
    def is_incident(self) -> bool:
        return self.event_type.lower() not in WORK_TYPES

    @property
    def severity(self) -> int:
        status = self.status.lower()
        return LANE_STATUS_ORDER.index(status) if status in LANE_STATUS_ORDER else len(LANE_STATUS_ORDER)


# ── Fetching ───────────────────────────────────────────────────────────────────

def auth_headers_from_env() -> dict[str, str]:
    """HTTP Basic auth exactly as the RCRS docs describe: base64("user:pass") in Authorization."""
    username = os.environ.get("PENNDOT_RCRS_USERNAME", "")
    password = os.environ.get("PENNDOT_RCRS_PASSWORD", "")
    if not username or not password:
        raise RcrsError("PENNDOT_RCRS_USERNAME and PENNDOT_RCRS_PASSWORD must be set")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def normalize_keys(obj: object) -> object:
    """Uppercase the first letter of every key, recursively.

    The live service serializes camelCase (`values`) while the docs show PascalCase
    (`Values`, `LaneStatus`, `EventID`); this maps either onto the documented names.
    """
    if isinstance(obj, dict):
        return {(k[:1].upper() + k[1:]): normalize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_keys(v) for v in obj]
    return obj


async def fetch_feed(feed: str, session: aiohttp.ClientSession, headers: dict[str, str]) -> list[dict]:
    """GET one RCRS method and return its statewide records, keys normalized. Raises RcrsError."""
    url = f"{BASE_URL}/{FEEDS[feed]}"
    try:
        async with session.get(url, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 401:
                raise RcrsError(f"{url}: 401 Unauthorized — check PENNDOT_RCRS_USERNAME/PASSWORD")
            if resp.status != 200:
                raise RcrsError(f"{url}: HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise RcrsError(f"{url}: {exc!r}") from exc
    except asyncio.TimeoutError as exc:
        raise RcrsError(f"{url}: timed out after {TIMEOUT.total:.0f}s") from exc
    except json.JSONDecodeError as exc:
        raise RcrsError(f"{url}: response is not JSON ({exc})") from exc

    data = normalize_keys(data)
    values = data.get("Values") if isinstance(data, dict) else None
    if not isinstance(values, list):
        shape = list(data)[:10] if isinstance(data, dict) else type(data).__name__
        raise RcrsError(f"{url}: no 'Values'/'values' list in response (got {shape})")
    # Every documented record carries County; if none do, the field names differ from the
    # docs and the Philadelphia filter would silently match nothing — say so instead.
    if values and not any(isinstance(r, dict) and "County" in r for r in values):
        sample = list(values[0])[:15] if isinstance(values[0], dict) else type(values[0]).__name__
        raise RcrsError(f"{url}: records have no County field — names differ from the docs (first record keys: {sample})")
    return values


# ── Field parsing ──────────────────────────────────────────────────────────────

def _as_int(value: object) -> Optional[int]:
    """Codes arrive as zero-padded text ("02") in the docs' examples; compare as ints."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_philadelphia(rec: dict) -> bool:
    """True if the record touches Philadelphia at its main, start, end, or incident location.

    Checking all four keeps events that cross a county line (e.g. I-95 from Bucks into
    the city), whose main `County` may be the neighbouring county.
    """
    keys = ("County", "CountyFrom", "CountyTo", "CountyInc")
    return any(_as_int(rec.get(k)) == PHILADELPHIA_COUNTY for k in keys)


def is_reopened(rec: dict) -> bool:
    """Reopened events linger in the live feed (docs say 30 min; real ones were months old)."""
    return str(rec.get("LaneStatus", "")).lower() == "open" or bool(rec.get("ActualDateTimeOpened"))


def parse_latlon(value: object) -> Optional[tuple[float, float]]:
    """Parse RCRS "lat,lon" text; None when blank or malformed."""
    parts = str(value or "").split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def parse_dt(value: object) -> Optional[datetime]:
    """Parse the RCRS date formats as Eastern time; None when blank or unrecognized."""
    text = str(value or "").strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=FEED_TZ)
        except ValueError:
            continue
    return None


def tidy_text(text: object) -> str:
    """Make RCRS road text readable: "0076 (I - 076)" / "(3010) MARKET ST" → "I-76" / "Market St"."""
    s = " ".join(str(text or "").split())                        # collapse doubled spaces
    s = re.sub(r"^\d{4}\s*\((.+)\)$", r"\1", s)                    # winter "0076 (I - 076)" → "I - 076"
    s = re.sub(r"\(\d{4}\)\s*", "", s)                             # state-route code "(3010) "
    s = re.sub(r"\b(I|US|PA|SR)\s*-\s*0*(\d+)\b", r"\1-\2", s)     # "I - 076" → "I-76"
    return re.sub(r"\b[A-Z]{2,}\b",                                # SHOUTED words → Title, keep acronyms
                  lambda m: m.group(0) if m.group(0) in KEEP_UPPER else m.group(0).capitalize(), s)


def _start(rec: dict, feed: str) -> Optional[datetime]:
    if feed == "planned":
        return parse_dt(rec.get("DateTimeEventOccurs"))
    return parse_dt(rec.get("DateTimeEventOccurs") or rec.get("DateTimeNotified"))


def _midpoint(rec: dict) -> Optional[tuple[float, float]]:
    keys = ("FromLocLatLong", "ToLocLatLong", "IncidentLocLatLong")
    points = [p for p in (parse_latlon(rec.get(k)) for k in keys) if p]
    if not points:
        return None
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def _miles_from_center(rec: dict) -> Optional[float]:
    """Distance to the incident point, else the start, else the end."""
    for key in ("IncidentLocLatLong", "FromLocLatLong", "ToLocLatLong"):
        point = parse_latlon(rec.get(key))
        if point:
            return haversine_miles(config.CENTER_LOCATION["lat"], config.CENTER_LOCATION["lon"], *point)
    return None


def _schedule_line(rec: dict) -> Optional[str]:
    rep = rec.get("EventRepetitionDetails") or {}
    if not isinstance(rep, dict) or rep.get("Occurs") in (None, "", "N/A"):
        return None
    holidays = " (skips holidays)" if str(rep.get("SkipHolidays")).upper() == "YES" else ""
    return (f"{rep.get('Occurs')} {rep.get('DailyStartTime')}–{rep.get('DailyEndTime')}, "
            f"{rep.get('StartDate')} → {rep.get('EndDate')}{holidays}")


# ── Grouping into entries ──────────────────────────────────────────────────────

def _is_direction_pair(a: dict, b: dict, feed: str) -> bool:
    """Same road, status, type and start day, opposite directions, and the same stretch of road."""
    if str(a.get("Direction", "")).lower() == str(b.get("Direction", "")).lower():
        return False
    same = (tidy_text(a.get("Facility")) == tidy_text(b.get("Facility"))
            and str(a.get("LaneStatus", "")).lower() == str(b.get("LaneStatus", "")).lower()
            and a.get("EventType") == b.get("EventType"))
    start_a, start_b = _start(a, feed), _start(b, feed)
    if not same or start_a is None or start_b is None or start_a.date() != start_b.date():
        return False
    mid_a, mid_b = _midpoint(a), _midpoint(b)
    return mid_a is not None and mid_b is not None and haversine_miles(*mid_a, *mid_b) <= MERGE_MAX_MILES


def _group_direction_pairs(recs: list[dict], feed: str) -> list[list[dict]]:
    """Pair each event with at most one opposite-direction twin; order follows `recs`."""
    groups: list[list[dict]] = []
    used: set[int] = set()
    for i, rec in enumerate(recs):
        if i in used:
            continue
        group = [rec]
        for j in range(i + 1, len(recs)):
            if j not in used and _is_direction_pair(rec, recs[j], feed):
                group.append(recs[j])
                used.add(j)
                break
        groups.append(group)
    return groups


def _entry_from_group(recs: list[dict], feed: str) -> RcrsEntry:
    first = recs[0]
    directions = " + ".join(str(r.get("Direction") or "?") for r in recs)
    starts = [s for s in (_start(r, feed) for r in recs) if s]
    updates = [u for u in (parse_dt(r.get("LastUpdate")) for r in recs) if u]
    distances = [d for d in (_miles_from_center(r) for r in recs) if d is not None]
    mid = _midpoint(first)
    return RcrsEntry(
        feed=feed,
        status=str(first.get("LaneStatus") or "unknown"),
        road=" ".join(p for p in (tidy_text(first.get("Facility")), directions) if p),
        descriptions=[tidy_text(r.get("Description")) or "(no description)" for r in recs],
        event_type=str(first.get("EventType") or "unknown"),
        event_ids=[str(r.get("EventID")) for r in recs],
        start=min(starts) if starts else None,
        updated=max(updates) if updates else None,
        miles=min(distances) if distances else None,
        lat=mid[0] if mid else None,
        lon=mid[1] if mid else None,
        detour=any(r.get("DetourInEffect") == "T" for r in recs),
        lanes=sorted({str(r["AffectedLanes"]) for r in recs if r.get("AffectedLanes")}),
        speeds=sorted({str(r["RestrictedSpeedValue"]) for r in recs if r.get("RestrictedSpeedValue")}),
        schedules=[s for s in (_schedule_line(r) for r in recs) if s],
    )


def build_entries(records: list[dict], feed: str) -> list[RcrsEntry]:
    """Active Philadelphia records → entries, most disruptive / nearest first, pairs merged.

    Expects records already filtered with `is_philadelphia`; drops reopened ones.
    """
    def order(rec: dict) -> tuple[int, float]:
        status = str(rec.get("LaneStatus", "")).lower()
        rank = LANE_STATUS_ORDER.index(status) if status in LANE_STATUS_ORDER else len(LANE_STATUS_ORDER)
        miles = _miles_from_center(rec)
        return rank, miles if miles is not None else 999.0

    active = sorted((r for r in records if not is_reopened(r)), key=order)
    return [_entry_from_group(g, feed) for g in _group_direction_pairs(active, feed)]


# ── Splitting between the two channels ─────────────────────────────────────────

def _within(entry: RcrsEntry, max_miles: float) -> bool:
    """Near enough to show. No coordinates means unknown distance, so it's not shown."""
    return entry.miles is not None and entry.miles <= max_miles


def split_live_entries(entries: list[RcrsEntry], now: datetime) -> tuple[list[RcrsEntry], list[RcrsEntry]]:
    """Live-feed entries → (live board, ongoing); anything else is dropped.

    - Incidents within PENNDOT_INCIDENT_MAX_MILES → live board.
    - Road work within PENNDOT_WORK_MAX_MILES that started within PENNDOT_LIVE_NEW_HOURS
      → live board; older (or undated) near-home work → ongoing (planned digest).
    - Farther incidents/work, and anything without coordinates → dropped.
    """
    cutoff = now - timedelta(hours=config.PENNDOT_LIVE_NEW_HOURS)
    live: list[RcrsEntry] = []
    ongoing: list[RcrsEntry] = []
    for e in entries:
        if e.is_incident:
            if _within(e, config.PENNDOT_INCIDENT_MAX_MILES):
                live.append(e)
        elif _within(e, config.PENNDOT_WORK_MAX_MILES):
            is_new = e.start is not None and e.start >= cutoff
            (live if is_new else ongoing).append(e)
    return live, ongoing


def upcoming_planned(entries: list[RcrsEntry], now: datetime) -> list[RcrsEntry]:
    """Planned-feed entries within PENNDOT_WORK_MAX_MILES starting within
    PENNDOT_PLANNED_LOOKAHEAD_DAYS, soonest first.

    Entries whose start has passed, or that have no start, are kept: the planned feed lists
    them only while they're still scheduled.
    """
    ceiling = now + timedelta(days=config.PENNDOT_PLANNED_LOOKAHEAD_DAYS)
    kept = [e for e in entries if _within(e, config.PENNDOT_WORK_MAX_MILES)
            and (e.start is None or e.start <= ceiling)]
    return sorted(kept, key=lambda e: e.start or ceiling)


def now_eastern() -> datetime:
    return datetime.now(FEED_TZ)


# ── Channel loader ─────────────────────────────────────────────────────────────

@dataclass
class PenndotSections:
    """What #traffic-alerts shows, in display order."""
    live: list[RcrsEntry]       # nearby incidents + near-home work started in the last N hours
    ongoing: list[RcrsEntry]    # older near-home work (live feed)
    upcoming: list[RcrsEntry]   # near-home planned-feed events in the lookahead window


async def load_channel_sections() -> PenndotSections:
    """Fetch both RCRS feeds and split them into the #traffic-alerts sections.

    Raises RcrsError if either feed fails, so the caller leaves the channel as-is rather
    than replacing it with a partial or empty one.
    """
    headers = auth_headers_from_env()
    now = now_eastern()
    async with aiohttp.ClientSession() as session:
        live_recs = await fetch_feed("live", session, headers)
        planned_recs = await fetch_feed("planned", session, headers)
    live_entries = build_entries([r for r in live_recs if is_philadelphia(r)], "live")
    planned_entries = build_entries([r for r in planned_recs if is_philadelphia(r)], "planned")
    live, ongoing = split_live_entries(live_entries, now)
    sections = PenndotSections(live=live, ongoing=ongoing, upcoming=upcoming_planned(planned_entries, now))
    logger.info("penndot: %d live, %d ongoing, %d upcoming",
                len(sections.live), len(sections.ongoing), len(sections.upcoming))
    return sections
