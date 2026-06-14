"""GeoIP provider signal.

Queries two public, no-auth GeoIP databases concurrently and normalizes their
responses into :class:`ProviderResult` objects, then converts those into
:class:`Estimate` objects for the fusion pipeline.

Providers:
  * ip-api.com   - returns mobile/proxy/hosting flags (useful for gating).
  * ipapi.co     - second opinion; no mobile/proxy flags.

Both lookups are defensive: any network or parsing failure degrades to a
``ProviderResult(ok=False, ...)`` and ``to_estimates`` simply skips it. No
exception ever escapes these functions.
"""
from __future__ import annotations

import asyncio

import httpx

from app.models import ProviderResult, Estimate

# Fields requested from ip-api.com. ``query`` echoes back the resolved IP,
# which we rely on when no IP is supplied (lookup of this server's own IP).
_IPAPI_FIELDS = (
    "status,message,country,countryCode,regionName,city,lat,lon,"
    "isp,org,as,asname,mobile,proxy,hosting,query"
)


async def _lookup_ip_api(ip: str, client: httpx.AsyncClient) -> ProviderResult:
    """Provider A: ip-api.com (HTTP, free tier)."""
    try:
        if ip:
            url = f"http://ip-api.com/json/{ip}?fields={_IPAPI_FIELDS}"
        else:
            # No IP -> ip-api resolves THIS server's public IP.
            url = f"http://ip-api.com/json/?fields={_IPAPI_FIELDS}"
        r = await client.get(url, timeout=10.0)
        data = r.json()
        return ProviderResult(
            source="ip-api",
            ok=data.get("status") == "success",
            lat=data.get("lat"),
            lon=data.get("lon"),
            city=data.get("city"),
            region=data.get("regionName"),
            country=data.get("countryCode"),
            asn=data.get("as"),
            org=data.get("org") or data.get("isp"),
            is_mobile=bool(data.get("mobile", False)),
            is_hosting=bool(data.get("hosting", False)),
            is_proxy=bool(data.get("proxy", False)),
            raw=data,
        )
    except Exception as e:  # noqa: BLE001 - degrade to a not-ok result
        return ProviderResult(source="ip-api", ok=False, raw={"error": str(e)})


async def _lookup_ipapi_co(ip: str, client: httpx.AsyncClient) -> ProviderResult:
    """Provider B: ipapi.co (HTTPS, free tier)."""
    try:
        if ip:
            url = f"https://ipapi.co/{ip}/json/"
        else:
            # No IP -> ipapi.co resolves THIS server's public IP.
            url = "https://ipapi.co/json/"
        r = await client.get(url, timeout=10.0)
        data = r.json()
        latitude = data.get("latitude")
        ok = ("error" not in data) and (latitude is not None)
        return ProviderResult(
            source="ipapi.co",
            ok=ok,
            lat=latitude,
            lon=data.get("longitude"),
            city=data.get("city"),
            region=data.get("region"),
            country=data.get("country_code"),
            asn=data.get("asn"),
            org=data.get("org"),
            # ipapi.co does not expose mobile/proxy flags -> leave False.
            network=data.get("network"),
            raw=data,
        )
    except Exception as e:  # noqa: BLE001 - degrade to a not-ok result
        return ProviderResult(source="ipapi.co", ok=False, raw={"error": str(e)})


async def lookup_all(ip: str, client: httpx.AsyncClient) -> list[ProviderResult]:
    """Query both GeoIP providers concurrently and return their results.

    ``ip`` may be falsy/empty, in which case each provider resolves the
    server's own public IP. Always returns a two-element list (one
    ``ProviderResult`` per provider); failing providers report ``ok=False``.
    """
    results = await asyncio.gather(
        _lookup_ip_api(ip, client),
        _lookup_ipapi_co(ip, client),
        return_exceptions=True,
    )

    out: list[ProviderResult] = []
    for source, res in zip(("ip-api", "ipapi.co"), results):
        if isinstance(res, BaseException):
            out.append(ProviderResult(source=source, ok=False, raw={"error": str(res)}))
        else:
            out.append(res)
    return out


def to_estimates(providers: list[ProviderResult]) -> list[Estimate]:
    """Convert usable provider results into fusion ``Estimate`` objects.

    Skips any provider that is not ok or lacks coordinates. The reported
    ``radius_km`` reflects the granularity of the locality (city < region <
    country) and ``weight`` reflects provider reliability. May return [].
    """
    estimates: list[Estimate] = []
    for p in providers:
        if not p.ok or p.lat is None or p.lon is None:
            continue

        if p.city:
            radius_km = 25.0
        elif p.region:
            radius_km = 120.0
        else:
            radius_km = 600.0

        weight = 0.8 if p.source == "ip-api" else 0.75
        label = f"{p.source} → {p.city or p.region or p.country}"

        estimates.append(
            Estimate(
                signal=f"geoip:{p.source}",
                lat=p.lat,
                lon=p.lon,
                radius_km=radius_km,
                weight=weight,
                label=label,
                meta={
                    "asn": p.asn,
                    "org": p.org,
                    "country": p.country,
                    "city": p.city,
                },
            )
        )
    return estimates
