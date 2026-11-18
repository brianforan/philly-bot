"""Format normalized EventItem objects into Discord Embeds and post/edit digests."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import discord

import config
from scraper import LOCAL_TZ, EventItem, as_utc, event_is_active, is_date_only

if TYPE_CHECKING:  # type hints only; penndot isn't needed at runtime here
    from penndot import PenndotSections, RcrsEntry

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_BUCKET_COLORS: dict[str | None, int] = {
    "< 1 mi": 0x00B140,  # green
    "1–3 mi": 0x0057A8,  # blue
    "3–7 mi": 0xF4AC00,  # amber
    "7+ mi":  0x9B59B6,  # purple
    None:     0x95A5A6,  # grey – unknown distance
}

# Distance bucket ordering (closest first); None/unknown last.
_BUCKET_RANK: dict[str | None, int] = {"< 1 mi": 0, "1–3 mi": 1, "3–7 mi": 2, "7+ mi": 3, None: 4}

_EMPTY_COLOR       = 0x2C2F33


@dataclass
class DigestOptions:
    """Runtime display options for a digest, built from DB settings."""
    group_by: str = "week"        # week | day | distance | flat
    sort: str = "date"            # date | distance
    show_index: bool = True
    show_distance: bool = True
    show_price: bool = True
    show_location: bool = True
    max_per_channel: int = 0      # 0 = unlimited
    jump_links: bool = True
    index_position: str = "bottom"  # top | bottom

    @classmethod
    def from_settings(cls, settings: dict[str, str]) -> "DigestOptions":
        """Build from a merged settings dict (DB values over DEFAULT_SETTINGS)."""
        merged = {**config.DEFAULT_SETTINGS, **(settings or {})}

        def flag(key: str) -> bool:
            return str(merged.get(key, "on")).lower() == "on"

        def num(key: str) -> int:
            try:
                return int(merged.get(key, "0"))
            except (TypeError, ValueError):
                return 0

        return cls(
            group_by=merged.get(config.SETTING_DIGEST_GROUP_BY, "week"),
            sort=merged.get(config.SETTING_DIGEST_SORT, "date"),
            show_index=flag(config.SETTING_DIGEST_SHOW_INDEX),
            show_distance=flag(config.SETTING_DIGEST_SHOW_DISTANCE),
            show_price=flag(config.SETTING_DIGEST_SHOW_PRICE),
            show_location=flag(config.SETTING_DIGEST_SHOW_LOCATION),
            max_per_channel=num(config.SETTING_DIGEST_MAX_PER_CHANNEL),
            jump_links=flag(config.SETTING_DIGEST_JUMP_LINKS),
            index_position=merged.get(config.SETTING_DIGEST_INDEX_POSITION, "bottom"),
        )


_DEFAULT_OPTIONS = DigestOptions()


# ── Helpers ───────────────────────────────────────────────────────────────────


# Dates are plain text in Philadelphia time ("Thu, Nov 12 · 8:00 PM"), not Discord
# <t:…> timestamps: shorter, they render anywhere (titles included), and ranges read
# naturally. The year is shown only when it isn't the current one.


def _local(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(LOCAL_TZ)


def _local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def _fmt_day(d: date) -> str:
    text = f"{d:%a, %b} {d.day}"
    return text if d.year == _local_today().year else f"{text}, {d.year}"


def _fmt_time(dt: datetime) -> str:
    """'8:00 PM' for a local datetime."""
    return f"{dt.hour % 12 or 12}:{dt:%M %p}"


def _fmt_moment(dt: datetime) -> str:
    """'Thu, Nov 12, 8:00 PM' (PennDOT start/updated times)."""
    local = _local(dt)
    return f"{_fmt_day(local.date())}, {_fmt_time(local)}"


def _spans_days(start: datetime, end: Optional[datetime]) -> bool:
    """True when an event runs over several days: it ends on a later local day and
    either ends date-only or lasts 24h+ (so a 10 PM – 2 AM show is still one day)."""
    if end is None or as_utc(end) <= as_utc(start):
        return False
    s, e = _local(start), _local(end)
    return e.date() > s.date() and (is_date_only(e) or e - s >= timedelta(hours=24))


def _fmt_when(start: datetime, end: Optional[datetime]) -> str:
    """An event's date text.

    One day:  "Thu, Nov 12 · 8:00 PM", "… · 8:00 – 10:00 PM", or "Thu, Nov 12" (date only).
    Overnight (ends next day, under 24h): shown like one day, "… · 10:00 PM – 2:00 AM".
    Several days, from the actual start: "Fri, Nov 13 – Sat, Nov 14".
    """
    s = _local(start)
    if _spans_days(start, end):
        return f"{_fmt_day(s.date())} – {_fmt_day(_local(end).date())}"
    e = _local(end) if end is not None and as_utc(end) > as_utc(start) else None
    text = _fmt_day(s.date())
    if is_date_only(s):
        return text
    start_text = _fmt_time(s)
    if e is not None and not is_date_only(e):
        if f"{s:%p}" == f"{e:%p}":
            start_text = start_text[:-3]  # "8:00 – 10:00 PM", not "8:00 PM – 10:00 PM"
        return f"{text} · {start_text} – {_fmt_time(e)}"
    return f"{text} · {start_text}"


def _bucket_label(bucket: str) -> str:
    """Display text for a stored distance bucket: "< 1 mi" → "< 1 mi from <CENTER_NAME>"."""
    return f"{bucket} from {config.CENTER_LOCATION['name']}"


def _location_str(item: EventItem) -> str:
    """Venue + address, de-duplicated.

    Some sources set location_name and address to the same (or overlapping) value;
    showing both produces a redundant string, so collapse when one contains the other.
    """
    name = (item.location_name or "").strip()
    addr = (item.address or "").strip()
    if not name:
        return addr
    if not addr:
        return name
    n, a = name.casefold(), addr.casefold()
    if n == a or n in a:
        return addr
    if a in n:
        return name
    return f"{name}, {addr}"


def _norm_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for fuzzy dup matching."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _completeness(item: EventItem) -> int:
    """Rough score of how much detail an item carries — used to pick the best of a dup set."""
    score = 0
    if item.address:
        score += 2
    if item.location_name:
        score += 1
    if item.price:
        score += 1
    if item.lat is not None and item.lon is not None:
        score += 1
    if item.end_time:
        score += 1
    return score


def dedupe_events(items: list[EventItem]) -> list[EventItem]:
    """Collapse near-duplicate events that came from different sources.

    Two items are considered the same event when their normalized title and
    calendar date match. Among duplicates, keep the most complete record.
    Preserves the original ordering of the kept items.
    """
    best: dict[tuple[str, Optional[str]], EventItem] = {}
    for item in items:
        day = _local(item.date).date().isoformat() if item.date else None
        key = (_norm_title(item.title), day)
        existing = best.get(key)
        if existing is None or _completeness(item) > _completeness(existing):
            best[key] = item

    kept_ids = {id(v) for v in best.values()}
    deduped = [it for it in items if id(it) in kept_ids]
    dropped = len(items) - len(deduped)
    if dropped:
        logger.info("dedupe_events: removed %d cross-source duplicate(s)", dropped)
    return deduped


def _fmt_run(dates: list[datetime]) -> str:
    """Date text for a run of separate dates (sorted), from its first remaining date.

    "Sat, Nov 14 – Thu, Dec 31 · 8:00 PM · 37 dates"; all on one weekday:
    "Thursdays, Nov 12 – Dec 17 · 9:00 PM". The time shows only when every date shares it.
    """
    local = [_local(d) for d in dates]
    first, last = local[0].date(), local[-1].date()
    times = {(d.hour, d.minute) for d in local}
    time_text = "" if len(times) > 1 or is_date_only(local[0]) else f" · {_fmt_time(local[0])}"
    if len({d.weekday() for d in local}) == 1:
        short = lambda d: _fmt_day(d).split(", ", 1)[1]  # "Nov 12" (+ year if not this year)
        return f"{first:%A}s, {short(first)} – {short(last)}{time_text}"
    return f"{_fmt_day(first)} – {_fmt_day(last)}{time_text} · {len(local)} dates"


@dataclass
class _Line:
    """One rendered line: an event or a whole series, its date text, and the moment it
    groups and sorts by. A series spanning weeks gets one _Line per week, all sharing
    `key`, so the index can count it once."""
    item: EventItem
    when: str
    at: Optional[datetime]
    key: int


def _week_monday(dt: datetime) -> date:
    d = _local(dt).date()
    return d - timedelta(days=d.weekday())


def _event_lines(items: list[EventItem], options: DigestOptions) -> list[_Line]:
    """Turn items into display lines, merging series.

    A run (same title + venue on several dates) becomes one line with its full range;
    a multi-day event shows its actual start – end. With week grouping, each appears
    in every week it covers: a run in each week it has a date, a multi-day event in
    each week from now to its end, but never past the channel's last week of other
    events (so a months-long exhibit never adds weeks by itself). Other groupings
    show each series once, at its next date.
    """
    weekly = options.group_by not in ("day", "distance", "flat")
    groups: dict[tuple[str, str], list[EventItem]] = {}
    for item in items:
        groups.setdefault((_norm_title(item.title), (item.location_name or "").casefold()), []).append(item)

    far_future = datetime.max.replace(tzinfo=timezone.utc)
    lines: list[_Line] = []
    spans: list[tuple[int, EventItem]] = []
    for key, group in enumerate(groups.values()):
        group.sort(key=lambda e: as_utc(e.date) if e.date else far_future)
        first = group[0]
        if first.date is None:
            lines.extend(_Line(it, "", None, key) for it in group)
        elif len(group) > 1:
            when = _fmt_run([e.date for e in group])
            if weekly:
                seen: set[date] = set()
                for e in group:
                    if _week_monday(e.date) not in seen:
                        seen.add(_week_monday(e.date))
                        lines.append(_Line(first, when, e.date, key))
            else:
                lines.append(_Line(first, when, first.date, key))
        elif _spans_days(first.date, first.end_time):
            spans.append((key, first))
        else:
            lines.append(_Line(first, _fmt_when(first.date, first.end_time), first.date, key))

    now = datetime.now(LOCAL_TZ)
    this_monday = _week_monday(now)
    horizon = max((_week_monday(l.at) for l in lines if l.at), default=this_monday)
    for key, item in spans:
        when = _fmt_when(item.date, item.end_time)
        start = max(_local(item.date), now)
        if not weekly:
            lines.append(_Line(item, when, start, key))
            continue
        monday, last = _week_monday(start), min(_week_monday(item.end_time), max(horizon, this_monday))
        lines.append(_Line(item, when, start, key))
        monday += timedelta(days=7)
        while monday <= last:
            # Sorts at the start of each later week it covers.
            lines.append(_Line(item, when, datetime.combine(monday, datetime.min.time(), LOCAL_TZ), key))
            monday += timedelta(days=7)
    return lines


def _event_line(line: _Line, options: DigestOptions) -> str:
    """One compact markdown line for an event (renders links — used in descriptions)."""
    item = line.item
    title = item.title.strip() or "Untitled"
    head = f"**[{title}]({item.url})**" if item.url else f"**{title}**"
    bits: list[str] = []
    if line.when:
        bits.append(line.when)
    if options.show_location:
        loc = _location_str(item)
        if loc:
            bits.append(loc)
    if options.show_price and item.price:
        bits.append(item.price)
    if options.show_distance and item.distance_bucket:
        bits.append(_bucket_label(item.distance_bucket))
    detail = " · ".join(bits)
    return f"{head}\n{detail}" if detail else head


def _sort_lines(lines: list[_Line], options: DigestOptions) -> None:
    """Sort lines in place per options.sort."""
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    if options.sort == "distance":
        lines.sort(key=lambda l: (_BUCKET_RANK.get(l.item.distance_bucket, 4), l.at or far_future))
    else:  # date
        lines.sort(key=lambda l: l.at or far_future)


def _week_bucket(dt: datetime, now_local: datetime) -> tuple[date, str]:
    """Map an event datetime to (week_monday, label) relative to now.

    Labels: "This Week", "Next Week", then "Week of <Mon D>" for later weeks.
    """
    d = _local(dt).date()
    monday = d - timedelta(days=d.weekday())
    now_monday = now_local.date() - timedelta(days=now_local.weekday())
    delta_weeks = (monday - now_monday).days // 7
    if delta_weeks <= 0:
        return monday, "This Week"
    if delta_weeks == 1:
        return monday, "Next Week"
    return monday, f"Week of {monday.strftime('%b %-d')}"


def _group_by_week(lines: list[_Line]) -> list[tuple[str, list[_Line]]]:
    """Group lines into chronological week sections; undated last."""
    now_local = datetime.now(LOCAL_TZ)
    groups: dict[str, list[_Line]] = {}
    order: dict[str, date] = {}
    for line in lines:
        if line.at:
            monday, label = _week_bucket(line.at, now_local)
        else:
            monday, label = date.max, "Date TBD"
        groups.setdefault(label, []).append(line)
        order[label] = monday
    return sorted(groups.items(), key=lambda kv: order[kv[0]])


def _group_by_day(lines: list[_Line]) -> list[tuple[str, list[_Line]]]:
    """Group lines by calendar day; undated last."""
    groups: dict[str, list[_Line]] = {}
    order: dict[str, date] = {}
    for line in lines:
        if line.at:
            d = _local(line.at).date()
            label = _fmt_day(d)
        else:
            d, label = date.max, "Date TBD"
        groups.setdefault(label, []).append(line)
        order[label] = d
    return sorted(groups.items(), key=lambda kv: order[kv[0]])


def _group_by_distance(lines: list[_Line]) -> list[tuple[str, list[_Line]]]:
    """Group lines by distance bucket, closest first."""
    groups: dict[str | None, list[_Line]] = {}
    for line in lines:
        groups.setdefault(line.item.distance_bucket, []).append(line)
    ordered = sorted(groups.items(), key=lambda kv: _BUCKET_RANK.get(kv[0], 4))
    return [(_bucket_label(label) if label else "Unknown distance", lst) for label, lst in ordered]


def _build_sections(
    items: list[EventItem], options: DigestOptions
) -> list[tuple[str, list[_Line]]]:
    """Merge series into lines, group them into labelled sections per options.group_by,
    then sort within."""
    lines = _event_lines(items, options)
    if options.group_by == "day":
        sections = _group_by_day(lines)
    elif options.group_by == "distance":
        sections = _group_by_distance(lines)
    elif options.group_by == "flat":
        sections = [("All", lines)]
    else:  # week (default / fallback)
        sections = _group_by_week(lines)
    for _, lst in sections:
        _sort_lines(lst, options)
    return sections


def _index_embed(
    sections: list[tuple[str, list]],
    color: int,
    title: str,
    dropped: int = 0,
    urls: Optional[list[str]] = None,
    total: Optional[int] = None,
) -> discord.Embed:
    """A leading 'table of contents' embed: total + per-section counts.

    When urls is provided (parallel to sections), each section becomes a clickable
    jump link to that section's message. `total` overrides the sum of section sizes
    (a series listed in several weeks counts once).
    """
    if total is None:
        total = sum(len(items) for _, items in sections)
    lines = [f"**{total} event{'s' if total != 1 else ''}** this digest", ""]
    for idx, (label, items) in enumerate(sections):
        text = f"📅 **{label}** — {len(items)}"
        if urls and idx < len(urls) and urls[idx]:
            text = f"[{text}]({urls[idx]})"
        lines.append(text)
    if dropped:
        lines.append("")
        lines.append(f"_+{dropped} more not shown (per-channel cap)_")
    embed = discord.Embed(title=f"📑 {title}", description="\n".join(lines), color=color)
    _stamp_footer(embed)
    return embed



def _stamp_footer(embed: discord.Embed) -> None:
    """Set an 'Updated' footer using the embed's native timestamp.

    Discord does NOT render <t:...> timestamp markdown inside footers (only in
    descriptions/fields), so use the embed timestamp, which it renders natively.
    """
    embed.set_footer(text="Updated")
    embed.timestamp = datetime.now(timezone.utc)


def _empty_embed(label: str, color: int = _EMPTY_COLOR) -> discord.Embed:
    embed = discord.Embed(
        title=f"{label} — Nothing scheduled right now",
        description="Check back soon!",
        color=color,
    )
    _stamp_footer(embed)
    return embed


# ── Public formatters ─────────────────────────────────────────────────────────


def _apply_cap(items: list[EventItem], options: DigestOptions) -> tuple[list[EventItem], int]:
    """Truncate to the soonest max_per_channel events (0 = unlimited). Returns (kept, dropped)."""
    if options.max_per_channel and len(items) > options.max_per_channel:
        ordered = sorted(items, key=lambda e: e.date or datetime.max.replace(tzinfo=timezone.utc))
        kept = ordered[: options.max_per_channel]
        return kept, len(items) - len(kept)
    return items, 0


@dataclass
class RenderedDigest:
    """A grouped digest ready to post: embeds + section→embed mapping for linking."""
    embeds: list[discord.Embed]
    sections: list[tuple[str, list]]
    index_pos: Optional[int]        # embed index of the index embed, or None
    section_starts: list[int]       # embed index of each section's first embed (parallel to sections)
    dropped: int = 0
    total: Optional[int] = None     # distinct events/series (None = sum of section sizes)


def _embeds_from_lines(lines: list[str], title: str, color: int) -> list[discord.Embed]:
    """Pack blank-line-separated entries into as few embeds as fit; later ones titled '(cont.)'."""
    _DESC_LIMIT = 3800
    embeds: list[discord.Embed] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        suffix = " (cont.)" if embeds else ""
        e = discord.Embed(title=f"{title}{suffix}", description="\n\n".join(buf), color=color)
        _stamp_footer(e)
        embeds.append(e)
        buf = []
        buf_len = 0

    for line in lines:
        if buf_len + len(line) + 2 > _DESC_LIMIT:
            flush()
        buf.append(line)
        buf_len += len(line) + 2
    flush()
    return embeds


def _section_embeds(
    label: str, lines: list[_Line], color: int, base_title: str, options: DigestOptions
) -> list[discord.Embed]:
    """One or more embeds for a single section, titled '<base> — <label>'."""
    return _embeds_from_lines([_event_line(line, options) for line in lines], f"{base_title} — {label}", color)


def build_grouped_digest(
    items: list[EventItem], title: str, color: int, options: DigestOptions
) -> RenderedDigest:
    """Build a grouped digest with one (or more) embed per section for jump-linking."""
    items = [i for i in dedupe_events(items) if event_is_active(i)]
    if not items:
        return RenderedDigest([_empty_embed(title, color)], [], None, [])

    items, dropped = _apply_cap(items, options)
    sections = _build_sections(items, options)
    total = len({line.key for _, lines in sections for line in lines})

    embeds: list[discord.Embed] = []
    index_pos: Optional[int] = None
    index_embed = (
        _index_embed(sections, color, title, dropped=dropped, total=total) if options.show_index else None
    )

    # Index at top: prepend it before the sections.
    if index_embed is not None and options.index_position == "top":
        index_pos = 0
        embeds.append(index_embed)

    section_starts: list[int] = []
    for label, sec_items in sections:
        section_starts.append(len(embeds))
        embeds.extend(_section_embeds(label, sec_items, color, title, options))

    # Index at bottom (default): append it after the sections so it's the last,
    # always-visible message in the channel.
    if index_embed is not None and options.index_position != "top":
        index_pos = len(embeds)
        embeds.append(index_embed)

    return RenderedDigest(embeds, sections, index_pos, section_starts, dropped, total)


# ── PennDOT layout ────────────────────────────────────────────────────────────


def _penndot_line(entry: "RcrsEntry") -> str:
    """One PennDOT entry: bold status + road, the description(s), then when/extras."""
    esc = discord.utils.escape_markdown
    head = f"**{esc(entry.status.upper())} · {esc(entry.road or '(no road named)')}**"
    if entry.miles is not None:
        head += f" · {entry.miles:.1f} mi from {esc(config.CENTER_LOCATION['name'])}"
    lines = [head, *(esc(d) for d in entry.descriptions)]

    bits: list[str] = []
    if entry.start is not None:
        verb = "Starts" if entry.feed == "planned" else "Since"
        bits.append(f"{verb} {_fmt_moment(entry.start)}")
    if entry.updated is not None:
        bits.append(f"updated {_fmt_moment(entry.updated)}")
    if entry.detour:
        bits.append("⚠️ detour in effect")
    if entry.lanes:
        bits.append(f"lanes: {esc('; '.join(entry.lanes))}")
    if entry.speeds:
        bits.append(f"speed: {esc('; '.join(entry.speeds))}")
    if bits:
        lines.append(" · ".join(bits))
    lines.extend(f"🔁 {esc(s)}" for s in entry.schedules)
    return "\n".join(lines)


def build_penndot_digest(
    sections: "PenndotSections", title: str, color: int, options: DigestOptions, now: datetime
) -> RenderedDigest:
    """#traffic-alerts: Live now / Ongoing / Upcoming sections plus the standard index embed.

    Every section is always present (an empty one says so) so the message layout stays
    stable and refreshes edit in place. The freshness + scope note heads "Live now".
    Index placement and jump links follow the same DigestOptions as the other digests.
    """
    center = config.CENTER_LOCATION["name"]
    near = f"within {config.PENNDOT_WORK_MAX_MILES:g} mi of {center}"
    intro = (f"🕒 Updated {_fmt_moment(now)} · refreshes every 15 min\n"
             f"Incidents within {config.PENNDOT_INCIDENT_MAX_MILES:g} mi of {center}, plus road work "
             f"within {config.PENNDOT_WORK_MAX_MILES:g} mi that started in the last "
             f"{config.PENNDOT_LIVE_NEW_HOURS}h.")
    named = [
        ("Live now", sections.live, [intro]),
        (f"Ongoing work {near}", sections.ongoing, []),
        (f"Upcoming {near} — next {config.PENNDOT_PLANNED_LOOKAHEAD_DAYS} days", sections.upcoming, []),
    ]
    index_sections = [(label, items) for label, items, _ in named]
    index_embed = _index_embed(index_sections, color, title) if options.show_index else None

    embeds: list[discord.Embed] = []
    index_pos: Optional[int] = None
    if index_embed is not None and options.index_position == "top":
        index_pos = 0
        embeds.append(index_embed)
    section_starts: list[int] = []
    for label, items, lead in named:
        section_starts.append(len(embeds))
        lines = lead + ([_penndot_line(e) for e in items] or ["_Nothing right now._"])
        embeds.extend(_embeds_from_lines(lines, f"{title} — {label}", color))
    if index_embed is not None and options.index_position != "top":
        index_pos = len(embeds)
        embeds.append(index_embed)
    return RenderedDigest(embeds, index_sections, index_pos, section_starts)


async def post_penndot_channel(
    channel: discord.TextChannel,
    sections: "PenndotSections",
    stored_message_id: Optional[int],
    title: str,
    options: Optional[DigestOptions] = None,
) -> int:
    """Edit #traffic-alerts in place (reposts only if its embed count changed), then add jump links."""
    options = options or _DEFAULT_OPTIONS
    render = build_penndot_digest(sections, title, _LAYOUT_COLORS["penndot"], options, datetime.now(timezone.utc))
    messages = await _reconcile_messages(channel, stored_message_id, render.embeds)
    await _apply_navigation(channel, render, messages, options)
    return messages[0].id


