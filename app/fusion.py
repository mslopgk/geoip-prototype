"""Multi-signal fusion: combine all per-signal Estimates into one LocateResult.

This is the decision layer of the pipeline. It applies the gating rules
(mobile/CGNAT cannot be located), prioritises consent-based crowdsourced GPS
when available, otherwise performs inverse-variance fusion of the remaining
signals, applies ASN-based confidence penalties, and runs a physical
sanity-check against the measured latency constraint.

Contract: consumes ``list[Estimate]`` plus a ``Classification`` and returns a
``LocateResult`` (see app.models). Pure decision logic - no I/O or network.
"""
from __future__ import annotations

from app.models import Estimate, Classification, LocateResult
from app import geo


def _band_label(radius_km: float) -> str:
    """Map a confidence radius to a coarse, honest precision band."""
    if radius_km < 50:
        return "도시급"
    if radius_km < 300:
        return "광역"
    return "국가/대륙급"


def fuse(ip: str, classification: Classification, estimates: list) -> LocateResult:
    """Fuse all signal estimates into a single final location result.

    Args:
        ip: target IP string (echoed back in the result).
        classification: ASN/network classification used for gating and
            confidence penalties.
        estimates: list[Estimate] gathered from every signal collector.

    Returns:
        A LocateResult. ``early_return`` is True for un-locatable cases
        (mobile/CGNAT). When no signal is usable the result carries a
        "추정 불가" label and no fused coordinates.
    """
    messages: list = []

    # 1) Mobile / CGNAT early return -------------------------------------
    # Carrier-grade NAT pools map many subscribers behind one address, so
    # per-user geolocation is fundamentally impossible. Bail out early.
    if classification.is_mobile:
        return LocateResult(
            ip=ip,
            classification=classification,
            estimates=estimates,
            early_return=True,
            confidence_label="불가(모바일/CGNAT)",
            messages=["모바일 통신사 대역(CGNAT) — 개인 단위 위치 추적 불가"],
        )

    # 2) Crowdsourced GPS dominates if present ---------------------------
    # Consent-based GPS clusters are by far the most precise signal; when we
    # have one, it overrides the noisier geoip/latency estimates entirely.
    crowd = [e for e in estimates if e.signal.startswith("crowdsource")]
    # Active-latency proximity is a physical MEASUREMENT (target is adjacent to a
    # known-location vantage), so it dominates the noisier GeoIP guess — second
    # only to consent GPS.
    anchor = [e for e in estimates if e.signal == "anchor_latency"]
    # ``label`` is fixed for the crowdsource/anchor branches (they describe a
    # measurement, not a radius band); for the inverse-variance branch it is
    # derived from the radius *after* the penalties below, so it stays honest
    # when inflated.
    label = ""
    band_from_radius = False
    geoip_only = False
    measured = False
    if crowd:
        best = max(crowd, key=lambda e: e.weight)
        fused_lat, fused_lon, radius = best.lat, best.lon, best.radius_km
        # Reflect the actual data quality: a DBSCAN cluster is precise, but
        # sparse/scattered points are only a rough hint. crowdsource encodes
        # this in meta["cluster"].
        if best.meta.get("cluster"):
            label = "정밀(GPS 군집)"
            messages.append("동의 기반 GPS 군집으로 정밀 측위")
        else:
            label = "GPS 희소(낮은 신뢰)"
            messages.append("동의 GPS 데이터가 희소·분산 — 신뢰도 낮음")
    elif anchor:
        # Physical proximity measurement to a configured vantage. Overrides
        # GeoIP (which may be hundreds of km off for residential IPs).
        best = max(anchor, key=lambda e: e.weight)
        fused_lat, fused_lon, radius = best.lat, best.lon, best.radius_km
        measured = True
        label = "정밀(능동 측정)" if radius < 50 else "측정 기반(광역)"
        messages.append(
            "능동 레이턴시 측정으로 측정 vantage 인접 확인 — GeoIP 추정보다 우선"
        )
    else:
        # Inverse-variance fuse the point-like signals (geoip, traceroute).
        # Latency triangulation is a coarse CONSTRAINT with an unreliable center,
        # not a point estimate — feeding it in lets the coverage floor inflate an
        # otherwise-tight result toward a noisy blob. It is used only as the
        # physical cross-check below. Fall back to it only when nothing else
        # exists, so a latency-only IP still yields some (coarse) answer.
        others = [
            e for e in estimates
            if not e.signal.startswith("crowdsource") and e.signal != "latency"
        ]
        if not others:
            others = [e for e in estimates if e.signal == "latency"]
        fused = geo.inverse_variance_fuse(others)
        if fused is None:
            return LocateResult(
                ip=ip,
                classification=classification,
                estimates=estimates,
                confidence_label="추정 불가",
                messages=["사용 가능한 위치 신호 없음"],
            )
        fused_lat, fused_lon, radius = fused
        band_from_radius = True
        # No independent corroboration: the answer rests solely on (correlated)
        # GeoIP databases. Their agreement is not evidence of correctness — for
        # residential IPs the true location can be hundreds of km away (e.g. a
        # Busan subscriber confidently placed in Seoul). Flag it honestly.
        geoip_only = bool(others) and all(e.signal.startswith("geoip") for e in others)

    # 3) ASN flag warnings + confidence penalty --------------------------
    # Hosting/proxy IPs frequently do not reflect the real user location, so
    # we inflate the confidence radius and surface a warning to the caller.
    # (A direct measurement — consent GPS or active latency — is not second-
    # guessed by ASN flags or the coarse latency cross-check below.)
    if classification.is_hosting and not measured:
        messages.append("데이터센터/호스팅 IP — 실제 사용자 위치가 아닐 수 있음")
        radius = radius * 1.5
    if classification.is_proxy and not measured:
        messages.append("프록시/VPN 의심 — 위치 신뢰도 낮음")
        radius = radius * 1.5

    # 4) Latency physical cross-check ------------------------------------
    # Speed-of-light latency is a HARD physical bound: the target must lie within
    # the measured latency disk. If the GeoIP-fused center sits well outside it,
    # GeoIP is physically implausible (gross DB error, VPN/proxy, routing
    # anomaly). Don't just warn — inflate the radius so the confidence circle
    # still covers the physically-plausible region (the whole latency disk).
    lat_est = next((e for e in estimates if e.signal == "latency"), None)
    if lat_est is not None and not crowd and not measured:
        d = geo.haversine_km(fused_lat, fused_lon, lat_est.lat, lat_est.lon)
        if d > lat_est.radius_km * 1.5:
            radius = max(radius, d + lat_est.radius_km)
            messages.append(
                "융합 위치가 실측 레이턴시 제약과 모순 — 신뢰반경을 물리적 "
                "가능 범위로 확대(VPN/프록시/라우팅 이상 또는 GeoIP 오류 가능성)"
            )

    # Derive the precision band from the FINAL (post-penalty, post-crosscheck)
    # radius so an inflated radius is never mislabeled as more precise than it is.
    if band_from_radius:
        label = _band_label(radius)

    if geoip_only:
        messages.append(
            "GeoIP 단일 출처 추정 — 실제 위치와 수백 km 차이날 수 있습니다"
            "(특히 주거용 IP). 정밀 측위는 /contribute 동의 GPS 기여가 필요합니다."
        )

    return LocateResult(
        ip=ip,
        classification=classification,
        estimates=estimates,
        fused_lat=fused_lat,
        fused_lon=fused_lon,
        confidence_radius_km=radius,
        confidence_label=label,
        early_return=False,
        messages=messages,
    )
