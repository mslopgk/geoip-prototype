"""Signal-level accuracy tests (geoip, crowdsource, traceroute).

Deterministic and offline: build inputs directly, no network/subprocess.
"""
from __future__ import annotations

import asyncio
import time

from app import geo, store
from app.models import ProviderResult
from app.signals import geoip, crowdsource, traceroute, latency


CHANGWON = (35.2317, 128.6822)
MYEONGJI = (35.094, 128.912)  # real residential address ~26 km from Changwon


def test_city_radius_covers_observed_residential_error():
    p = ProviderResult(
        source="ip-api", ok=True, lat=CHANGWON[0], lon=CHANGWON[1],
        city="Changwon", country="KR",
    )
    ests = geoip.to_estimates([p])
    assert len(ests) == 1
    observed_error = geo.haversine_km(*CHANGWON, *MYEONGJI)  # ~25.9 km
    assert ests[0].radius_km >= observed_error, (
        f"city 1-sigma {ests[0].radius_km} km is tighter than the observed "
        f"residential error {observed_error:.1f} km -- structurally overconfident"
    )


def test_crowdsource_radius_never_below_gps_accuracy(monkeypatch):
    # Two near-identical points whose device GPS was only accurate to 3 km.
    # The cluster spread is tiny, but the radius must NOT claim sub-km precision
    # the GPS never had: device accuracy is a LOWER bound on uncertainty.
    now = time.time()
    pts = [
        {"ip": "1.2.3.4", "lat": 35.1000, "lon": 129.0000, "accuracy_m": 3000.0, "ts": now},
        {"ip": "1.2.3.4", "lat": 35.1001, "lon": 129.0001, "accuracy_m": 3000.0, "ts": now},
    ]
    monkeypatch.setattr(store, "get_points_for_ip", lambda ip, include_prefix=True: pts)
    ests = crowdsource.collect("1.2.3.4")
    assert len(ests) == 1
    assert ests[0].radius_km >= 3.0, (
        f"GPS accurate only to 3 km but crowdsource radius is "
        f"{ests[0].radius_km} km -- falsely precise"
    )


def _codes(hostname: str) -> set:
    iata = traceroute._load_iata()
    return {c for (c, *_rest) in traceroute._hints_from_hostname(hostname, iata)}


def test_interface_tokens_not_geolocated():
    # "gig0" = GigabitEthernet, "tun3" = tunnel -- NOT Rio (GIG) / Tunis (TUN).
    # A real site code in the same host must still be found.
    codes = _codes("gig0.tun3.icn01.example.net")
    assert "GIG" not in codes, "gigabit interface token matched as Rio de Janeiro"
    assert "TUN" not in codes, "tunnel interface token matched as Tunis"
    assert "ICN" in codes, "legit site code ICN should still be found"


def test_wrong_country_iata_rejected():
    # "sea01" -> SEA (Seattle, US), but the host clearly names .kr -> reject the
    # cross-country coincidence; keep the KR code.
    codes = _codes("sea01.r1.icn.kr.example.net")
    assert "SEA" not in codes, "US airport code accepted inside a .kr hostname"
    assert "ICN" in codes


def test_interface_2letter_prefix_not_treated_as_country():
    # "ae-1"/"be-2"/"ge-0" are aggregated-ethernet/bundle interface prefixes, NOT
    # country codes (AE/BE/GE). They must not be read as a contradicting country
    # and suppress the real IATA hop hint.
    assert "ICN" in _codes("ae-1.r02.icn01.bb.example.net"), "'ae' bundle prefix read as UAE, dropped ICN"
    assert "NRT" in _codes("be-2.nrt.example.net"), "'be' bundle prefix read as Belgium, dropped NRT"
    assert "LAX" in _codes("ge-0-0-1.lax.example.net"), "'ge' bundle prefix read as Georgia, dropped LAX"


# --- latency: suppress non-informative (continental-scale) triangulation -----
def _probe(lat, lon, min_rtt):
    return {"probe": {"latitude": lat, "longitude": lon},
            "result": {"stats": {"min": min_rtt}}}


def _patch_measure(monkeypatch, probes):
    async def fake_measure(*a, **k):
        return probes
    monkeypatch.setattr(latency, "measure", fake_measure)


def test_latency_suppressed_when_noninformative(monkeypatch):
    # All probes far with high RTT -> every disk is continental (~8000 km) -> the
    # triangulated radius is meaningless and must be SUPPRESSED, not emitted as a
    # bogus "constraints satisfied" estimate.
    probes = [_probe(0.0, 0.0, 80.0), _probe(0.0, 60.0, 82.0), _probe(40.0, 0.0, 85.0)]
    _patch_measure(monkeypatch, probes)
    out = asyncio.run(latency.collect("1.2.3.4", None))
    assert out == [], f"non-informative latency disk should be suppressed, got {out}"


def test_latency_kept_when_informative(monkeypatch):
    # Tight, mutually-consistent low-RTT probes around a point -> a usable
    # regional estimate should still be produced.
    probes = [_probe(35.0, 128.0, 3.0), _probe(35.5, 128.5, 3.2), _probe(34.5, 127.5, 3.1)]
    _patch_measure(monkeypatch, probes)
    out = asyncio.run(latency.collect("1.2.3.4", None))
    assert len(out) == 1
    assert out[0].radius_km <= 3000.0


