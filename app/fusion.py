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
    if crowd:
        best = max(crowd, key=lambda e: e.weight)
        fused_lat, fused_lon, radius = best.lat, best.lon, best.radius_km
        label = "정밀(GPS 군집)"
        messages.append("동의 기반 GPS 데이터로 정밀 측위")
    else:
        # Inverse-variance fuse every non-crowdsource estimate.
        others = [e for e in estimates if not e.signal.startswith("crowdsource")]
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
        label = "도시급" if radius < 50 else ("광역" if radius < 300 else "국가/대륙급")

    # 3) ASN flag warnings + confidence penalty --------------------------
    # Hosting/proxy IPs frequently do not reflect the real user location, so
    # we inflate the confidence radius and surface a warning to the caller.
    if classification.is_hosting:
        messages.append("데이터센터/호스팅 IP — 실제 사용자 위치가 아닐 수 있음")
        radius = radius * 1.5
    if classification.is_proxy:
        messages.append("프록시/VPN 의심 — 위치 신뢰도 낮음")
        radius = radius * 1.5

    # 4) Latency physical cross-check ------------------------------------
    # The measured latency implies a hard physical distance ceiling. If the
    # fused location sits well outside it, routing/VPN tricks are likely.
    lat_est = next((e for e in estimates if e.signal == "latency"), None)
    if lat_est is not None and not crowd:
        d = geo.haversine_km(fused_lat, fused_lon, lat_est.lat, lat_est.lon)
        if d > lat_est.radius_km * 1.5:
            messages.append(
                "융합 위치가 실측 레이턴시 제약과 모순 — VPN/프록시/라우팅 이상 가능성"
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