# ── Post / edit orchestration ─────────────────────────────────────────────────


# digest_key → (display title, layout), derived from the canonical channel definitions.
# All layouts ("events" / "music" / "closures") render through the same configurable
# grouped layout; the layout only selects an accent colour and a default title.
# Grouping/sort/visibility/index/navigation all come from DigestOptions, uniformly.
_DIGEST_META: dict[str, tuple[str, str]] = {
    d["key"]: (d["title"], d["layout"]) for d in config.CHANNEL_DEFINITIONS
}

# Per-layout accent colour and fallback title (used when a channel has no _DIGEST_META).
_LAYOUT_COLORS: dict[str, int] = {
    "music":    0xE91E63,
    "closures": 0xE74C3C,
    "events":   _BUCKET_COLORS["< 1 mi"],
    "penndot":  0xF39C12,
}
_LAYOUT_DEFAULT_TITLES: dict[str, str] = {
    "music":    "Philly Live Music",
    "closures": "Road Closures & Transit Alerts",
    "events":   "Philly Events",
}

# Lookahead window (days) backing each layout — mirrors the source-type windows in
# config / scraper._apply_lookahead. Channels whose window is a single week or less
# are grouped by DAY (see post_or_edit_digest): week grouping would put everything in
# one "This Week" bucket, so a per-day index is the useful one.
_LAYOUT_LOOKAHEAD_DAYS: dict[str, int] = {
    "music":    config.MUSIC_LOOKAHEAD_DAYS,
    "closures": config.CLOSURES_LOOKAHEAD_DAYS,
    "events":   config.EVENTS_LOOKAHEAD_DAYS,
}
# A lookahead at or under this many days uses day grouping instead of week.
_SINGLE_WEEK_DAYS = 7


