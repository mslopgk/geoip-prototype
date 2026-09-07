"""Best-effort reverse geocoding: (lat, lon) -> human-readable admin name.

Turns a fused coordinate into a readable place (e.g. "부산광역시 금정구 구서동")
so a result can be sanity-checked against a known address. Uses OSM Nominatim
(free, no-auth; please respect its usage policy). Any failure degrades to None —
this never raises, and results are cached by rounded coordinate.
"""
from __future__ import annotations

from typing import Optional

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "geoip-prototype/0.1 (research prototype; reverse-geocode)"

# Address-component keys to try, coarsest -> finest, one pick per level.
_LEVELS = (
    ("province", "state", "city", "region"),                 # 시/도
    ("city_district", "borough", "county", "district", "town"),  # 시/군/구
    ("suburb", "quarter", "neighbourhood", "village"),       # 읍/면/동
)

_cache: dict[tuple, Optional[str]] = {}
CACHE_MAX = 4096


def _cache_put(key: tuple, value: str) -> None:
    """Cache a successful lookup, bounding size (FIFO) to avoid unbounded growth."""
    _cache[key] = value
    while len(_cache) > CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)


def _is_korea(address: dict) -> bool:
    cc = (address.get("country_code") or "").lower()
    country = (address.get("country") or "")
    return (
        cc == "kr"
        or "한국" in country
        or "대한민국" in country
        or country.strip().lower() in ("south korea", "korea")
    )


def _compose(address: dict) -> Optional[str]:
    parts: list[str] = []
    for keys in _LEVELS:
        for k in keys:
            v = address.get(k)
            if v and v not in parts:
                parts.append(v)
                break
    return " ".join(parts) if parts else None


async def reverse_geocode(lat: float, lon: float, client) -> Optional[str]:
    """Return a readable admin name for (lat, lon), or None on any failure."""
    if lat is None or lon is None:
        return None
    key = (round(float(lat), 3), round(float(lon), 3))
    if key in _cache:
        return _cache[key]
    result: Optional[str] = None
    try:
        r = await client.get(
            NOMINATIM_URL,
            params={
                "lat": lat, "lon": lon, "format": "json",
                "accept-language": "ko", "zoom": 14, "addressdetails": 1,
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=8.0,
        )
        data = r.json() or {}
        addr = data.get("address") or {}
        # Korean addresses read big->small, so the composed 시/구/동 is natural;
        # Western addresses read small->big, so prefer Nominatim's display_name.
        if _is_korea(addr):
            result = _compose(addr) or data.get("display_name")
        else:
            result = data.get("display_name") or _compose(addr)
    except Exception:
        result = None
    # Only cache a real answer — never cache a transient failure (timeout/429/
    # garbage JSON), which would poison this coordinate for the process lifetime.
    if result is not None:
        _cache_put(key, result)
    return result
