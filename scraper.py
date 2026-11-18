"""Per-source scrape/fetch logic. Returns normalized EventItem instances."""

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup

import config
from geocoder import geocode, distance_bucket

logger = logging.getLogger(__name__)


@dataclass
class EventItem:
    id: str
    title: str
    date: datetime
    end_time: Optional[datetime]
    location_name: Optional[str]
    address: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    distance_bucket: Optional[str]
    price: Optional[str]
    url: str
    source: str
    channel_type: str
    first_seen: datetime
    is_new: bool = True


def _make_id(title: str, date: datetime) -> str:
    raw = f"{title.lower().strip()}|{date.date().isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def event_is_active(item: "EventItem", now: Optional[datetime] = None) -> bool:
    """True if an event is current/future, or a still-ongoing multi-day period.

    An event is kept when its start is now/future, OR (for events that span a
    period) its end_time is now/future. Fully-past single events are dropped.
    Undated events (no date and no end) are kept.
    """
    now = now or datetime.now(timezone.utc)
    # Reference point: the period end if present, else the start date.
    ref = item.end_time or item.date
    if ref is None:
        return True
    ref = as_utc(ref)
    if is_date_only(ref):
        ref += timedelta(days=1)  # a date with no time runs through the end of that day
    return ref >= now


LOCAL_TZ = ZoneInfo(config.TIMEZONE)


def as_utc(dt: datetime) -> datetime:
    """Aware UTC datetime. A naive one is Philadelphia wall-clock time, which is what
    the local calendars and the discovery model report."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def is_date_only(dt: datetime) -> bool:
    """True for exactly local midnight: the convention for "a date with no known time"
    (sources that give only a date are stored as local midnight)."""
    return as_utc(dt).astimezone(LOCAL_TZ).time() == time(0)


_SENTINEL = object()

# Philadelphia city bounding box — used to reject Google results that fall
# outside city limits (e.g. "Chestnut Street" resolving to Collingdale).
_PHL_LAT_MIN, _PHL_LAT_MAX = 39.867, 40.138
_PHL_LON_MIN, _PHL_LON_MAX = -75.280, -74.956

# Cache geocoding results for the lifetime of the process.
# Lock serializes all geocoding requests so concurrent sources don't race past
# the cache check and flood Nominatim before any result is stored.
_GEOCODE_CACHE: dict[str, Optional[tuple[float, float]]] = {}
_GEOCODE_LOCK: Optional[asyncio.Lock] = None


def _get_geocode_lock() -> asyncio.Lock:
    global _GEOCODE_LOCK
    if _GEOCODE_LOCK is None:
        _GEOCODE_LOCK = asyncio.Lock()
    return _GEOCODE_LOCK


def _in_philadelphia(lat: float, lon: float) -> bool:
    return (_PHL_LAT_MIN <= lat <= _PHL_LAT_MAX and
            _PHL_LON_MIN <= lon <= _PHL_LON_MAX)


async def _geocode_google(
    query: str, session: aiohttp.ClientSession
) -> Optional[tuple[float, float]]:
    """Geocode via Google Maps API. Returns None if result is outside Philadelphia."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": query,
        "bounds": f"{_PHL_LAT_MIN},{_PHL_LON_MIN}|{_PHL_LAT_MAX},{_PHL_LON_MAX}",
        "key": api_key,
    }
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        if data.get("status") != "OK" or not data.get("results"):
            return None
        loc = data["results"][0]["geometry"]["location"]
        lat, lon = float(loc["lat"]), float(loc["lng"])
        if not _in_philadelphia(lat, lon):
            logger.debug("google geocode: %r resolved outside Philadelphia (%s,%s)", query, lat, lon)
            return None
        return lat, lon
    except Exception as exc:
        logger.warning("google geocode failed for %r: %s", query, exc)
        return None