async def _reconcile_messages(
    channel: discord.TextChannel,
    stored_message_id: Optional[int],
    embeds: list[discord.Embed],
) -> list[discord.Message]:
    """Make the channel hold exactly `embeds` (one per message), in order.

    Edits in place when the existing bot-message count matches; otherwise deletes
    the existing run and posts fresh. Returns the messages parallel to embeds.
    """
    existing: list[discord.Message] = []
    if stored_message_id is not None:
        try:
            primary = await channel.fetch_message(stored_message_id)
            existing.append(primary)
            bot_user = channel.guild.me
            async for msg in channel.history(after=primary, limit=50):
                if msg.author == bot_user:
                    existing.append(msg)
                else:
                    break
        except discord.NotFound:
            logger.warning("Stored message %d not found in #%s — posting fresh", stored_message_id, channel.name)
        except discord.HTTPException as exc:
            logger.error("Failed to fetch stored message %d: %s", stored_message_id, exc)

    if existing and len(existing) == len(embeds):
        for msg, embed in zip(existing, embeds):
            await msg.edit(content=None, embeds=[embed])
        return existing

    # Mismatch (or fresh): delete the old run and post anew.
    for msg in existing:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
    return [await channel.send(embeds=[embed]) for embed in embeds]


async def _apply_navigation(
    channel: discord.TextChannel,
    render: RenderedDigest,
    messages: list[discord.Message],
    options: DigestOptions,
) -> None:
    """Second pass: link the index to each section message and each section back.

    Requires message IDs (known only after posting), so the affected messages are
    edited once more with the cross-links injected.
    """
    if render.index_pos is None or not options.jump_links or channel.guild is None:
        return
    if not render.section_starts:
        return

    base = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/"
    index_url = base + str(messages[render.index_pos].id)
    section_urls = [base + str(messages[s].id) for s in render.section_starts]

    # Arrow points toward the index (down if it's at the bottom, up if at the top).
    arrow = "↓" if options.index_position != "top" else "↑"
    back_link = f"[{arrow} Index]({index_url})"

    edited: set[int] = set()
    # Back-link on each section's first embed.
    for emb_idx in render.section_starts:
        embed = render.embeds[emb_idx]
        desc = embed.description or ""
        if not (desc.startswith("[↑") or desc.startswith("[↓")):
            embed.description = f"{back_link}\n\n{desc}"
            edited.add(emb_idx)
    # Jump links on the index.
    color = render.embeds[render.index_pos].color
    title = (render.embeds[render.index_pos].title or "📑").removeprefix("📑 ")
    render.embeds[render.index_pos] = _index_embed(
        render.sections, color.value if color else 0, title, dropped=render.dropped, urls=section_urls,
        total=render.total,
    )
    edited.add(render.index_pos)

    for i in sorted(edited):
        try:
            await messages[i].edit(embeds=[render.embeds[i]])
        except discord.HTTPException as exc:
            logger.warning("Navigation edit failed for message %d: %s", messages[i].id, exc)


