"""Calibration/accuracy tests for multi-signal fusion.

These are DETERMINISTIC and OFFLINE: they construct Estimate objects directly
(no network), so they encode the desired fusion behaviour as fixed contracts.

Ground truth was observed by running the live server:
  * IP 182.216.201.180 (residential LG POWERCOMM): both GeoIP providers return
    the SAME coordinates = Changwon (35.2317, 128.6822). The real subscriber
    address (부산 강서구 명지동 3250-13) is ~26 km away at ~(35.094, 128.912).
  * IP 8.8.8.8: ip-api -> Ashburn (39.03, -77.5), ipapi.co -> Mountain View
    (37.42301, -122.083352) -- ~3700 km apart.
"""
from __future__ import annotations

import math

import pytest

from app import geo, fusion
from app.models import Estimate, Classification

# --- ground-truth fixtures --------------------------------------------------
CHANGWON = (35.2317, 128.6822)
MYEONGJI = (35.094, 128.912)          # real address, ~26 km from Changwon
ASHBURN = (39.03, -77.5)
MOUNTAIN_VIEW = (37.42301, -122.083352)


def _geoip(source: str, lat: float, lon: float, weight: float, radius: float = 25.0) -> Estimate:
    return Estimate(
        signal=f"geoip:{source}",
        lat=lat,
        lon=lon,
        radius_km=radius,
        weight=weight,
        label=f"{source} test",
        meta={},
    )


def _changwon_pair() -> list[Estimate]:
    return [
        _geoip("ip-api", *CHANGWON, weight=0.8),
        _geoip("ipapi.co", *CHANGWON, weight=0.75),
    ]


def _disagreeing_pair() -> list[Estimate]:
    return [
        _geoip("ip-api", *ASHBURN, weight=0.8),
        _geoip("ipapi.co", *MOUNTAIN_VIEW, weight=0.75),
    ]


# --- Acceptance A: honest circle contains the truth -------------------------
def test_A_changwon_circle_contains_real_address():
    lat, lon, radius = geo.inverse_variance_fuse(_changwon_pair())
    d = geo.haversine_km(lat, lon, *MYEONGJI)
    assert d <= radius, (
        f"real address is {d:.1f} km from fused center but confidence "
        f"radius is only {radius:.1f} km -- circle does not contain the truth"
    )


# --- Acceptance B: disagreement must widen the radius -----------------------
def test_B_disagreeing_sources_widen_radius():
    _lat, _lon, radius = geo.inverse_variance_fuse(_disagreeing_pair())
    # Two sources ~3700 km apart cannot honestly imply a city-scale radius.
    assert radius >= 1000.0, (
        f"sources ~3700 km apart but fused radius is {radius:.1f} km -- "
        f"radius ignores source disagreement"
    )


# --- Acceptance C: correlated agreement must not increase confidence --------
def test_C_correlated_agreement_does_not_shrink_below_single_source():
    # The most reliable single source alone:
    _la, _lo, r_single = geo.inverse_variance_fuse(
        [_geoip("ip-api", *CHANGWON, weight=0.8)]
    )
    _la2, _lo2, r_pair = geo.inverse_variance_fuse(_changwon_pair())
    assert r_pair >= r_single - 1e-6, (
        f"two co-located (correlated) sources shrank the radius to "
        f"{r_pair:.2f} km, below the single-source {r_single:.2f} km -- "
        f"agreement among shared-upstream providers is not independent evidence"
    )


# --- Acceptance D: edge cases still behave ----------------------------------
def test_D_single_estimate_is_usable():
    out = geo.inverse_variance_fuse([_geoip("ip-api", *CHANGWON, weight=0.8)])
    assert out is not None
    lat, lon, radius = out
    assert math.isclose(lat, CHANGWON[0], abs_tol=1e-6)
    assert math.isclose(lon, CHANGWON[1], abs_tol=1e-6)
    assert radius > 0 and math.isfinite(radius)


def test_D_empty_returns_none():
    assert geo.inverse_variance_fuse([]) is None


def test_chained_clusters_still_cover_every_source():
    # A=(35,128), B 45 km N, C 90 km N, all r40. Single-link de-correlation
    # transitively chains A-B-C into one cluster; the fused circle must still
    # CONTAIN the farthest source and never shrink below the 2-source [A,C] case.
    A = _geoip("a", 35.0, 128.0, weight=0.8, radius=40.0)
    B = _geoip("b", 35.0 + 45 / 111.195, 128.0, weight=0.8, radius=40.0)
    C = _geoip("c", 35.0 + 90 / 111.195, 128.0, weight=0.75, radius=40.0)
    lat, lon, r = geo.inverse_variance_fuse([A, B, C])
    r_ac = geo.inverse_variance_fuse([A, C])[2]
    assert r >= geo.haversine_km(lat, lon, C.lat, C.lon) - 1e-6, (
        f"farthest source C excluded from circle (r={r:.1f})"
    )
    assert r >= r_ac - 1e-6, f"adding a midpoint shrank the radius {r_ac:.1f}->{r:.1f}"


