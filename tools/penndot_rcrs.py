"""
PennDOT RCRS Event Data — terminal preview. Not imported by the runtime.

Fetches the RCRS feeds through `penndot.py` (the same code the bot uses) and prints the
three sections of the #traffic-alerts channel (refreshed every 15 min and by /refresh):
  - LIVE NOW:  incidents within PENNDOT_INCIDENT_MAX_MILES + road work within
               PENNDOT_WORK_MAX_MILES that started in the last 24h
  - ONGOING:   older near-home work
  - UPCOMING:  near-home planned events in the next PENNDOT_PLANNED_LOOKAHEAD_DAYS
  Everything farther out (or without coordinates) is dropped.
  - winter conditions (preview only for now)

Usage (run from the repo root):
    ./venv/bin/python tools/penndot_rcrs.py                  # all three feeds
    ./venv/bin/python tools/penndot_rcrs.py --feed planned   # one feed: live | planned | winter
    ./venv/bin/python tools/penndot_rcrs.py --json           # Philadelphia records as JSON instead

Needs PENNDOT_RCRS_USERNAME and PENNDOT_RCRS_PASSWORD in .env (HTTP Basic auth).
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

# This tool lives in tools/ but imports repo-root modules; make the root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # pulls in python-dotenv; .env must be present
import penndot
from penndot import RcrsEntry, RcrsError

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("penndot_rcrs")


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%a %b %d %H:%M") if dt else "?"


def _age(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return ""
    hours = (now - dt).total_seconds() / 3600
    if hours < 0:
        return ""
    if hours < 24:
        return f"{hours:.0f}h ago"
    days = hours / 24
    return f"{days:.0f}d ago" if days < 14 else f"{days / 7:.0f} wks ago"


def render_entry(entry: RcrsEntry, now: datetime) -> list[str]:
    where = (f"{entry.miles:.1f} mi from {config.CENTER_LOCATION['name']}"
             if entry.miles is not None else "no coordinates")
    lines = [f"  ■ {entry.status.upper()} · {entry.road or '(no facility)'} · {where}"]
    lines.extend(f"    {d}" for d in entry.descriptions)
    if entry.feed == "planned":
        when = f"Starts {_fmt(entry.start)}"
    else:
        age = _age(entry.start, now)
        when = f"Since {_fmt(entry.start)}" + (f" ({age})" if age else "")
    lines.append(f"    {when} · updated {_fmt(entry.updated)}")
    lines.extend(f"    Schedule: {s}" for s in entry.schedules)
    ids = ", ".join(entry.event_ids)
    extras = [f"type: {entry.event_type}", f"event{'s' if len(entry.event_ids) > 1 else ''} {ids}"]
    if entry.detour:
        extras.insert(0, "DETOUR IN EFFECT")
    if entry.lanes:
        extras.append(f"lanes: {'; '.join(entry.lanes)}")
    if entry.speeds:
        extras.append(f"speed: {'; '.join(entry.speeds)}")
    lines.append(f"    {' · '.join(extras)}")
    lines.append("")
    return lines


def render_section(title: str, entries: list[RcrsEntry], now: datetime, empty: str) -> list[str]:
    lines = [f"── {title}: {len(entries)} {'entry' if len(entries) == 1 else 'entries'} ──"]
    if not entries:
        return lines + [f"  {empty}", ""]
    lines.append("")
    for entry in entries:
        lines.extend(render_entry(entry, now))
    return lines


def feed_summary(feed: str, statewide: list[dict], philly: list[dict], entries: list[RcrsEntry]) -> list[str]:
    reopened = sum(1 for r in philly if penndot.is_reopened(r))
    active = len(philly) - reopened
    note = f" ({reopened} reopened, dropped)" if reopened else ""
    lines = [f"═══ {penndot.FEEDS[feed]}: {len(statewide)} statewide → {len(philly)} Philadelphia{note} ═══"]
    if active:
        kept = [r for r in philly if not penndot.is_reopened(r)]
        by_status = Counter(str(r.get("LaneStatus") or "?").lower() for r in kept)
        by_type = Counter(str(r.get("EventType") or "?") for r in kept)
        lines.append("  by lane status: " + ", ".join(f"{k} {v}" for k, v in by_status.most_common()))
        lines.append("  by event type:  " + ", ".join(f"{k} {v}" for k, v in by_type.most_common()))
        merged = active - len(entries)
        if merged:
            pairs = "pair" if merged == 1 else "pairs"
            lines.append(f"  {active} events → {len(entries)} entries ({merged} direction {pairs} merged)")
    lines.append("")
    return lines


def render_winter(statewide: list[dict], philly: list[dict]) -> list[str]:
    """Winter conditions: list only the sections that aren't Clear."""
    sections = "section" if len(statewide) == 1 else "sections"
    lines = [f"═══ winterConditions: {len(statewide)} {sections} statewide → {len(philly)} Philadelphia ═══"]
    not_clear = [r for r in philly if str(r.get("ConditionID")) != "1"]
    lines.append(f"  {len(philly) - len(not_clear)} Clear, {len(not_clear)} not Clear")
    for rec in sorted(not_clear, key=lambda r: -(penndot._as_int(r.get("ConditionID")) or 0)):
        lines.append(f"  ■ {str(rec.get('Condition') or '?').upper()} · {penndot.tidy_text(rec.get('Facility'))}: "
                     f"{penndot.tidy_text(rec.get('FromLoc'))} → {penndot.tidy_text(rec.get('ToLoc'))} "
                     f"(updated {_fmt(penndot.parse_dt(rec.get('LastUpdate')))})")
    lines.append("")
    return lines


