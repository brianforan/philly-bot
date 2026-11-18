"""Nominatim geocoding with 1 req/sec rate limit."""

import asyncio
import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

_last_request_time: float = 0.0


async def geocode(address: str, session: aiohttp.ClientSession) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for address string, or None on failure."""
    global _last_request_time

    now = asyncio.get_event_loop().time()
    wait = config.NOMINATIM_RATE_LIMIT_SEC - (now - _last_request_time)
    if wait > 0:
        await asyncio.sleep(wait)

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": config.NOMINATIM_USER_AGENT}

    try:
        _last_request_time = asyncio.get_event_loop().time()
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as exc:
        logger.warning("Geocode failed for %r: %s", address, exc)
    return None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def distance_bucket(lat: float, lon: float) -> str:
    miles = haversine_miles(config.CENTER_LOCATION["lat"], config.CENTER_LOCATION["lon"], lat, lon)
    thresholds = config.DISTANCE_THRESHOLDS_MI
    buckets = config.DISTANCE_BUCKETS
    for i, thresh in enumerate(thresholds):
        if miles < thresh:
            return buckets[i]
    return buckets[-1]
