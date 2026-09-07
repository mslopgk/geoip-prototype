"""Security-hardening tests (from the security review)."""
from __future__ import annotations

from app import main


def test_is_global_unicast_blocks_special_ranges():
    # Only globally-routable UNICAST addresses may be actively probed.
    for bad in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1",
                "100.64.0.1",          # CGNAT
                "224.0.0.1",           # multicast
                "0.0.0.0", "::1", "fe80::1",
                "64:ff9b::7f00:1",     # NAT64 (reserved)
                "not-an-ip"):
        assert not main._is_global_unicast(bad), f"{bad} should NOT be probe-eligible"
    for good in ("8.8.8.8", "1.1.1.1", "106.253.34.195"):
        assert main._is_global_unicast(good), f"{good} should be probe-eligible"


def test_valid_coord_rejects_insane_values():
    assert main._valid_coord(35.2476, 129.0892)
    assert main._valid_coord(-90.0, 180.0)
    assert not main._valid_coord(9999.0, 0.0)      # out of range
    assert not main._valid_coord(0.0, -200.0)
    assert not main._valid_coord(float("nan"), 0.0)
    assert not main._valid_coord(float("inf"), 0.0)


def test_contribute_rejects_out_of_range_coords():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    # validation happens before any network call -> offline 400
    r = client.post("/api/contribute", json={"consent": True, "lat": 9999, "lon": 0})
    assert r.status_code == 400


def test_locate_caches_repeated_lookups(monkeypatch):
    # A repeated lookup of the same IP must hit the cache (cheap) instead of
    # re-running the whole expensive pipeline — DoS mitigation + UX speedup.
    from fastapi.testclient import TestClient
    from app.signals import geoip, traceroute, latency, crowdsource, anchor_latency
    from app import geocode
    from app.models import ProviderResult

    main._result_cache.clear()
    calls = {"n": 0}

    async def fake_lookup(ip, c):
        calls["n"] += 1
        return [ProviderResult(source="ip-api", ok=True, lat=37.5, lon=127.0,
                               city="Seoul", country="KR")]

    async def fake_async(ip, c):
        return []

    async def fake_geo(lat, lon, c):
        return None

    monkeypatch.setattr(geoip, "lookup_all", fake_lookup)
    monkeypatch.setattr(traceroute, "collect", fake_async)
    monkeypatch.setattr(latency, "collect", fake_async)
    monkeypatch.setattr(crowdsource, "collect", lambda ip: [])
    monkeypatch.setattr(anchor_latency, "collect", lambda ip: [])
    monkeypatch.setattr(geocode, "reverse_geocode", fake_geo)

    client = TestClient(main.app)
    r1 = client.get("/api/locate", params={"ip": "8.8.8.8"})
    r2 = client.get("/api/locate", params={"ip": "8.8.8.8"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, "second identical lookup should be served from cache"


def test_result_cache_is_bounded(monkeypatch):
    # The DoS-mitigation cache must not itself grow unboundedly under a flood of
    # distinct IPs.
    main._result_cache.clear()
    monkeypatch.setattr(main, "RESULT_CACHE_MAX", 10)
    for i in range(50):
        main._cache_put(f"203.0.113.{i % 256}.{i}", {"ip": str(i)})
    assert len(main._result_cache) <= 10


def test_delete_requires_csrf_header():
    # A bodyless cross-site POST (simple request) must NOT be able to erase data;
    # require a custom header that a cross-origin request can't set without a
    # (failing) CORS preflight.
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.post("/api/delete")  # no CSRF header
    assert r.status_code == 403


def test_self_public_ip_uses_https(monkeypatch):
    import asyncio

    captured = {}

    class _R:
        def json(self):
            return {"ip": "203.0.113.9"}

    class _C:
        async def get(self, url, **kwargs):
            captured["url"] = url
            return _R()

    ip = asyncio.run(main._self_public_ip(_C()))
    assert captured["url"].startswith("https://"), "self-IP lookup must use HTTPS"
    assert ip == "203.0.113.9"