def test_nonfinite_weight_returns_none():
    # Defensive: a degenerate estimate must yield None (-> '추정 불가'), never a
    # (nan, nan, nan) tuple that later gets a confident label.
    bad = Estimate("geoip:x", 35.0, 128.0, 40.0, float("nan"), "bad", {})
    assert geo.inverse_variance_fuse([bad]) is None


def test_D_antimeridian_longitude_is_not_averaged_through_zero():
    est = [
        _geoip("ip-api", 0.0, 179.0, weight=0.8),
        _geoip("ipapi.co", 0.0, -179.0, weight=0.75),
    ]
    _lat, lon, _radius = geo.inverse_variance_fuse(est)
    assert abs(lon) > 170.0, f"antimeridian lon collapsed to {lon:.1f} (should be near +/-180)"


def test_B2_circle_covers_both_disagreeing_sources():
    # With N=2 the RMS dispersion equals only HALF the separation, so a
    # quadrature-only radius would exclude the farther source. The coverage
    # floor must make the circle actually contain BOTH input cities.
    lat, lon, radius = geo.inverse_variance_fuse(_disagreeing_pair())
    dA = geo.haversine_km(lat, lon, *ASHBURN)
    dB = geo.haversine_km(lat, lon, *MOUNTAIN_VIEW)
    assert radius >= max(dA, dB) - 1e-6, (
        f"fused circle (r={radius:.0f}) excludes a source it was built from "
        f"(Ashburn {dA:.0f} km, Mountain View {dB:.0f} km)"
    )


def test_C2_correlated_near_jitter_merges_not_shrinks():
    # Two GeoIP points 38 km apart (common centroid jitter for the same
    # upstream DB) overlap (eps = max(50, 25+25)=50 > 38) -> they must merge
    # to one representative, NOT be summed as independent precision.
    _la, _lo, r_single = geo.inverse_variance_fuse(
        [_geoip("ip-api", *CHANGWON, weight=0.8)]
    )
    jittered = (CHANGWON[0] + 0.341, CHANGWON[1])  # ~38 km north
    _la2, _lo2, r_pair = geo.inverse_variance_fuse([
        _geoip("ip-api", *CHANGWON, weight=0.8),
        _geoip("ipapi.co", *jittered, weight=0.75),
    ])
    assert r_pair >= r_single - 1e-6, (
        f"near-agreeing (correlated) sources 38 km apart shrank radius to "
        f"{r_pair:.2f} km below single-source {r_single:.2f} km"
    )


# --- Integration through fusion.fuse ----------------------------------------
def test_fuse_residential_changwon_circle_contains_truth():
    cls = Classification(
        asn="AS17858 LG POWERCOMM", org="LG POWERCOMM",
        is_mobile=False, is_hosting=False, is_proxy=False, note="가정용/일반 ISP 추정",
    )
    res = fusion.fuse("182.216.201.180", cls, _changwon_pair())
    assert res.fused_lat is not None and res.confidence_radius_km is not None
    d = geo.haversine_km(res.fused_lat, res.fused_lon, *MYEONGJI)
    assert d <= res.confidence_radius_km, (
        f"residential fuse: truth {d:.1f} km away, radius {res.confidence_radius_km:.1f} km"
    )


def test_hosting_penalty_recomputes_label():
    # Two co-located city estimates -> fused ~44.7 km ("도시급"). The hosting
    # 1.5x penalty inflates it past 50 km, so the label must be recomputed and
    # no longer claim city-scale confidence.
    cls = Classification(is_hosting=True)
    ests = [
        _geoip("ip-api", *CHANGWON, weight=0.8, radius=40.0),
        _geoip("ipapi.co", *CHANGWON, weight=0.75, radius=40.0),
    ]
    res = fusion.fuse("x", cls, ests)
    assert res.confidence_radius_km > 50.0
    assert res.confidence_label != "도시급", (
        f"radius inflated to {res.confidence_radius_km:.1f} km by hosting penalty "
        f"but label still '{res.confidence_label}'"
    )


def _latency(lat: float, lon: float, radius: float, weight: float = 0.35) -> Estimate:
    return Estimate("latency", lat, lon, radius, weight, "latency test", {})