def test_crowdsource_prefers_exact_ip_over_noisy_neighbor(monkeypatch):
    # A household's OWN GPS (exact IP) must win over a noisier /24 neighbour at a
    # different address — even when the neighbour contributed more points. Mixing
    # them lets a neighbour drag the result to the wrong dong.
    now = time.time()
    ip = "106.253.34.195"
    busan = (35.2476, 129.0892)     # our household (구서동)
    seoul = (37.5000, 127.0400)     # a /24 neighbour, ~320 km away
    pts = [
        {"ip": ip, "lat": busan[0], "lon": busan[1], "accuracy_m": 20.0, "ts": now},
        {"ip": ip, "lat": busan[0] + 5e-4, "lon": busan[1] + 5e-4, "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.50", "lat": seoul[0], "lon": seoul[1], "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.50", "lat": seoul[0] + 5e-4, "lon": seoul[1], "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.50", "lat": seoul[0], "lon": seoul[1] + 5e-4, "accuracy_m": 20.0, "ts": now},
    ]
    monkeypatch.setattr(store, "get_points_for_ip", lambda i, include_prefix=True: pts)
    ests = crowdsource.collect(ip)
    assert len(ests) == 1
    d = geo.haversine_km(ests[0].lat, ests[0].lon, *busan)
    assert d < 1.0, (
        f"exact-IP household location should win, but estimate is {d:.0f} km off "
        f"(noisy /24 neighbour polluted the result)"
    )


def test_fresh_household_cluster_beats_stale_exact_point(monkeypatch):
    # After a DHCP lease change the household's NEW IP (.197, same /24) has a
    # fresh GPS cluster, while the OLD exact IP (.195) retains a 2-year-stale fix.
    # Time-decay must let the fresh cluster win — a stale exact point must NOT
    # unconditionally shadow it.
    now = time.time()
    ip = "106.253.34.195"
    stale = (35.0, 128.0)            # 2-year-old exact-IP fix
    fresh = (35.2476, 129.0892)      # fresh sibling-IP household cluster
    pts = [
        {"ip": ip, "lat": stale[0], "lon": stale[1], "accuracy_m": 20.0, "ts": now - 730 * 86400},
        {"ip": "106.253.34.197", "lat": fresh[0], "lon": fresh[1], "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.197", "lat": fresh[0] + 5e-4, "lon": fresh[1], "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.197", "lat": fresh[0], "lon": fresh[1] + 5e-4, "accuracy_m": 20.0, "ts": now},
    ]
    monkeypatch.setattr(store, "get_points_for_ip", lambda i, include_prefix=True: pts)
    est = crowdsource.collect(ip)[0]
    assert geo.haversine_km(est.lat, est.lon, *fresh) < 5.0, (
        "a 2-year-stale exact-IP point shadowed the fresh household cluster"
    )


def test_sparse_unverified_point_not_overconfident(monkeypatch):
    # A single, uncorroborated GPS fix with no reported accuracy must not claim
    # sub-km (street-level) precision — the radius must match its low-trust label.
    now = time.time()
    pts = [{"ip": "1.2.3.4", "lat": 35.1, "lon": 129.0, "accuracy_m": None, "ts": now}]
    monkeypatch.setattr(store, "get_points_for_ip", lambda i, include_prefix=True: pts)
    est = crowdsource.collect("1.2.3.4")[0]
    assert est.meta["cluster"] is False
    assert est.radius_km >= 1.0, (
        f"lone unverified point claims {est.radius_km} km precision"
    )


def test_crowdsource_prefix_only_match_is_coarsened(monkeypatch):
    # Privacy: a /24 NEIGHBOUR (no exact-IP contribution of their own) must not
    # receive a household's street-level GPS — coarsen to neighbourhood level.
    now = time.time()
    busan = (35.2476, 129.0892)
    pts = [
        {"ip": "106.253.34.50", "lat": busan[0], "lon": busan[1], "accuracy_m": 10.0, "ts": now},
        {"ip": "106.253.34.50", "lat": busan[0] + 5e-4, "lon": busan[1], "accuracy_m": 10.0, "ts": now},
    ]
    monkeypatch.setattr(store, "get_points_for_ip", lambda i, include_prefix=True: pts)
    est = crowdsource.collect("106.253.34.195")[0]  # querying IP has NO exact points
    assert est.meta["exact_ip"] is False
    assert est.radius_km >= 2.0, "prefix-only neighbour exposed street-level precision"


def test_crowdsource_falls_back_to_prefix_when_no_exact(monkeypatch):
    # When the exact IP has no data (e.g. dynamic IP just changed), the /24
    # neighbour data is still used as a coarse fallback.
    now = time.time()
    busan = (35.2476, 129.0892)
    pts = [
        {"ip": "106.253.34.50", "lat": busan[0], "lon": busan[1], "accuracy_m": 20.0, "ts": now},
        {"ip": "106.253.34.50", "lat": busan[0] + 5e-4, "lon": busan[1], "accuracy_m": 20.0, "ts": now},
    ]
    monkeypatch.setattr(store, "get_points_for_ip", lambda i, include_prefix=True: pts)
    ests = crowdsource.collect("106.253.34.195")
    assert len(ests) == 1
    assert geo.haversine_km(ests[0].lat, ests[0].lon, *busan) < 1.0


def test_contribution_shared_across_household_prefix():
    # A consented GPS point from one address in a /24 must help a sibling IP in
    # the same /24 (same household pool) -- this is why a contribution from
    # 106.253.34.195 also locates 106.253.34.197. Uses TEST-NET-3 to avoid
    # colliding with real data, and cleans up after itself.
    ip_a, ip_b = "203.0.113.10", "203.0.113.20"
    store.init_db()
    store.add_contribution(
        ip=ip_a, lat=35.2476, lon=129.0892, accuracy_m=20.0,
        tz=None, lang=None, ua_hash=None, consent="test",
    )
    try:
        pts = store.get_points_for_ip(ip_b, include_prefix=True)
        assert any(abs(p["lat"] - 35.2476) < 1e-6 for p in pts), (
            "sibling IP in the same /24 should see the household contribution"
        )
    finally:
        store.delete_for_ip(ip_a)
