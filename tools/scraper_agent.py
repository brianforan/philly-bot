"""
Self-healing scraper diagnostic agent for philly-bot.

Phase 1 — Recon: probe each source for working data surfaces.
Phase 2 — Propose: print a complete replacement handler function.
Phase 3 — Apply: write to scraper.py on human approval.

Usage (run from the repo root):
    python tools/scraper_agent.py            # interactive per-patch approval
    python tools/scraper_agent.py --auto     # apply all approved patches without prompting
"""

import asyncio
import json
import logging
import random
import re
import ssl as _ssl
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

# This tool lives in tools/ but imports repo-root modules; make the root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # pulls in python-dotenv; .env must be present

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("scraper_agent")

SCRAPER_PY = Path(__file__).resolve().parent.parent / "scraper.py"
AUTO_APPLY = "--auto" in sys.argv

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]
_UA_POOL_REPR = repr(UA_POOL)

TIMEOUT = aiohttp.ClientTimeout(total=20)


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

def _browser_headers(referer: str = "", ua: Optional[str] = None) -> dict:
    return {
        "User-Agent": ua or random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
    }


async def probe_url(
    url: str,
    session: aiohttp.ClientSession,
    referer: str = "",
) -> dict:
    """Fetch a URL; retry with next UA on 403. Returns probe dict."""
    result: dict = {
        "url": url, "status": None, "content_type": None,
        "html": None, "redirects": [], "error": None,
    }
    for ua in UA_POOL[:2]:
        headers = _browser_headers(referer, ua=ua)
        try:
            async with session.get(
                url, headers=headers, timeout=TIMEOUT, allow_redirects=True
            ) as resp:
                result["status"] = resp.status
                result["content_type"] = str(resp.content_type)
                result["redirects"] = [str(h.url) for h in resp.history]
                if resp.status == 200:
                    result["html"] = await resp.text(errors="replace")
                    return result
                if resp.status == 403:
                    continue  # rotate UA
                result["error"] = f"HTTP {resp.status}"
                return result
        except Exception as exc:
            result["error"] = str(exc)
    return result


