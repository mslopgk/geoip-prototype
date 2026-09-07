"""Shared data contracts for the multi-signal IP geolocation engine.

Every signal module produces ``Estimate`` objects. ``fusion.fuse`` consumes a
list of them plus the ``Classification`` and returns a ``LocateResult``.

These dataclasses ARE the interface contract between modules. Do not change
field names without updating every consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Estimate:
    """A single location estimate produced by one signal source.

    radius_km is a rough 1-sigma uncertainty used for inverse-variance fusion;
    weight in [0, 1] is the source reliability multiplier.
    """

    signal: str          # "geoip:ip-api", "geoip:ipapi.co", "traceroute", "latency", "crowdsource"
    lat: float
    lon: float
    radius_km: float
    weight: float
    label: str           # human-readable basis, e.g. "ip-api.com -> Ashburn, US"
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderResult:
    """Normalized output of one GeoIP provider lookup."""

    source: str
    ok: bool
    lat: Optional[float] = None
    lon: Optional[float] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    asn: Optional[str] = None
    org: Optional[str] = None
    is_mobile: bool = False
    is_hosting: bool = False
    is_proxy: bool = False
    network: Optional[str] = None      # CIDR if known
    accuracy_radius_km: Optional[float] = None  # provider-reported precision, if any
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Classification:
    """ASN / network classification of the target IP (gating + confidence)."""

    asn: Optional[str] = None
    org: Optional[str] = None
    is_mobile: bool = False
    is_hosting: bool = False
    is_proxy: bool = False
    network: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LocateResult:
    """Final response of the /api/locate pipeline."""

    ip: str
    classification: Classification
    estimates: list  # list[Estimate]
    fused_lat: Optional[float] = None
    fused_lon: Optional[float] = None
    confidence_radius_km: Optional[float] = None
    confidence_label: str = ""       # e.g. "정밀(GPS)", "도시급", "광역", "불가(모바일)"
    early_return: bool = False
    messages: list = field(default_factory=list)  # list[str]
    address: Optional[str] = None    # human-readable reverse-geocoded place, if any

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "classification": self.classification.to_dict(),
            "estimates": [e.to_dict() for e in self.estimates],
            "fused_lat": self.fused_lat,
            "fused_lon": self.fused_lon,
            "confidence_radius_km": self.confidence_radius_km,
            "confidence_label": self.confidence_label,
            "early_return": self.early_return,
            "messages": self.messages,
            "address": self.address,
        }
