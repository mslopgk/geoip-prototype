"""ASN / network classification signal.

Distils the per-provider GeoIP lookups (``ProviderResult``) into a single
``Classification`` that describes the *character* of the target network:
mobile carrier, datacenter/hosting, proxy/VPN, or a plain residential ISP.

This classification gates the rest of the pipeline. In particular a mobile
(CGNAT) network means the IP cannot be tied to an individual user's location,
so ``should_early_return`` returns ``True`` to short-circuit measurement.
"""
from __future__ import annotations

from app.models import ProviderResult, Classification

# Provider whose flags (mobile/hosting/proxy) and ASN/org we trust first.
PREFERRED_SOURCE = "ip-api"

# Korean notes describing each network character.
_NOTE_MOBILE = "모바일 통신사 대역(CGNAT) — 개인 단위 측위 불가"
_NOTE_HOSTING = "데이터센터/클라우드/호스팅 — 실사용자 위치 아닐 가능성 높음"
_NOTE_PROXY = "프록시/VPN 의심"
_NOTE_RESIDENTIAL = "가정용/일반 ISP 추정"


def _first_nonempty(providers: list[ProviderResult], attr: str) -> str | None:
    """Return the first non-empty value of ``attr`` across providers.

    Providers whose ``source == PREFERRED_SOURCE`` are consulted first, then the
    rest in order. ``None`` if no provider supplies a non-empty value.
    """
    preferred = [p for p in providers if p.source == PREFERRED_SOURCE]
    others = [p for p in providers if p.source != PREFERRED_SOURCE]
    for p in preferred + others:
        value = getattr(p, attr, None)
        if value:  # non-empty string
            return value
    return None


def classify(providers: list[ProviderResult]) -> Classification:
    """Merge provider lookups into a single network ``Classification``.

    Flags are OR-ed across all providers (any provider flagging mobile/hosting/
    proxy is enough). ASN/org/network take the first non-empty value, preferring
    the ip-api provider. The Korean ``note`` combines every applicable label.
    """
    providers = providers or []

    is_mobile = any(p.is_mobile for p in providers)
    is_hosting = any(p.is_hosting for p in providers)
    is_proxy = any(p.is_proxy for p in providers)

    asn = _first_nonempty(providers, "asn")
    org = _first_nonempty(providers, "org")
    network = _first_nonempty(providers, "network")

    # Combine applicable notes; fall back to residential when nothing flagged.
    notes: list[str] = []
    if is_mobile:
        notes.append(_NOTE_MOBILE)
    if is_hosting:
        notes.append(_NOTE_HOSTING)
    if is_proxy:
        notes.append(_NOTE_PROXY)
    if not notes:
        notes.append(_NOTE_RESIDENTIAL)
    note = " / ".join(notes)

    return Classification(
        asn=asn,
        org=org,
        is_mobile=is_mobile,
        is_hosting=is_hosting,
        is_proxy=is_proxy,
        network=network,
        note=note,
    )


def should_early_return(c: Classification) -> bool:
    """Mobile (CGNAT) networks cannot be located per-user — stop early."""
    return c.is_mobile