async def run(feeds: list[str], as_json: bool) -> int:
    try:
        headers = penndot.auth_headers_from_env()
    except RcrsError as exc:
        logger.error("%s", exc)
        return 2

    now = penndot.now_eastern()
    failed = 0
    fetched: dict[str, tuple[list[dict], list[dict]]] = {}
    async with aiohttp.ClientSession() as session:
        for feed in feeds:
            try:
                statewide = await penndot.fetch_feed(feed, session, headers)
            except RcrsError as exc:
                logger.error("%s", exc)
                failed += 1
                continue
            fetched[feed] = (statewide, [r for r in statewide if penndot.is_philadelphia(r)])

    if as_json:
        print(json.dumps({penndot.FEEDS[f]: philly for f, (_, philly) in fetched.items()}, indent=2))
        return 1 if failed else 0

    out: list[str] = []
    live_board: list[RcrsEntry] = []
    ongoing: list[RcrsEntry] = []
    upcoming: list[RcrsEntry] = []
    if "live" in fetched:
        statewide, philly = fetched["live"]
        entries = penndot.build_entries(philly, "live")
        out += feed_summary("live", statewide, philly, entries)
        live_board, ongoing = penndot.split_live_entries(entries, now)
    if "planned" in fetched:
        statewide, philly = fetched["planned"]
        entries = penndot.build_entries(philly, "planned")
        out += feed_summary("planned", statewide, philly, entries)
        upcoming = penndot.upcoming_planned(entries, now)

    addr = config.CENTER_LOCATION["name"]
    if "live" in fetched or "planned" in fetched:
        out.append("════ #traffic-alerts (every 15 min) ════")
        out.append("")
    if "live" in fetched:
        out += render_section(
            f"LIVE NOW — incidents within {config.PENNDOT_INCIDENT_MAX_MILES:g} mi of {addr} + work within "
            f"{config.PENNDOT_WORK_MAX_MILES:g} mi started in the last {config.PENNDOT_LIVE_NEW_HOURS}h",
            live_board, now, "No nearby incidents or new road work.")
    if "live" in fetched or "planned" in fetched:
        if "live" in fetched:
            out += render_section(f"ONGOING — work within {config.PENNDOT_WORK_MAX_MILES:g} mi of {addr}",
                                  ongoing, now, "No ongoing work nearby.")
        if "planned" in fetched:
            out += render_section(f"UPCOMING — within {config.PENNDOT_WORK_MAX_MILES:g} mi of {addr} "
                                  f"(next {config.PENNDOT_PLANNED_LOOKAHEAD_DAYS} days)",
                                  upcoming, now, "No planned closures nearby.")
    if "winter" in fetched:
        out += render_winter(*fetched["winter"])

    print("\n".join(out))
    print(f"Distances are from {addr} (config.CENTER_LOCATION). Times shown in Eastern "
          f"(the feed doesn't state a timezone; Eastern fits the data).")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview PennDOT RCRS channels for Philadelphia.")
    parser.add_argument("--feed", choices=[*penndot.FEEDS, "all"], default="all")
    parser.add_argument("--json", action="store_true",
                        help="print Philadelphia records as JSON (values as received; keys first-letter-uppercased)")
    args = parser.parse_args()
    feeds = list(penndot.FEEDS) if args.feed == "all" else [args.feed]
    sys.exit(asyncio.run(run(feeds, args.json)))


if __name__ == "__main__":
    main()