async def probe_json_url(
    url: str,
    session: aiohttp.ClientSession,
    referer: str = "",
    params: Optional[dict] = None,
) -> dict:
    result: dict = {"url": url, "status": None, "data": None, "error": None}
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
    }
    try:
        async with session.get(
            url, headers=headers, params=params,
            timeout=TIMEOUT, allow_redirects=True
        ) as resp:
            result["status"] = resp.status
            if resp.status == 200:
                try:
                    result["data"] = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text(errors="replace")
                    try:
                        result["data"] = json.loads(text)
                    except Exception:
                        result["error"] = "not JSON"
            else:
                result["error"] = f"HTTP {resp.status}"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _extract_next_data(html: str) -> Optional[dict]:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def _extract_initial_state(html: str) -> Optional[dict]:
    for pat in [
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
        r'window\.__REDUX_STATE__\s*=\s*(\{.*?\});',
        r'__PRELOADED_STATE__\s*=\s*(\{.*?\});',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


def _count_event_words(html: str) -> int:
    kw = ["event", "concert", "festival", "show", "performance", "venue", "ticket"]
    lower = html.lower()
    return sum(lower.count(k) for k in kw)


# ---------------------------------------------------------------------------
# Diagnosis result
# ---------------------------------------------------------------------------

class Diagnosis:
    def __init__(self, name: str):
        self.name = name
        self.status: Optional[int] = None
        self.content_type: Optional[str] = None
        self.redirects: list = []
        self.data_surface: Optional[str] = None
        self.failure_reason: Optional[str] = None
        self.fn_name: Optional[str] = None   # target function in scraper.py
        self.patch: Optional[str] = None      # complete replacement function source
        self.skip_js: bool = False
        self.skip_reason: Optional[str] = None
        self.replacement_url: Optional[str] = None
        self.item_estimate: int = 0

    def print_report(self) -> None:
        if self.skip_js:
            icon = "⏭"
        elif self.patch:
            icon = "✅"
        else:
            icon = "❌"
        print(f"\n{'='*64}")
        print(f"{icon}  [{self.name}]")
        print(f"   Status: {self.status}  |  Content-Type: {self.content_type}")
        redir = " → ".join(self.redirects) if self.redirects else "none"
        print(f"   Redirects: {redir}")
        if self.data_surface:
            print(f"   Data surface: {self.data_surface}")
        if self.failure_reason:
            print(f"   Failure reason: {self.failure_reason}")
        if self.skip_js:
            print(f"   SKIP_JS: {self.skip_reason}")
            if self.replacement_url:
                print(f"   Suggested replacement: {self.replacement_url}")
        elif self.patch:
            print(f"\n   Proposed patch → {self.fn_name}:")
            print(textwrap.indent(self.patch.strip(), "   "))


# ---------------------------------------------------------------------------
# Patch template generators
# All patches embed their own UA rotation and headers; no external deps beyond
# what is already imported in scraper.py.
# ---------------------------------------------------------------------------

def _make_rss_patch(fn_name: str, primary_rss: str, fallback_rss: Optional[str],
                     site_home: str) -> str:
    fallback_block = ""
    if fallback_rss:
        fallback_block = f"""
    if not html:
        try:
            async with session.get(
                {fallback_rss!r},
                headers={{**headers, "User-Agent": _random.choice(_UA)}},
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp2:
                if resp2.status == 200:
                    html = await resp2.text(errors="replace")
        except Exception:
            pass"""

    return textwrap.dedent(f"""\
    async def {fn_name}(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random
        from email.utils import parsedate_to_datetime
        _UA = {_UA_POOL_REPR}
        headers = {{
            "User-Agent": _random.choice(_UA),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": {site_home!r},
        }}
        html: Optional[str] = None
        try:
            async with session.get(
                {primary_rss!r},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                else:
                    logger.warning("%s RSS status %s", source["name"], resp.status)
        except Exception as exc:
            logger.error("%s RSS fetch failed: %s", source["name"], exc){fallback_block}
        if not html:
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
                logger.warning("%s RSS item parse error: %s", source["name"], exc)
        logger.info("%s: %d items from RSS", source["name"], len(items))
        return items
    """)


def _make_next_data_patch(fn_name: str, url: str, site_home: str,
                           events_path: list[str]) -> str:
    path_repr = repr(events_path)
    return textwrap.dedent(f"""\
    async def {fn_name}(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random, json as _json, re as _re
        _UA = {_UA_POOL_REPR}
        headers = {{
            "User-Agent": _random.choice(_UA),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": {site_home!r},
        }}
        html: Optional[str] = None
        try:
            async with session.get(
                {url!r},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                else:
                    logger.warning("%s status %s", source["name"], resp.status)
                    return []
        except Exception as exc:
            logger.error("%s fetch failed: %s", source["name"], exc)
            return []
        m = _re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html, _re.DOTALL,
        )
        if not m:
            logger.warning("%s: __NEXT_DATA__ not found in page", source["name"])
            return []
        try:
            data = _json.loads(m.group(1))
        except Exception as exc:
            logger.error("%s: failed to parse __NEXT_DATA__: %s", source["name"], exc)
            return []
        # Navigate to events list via configured path
        node = data
        for key in {path_repr}:
            if isinstance(node, dict):
                node = node.get(key, {{}})
            else:
                node = {{}}
        raw_events = node if isinstance(node, list) else []
        if not raw_events:
            logger.warning("%s: no events at path %s in __NEXT_DATA__", source["name"], {path_repr})
            return []
        items: list[EventItem] = []
        now = datetime.now(timezone.utc)
        for ev_raw in raw_events:
            try:
                if not isinstance(ev_raw, dict):
                    continue
                title = (ev_raw.get("name") or ev_raw.get("title") or
                         ev_raw.get("displayName") or "")
                if not title:
                    continue
                url_val = (ev_raw.get("url") or ev_raw.get("link") or
                           ev_raw.get("uri") or source["url"])
                date_str = (ev_raw.get("startDate") or ev_raw.get("start_date") or
                            ev_raw.get("date") or ev_raw.get("datetime") or "")
                if not date_str:
                    continue
                try:
                    event_date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
                except ValueError:
                    continue
                location = (ev_raw.get("venue", {{}}).get("name") if isinstance(ev_raw.get("venue"), dict)
                            else ev_raw.get("location") or ev_raw.get("venue"))
                ev = EventItem(
                    id=_make_id(title, event_date),
                    title=title,
                    date=event_date,
                    end_time=None,
                    location_name=str(location) if location else None,
                    address=None,
                    lat=None,
                    lon=None,
                    distance_bucket=None,
                    price=ev_raw.get("price") or ev_raw.get("cost"),
                    url=str(url_val),
                    source=source["name"],
                    channel_type=source["channel"],
                    first_seen=now,
                )
                await _geocode_event(ev, session)
                items.append(ev)
            except Exception as exc:
                logger.warning("%s event parse error: %s", source["name"], exc)
        logger.info("%s: %d items from __NEXT_DATA__", source["name"], len(items))
        return items
    """)


def _make_ical_patch(fn_name: str, ical_url: str, site_home: str) -> str:
    return textwrap.dedent(f"""\
    async def {fn_name}(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random
        _UA = {_UA_POOL_REPR}
        headers = {{
            "User-Agent": _random.choice(_UA),
            "Accept": "text/calendar,text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": {site_home!r},
        }}
        text: Optional[str] = None
        try:
            async with session.get(
                {ical_url!r},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                else:
                    logger.warning("%s iCal status %s", source["name"], resp.status)
                    return []
        except Exception as exc:
            logger.error("%s iCal fetch failed: %s", source["name"], exc)
            return []
        if not text:
            return []
        items: list[EventItem] = []
        now = datetime.now(timezone.utc)
        current: dict = {{}}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line == "BEGIN:VEVENT":
                current = {{}}
            elif line == "END:VEVENT":
                try:
                    title = current.get("SUMMARY", "")
                    date_str = current.get("DTSTART", "")
                    url = current.get("URL", source["url"])
                    if not title or not date_str:
                        continue
                    if "T" in date_str:
                        event_date = datetime.strptime(
                            date_str.rstrip("Z"), "%Y%m%dT%H%M%S"
                        ).replace(tzinfo=timezone.utc)
                    else:
                        event_date = datetime.strptime(date_str, "%Y%m%d").replace(
                            tzinfo=timezone.utc
                        )
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
                    logger.warning("%s iCal event parse error: %s", source["name"], exc)
            elif ":" in line:
                key, _, val = line.partition(":")
                current[key.split(";")[0]] = val
        logger.info("%s: %d items from iCal", source["name"], len(items))
        return items
    """)


def _make_songkick_patch(json_works: bool, ical_url: str) -> str:
    """Songkick: try JSON API (with browser headers), fall back to iCal."""
    return textwrap.dedent(f"""\
    async def _scrape_songkick(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random
        _UA = {_UA_POOL_REPR}
        metro_id = "28563"
        api_url = f"https://www.songkick.com/metro_areas/{{metro_id}}/calendar.json"
        headers = {{
            "User-Agent": _random.choice(_UA),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.songkick.com/",
        }}
        data = None
        try:
            async with session.get(
                api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("songkick JSON API failed: %s", exc)
        if isinstance(data, dict):
            entries = (data.get("resultsPage", {{}})
                       .get("results", {{}})
                       .get("calendarEntry", []))
            if entries:
                items: list[EventItem] = []
                now = datetime.now(timezone.utc)
                for entry in entries:
                    try:
                        event = entry.get("event", {{}})
                        title = event.get("displayName", "")
                        if not title:
                            continue
                        url = event.get("uri", source["url"])
                        start = event.get("start", {{}})
                        date_str = start.get("datetime") or start.get("date")
                        if not date_str:
                            continue
                        try:
                            event_date = datetime.fromisoformat(
                                date_str.replace("Z", "+00:00")
                            )
                        except ValueError:
                            continue
                        venue = event.get("venue", {{}})
                        loc = venue.get("displayName")
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
                            location_name=loc,
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
                        logger.warning("songkick JSON entry error: %s", exc)
                if items:
                    logger.info("songkick: %d items from JSON API", len(items))
                    return items
        # iCal fallback
        logger.info("songkick: falling back to iCal")
        ical_url = {ical_url!r}
        text: Optional[str] = None
        try:
            async with session.get(
                ical_url, headers={{**headers, "Accept": "text/calendar,*/*"}},
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
        except Exception as exc:
            logger.error("songkick iCal failed: %s", exc)
            return []
        if not text:
            return []
        items = []
        now = datetime.now(timezone.utc)
        current: dict = {{}}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line == "BEGIN:VEVENT":
                current = {{}}
            elif line == "END:VEVENT":
                try:
                    title = current.get("SUMMARY", "")
                    date_str = current.get("DTSTART", "")
                    url = current.get("URL", source["url"])
                    if not title or not date_str:
                        continue
                    if "T" in date_str:
                        event_date = datetime.strptime(
                            date_str.rstrip("Z"), "%Y%m%dT%H%M%S"
                        ).replace(tzinfo=timezone.utc)
                    else:
                        event_date = datetime.strptime(date_str, "%Y%m%d").replace(
                            tzinfo=timezone.utc
                        )
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
                    logger.warning("songkick iCal event error: %s", exc)
            elif ":" in line:
                key, _, val = line.partition(":")
                current[key.split(";")[0]] = val
        logger.info("songkick: %d items from iCal", len(items))
        return items
    """)


def _make_phl_streets_patch() -> str:
    return textwrap.dedent(f"""\
    async def _fetch_phl_streets(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random
        _UA = {_UA_POOL_REPR}
        base_url = source["url"]
        headers = {{
            "User-Agent": _random.choice(_UA),
            "Accept": "application/json, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://phl.carto.com/",
        }}
        # Discover available columns first
        schema_params = {{"q": "SELECT * FROM li_permits LIMIT 1", "format": "json"}}
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
            cols = list((schema.get("fields") or {{}}).keys())
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
        params = {{"q": query, "format": "json"}}
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
                title = (f"{{work}}: {{desc}}".strip(": ") or "Street Closure")[:200]
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
    """)


def _make_wxpn_patch(rss_works: bool, rss_url: str) -> str:
    """WXPN: prefer RSS; fall back to HTML with SSL workaround."""
    return textwrap.dedent(f"""\
    async def _scrape_wxpn(source: dict, session: aiohttp.ClientSession) -> list[EventItem]:
        import random as _random, ssl as _ssl
        from email.utils import parsedate_to_datetime
        _UA = {_UA_POOL_REPR}
        # Force HTTP/1.1 via ALPN — WXPN returns 421 Misdirected Request on HTTP/2
        _ssl_ctx = _ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = _ssl.CERT_NONE
        _ssl_ctx.set_alpn_protocols(["http/1.1"])
        connector = aiohttp.TCPConnector(ssl=_ssl_ctx, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as _s:
            headers = {{
                "User-Agent": _random.choice(_UA),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://thekey.xpn.org/",
            }}
            # Try RSS first — lighter and more reliable
            rss_html: Optional[str] = None
            try:
                async with _s.get(
                    {rss_url!r},
                    headers={{**headers, "Accept": "text/xml,application/xml,*/*"}},
                    timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        rss_html = await resp.text(errors="replace")
            except Exception as exc:
                logger.warning("wxpn RSS fetch failed: %s", exc)
            if rss_html:
                soup = BeautifulSoup(rss_html, "xml")
                rss_items = soup.select("item")
                if rss_items:
                    items: list[EventItem] = []
                    now = datetime.now(timezone.utc)
                    for item in rss_items:
                        try:
                            title_el = item.find("title")
                            if not title_el:
                                continue
                            title = BeautifulSoup(
                                title_el.get_text(strip=True), "html.parser"
                            ).get_text(strip=True)
                            if not title:
                                continue
                            link_el = item.find("link")
                            url = link_el.get_text(strip=True) if link_el else source["url"]
                            pub_el = item.find("pubDate")
                            if not pub_el:
                                continue
                            try:
                                event_date = parsedate_to_datetime(
                                    pub_el.get_text(strip=True)
                                )
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
                    logger.info("wxpn: %d items from RSS", len(items))
                    return items
            # Fall back to HTML scraping
            html: Optional[str] = None
            try:
                async with _s.get(
                    source["url"], headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                    else:
                        logger.warning("wxpn HTML status %s", resp.status)
                        return []
            except Exception as exc:
                logger.error("wxpn HTML fetch failed: %s", exc)
                return []
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            items = []
            now = datetime.now(timezone.utc)
            for post in soup.select("article.post, .post"):
                try:
                    title_el = post.select_one("h2.entry-title a, h1.entry-title a, h2 a")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", source["url"])
                    date_el = post.select_one("time.entry-date[datetime], time[datetime]")
                    if not date_el:
                        continue
                    try:
                        event_date = datetime.fromisoformat(
                            date_el["datetime"].replace("Z", "+00:00")
                        )
                    except (ValueError, KeyError):
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
                    logger.warning("wxpn HTML post parse error: %s", exc)
            logger.info("wxpn: %d items from HTML", len(items))
            return items
    """)


# ---------------------------------------------------------------------------
# Per-source diagnosis coroutines
# ---------------------------------------------------------------------------

async def _diag_visit_philly(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    events_rss = "https://www.visitphilly.com/events/feed/"
    main_rss = "https://www.visitphilly.com/feed/"

    for rss_url in (events_rss, main_rss):
        probe = await probe_url(rss_url, session, referer="https://www.visitphilly.com/")
        d.status = probe["status"]
        d.content_type = probe["content_type"]
        d.redirects = probe["redirects"]
        if probe["html"]:
            soup = BeautifulSoup(probe["html"], "xml")
            items = soup.select("item")
            if items:
                d.data_surface = f"RSS at {rss_url} — {len(items)} items"
                d.item_estimate = len(items)
                d.failure_reason = (
                    "Current handler works but uses generic /feed/; events-specific RSS preferred"
                    if "events" in rss_url else
                    "Current handler uses RSS correctly"
                )
                d.fn_name = "_scrape_visitphilly"
                d.patch = _make_rss_patch(
                    "_scrape_visitphilly", rss_url, None,
                    "https://www.visitphilly.com/",
                )
                return d
        if probe["error"]:
            d.failure_reason = probe["error"]

    d.failure_reason = "Both RSS feeds returned 0 items or failed"
    d.skip_js = True
    d.skip_reason = "VisitPhilly RSS inaccessible"
    d.replacement_url = "https://whyy.org/events/"
    return d


async def _diag_eventbrite(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    probe = await probe_url(source["url"], session, referer="https://www.eventbrite.com/")
    d.status = probe["status"]
    d.content_type = probe["content_type"]
    d.redirects = probe["redirects"]

    if probe["html"]:
        nd = _extract_next_data(probe["html"])
        if nd:
            # Walk common paths looking for an events array
            candidates = [
                nd.get("props", {}).get("pageProps", {}).get("events", []),
                nd.get("props", {}).get("pageProps", {}).get("serverPayload", {}).get("events", []),
            ]
            for events_list in candidates:
                if isinstance(events_list, list) and events_list:
                    d.data_surface = "__NEXT_DATA__.props.pageProps.events"
                    d.item_estimate = len(events_list)
                    d.failure_reason = "Current handler uses CSS selectors; page is Next.js — data is in __NEXT_DATA__"
                    d.fn_name = "_scrape_eventbrite"
                    d.patch = _make_next_data_patch(
                        "_scrape_eventbrite",
                        source["url"],
                        "https://www.eventbrite.com/",
                        ["props", "pageProps", "events"],
                    )
                    return d
            d.failure_reason = "__NEXT_DATA__ present but no events array at expected paths"
        else:
            d.failure_reason = "No __NEXT_DATA__ found; page fully JS-rendered or blocked"
    else:
        d.failure_reason = probe.get("error", "No HTML (403 or network error)")

    d.skip_js = True
    d.skip_reason = "Eventbrite requires JS rendering; no static data surface found"
    d.replacement_url = "https://www.phillymag.com/things-to-do/"
    return d


async def _diag_do215(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    for rss_url in ("https://do215.com/events/feed/", "https://do215.com/feed/"):
        probe = await probe_url(rss_url, session, referer="https://do215.com/")
        d.status = probe["status"]
        d.content_type = probe["content_type"]
        d.redirects = probe["redirects"]
        if probe["html"]:
            soup = BeautifulSoup(probe["html"], "xml")
            items = soup.select("item")
            if items:
                d.data_surface = f"RSS at {rss_url} — {len(items)} items"
                d.item_estimate = len(items)
                fallback = "https://do215.com/feed/" if "events" in rss_url else None
                d.fn_name = "_scrape_do215"
                d.patch = _make_rss_patch(
                    "_scrape_do215", rss_url, fallback, "https://do215.com/",
                )
                return d
        if probe["error"]:
            d.failure_reason = probe["error"]

    # Try HTML
    html_probe = await probe_url(source["url"], session, referer="https://do215.com/")
    if html_probe["html"]:
        score = _count_event_words(html_probe["html"])
        d.status = html_probe["status"]
        d.data_surface = f"HTML page (event keyword score={score})"
        d.failure_reason = "RSS failed; HTML scraping likely needed"
        # Keep existing HTML handler — it handles this case already
    else:
        d.failure_reason = "Both RSS and HTML fetch failed"
        d.skip_js = True
        d.skip_reason = "Do215 inaccessible"
        d.replacement_url = "https://www.uwishunu.com/category/things-to-do/"
    return d


async def _diag_songkick(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    metro_id = "28563"
    ical_url = f"https://www.songkick.com/metro_areas/{metro_id}/calendar.ics"

    # Try JSON API with browser headers
    json_probe = await probe_json_url(
        f"https://www.songkick.com/metro_areas/{metro_id}/calendar.json",
        session, referer="https://www.songkick.com/",
    )
    d.status = json_probe.get("status")
    json_works = False
    if isinstance(json_probe.get("data"), dict):
        entries = (json_probe["data"].get("resultsPage", {})
                   .get("results", {})
                   .get("calendarEntry", []))
        if entries:
            json_works = True
            d.data_surface = f"JSON API — {len(entries)} calendar entries"
            d.item_estimate = len(entries)

    # Try iCal
    ical_probe = await probe_url(ical_url, session, referer="https://www.songkick.com/")
    if not d.status:
        d.status = ical_probe["status"]
    d.content_type = ical_probe["content_type"]
    d.redirects = ical_probe["redirects"]
    ical_works = False
    if ical_probe["html"]:
        vevent_count = ical_probe["html"].count("BEGIN:VEVENT")
        if vevent_count > 0:
            ical_works = True
            if not d.data_surface:
                d.data_surface = f"iCal at {ical_url} — {vevent_count} VEVENTs"
                d.item_estimate = vevent_count

    if not json_works and not ical_works:
        # Try HTML
        html_probe = await probe_url(source["url"], session, referer="https://www.songkick.com/")
        d.status = d.status or html_probe["status"]
        if html_probe["html"]:
            nd = _extract_next_data(html_probe["html"])
            if nd:
                d.data_surface = "__NEXT_DATA__ in HTML (JSON API and iCal both blocked)"
                d.failure_reason = "JSON API and iCal blocked; need __NEXT_DATA__ extraction"
            else:
                d.skip_js = True
                d.skip_reason = "All Songkick data surfaces blocked (JSON API, iCal, HTML)"
                d.replacement_url = "https://www.bandcamp.com/tag/philadelphia"
                return d
        else:
            d.skip_js = True
            d.skip_reason = "Songkick unreachable"
            d.replacement_url = "https://www.bandcamp.com/tag/philadelphia"
            return d

    if not d.failure_reason:
        d.failure_reason = (
            "Current JSON handler uses plain bot UA — may get 403; browser headers fix this"
            if json_works else
            "JSON API blocked; iCal fallback available"
        )
    d.fn_name = "_scrape_songkick"
    d.patch = _make_songkick_patch(json_works, ical_url)
    return d


async def _diag_phl_streets(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    params = {
        "q": "SELECT typeofwork, descriptionofwork, address, permitissuedate FROM li_permits WHERE LOWER(typeofwork) LIKE '%closure%' AND permitissuedate >= now() - INTERVAL '30 days' ORDER BY permitissuedate DESC LIMIT 5",
        "format": "json",
    }
    probe = await probe_json_url(source["url"], session,
                                  referer="https://phl.carto.com/", params=params)
    d.status = probe.get("status")
    d.content_type = "application/json"

    if isinstance(probe.get("data"), dict):
        rows = probe["data"].get("rows", [])
        d.data_surface = f"Carto SQL API — {len(rows)} rows returned"
        d.item_estimate = len(rows)
        d.failure_reason = (
            "Current handler works; patch improves error handling and ILIKE robustness"
        )
        d.fn_name = "_fetch_phl_streets"
        d.patch = _make_phl_streets_patch()
    else:
        error_detail = str(probe.get("data", probe.get("error", "unknown")))[:300]
        d.failure_reason = f"Carto API error: {error_detail}"
        # Still provide the patch — it may work with the right columns
        d.fn_name = "_fetch_phl_streets"
        d.patch = _make_phl_streets_patch()
    return d


async def _diag_bandsintown(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    probe = await probe_url(source["url"], session, referer="https://www.bandsintown.com/")
    d.status = probe["status"]
    d.content_type = probe["content_type"]
    d.redirects = probe["redirects"]

    if probe["html"]:
        nd = _extract_next_data(probe["html"])
        if nd:
            # Walk the state looking for events
            pp = nd.get("props", {}).get("pageProps", {})
            events = pp.get("events") or pp.get("upcomingEvents") or pp.get("concerts") or []
            if isinstance(events, list) and events:
                d.data_surface = f"__NEXT_DATA__.props.pageProps.events — {len(events)} events"
                d.item_estimate = len(events)
                d.failure_reason = "Current handler uses CSS selectors; page is Next.js"
                d.fn_name = "_scrape_bandsintown"
                d.patch = _make_next_data_patch(
                    "_scrape_bandsintown",
                    source["url"],
                    "https://www.bandsintown.com/",
                    ["props", "pageProps", "events"],
                )
                return d
            d.failure_reason = "__NEXT_DATA__ present but no events at expected paths"
        else:
            d.failure_reason = "No __NEXT_DATA__; likely hard blocked"
    else:
        d.failure_reason = probe.get("error", "No HTML (probably 403)")

    d.skip_js = True
    d.skip_reason = "Bandsintown returns 403 or empty JS shell; no extractable data"
    d.replacement_url = "https://www.phillymag.com/music/"
    return d


async def _diag_wxpn(source: dict, session: aiohttp.ClientSession) -> Diagnosis:
    d = Diagnosis(source["name"])
    import ssl as _ssl2
    ssl_ctx = _ssl2.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl2.CERT_NONE
    ssl_ctx.set_alpn_protocols(["http/1.1"])
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, force_close=True)

    rss_url = "https://thekey.xpn.org/feed/"
    rss_works = False
    rss_count = 0

    async with aiohttp.ClientSession(connector=connector) as wxpn_session:
        rss_probe = await probe_url(rss_url, wxpn_session, referer="https://thekey.xpn.org/")
        d.status = rss_probe["status"]
        d.content_type = rss_probe["content_type"]
        d.redirects = rss_probe["redirects"]
        if rss_probe["html"]:
            soup = BeautifulSoup(rss_probe["html"], "xml")
            rss_count = len(soup.select("item"))
            if rss_count > 0:
                rss_works = True
                d.data_surface = f"WordPress RSS at {rss_url} — {rss_count} items"
                d.item_estimate = rss_count

        if not rss_works:
            html_probe = await probe_url(source["url"], wxpn_session,
                                          referer="https://thekey.xpn.org/")
            d.status = d.status or html_probe["status"]
            if html_probe["html"]:
                posts = BeautifulSoup(html_probe["html"], "html.parser").select(
                    "article.post, .post"
                )
                d.data_surface = d.data_surface or f"WordPress HTML — {len(posts)} post elements"
                d.item_estimate = d.item_estimate or len(posts)
            else:
                d.failure_reason = html_probe.get("error", "Both RSS and HTML failed")

    d.failure_reason = (
        "Current handler uses HTML scraping; RSS is lighter and more reliable"
        if rss_works else
        (d.failure_reason or "Could not fetch WXPN content")
    )
    if d.data_surface:
        d.fn_name = "_scrape_wxpn"
        d.patch = _make_wxpn_patch(rss_works, rss_url)
    else:
        d.skip_js = True
        d.skip_reason = "Cannot fetch WXPN content even with HTTP/1.1 SSL workaround"
        d.replacement_url = "https://www.wxpn.org/category/the-key/"
    return d


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def _find_function_span(source: str, fn_name: str) -> Optional[tuple[int, int]]:
    """Return (start_line_idx, end_line_idx) for a top-level function in source text."""
    lines = source.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^async def {re.escape(fn_name)}\b|^def {re.escape(fn_name)}\b", line):
            start = i
            break
    if start is None:
        return None
    # End: next top-level def/class/async def at column 0, or EOF
    for i in range(start + 1, len(lines)):
        if re.match(r"^(async def |def |class )", lines[i]):
            return (start, i)
    return (start, len(lines))


def apply_patch(fn_name: str, patch_code: str) -> bool:
    """Replace fn_name in scraper.py with patch_code. Returns True on success."""
    original = SCRAPER_PY.read_text(encoding="utf-8")
    span = _find_function_span(original, fn_name)
    if span is None:
        logger.error("apply_patch: %s not found in scraper.py", fn_name)
        return False
    lines = original.splitlines(keepends=True)
    start, end = span
    # Build replacement: ensure patch ends with exactly one newline before next section
    new_fn = patch_code.rstrip("\n") + "\n\n\n"
    new_lines = lines[:start] + [new_fn] + lines[end:]
    SCRAPER_PY.write_text("".join(new_lines), encoding="utf-8")
    logger.info("✅  Applied patch for %s", fn_name)
    return True


# ---------------------------------------------------------------------------
# Main diagnostic loop
# ---------------------------------------------------------------------------

# Map source name → diagnosis coroutine
_DIAG_MAP = {
    "Visit Philadelphia": _diag_visit_philly,
    "Eventbrite Philly": _diag_eventbrite,
    "Do215": _diag_do215,
    "Songkick Philly": _diag_songkick,
    "PHL Streets Dept": _diag_phl_streets,
    "Bandsintown Philly": _diag_bandsintown,
    "The Key (WXPN)": _diag_wxpn,
}


async def run_diagnostics() -> None:
    # Deduplicate sources by name
    seen: set[str] = set()
    sources: list[dict] = []
    for s in config.ALL_SOURCES:
        if s["name"] not in seen:
            seen.add(s["name"])
            sources.append(s)

    print(f"\n🔍  philly-bot scraper diagnostics — {len(sources)} sources\n")

    # Use a permissive connector (no SSL verification for probe purposes)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for source in sources:
            name = source["name"]
            diag_fn = _DIAG_MAP.get(name)
            if diag_fn is None:
                print(f"\n⚠️  [{name}] — no diagnosis function registered; skipping")
                continue
            try:
                d = await diag_fn(source, session)
            except Exception as exc:
                logger.error("[%s] diagnosis raised: %s", name, exc)
                continue

            d.print_report()

            if d.skip_js or d.patch is None:
                continue

            if AUTO_APPLY:
                apply_patch(d.fn_name, d.patch)
                print(f"\n🔧  [{name}] patch auto-applied to scraper.py")
            else:
                ans = input(f"\nApply patch for [{name}]? [y/N] ").strip().lower()
                if ans == "y":
                    if apply_patch(d.fn_name, d.patch):
                        print(f"🔧  [{name}] patch applied to scraper.py")
                    else:
                        print(f"❌  [{name}] patch application failed — check logs")
                else:
                    print(f"⏭   [{name}] patch skipped")

    print("\n✅  Diagnostic run complete.")


if __name__ == "__main__":
    asyncio.run(run_diagnostics())