async def _fetch_html(url: str, session: aiohttp.ClientSession, ssl=_SENTINEL) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; philly-discord-bot/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=20)}
    if ssl is not _SENTINEL:
        kwargs["ssl"] = ssl
    try:
        async with session.get(url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.text()
    except Exception as exc:
        logger.error("HTTP fetch failed for %s: %s", url, exc)
        return None


async def _fetch_json(url: str, session: aiohttp.ClientSession, params: Optional[dict] = None) -> Optional[dict | list]:
    headers = {"User-Agent": "philly-discord-bot/1.0"}
    try:
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if not resp.ok:
                body = await resp.text()
                logger.error("JSON fetch failed for %s: %s — %s", url, resp.status, body[:300])
                return None
            return await resp.json(content_type=None)
    except Exception as exc:
        logger.error("JSON fetch failed for %s: %s", url, exc)
        return None


async def _geocode_event(ev: EventItem, session: aiohttp.ClientSession) -> None:
    """Populate lat/lon and distance_bucket in-place if address is known.

    Uses a process-lifetime cache + a lock to guarantee that concurrent sources
    running in asyncio.gather never send the same query to Nominatim more than
    once, and never fire multiple requests simultaneously (which causes 429s
    even when they're for different addresses, because Nominatim's per-IP rate
    limit is 1 req/sec across all concurrent callers).
    """
    def _with_philly(s: str) -> str:
        low = s.lower()
        if "philadelphia" in low or ", pa" in low:
            return s  # already has city/state — don't double-append
        return f"{s}, Philadelphia, PA"

    query = ""
    if ev.address and ev.lat is None:
        query = _with_philly(ev.address)
    elif ev.location_name and ev.lat is None:
        query = _with_philly(ev.location_name)
    if not query:
        if ev.lat is not None and ev.lon is not None:
            ev.distance_bucket = distance_bucket(ev.lat, ev.lon)
        return
    # Fast path: already cached — no lock needed.
    if query in _GEOCODE_CACHE:
        result = _GEOCODE_CACHE[query]
    else:
        # Slow path: acquire lock so only one geocoder is called at a time.
        async with _get_geocode_lock():
            # Re-check after acquiring — another coroutine may have filled it.
            if query not in _GEOCODE_CACHE:
                # Try Google Maps first (handles venue names, bare street names,
                # intersections); fall back to Nominatim if unavailable or fails.
                result = await _geocode_google(query, session)
                if result is None:
                    result = await geocode(query, session)
                _GEOCODE_CACHE[query] = result
        result = _GEOCODE_CACHE[query]
    if result:
        ev.lat, ev.lon = result
    if ev.lat is not None and ev.lon is not None:
        ev.distance_bucket = distance_bucket(ev.lat, ev.lon)


# ---------------------------------------------------------------------------
# Source-specific scrapers
# ---------------------------------------------------------------------------

async def _scrape_visitphilly(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    # VisitPhilly's own RSS feeds are empty; their events are AJAX-loaded.
    # Fall back to PhillyMag things-to-do RSS which is publicly accessible.
    import random as _random
    from email.utils import parsedate_to_datetime
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    rss_candidates = [
        "https://www.visitphilly.com/events/feed/",
        "https://www.visitphilly.com/feed/",
        "https://www.phillymag.com/things-to-do/feed/",
    ]
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    html: Optional[str] = None
    used_url = source["url"]
    for rss_url in rss_candidates:
        headers["Referer"] = rss_url.split("/")[0] + "//" + rss_url.split("/")[2] + "/"
        try:
            async with session.get(
                rss_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    if text.count("<item>") > 0:
                        html = text
                        used_url = rss_url
                        break
        except Exception as exc:
            logger.warning("visitphilly RSS attempt %s failed: %s", rss_url, exc)
    if not html:
        logger.warning("visitphilly: all RSS feeds empty or failed")
        return []
    soup = BeautifulSoup(html, "xml")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    for item in soup.select("item"):
        try:
            title_el = item.find("title")
            if not title_el:
                continue
            title = BeautifulSoup(title_el.get_text(strip=True), "html.parser").get_text(strip=True)
            if not title:
                continue
            link_el = item.find("link")
            url = link_el.get_text(strip=True) if link_el else source["url"]
            pub_el = item.find("pubDate")
            if not pub_el:
                continue
            try:
                event_date = parsedate_to_datetime(pub_el.get_text(strip=True))
            except Exception:
                continue
            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=None,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            await _geocode_event(ev, session)
            items.append(ev)
        except Exception as exc:
            logger.warning("visitphilly RSS item parse error: %s", exc)
    logger.info("visitphilly: %d items (via %s)", len(items), used_url)
    return items


_DO215_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://do215.com/",
    "DNT": "1",
}


async def _fetch_do215(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.get(
            url,
            headers=_DO215_HEADERS,
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            return await resp.text()
    except Exception as exc:
        logger.error("Do215 fetch failed for %s: %s", url, exc)
        return None


async def _scrape_do215(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    # do215.com serves full schema.org Event microdata on /events/ page.
    # RSS (/feed/) returns HTML — not an RSS feed. Microdata is the correct surface.
    import random as _random
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    events_url = "https://do215.com/events/"
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://do215.com/",
    }
    html: Optional[str] = None
    try:
        async with session.get(
            events_url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                html = await resp.text(errors="replace")
            else:
                logger.warning("do215 events page status %s", resp.status)
                return []
    except Exception as exc:
        logger.error("do215 fetch failed: %s", exc)
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    for card in soup.select('[itemtype="http://schema.org/Event"]'):
        try:
            name_el = card.select_one('[itemprop="name"]')
            if not name_el:
                continue
            title = name_el.get_text(strip=True)
            if not title:
                continue
            url_el = card.select_one('a[itemprop="url"]')
            href = url_el["href"] if url_el else ""
            event_url = ("https://do215.com" + href if href.startswith("/") else href) or events_url
            date_el = card.select_one('[itemprop="startDate"]')
            if not date_el:
                continue
            date_str = date_el.get("content") or date_el.get("datetime") or date_el.get_text(strip=True)
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(date_str)
            except ValueError:
                continue
            loc_el = card.select_one('[itemprop="location"] [itemprop="name"]')
            location = loc_el.get_text(strip=True) if loc_el else None
            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=event_url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            await _geocode_event(ev, session)
            items.append(ev)
        except Exception as exc:
            logger.warning("do215 event parse error: %s", exc)
    logger.info("do215: %d items from schema.org microdata", len(items))
    return items


def _parse_do215_rss(xml: str, source: dict) -> list[EventItem]:
    now = datetime.now(timezone.utc)
    soup = BeautifulSoup(xml, "xml")
    items: list[EventItem] = []
    for item in soup.select("item"):
        try:
            title = item.find("title")
            if not title:
                continue
            title_text = title.get_text(strip=True)
            link = item.find("link")
            url = link.get_text(strip=True) if link else source["url"]
            pub_date = item.find("pubDate")
            if not pub_date:
                continue
            from email.utils import parsedate_to_datetime
            try:
                event_date = parsedate_to_datetime(pub_date.get_text(strip=True))
            except Exception:
                continue
            ev = EventItem(
                id=_make_id(title_text, event_date),
                title=title_text,
                date=event_date,
                end_time=None,
                location_name=None,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            items.append(ev)
        except Exception as exc:
            logger.warning("do215 rss item parse error: %s", exc)
    return items


def _parse_do215_html(html: str, source: dict) -> list[EventItem]:
    now = datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    items: list[EventItem] = []
    for card in soup.select("article, .event, .event-listing, [class*='event']"):
        try:
            title_el = card.select_one("h2, h3, h4, .event-name, a.event-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            link_el = card.select_one("a[href]")
            url = link_el["href"] if link_el else source["url"]
            if url.startswith("/"):
                url = "https://do215.com" + url

            date_el = card.select_one("time[datetime], .date, .event-date, [class*='date']")
            if not date_el:
                continue
            date_str = date_el.get("datetime") or date_el.get_text(strip=True)
            try:
                event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            venue_el = card.select_one(".venue, .location, [class*='venue']")
            location = venue_el.get_text(strip=True) if venue_el else None

            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            items.append(ev)
        except Exception as exc:
            logger.warning("do215 html card parse error: %s", exc)
    return items


async def _scrape_songkick(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    # Songkick renders via JS; use their undocumented JSON endpoint instead
    metro_id = "28563"  # Philadelphia metro area
    api_url = f"https://www.songkick.com/metro_areas/{metro_id}/calendar.json"
    data = await _fetch_json(api_url, session)
    if not isinstance(data, dict):
        # Fallback: try ical feed which is plain text and not JS-gated
        logger.warning("songkick: JSON endpoint failed, trying iCal fallback")
        return await _scrape_songkick_ical(source, session)

    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    events = (data.get("resultsPage", {})
                  .get("results", {})
                  .get("calendarEntry", []))
    for entry in events:
        try:
            event = entry.get("event", {})
            title = event.get("displayName", "")
            if not title:
                continue
            url = event.get("uri", source["url"])
            start = event.get("start", {})
            date_str = start.get("datetime") or start.get("date")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            venue = event.get("venue", {})
            location = venue.get("displayName")
            lat = venue.get("lat")
            lon = venue.get("lng")
            db = None
            if lat and lon:
                db = distance_bucket(float(lat), float(lon))
            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=None,
                lat=float(lat) if lat else None,
                lon=float(lon) if lon else None,
                distance_bucket=db,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            items.append(ev)
        except Exception as exc:
            logger.warning("songkick entry parse error: %s", exc)
    return items


async def _scrape_songkick_ical(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """iCal fallback for Songkick — plain text, no JS."""
    metro_id = "28563"
    ical_url = f"https://www.songkick.com/metro_areas/{metro_id}/calendar.ics"
    html = await _fetch_html(ical_url, session)
    if not html:
        return []
    logger.warning("songkick ical snippet: %s", html[:400])
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    current: dict = {}
    for line in html.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            try:
                title = current.get("SUMMARY", "")
                date_str = current.get("DTSTART", "")
                url = current.get("URL", source["url"])
                if not title or not date_str:
                    continue
                # iCal date format: 20240615T190000Z or 20240615
                try:
                    if "T" in date_str:
                        event_date = datetime.strptime(date_str.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                    else:
                        event_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                location = current.get("LOCATION")
                ev = EventItem(
                    id=_make_id(title, event_date),
                    title=title,
                    date=event_date,
                    end_time=None,
                    location_name=location,
                    address=None,
                    lat=None,
                    lon=None,
                    distance_bucket=None,
                    price=None,
                    url=url,
                    source=source["name"],
                    channel_type=source["channel"],
                    first_seen=now,
                )
                items.append(ev)
            except Exception as exc:
                logger.warning("songkick ical event parse error: %s", exc)
        elif ":" in line:
            key, _, val = line.partition(":")
            current[key.split(";")[0]] = val
    return items


async def _scrape_bandsintown(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    html = await _fetch_html(source["url"], session)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)

    for card in soup.select("[data-event-id], .event-card, article.event"):
        try:
            title_el = card.select_one("h3, h2, .artist-name, .event-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            link_el = card.select_one("a[href]")
            url = link_el["href"] if link_el else source["url"]

            date_el = card.select_one("time[datetime], [data-date]")
            if not date_el:
                continue
            date_str = date_el.get("datetime") or date_el.get("data-date", "")
            try:
                event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            venue_el = card.select_one(".venue, .location, [data-venue]")
            location = venue_el.get_text(strip=True) if venue_el else None

            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            await _geocode_event(ev, session)
            items.append(ev)
        except Exception as exc:
            logger.warning("bandsintown card parse error: %s", exc)
    return items


async def _scrape_wxpn(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    # thekey.xpn.org has a Fastly CDN TLS SAN mismatch — returns 421 or routing error.
    # xpn.org redirects to xpn.org which is a broken Next.js app (__next_error__).
    # Try a cascade of known RSS/feed URLs across both domains; skip those that fail.
    import random as _random, ssl as _ssl
    from email.utils import parsedate_to_datetime
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    rss_candidates = [
        "https://xpn.org/feed/",
        "https://xpn.org/category/music/feed/",
        "https://xpn.org/category/the-key/feed/",
        "https://thekey.xpn.org/feed/",
        "https://thekey.xpn.org/category/show-previews/feed/",
    ]
    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE
    ssl_ctx.set_alpn_protocols(["http/1.1"])
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, force_close=True)
    html: Optional[str] = None
    used_url = source["url"]
    async with aiohttp.ClientSession(connector=connector) as _s:
        for rss_url in rss_candidates:
            headers = {
                "User-Agent": _random.choice(_UA),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://xpn.org/",
            }
            try:
                async with _s.get(
                    rss_url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="replace")
                        if text.count("<item>") > 0:
                            html = text
                            used_url = rss_url
                            break
            except Exception as exc:
                logger.debug("wxpn RSS attempt %s failed: %s", rss_url, exc)
    if not html:
        logger.warning("wxpn: all RSS feeds failed or empty")
        return []
    soup = BeautifulSoup(html, "xml")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    for item in soup.select("item"):
        try:
            title_el = item.find("title")
            if not title_el:
                continue
            title = BeautifulSoup(title_el.get_text(strip=True), "html.parser").get_text(strip=True)
            if not title:
                continue
            link_el = item.find("link")
            url = link_el.get_text(strip=True) if link_el else source["url"]
            pub_el = item.find("pubDate")
            if not pub_el:
                continue
            try:
                event_date = parsedate_to_datetime(pub_el.get_text(strip=True))
            except Exception:
                continue
            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=None,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            items.append(ev)
        except Exception as exc:
            logger.warning("wxpn RSS item parse error: %s", exc)
    logger.info("wxpn: %d items (via %s)", len(items), used_url)
    return items


async def _scrape_eventbrite(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    html = await _fetch_html(source["url"], session)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)

    for card in soup.select("[data-event-id], .eds-event-card, article"):
        try:
            title_el = card.select_one(".eds-event-card__formatted-name, h3, h2")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            link_el = card.select_one("a[href*='eventbrite.com/e/']")
            url = link_el["href"] if link_el else source["url"]

            date_el = card.select_one("time[datetime], .eds-text-bm")
            if not date_el:
                continue
            date_str = date_el.get("datetime", "")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            venue_el = card.select_one(".card-text--where, .eds-event-card-content__sub")
            location = venue_el.get_text(strip=True) if venue_el else None

            price_el = card.select_one(".eds-event-card-content__sub--price, .is-hidden-mobile")
            price = price_el.get_text(strip=True) if price_el else None

            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=price,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            await _geocode_event(ev, session)
            items.append(ev)
        except Exception as exc:
            logger.warning("eventbrite card parse error: %s", exc)
    return items


# ---------------------------------------------------------------------------
# API-based sources
# ---------------------------------------------------------------------------

async def _fetch_septa(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    data = await _fetch_json(source["url"], session)
    if not data:
        return []
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)

    alert_list = data if isinstance(data, list) else data.get("alert", [])
    for alert in alert_list:
        try:
            raw_msg = alert.get("advisory_message") or alert.get("title") or "SEPTA Alert"
            # Strip HTML tags that SEPTA includes in advisory_message
            title = BeautifulSoup(raw_msg, "html.parser").get_text(separator=" ", strip=True)
            # Collapse whitespace and truncate
            title = " ".join(title.split())[:200]

            start_str = alert.get("last_updated") or alert.get("post_dt")
            if not start_str:
                event_date = now
            else:
                try:
                    event_date = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
                except ValueError:
                    event_date = now

            route = alert.get("route_id", "")
            route_label = f"SEPTA {route}: " if route else "SEPTA: "
            display_title = f"{route_label}{title}"[:200]
            ev = EventItem(
                id=_make_id(display_title, event_date),
                title=display_title,
                date=event_date,
                end_time=None,
                location_name=f"Route {route}" if route else None,
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url="https://www.septa.org/service-alerts/",
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            items.append(ev)
        except Exception as exc:
            logger.warning("SEPTA alert parse error: %s", exc)
    return items


async def _fetch_phl_streets(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    import random as _random
    _UA = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36', 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1']
    base_url = source["url"]
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://phl.carto.com/",
    }
    # Discover available columns first
    schema_params = {"q": "SELECT * FROM li_permits LIMIT 1", "format": "json"}
    schema: Optional[dict] = None
    try:
        async with session.get(
            base_url, headers=headers, params=schema_params,
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                schema = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("PHL Streets schema probe failed: %s", exc)
    if schema and isinstance(schema, dict):
        cols = list((schema.get("fields") or {}).keys())
        logger.info("li_permits columns: %s", cols)
    # Main query — cast date safely, handle missing columns gracefully
    query = (
        "SELECT typeofwork, descriptionofwork, address, permitissuedate "
        "FROM li_permits "
        "WHERE (LOWER(typeofwork) LIKE '%closure%' "
        "   OR LOWER(descriptionofwork) LIKE '%road closure%' "
        "   OR LOWER(descriptionofwork) LIKE '%street closure%') "
        "AND permitissuedate >= now() - INTERVAL '30 days' "
        "ORDER BY permitissuedate DESC LIMIT 100"
    )
    params = {"q": query, "format": "json"}
    data: Optional[dict] = None
    try:
        async with session.get(
            base_url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
            else:
                body = await resp.text()
                logger.error("PHL Streets query failed %s: %s", resp.status, body[:300])
                return []
    except Exception as exc:
        logger.error("PHL Streets fetch failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("rows", [])
    logger.info("PHL Streets: %d closure permits", len(rows))
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            work = row.get("typeofwork") or ""
            desc = row.get("descriptionofwork") or ""
            title = (f"{work}: {desc}".strip(": ") or "Street Closure")[:200]
            start_str = row.get("permitissuedate")
            if not start_str:
                continue
            try:
                event_date = datetime.fromisoformat(
                    str(start_str).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            location = row.get("address")
            ev = EventItem(
                id=_make_id(title + str(location), event_date),
                title=title,
                date=event_date,
                end_time=None,
                location_name=location,
                address=location,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url="https://phl.carto.com/",
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            await _geocode_event(ev, session)
            items.append(ev)
        except Exception as exc:
            logger.warning("PHL Streets row parse error: %s", exc)
    return items


async def _scrape_ticketmaster(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Ticketmaster Discovery API v2 — free key, Philadelphia music & events."""
    api_key = os.environ.get("TICKETMASTER_API_KEY", "")
    if not api_key:
        logger.warning("TICKETMASTER_API_KEY not set — skipping Ticketmaster source")
        return []
    base = "https://app.ticketmaster.com/discovery/v2/events.json"
    now = datetime.now(timezone.utc)
    items: list[EventItem] = []
    # Fetch each classification in its own pass to stay within rate limits.
    # "sports" added so professional/stadium events (e.g. World Cup matches at
    # Lincoln Financial Field) are captured; routing is by classified category,
    # so these reach sports_fitness / major_events despite the source's channel.
    for classification in ("music", "sports", "arts & theatre"):
        page = 0
        while True:
            params = {
                "apikey": api_key,
                "city": "Philadelphia",
                "stateCode": "PA",
                "classificationName": classification,
                "size": "50",
                "page": str(page),
                "sort": "date,asc",
            }
            data: Optional[dict] = None
            try:
                async with session.get(
                    base, params=params, timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                    elif resp.status == 429:
                        logger.warning("ticketmaster rate-limited on page %d", page)
                        break
                    else:
                        logger.warning("ticketmaster status %s", resp.status)
                        break
            except Exception as exc:
                logger.error("ticketmaster fetch failed: %s", exc)
                break
            if not isinstance(data, dict):
                break
            events = data.get("_embedded", {}).get("events", [])
            if not events:
                break
            for ev_raw in events:
                try:
                    title = ev_raw.get("name", "").strip()
                    if not title:
                        continue
                    url = ev_raw.get("url", source["url"])
                    dates = ev_raw.get("dates", {}).get("start", {})
                    date_str = dates.get("dateTime") or dates.get("localDate")
                    if not date_str:
                        continue
                    try:
                        # dateTime is UTC ("Z"); the localDate fallback (time TBA) is a
                        # bare date, stored as local midnight = date only.
                        event_date = as_utc(datetime.fromisoformat(date_str.replace("Z", "+00:00")))
                    except ValueError:
                        continue
                    venues = ev_raw.get("_embedded", {}).get("venues", [{}])
                    venue = venues[0] if venues else {}
                    location = venue.get("name")
                    address_parts = [
                        venue.get("address", {}).get("line1", ""),
                        venue.get("city", {}).get("name", ""),
                        venue.get("state", {}).get("stateCode", ""),
                    ]
                    address = ", ".join(p for p in address_parts if p) or None
                    lat = venue.get("location", {}).get("latitude")
                    lon = venue.get("location", {}).get("longitude")
                    try:
                        lat = float(lat) if lat else None
                        lon = float(lon) if lon else None
                    except (TypeError, ValueError):
                        lat = lon = None
                    db = None
                    if lat and lon:
                        db = distance_bucket(lat, lon)
                    price_ranges = ev_raw.get("priceRanges")
                    price: Optional[str] = None
                    if price_ranges:
                        pr = price_ranges[0]
                        lo = pr.get("min")
                        hi = pr.get("max")
                        currency = pr.get("currency", "USD")
                        if lo and hi and lo != hi:
                            price = f"${lo:.0f}–${hi:.0f}"
                        elif lo:
                            price = f"${lo:.0f}"
                    ev = EventItem(
                        id=_make_id(title, event_date),
                        title=title,
                        date=event_date,
                        end_time=None,
                        location_name=location,
                        address=address,
                        lat=lat,
                        lon=lon,
                        distance_bucket=db,
                        price=price,
                        url=url,
                        source=source["name"],
                        channel_type=source["channel"],
                        first_seen=now,
                    )
                    items.append(ev)
                except Exception as exc:
                    logger.warning("ticketmaster event parse error: %s", exc)
            page_meta = data.get("page", {})
            if page >= page_meta.get("totalPages", 1) - 1:
                break
            # Cap at 10 pages (500 events) per classification to avoid hammering rate limit
            if page >= 9:
                break
            page += 1
    # Deduplicate by id across both classification passes
    seen: set[str] = set()
    deduped: list[EventItem] = []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            deduped.append(item)
    logger.info("ticketmaster: %d items", len(deduped))
    return deduped


async def _scrape_phila_gov_calendars(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Fetch multiple City of Philadelphia Google Calendars via public iCal feeds.
    IDs are discovered from api.phila.gov/phila/calendars/v1 (no key required)
    and cached here; update by re-running tools/scraper_agent.py."""
    import random as _random, urllib.parse as _up
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    # Hardcoded calendar IDs from api.phila.gov/phila/calendars/v1 scan (2026-06-04)
    CALENDARS = {
        "Philadelphia Parks & Recreation": "3efanutsrofqu273785lh789ko@group.calendar.google.com",
        "Love Park": "rvua773ubdecb44n1ienf6kjeo@group.calendar.google.com",
        "The Oval": "c_87a5fb2888cbb0d71559573e11b0a6c270183c11e0027079a52434518e229bcb@group.calendar.google.com",
        "Commerce Events": "c_be625b034c255712d129a51d650f2246262effc4ce35e051df2f0cebec5f457f@group.calendar.google.com",
        "Play it Safe PHL": "vvts0rlbd16knddi3o1n1rk0fs@group.calendar.google.com",
    }
    now = datetime.now(timezone.utc)
    items: list[EventItem] = []
    for cal_label, cal_id in CALENDARS.items():
        enc = _up.quote(cal_id, safe="")
        ical_url = f"https://calendar.google.com/calendar/ical/{enc}/public/basic.ics"
        headers = {
            "User-Agent": _random.choice(_UA),
            "Accept": "text/calendar, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.phila.gov/",
        }
        text: Optional[str] = None
        try:
            async with session.get(
                ical_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                else:
                    logger.warning("phila_gov_calendars %s status %s", cal_label, resp.status)
                    continue
        except Exception as exc:
            logger.warning("phila_gov_calendars %s fetch failed: %s", cal_label, exc)
            continue
        if not text:
            continue
        # Parse iCal manually — no icalendar library
        current: dict = {}
        in_event = False
        prop_buf: list[str] = []
        for raw_line in text.splitlines():
            # iCal line folding: continuation lines start with space or tab
            if raw_line.startswith((" ", "\t")) and prop_buf:
                prop_buf[-1] += raw_line[1:]
                continue
            if prop_buf:
                line = prop_buf.pop()
                key_part, _, val = line.partition(":")
                key = key_part.split(";")[0].upper()
                current[key] = val
            prop_buf.append(raw_line.rstrip())
            if raw_line.rstrip() == "BEGIN:VEVENT":
                current = {}
                in_event = True
            elif raw_line.rstrip() == "END:VEVENT":
                in_event = False
                if prop_buf:
                    line = prop_buf.pop()
                    key_part, _, val = line.partition(":")
                    key = key_part.split(";")[0].upper()
                    current[key] = val
                try:
                    title = current.get("SUMMARY", "").replace("\\,", ",").replace("\\n", " ").strip()
                    if not title:
                        continue
                    date_str = current.get("DTSTART", "")
                    if not date_str:
                        continue
                    try:
                        # A trailing "Z" means UTC. Without it (a TZID-local or floating
                        # time, or a bare date) it's Philadelphia wall-clock time.
                        if "T" in date_str:
                            event_date = datetime.strptime(date_str.rstrip("Z"), "%Y%m%dT%H%M%S")
                            if date_str.endswith("Z"):
                                event_date = event_date.replace(tzinfo=timezone.utc)
                        else:
                            event_date = datetime.strptime(date_str, "%Y%m%d")
                        event_date = as_utc(event_date)
                    except ValueError:
                        continue
                    # Skip past events
                    if event_date < now:
                        continue
                    url = current.get("URL", source["url"]).replace("\\", "")
                    location = current.get("LOCATION", "").replace("\\,", ",").replace("\\n", ", ").strip() or None
                    description = current.get("DESCRIPTION", "").replace("\\n", " ").strip()[:200] or None
                    # Only geocode if location looks like a street address (has digits).
                    # iCal LOCATION is often a venue name — geocoding those hammers Nominatim.
                    has_street_number = bool(location and any(c.isdigit() for c in location))
                    ev = EventItem(
                        id=_make_id(title, event_date),
                        title=f"[{cal_label}] {title}",
                        date=event_date,
                        end_time=None,
                        location_name=location,
                        address=location if has_street_number else None,
                        lat=None,
                        lon=None,
                        distance_bucket=None,
                        price=None,
                        url=url,
                        source=source["name"],
                        channel_type=source["channel"],
                        first_seen=now,
                    )
                    if has_street_number:
                        await _geocode_event(ev, session)
                    items.append(ev)
                except Exception as exc:
                    logger.warning("phila_gov_calendars %s event parse error: %s", cal_label, exc)
                current = {}
        logger.info("phila_gov_calendars %s: items so far %d", cal_label, len(items))
    logger.info("phila_gov_calendars: %d total items", len(items))
    return items


async def _scrape_center_city_district(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Center City District uses The Events Calendar (Tribe Events) WordPress plugin.
    The REST endpoint returns structured JSON — no key required."""
    import random as _random
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    api_url = "https://centercityphila.org/wp-json/tribe/events/v1/events"
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://centercityphila.org/",
    }
    items: list[EventItem] = []
    page = 1
    while True:
        params = {
            "per_page": "50",
            "start_date": now.strftime("%Y-%m-%d"),
            "page": str(page),
        }
        data: Optional[dict] = None
        try:
            async with session.get(
                api_url, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                else:
                    logger.warning("center_city_district API status %s page %d", resp.status, page)
                    break
        except Exception as exc:
            logger.error("center_city_district fetch failed: %s", exc)
            break
        if not isinstance(data, dict):
            break
        events = data.get("events", [])
        if not events:
            break
        for ev_raw in events:
            try:
                title = ev_raw.get("title", "").strip()
                if not title:
                    continue
                url = ev_raw.get("url") or source["url"]
                date_str = ev_raw.get("start_date", "")
                if not date_str:
                    continue
                # start_date/end_date are the calendar's local wall-clock times.
                try:
                    event_date = as_utc(datetime.fromisoformat(date_str))
                except ValueError:
                    continue
                end_str = ev_raw.get("end_date")
                end_time: Optional[datetime] = None
                if end_str:
                    try:
                        end_time = as_utc(datetime.fromisoformat(end_str))
                    except ValueError:
                        logger.debug("center city district: bad end_date %r for %r", end_str, title)
                venue = ev_raw.get("venue") or {}
                location = venue.get("venue") if isinstance(venue, dict) else None
                address = venue.get("address") if isinstance(venue, dict) else None
                cost = ev_raw.get("cost") or None
                ev = EventItem(
                    id=_make_id(title, event_date),
                    title=title,
                    date=event_date,
                    end_time=end_time,
                    location_name=location,
                    address=address,
                    lat=None,
                    lon=None,
                    distance_bucket=None,
                    price=cost,
                    url=url,
                    source=source["name"],
                    channel_type=source["channel"],
                    first_seen=now,
                )
                await _geocode_event(ev, session)
                items.append(ev)
            except Exception as exc:
                logger.warning("center_city_district event parse error: %s", exc)
        # Tribe Events paginates; stop if fewer than per_page returned
        if len(events) < 50:
            break
        page += 1
    logger.info("center_city_district: %d items", len(items))
    return items


def _old_city_time(attr: str, shown_html: str, day: Optional[date] = None) -> datetime:
    """One Old City <time> as aware UTC. Raises ValueError if nothing parses.

    The visible text is Philadelphia time and is the truth. The datetime attribute is
    usually that same wall-clock time mislabeled "Z" (75 of 80 on 2026-11-12; the rest
    were real UTC), so it's only a fallback, read as local. A time-only text ("10:00pm",
    an end time) is on `day`.
    """
    import re as _re

    shown = " ".join(BeautifulSoup(shown_html, "html.parser").get_text(" ").split())
    try:
        return as_utc(datetime.strptime(shown, "%m/%d/%Y - %I:%M%p"))
    except ValueError:
        pass
    if day is not None and _re.fullmatch(r"\d{1,2}:\d{2}[ap]m", shown):
        return as_utc(datetime.combine(day, datetime.strptime(shown, "%I:%M%p").time()))
    return as_utc(datetime.fromisoformat(attr.rstrip("Z")))


async def _scrape_old_city_district(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Old City District runs Drupal with structured event cards exposing
    ISO 8601 datetime attributes and /events/<slug> hrefs."""
    import random as _random, re as _re
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.oldcitydistrict.org/",
    }
    html: Optional[str] = None
    try:
        async with session.get(
            source["url"], headers=headers,
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                html = await resp.text(errors="replace")
            else:
                logger.warning("old_city_district status %s", resp.status)
                return []
    except Exception as exc:
        logger.error("old_city_district fetch failed: %s", exc)
        return []
    if not html:
        return []
    # Parse card by card (each starts at href="/events/<slug>"), so a title or time is
    # never paired with another card's. A card has a .field--node-title and a start
    # <time datetime="ISO">11/14/2026 - 1:00pm</time>, optionally an end <time> (a full
    # date for a run like an exhibition, or just "10:00pm" for the same day).
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    seen_hrefs: set[str] = set()
    for card in _re.split(r'(?=href="/events/)', html)[1:]:
        href = _re.match(r'href="(/events/[^"]+)"', card).group(1)
        title_m = _re.search(r'field--node-title[^>]*>\s*(.*?)\s*</div>', card, _re.DOTALL)
        times = _re.findall(r'<time[^>]*datetime="(20[^"]+)"[^>]*>(.*?)</time>', card, _re.DOTALL)
        if href in seen_hrefs or not title_m or not times:
            continue  # nav links and other non-card hrefs have no title + time
        seen_hrefs.add(href)
        try:
            title = BeautifulSoup(title_m.group(1), "html.parser").get_text(strip=True)
            if not title:
                continue
            url = "https://www.oldcitydistrict.org" + href
            try:
                event_date = _old_city_time(*times[0])
                end_time = (_old_city_time(*times[1], day=event_date.astimezone(LOCAL_TZ).date())
                            if len(times) > 1 else None)
            except ValueError:
                logger.debug("old_city_district: unparseable time %r for %r", times, title)
                continue
            ev = EventItem(
                id=_make_id(title, event_date),
                title=title,
                date=event_date,
                end_time=end_time,
                location_name="Old City, Philadelphia",
                address=None,
                lat=None,
                lon=None,
                distance_bucket=None,
                price=None,
                url=url,
                source=source["name"],
                channel_type=source["channel"],
                first_seen=now,
            )
            # Listing page has no per-event address; skip geocoding to avoid 429s.
            items.append(ev)
        except Exception as exc:
            logger.warning("old_city_district card parse error: %s", exc)
    logger.info("old_city_district: %d items", len(items))
    return items


async def _scrape_special_events_closures(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """City of Philadelphia Office of Special Events — published street closure table.
    Columns: EVENT | STREET | FROM | TO | CLOSURE START | ANTICIPATED OPENING"""
    import random as _random, re as _re
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    ]
    headers = {
        "User-Agent": _random.choice(_UA),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.phila.gov/",
    }
    html: Optional[str] = None
    try:
        async with session.get(
            source["url"], headers=headers,
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                html = await resp.text(errors="replace")
            else:
                logger.warning("special_events_closures status %s", resp.status)
                return []
    except Exception as exc:
        logger.error("special_events_closures fetch failed: %s", exc)
        return []
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[EventItem] = []
    now = datetime.now(timezone.utc)
    header_found = False
    col_map: dict[str, int] = {}
    for table in soup.select("table"):
        rows = table.select("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.select("th, td")]
            if not cells:
                continue
            # Detect header row
            if any("EVENT" in c.upper() for c in cells):
                col_map = {}
                for i, c in enumerate(cells):
                    cu = c.upper().strip()
                    if "EVENT" in cu:
                        col_map["event"] = i
                    elif "STREET" in cu:
                        col_map["street"] = i
                    elif "FROM" in cu:
                        col_map["from"] = i
                    elif "TO" in cu:
                        col_map["to"] = i
                    elif "START" in cu or "CLOSURE" in cu:
                        col_map["start"] = i
                    elif "OPENING" in cu or "END" in cu:
                        col_map["end"] = i
                header_found = True
                continue
            if not header_found or not col_map:
                continue
            try:
                def cell(key: str) -> str:
                    idx = col_map.get(key)
                    return cells[idx].strip() if idx is not None and idx < len(cells) else ""
                event_name = cell("event")
                street = cell("street")
                from_st = cell("from")
                to_st = cell("to")
                start_time = cell("start")
                # Skip if any cell looks like a header leaking through
                if not event_name or not street:
                    continue
                if any(v.upper() in ("CLOSURE START", "ANTICIPATED OPENING", "EVENT", "STREET", "FROM", "TO")
                       for v in (event_name, street)):
                    continue
                title = f"{event_name}: {street}"
                if from_st and to_st:
                    title += f" ({from_st} to {to_st})"
                title = title[:200]
                if start_time:
                    try:
                        t = datetime.strptime(start_time, "%I:%M %p")
                        event_date = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                    except ValueError:
                        event_date = now
                else:
                    event_date = now
                # Only geocode if street starts with a house number — bare street
                # names resolve to random points and waste Nominatim quota.
                has_number = street[:1].isdigit()
                ev = EventItem(
                    id=_make_id(title, event_date),
                    title=title,
                    date=event_date,
                    end_time=None,
                    location_name=f"{street}, Philadelphia",
                    address=f"{street}, Philadelphia, PA" if has_number else None,
                    lat=None,
                    lon=None,
                    distance_bucket=None,
                    price=None,
                    url=source["url"],
                    source=source["name"],
                    channel_type=source["channel"],
                    first_seen=now,
                )
                if has_number:
                    await _geocode_event(ev, session)
                items.append(ev)
            except Exception as exc:
                logger.warning("special_events_closures row parse error: %s", exc)
    logger.info("special_events_closures: %d items", len(items))
    return items


async def _scrape_major_discovery(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Dispatch wrapper for the discovery agent. Imported lazily to preserve the
    leaf/local-import rule (discovery imports scraper.EventItem at module top)."""
    from discovery import discover_events
    return await discover_events(source, session)


# ---------------------------------------------------------------------------
# Dispatch table — keyed by source name
# ---------------------------------------------------------------------------

_SCRAPERS = {
    "Visit Philadelphia": _scrape_visitphilly,
    "Eventbrite Philly": _scrape_eventbrite,
    "Do215": _scrape_do215,
    "Songkick Philly": _scrape_songkick,
    "Bandsintown Philly": _scrape_bandsintown,
    "The Key (WXPN)": _scrape_wxpn,
    "SEPTA Alerts": _fetch_septa,
    "PHL Streets Dept": _fetch_phl_streets,
    "Ticketmaster Philly": _scrape_ticketmaster,
    "City of Philadelphia Calendars": _scrape_phila_gov_calendars,
    "Center City District Events": _scrape_center_city_district,
    "Old City District Events": _scrape_old_city_district,
    "Office of Special Events Closures": _scrape_special_events_closures,
    "Major Event Discovery": _scrape_major_discovery,
}


async def fetch_source(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
    """Dispatch to the correct scraper; return [] on any failure."""
    source_type = source.get("type", "scrape")
    if source_type == "skip_js":
        logger.warning("Skipping JS-rendered source: %s", source["name"])
        return []

    handler = _SCRAPERS.get(source["name"])
    if handler is None:
        logger.warning("No handler for source %r — skipping", source["name"])
        return []

    try:
        items = await handler(source, session)
        logger.info("Fetched %d items from %s", len(items), source["name"])
        return items
    except Exception as exc:
        logger.error("Unhandled error fetching %s: %s", source["name"], exc, exc_info=True)
        return []


def _apply_lookahead(items: list[EventItem], channel_type: str) -> list[EventItem]:
    """Drop events outside the lookahead window for this channel type."""
    lookahead_map = {
        "events":   config.EVENTS_LOOKAHEAD_DAYS,
        "closures": config.CLOSURES_LOOKAHEAD_DAYS,
        "music":    config.MUSIC_LOOKAHEAD_DAYS,
    }
    days = lookahead_map.get(channel_type, config.EVENTS_LOOKAHEAD_DAYS)
    now = datetime.now(timezone.utc)
    ceiling = now + timedelta(days=days)
    before = len(items)
    filtered = []
    for item in items:
        if item.date is None:
            filtered.append(item)
            continue
        if as_utc(item.date) <= ceiling:
            filtered.append(item)
    dropped = before - len(filtered)
    if dropped:
        logger.info(
            "%s lookahead: dropped %d/%d items beyond %d-day window (ceiling=%s)",
            channel_type, dropped, before, days, ceiling.date().isoformat(),
        )
    return filtered


def _apply_geo_filter(items: list[EventItem], channel_type: str) -> list[EventItem]:
    """Apply per-channel geographic filter.

    closures — must geocode within CLOSURES_MAX_MILES of center; no coords = drop.
    events   — must geocode within Philadelphia city limits; no coords = keep
                (all event sources are Philly-specific, so ungeocoded items are assumed local).
    music    — must be within MUSIC_MAX_MILES of center if geocoded; no coords = keep.
    """
    from geocoder import haversine_miles

    before = len(items)
    filtered: list[EventItem] = []

    center_lat = config.CENTER_LOCATION["lat"]
    center_lon = config.CENTER_LOCATION["lon"]

    for item in items:
        has_coords = item.lat is not None and item.lon is not None

        if channel_type == "closures":
            # Require confirmed proximity — drop anything ungeocoded
            if not has_coords:
                continue
            miles = haversine_miles(center_lat, center_lon, item.lat, item.lon)
            if miles <= config.CLOSURES_MAX_MILES:
                filtered.append(item)

        elif channel_type == "events":
            # Keep ungeocoded (source is Philly-specific); drop geocoded items outside city limits
            if not has_coords:
                filtered.append(item)
            elif _in_philadelphia(item.lat, item.lon):
                filtered.append(item)

        elif channel_type == "music":
            # Keep ungeocoded; drop geocoded items outside metro radius
            if not has_coords:
                filtered.append(item)
            else:
                miles = haversine_miles(center_lat, center_lon, item.lat, item.lon)
                if miles <= config.MUSIC_MAX_MILES:
                    filtered.append(item)

        else:
            filtered.append(item)

    dropped = before - len(filtered)
    if dropped:
        logger.info(
            "%s geo filter: dropped %d/%d items outside geographic bounds",
            channel_type, dropped, before,
        )
    return filtered


async def poll_all_sources(channel_type: str) -> list[EventItem]:
    """Fetch all sources for a given channel type. Per-source failures are silenced."""
    sources = [s for s in config.ALL_SOURCES if s["channel"] == channel_type]
    all_items: list[EventItem] = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_source(s, session) for s in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Source %s raised: %s", sources[i]["name"], result)
            else:
                all_items.extend(result)

    logger.info("%s: %d raw items before filtering", channel_type, len(all_items))

    # Drop fully-past events (keep current/future and still-ongoing periods)
    now = datetime.now(timezone.utc)
    before_active = len(all_items)
    all_items = [i for i in all_items if event_is_active(i, now)]
    if before_active != len(all_items):
        logger.info("%s: dropped %d past event(s)", channel_type, before_active - len(all_items))

    # Apply lookahead ceiling
    all_items = _apply_lookahead(all_items, channel_type)

    # Apply geographic filter
    all_items = _apply_geo_filter(all_items, channel_type)

    # Deduplicate across sources by normalized title + calendar date, keeping the
    # most complete record. This collapses the same event reported by multiple
    # sources (e.g. "Comedy Bang! Bang!" vs "Comedy Bang Bang") before storage and
    # classification, so we don't store or spend tokens classifying duplicates.
    # Same logic the digest uses; imported locally to avoid a scraper<->digest cycle.
    from digest import dedupe_events
    deduped = dedupe_events(all_items)

    # LLM relevance filter — drops garbage that scrapers pick up
    from validator import validate_items
    deduped = await validate_items(deduped)

    logger.info("%s: %d items after all filters", channel_type, len(deduped))
    return deduped
