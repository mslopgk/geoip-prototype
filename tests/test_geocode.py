"""Reverse-geocoding: fused (lat,lon) -> human-readable Korean admin name.

Best-effort enrichment (OSM Nominatim) so a result reads as 시/구/동 and can be
checked against a known address. Network failures degrade to None — never raise.
"""
from __future__ import annotations

import asyncio

from app import geocode
from app.models import LocateResult, Classification


class _FakeResp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class _FakeClient:
    def __init__(self, d):
        self._d = d

    async def get(self, url, **kwargs):
        return _FakeResp(self._d)


def test_reverse_geocode_composes_korean_admin():
    resp = {
        "address": {"city": "부산광역시", "borough": "금정구", "suburb": "구서동",
                     "country": "대한민국"},
        "display_name": "구서동, 금정구, 부산광역시, 대한민국",
    }
    s = asyncio.run(geocode.reverse_geocode(35.2476, 129.0892, _FakeClient(resp)))
    assert s and "부산" in s and "금정구" in s and "구서동" in s


def test_reverse_geocode_falls_back_to_display_name():
    resp = {"display_name": "Mountain View, Santa Clara County, California, USA"}
    s = asyncio.run(geocode.reverse_geocode(37.42, -122.08, _FakeClient(resp)))
    assert s == "Mountain View, Santa Clara County, California, USA"


def test_non_korean_address_uses_display_name():
    # For Western (small->big) addresses, the big->small _compose garbles order
    # and shadows the city; prefer Nominatim's display_name there.
    geocode._cache.clear()  # avoid coordinate-cache collisions with other tests
    resp = {
        "address": {"city": "Mountain View", "county": "Santa Clara County",
                     "state": "California", "country": "United States",
                     "neighbourhood": "Old Mountain View"},
        "display_name": "Mountain View, Santa Clara County, California, United States",
    }
    s = asyncio.run(geocode.reverse_geocode(37.42, -122.08, _FakeClient(resp)))
    assert s == "Mountain View, Santa Clara County, California, United States"


def test_reverse_geocode_none_on_failure():
    class _BadClient:
        async def get(self, url, **kwargs):
            raise RuntimeError("network down")

    assert asyncio.run(geocode.reverse_geocode(0.0, 0.0, _BadClient())) is None


def test_reverse_geocode_does_not_cache_transient_failure():
    geocode._cache.clear()

    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient blip / 429")
            return _FakeResp({"address": {"suburb": "구서동"}})

    c = _FlakyClient()
    assert asyncio.run(geocode.reverse_geocode(35.2476, 129.0892, c)) is None
    # Service recovered -> a second call must RE-QUERY, not return a cached None.
    s = asyncio.run(geocode.reverse_geocode(35.2476, 129.0892, c))
    assert s and "구서동" in s, "transient failure was cached and poisoned the coordinate"


def test_geocode_cache_is_bounded(monkeypatch):
    geocode._cache.clear()
    monkeypatch.setattr(geocode, "CACHE_MAX", 10)
    for i in range(50):
        geocode._cache_put((float(i), float(i)), f"place-{i}")
    assert len(geocode._cache) <= 10


def test_locateresult_serializes_address():
    r = LocateResult(ip="1.2.3.4", classification=Classification(), estimates=[],
                     address="부산광역시 금정구 구서동")
    assert r.to_dict()["address"] == "부산광역시 금정구 구서동"