async def post_or_edit_digest(
    channel: discord.TextChannel,
    items: list[EventItem],
    stored_message_id: Optional[int],
    digest_key: Optional[str] = None,
    options: Optional[DigestOptions] = None,
) -> int:
    """Post or edit-in-place the digest. Returns the primary message ID to persist."""
    options = options or _DEFAULT_OPTIONS
    title, layout = _DIGEST_META.get(digest_key, (None, None))
    fallback_type = items[0].channel_type if items else "events"
    if layout is None:
        layout = fallback_type if fallback_type in ("music", "closures") else "events"
    channel_type = digest_key or fallback_type

    # Channels with a single-week (≤ 7-day) lookahead are always grouped by DAY: a
    # week index would collapse the whole window into one "This Week" bucket, so the
    # per-day index is the useful one. This overrides the global digest_group_by for
    # those channels only. replace() (not mutation) keeps the shared options intact.
    lookahead_days = _LAYOUT_LOOKAHEAD_DAYS.get(layout, config.EVENTS_LOOKAHEAD_DAYS)
    if lookahead_days <= _SINGLE_WEEK_DAYS and options.group_by != "day":
        options = replace(options, group_by="day")

    # All channels (events / music / closures) use the same grouped layout, so the
    # index, jump-navigation, grouping, sort, and visibility options apply uniformly.
    # Per-layout colour + default title are the only differences.
    color = _LAYOUT_COLORS.get(layout, _BUCKET_COLORS["< 1 mi"])
    default_title = _LAYOUT_DEFAULT_TITLES.get(layout, "Philly Events")
    render = build_grouped_digest(items, title or default_title, color, options)
    logger.info(
        "[%s] %d embed(s), %d section(s), index=%s",
        channel_type, len(render.embeds), len(render.section_starts), render.index_pos is not None,
    )
    messages = await _reconcile_messages(channel, stored_message_id, render.embeds)
    await _apply_navigation(channel, render, messages, options)
    return messages[0].id
