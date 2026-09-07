"""Multi-provider GeoIP voter tests (offline via a fake httpx client).

Adding more independent-lineage providers gives the existing de-correlation /
dispersion fusion more votes for outlier rejection, and ipleak's reported
accuracy_radius lets per-estimate radii reflect real provider confidence.
"""
from __future__ import annotations

import asyncio

from app.signals import geoip
from app.models import ProviderResult


class _FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _FakeClient:
    """Routes by URL substring to a canned JSON body."""

    def __init__(self, routes):
        self.routes = routes

    async def get(self, url, timeout=None):
        for sub, data in self.routes:
            if sub in url:
                return _FakeResp(data)
        return _FakeResp({})


def test_lookup_all_queries_extra_independent_providers():
    routes = [
        ("ip-api.com", {"status": "success", "city": "Busan", "lat": 35.1, "lon": 129.0,
                         "as": "AS3786", "org": "LG", "countryCode": "KR", "query": "1.2.3.4"}),
        ("ipapi.co", {"city": "Busan", "latitude": 35.1, "longitude": 129.0, "country_code": "KR"}),
        ("ip.guide", {"location": {"city": "Changwon", "latitude": 35.23, "longitude": 128.68}}),
        ("ipleak.net", {"city_name": "Changwon", "latitude": 35.23, "longitude": 128.68,
                         "region_name": "Gyeongsangnam-do", "accuracy_radius": 50}),
    ]
    results = asyncio.run(geoip.lookup_all("1.2.3.4", _FakeClient(routes)))
    sources = {r.source for r in results if r.ok}
    assert {"ip-api", "ipapi.co", "ip.guide", "ipleak"} <= sources, (
        f"expected 4 independent providers, got {sources}"
    )
    ipleak = next(r for r in results if r.source == "ipleak")
    assert ipleak.city == "Changwon"
    assert ipleak.accuracy_radius_km == 50


def test_to_estimates_uses_provider_accuracy_radius():
    p = ProviderResult(source="ipleak", ok=True, lat=35.23, lon=128.68,
                       city="Changwon", country="KR", accuracy_radius_km=50.0)
    est = geoip.to_estimates([p])
    assert len(est) == 1
    assert est[0].radius_km >= 50.0, "reported accuracy_radius should set a more honest radius"


def test_to_estimates_defaults_radius_without_accuracy():
    p = ProviderResult(source="ip-api", ok=True, lat=35.1, lon=129.0, city="Busan", country="KR")
    est = geoip.to_estimates([p])
    assert est[0].radius_km == 40.0  # unchanged city default when no accuracy given
