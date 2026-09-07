"""End-to-end /api/locate pipeline tests (all network/IO mocked).

Unit tests cover each piece; these guard the WIRING in main.api_locate —
provider lookup -> classify -> signal collectors -> fusion -> reverse-geocode ->
response — so a future plumbing regression (a signal not gathered, a field not
threaded through) is caught.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import geo, geocode
from app.main import app
from app.signals import geoip, traceroute, latency, crowdsource, anchor_latency
from app.models import ProviderResult, Estimate

client = TestClient(app)


def _mock_pipeline(monkeypatch, providers, anchor=None, crowd=None):
    async def fake_lookup_all(ip, c):
        return providers

    async def fake_async_collect(ip, c):
        return []

    async def fake_geocode(lat, lon, c):
        return "부산광역시 금정구 구서동"

    monkeypatch.setattr(geoip, "lookup_all", fake_lookup_all)
    monkeypatch.setattr(traceroute, "collect", fake_async_collect)
    monkeypatch.setattr(latency, "collect", fake_async_collect)
    monkeypatch.setattr(crowdsource, "collect", lambda ip: list(crowd or []))
    monkeypatch.setattr(anchor_latency, "collect", lambda ip: list(anchor or []))
    monkeypatch.setattr(geocode, "reverse_geocode", fake_geocode)


def test_pipeline_anchor_measurement_dominates_and_geocodes(monkeypatch):
    seoul = ProviderResult(source="ip-api", ok=True, lat=37.5, lon=127.0,
                           city="Seoul", country="KR")
    anchor = [Estimate("anchor_latency", 35.2476, 129.0892, 15.0, 0.92,
                       "anchor", {"rtt_ms": 0})]
    _mock_pipeline(monkeypatch, [seoul], anchor=anchor)

    r = client.get("/api/locate", params={"ip": "106.253.34.195"})
    assert r.status_code == 200
    d = r.json()
    # The active-latency measurement overrides the Seoul GeoIP guess...
    assert geo.haversine_km(d["fused_lat"], d["fused_lon"], 35.2476, 129.0892) < 1.0
    # ...the anchor estimate is present, and the center is reverse-geocoded.
    assert any(e["signal"] == "anchor_latency" for e in d["estimates"])
    assert d["address"] == "부산광역시 금정구 구서동"


def test_pipeline_geoip_only_carries_single_source_caveat(monkeypatch):
    seoul = ProviderResult(source="ip-api", ok=True, lat=37.5, lon=127.0,
                           city="Seoul", country="KR")
    _mock_pipeline(monkeypatch, [seoul])  # no anchor, no crowd

    r = client.get("/api/locate", params={"ip": "1.2.3.4"})
    assert r.status_code == 200
    d = r.json()
    assert d["fused_lat"] is not None
    assert any(("GeoIP" in m) or ("동의 GPS" in m) for m in d["messages"]), (
        f"geoip-only result lost its uncertainty caveat through the pipeline; {d['messages']}"
    )