def test_latency_does_not_move_the_center():
    # Latency's triangulated center is unreliable, so it is excluded from
    # CENTERING — even a disagreeing latency disk must not pull the fused center
    # off the GeoIP consensus. (A gross contradiction may inflate the RADIUS;
    # that honesty behaviour is covered by the contradiction test below.)
    far = (37.93, 128.6822)  # ~300 km north of Changwon
    ests = [
        _geoip("ip-api", *CHANGWON, weight=0.8, radius=40.0),
        _geoip("ipapi.co", *CHANGWON, weight=0.75, radius=40.0),
        _latency(*far, radius=200.0),
    ]
    res = fusion.fuse("1.2.3.4", Classification(), ests)
    assert geo.haversine_km(res.fused_lat, res.fused_lon, *CHANGWON) < 1.0, (
        "latency pulled the fused center away from the geoip consensus"
    )


def test_latency_only_still_produces_an_estimate():
    # When latency is the ONLY signal, it must still be used (fallback), not
    # dropped into '추정 불가'.
    res = fusion.fuse("1.2.3.4", Classification(), [_latency(16.25, 107.15, radius=3848.0)])
    assert res.fused_lat is not None and res.confidence_radius_km is not None


def test_geoip_only_result_warns_single_source():
    # A result resting solely on (correlated) GeoIP is inherently unreliable —
    # esp. for residential IPs, which can be hundreds of km off. The engine must
    # say so, not present a single-source guess as trustworthy.
    res = fusion.fuse("1.2.3.4", Classification(), _changwon_pair())
    assert any(("GeoIP" in m) or ("동의 GPS" in m) for m in res.messages), (
        f"geoip-only result lacks an uncertainty caveat; messages={res.messages}"
    )


def test_crowdsource_result_has_no_single_source_warning():
    crowd = Estimate("crowdsource", 35.2476, 129.0892, 0.1, 0.95,
                     "동의 GPS 정밀 군집", {"cluster": True})
    res = fusion.fuse("1.2.3.4", Classification(), [_geoip("ip-api", *CHANGWON, weight=0.8), crowd])
    assert not any("단일 출처" in m for m in res.messages), (
        f"precise crowdsource result wrongly flagged single-source; {res.messages}"
    )


def test_crowdsource_overrides_wrong_geoip():
    # GeoIP can be catastrophically wrong for residential IPs (e.g. a Busan
    # subscriber placed in Seoul, ~300 km off). A consented GPS cluster must
    # OVERRIDE it and recover precise truth -- the system's core accuracy path.
    guseo = (35.2476, 129.0892)
    seoul_geoip = _geoip("ip-api", 37.482, 127.139, weight=0.8, radius=40.0)  # wrong
    crowd = Estimate("crowdsource", guseo[0], guseo[1], 0.1, 0.95,
                     "동의 GPS 정밀 군집", {"cluster": True})
    res = fusion.fuse("106.253.34.195", Classification(), [seoul_geoip, crowd])
    d = geo.haversine_km(res.fused_lat, res.fused_lon, *guseo)
    assert d < 1.0, f"crowdsource should pin truth, but center is {d:.0f} km off"
    assert res.confidence_radius_km < 5.0
    assert "정밀" in res.confidence_label


def test_latency_contradiction_inflates_radius_and_label():
    # Speed-of-light latency is a HARD physical bound. If GeoIP places the target
    # far outside the measured latency disk, the result cannot honestly stay
    # city-scale — the radius must inflate to cover the physically-plausible
    # region, and the label must follow.
    seoul = _geoip("ip-api", 37.5, 127.0, weight=0.8, radius=40.0)
    tokyo_latency = _latency(35.68, 139.69, radius=500.0)  # ~1160 km from Seoul
    res = fusion.fuse("1.2.3.4", Classification(), [seoul, tokyo_latency])
    assert res.confidence_radius_km >= 1000.0, (
        f"latency contradiction ignored; radius still {res.confidence_radius_km:.0f} km"
    )
    assert res.confidence_label != "도시급"
    assert any(("레이턴시" in m) or ("모순" in m) for m in res.messages)


def test_latency_consistent_does_not_inflate():
    seoul = _geoip("ip-api", 37.5, 127.0, weight=0.8, radius=40.0)
    near_latency = _latency(37.6, 127.1, radius=500.0)  # ~15 km from Seoul
    res = fusion.fuse("1.2.3.4", Classification(), [seoul, near_latency])
    assert res.confidence_radius_km < 100.0, (
        f"consistent latency wrongly inflated radius to {res.confidence_radius_km:.0f} km"
    )


def test_fuse_hosting_8888_not_overconfident():
    cls = Classification(
        asn="AS15169 Google LLC", org="Google Public DNS",
        is_mobile=False, is_hosting=True, is_proxy=False,
    )
    res = fusion.fuse("8.8.8.8", cls, _disagreeing_pair())
    assert res.confidence_radius_km >= 1000.0
    assert res.confidence_label != "도시급"
