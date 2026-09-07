"""Active-latency proximity signal (operator-vantage geolocation).

Verified physics: a target answering ICMP at near-zero RTT from a known-location
measurement vantage is physically adjacent to it (a 300 km link alone is >3 ms
round-trip). Locating such a target at the vantage EARNS accuracy from
measurement (not address seeding) and only fires for local targets — remote /
unreachable targets and an unconfigured vantage leave the signal silent, so the
existing pipeline is unchanged when it does not apply.
"""
from __future__ import annotations

from app import geo, fusion
from app.models import Estimate, Classification
from app.signals import anchor_latency

VANTAGE = (35.2476, 129.0892)  # configured measurement vantage (Busan Geumjeong)


def test_local_target_located_at_vantage(monkeypatch):
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 0.0)
    ests = anchor_latency.collect("106.253.34.195", vantage=VANTAGE)
    assert len(ests) == 1
    e = ests[0]
    assert e.signal == "anchor_latency"
    assert geo.haversine_km(e.lat, e.lon, *VANTAGE) < 1.0
    assert e.radius_km <= 30.0
    assert e.weight >= 0.9


def test_remote_target_is_silent(monkeypatch):
    # High RTT => not local to the vantage => no (false) location.
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 80.0)
    assert anchor_latency.collect("8.8.8.8", vantage=VANTAGE) == []


def test_disabled_without_vantage(monkeypatch):
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 0.0)
    assert anchor_latency.collect("106.253.34.195", vantage=None) == []


def test_unreachable_target_is_silent(monkeypatch):
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: None)
    assert anchor_latency.collect("1.2.3.4", vantage=VANTAGE) == []


def test_radius_grows_with_rtt(monkeypatch):
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 3.0)
    e = anchor_latency.collect("182.216.201.180", vantage=VANTAGE)[0]
    # 3 ms one-way fiber bound ~ a couple hundred km; honest, finite, > the floor.
    assert 30.0 < e.radius_km < 1000.0


def test_radius_subtracts_last_mile_floor(monkeypatch):
    # The honest distance bound uses only the PROPAGATION part of the RTT — a
    # conservative last-mile floor is subtracted so the circle isn't needlessly
    # huge, while staying an upper bound that still contains a same-metro truth.
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 3.0)
    e = anchor_latency.collect("182.216.201.180", vantage=VANTAGE)[0]
    raw_sol = geo.rtt_to_max_distance_km(3.0)  # ~296.8 km, no last-mile subtraction
    assert e.radius_km < raw_sol - 1.0, "last-mile floor not applied (radius too loose)"
    assert e.radius_km >= geo.haversine_km(*VANTAGE, 35.094, 128.912), (
        "radius must still contain the same-metro Myeongji truth (~23.5 km)"
    )


def test_radius_capped_at_metro_scale(monkeypatch):
    # radius_km is a ~1-sigma (not a hard SoL ceiling). A target local to the
    # vantage (<=5 ms) is same-metro, so the radius is capped at metro scale —
    # useful, and still containing a same-metro truth.
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 5.0)
    e = anchor_latency.collect("182.216.201.180", vantage=VANTAGE)[0]
    assert e.radius_km <= anchor_latency.ANCHOR_MAX_LOCAL_KM
    assert e.radius_km >= geo.haversine_km(*VANTAGE, 35.094, 128.912)  # contains Myeongji


def test_near_floor_rtt_gives_floor_radius(monkeypatch):
    # RTT at/below the last-mile floor implies ~0 propagation -> floor radius.
    monkeypatch.setattr(anchor_latency, "_ping_min_rtt", lambda ip: 1.0)
    e = anchor_latency.collect("106.253.34.195", vantage=VANTAGE)[0]
    assert e.radius_km == anchor_latency.ANCHOR_FLOOR_KM


WIN_BOGUS = """Pinging 1.2.3.4 with 32 bytes of data:
Reply from 192.168.0.1: bytes=32 time<1ms TTL=64
Request timed out.
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 0ms, Average = 0ms"""

WIN_GOOD = """Pinging 1.2.3.4 with 32 bytes of data:
Reply from 1.2.3.4: bytes=32 time<1ms TTL=64
Reply from 1.2.3.4: bytes=32 time=2ms TTL=64"""

LINUX_OUT = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=34.5 ms
rtt min/avg/max/mdev = 33.215/34.123/35.001/0.700 ms"""


def test_parse_rtt_ignores_non_target_responder():
    # An on-link NAT/proxy answering for a remote target must NOT count: the
    # reply did not come from the target, so no RTT is attributable to it.
    assert anchor_latency._parse_min_rtt(WIN_BOGUS, "1.2.3.4") is None


def test_collect_silent_when_only_non_target_replies():
    out = anchor_latency.collect(
        "1.2.3.4", vantage=VANTAGE,
        ping_fn=lambda ip: anchor_latency._parse_min_rtt(WIN_BOGUS, ip),
    )
    assert out == [], "remote target pulled to vantage by a non-target responder"


def test_parse_rtt_from_real_target_reply():
    assert anchor_latency._parse_min_rtt(WIN_GOOD, "1.2.3.4") == 1.0


def test_parse_rtt_linux_decimal():
    # Source-aware + decimal parsing also makes the signal work on POSIX ping.
    assert anchor_latency._parse_min_rtt(LINUX_OUT, "8.8.8.8") == 34.5


def test_fusion_anchor_measurement_beats_wrong_geoip():
    # The measurement must OVERRIDE a confidently-wrong Seoul GeoIP guess.
    seoul = Estimate("geoip:ip-api", 37.5, 127.0, 40.0, 0.8, "seoul", {})
    anchor = Estimate("anchor_latency", VANTAGE[0], VANTAGE[1], 15.0, 0.92, "anchor", {"rtt_ms": 0})
    res = fusion.fuse("106.253.34.195", Classification(), [seoul, anchor])
    assert geo.haversine_km(res.fused_lat, res.fused_lon, *VANTAGE) < 1.0, (
        f"anchor measurement did not override GeoIP; center {res.fused_lat},{res.fused_lon}"
    )
    assert res.confidence_radius_km <= 30.0
