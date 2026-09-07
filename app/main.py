"""FastAPI application wiring all signals into the locate/contribute pipeline.

Run:  uvicorn app.main:app --reload   (from the project root)
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app import store
from app.signals import geoip, asn, traceroute, latency, crowdsource, anchor_latency
from app import fusion
from app import geocode

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

# Short-TTL cache of /api/locate results, keyed by target IP. Repeated lookups
# of the same IP (common during testing) are served cheaply instead of re-
# running the whole pipeline — also blunts the amplification/DoS surface.
RESULT_CACHE_TTL = 120.0
RESULT_CACHE_MAX = 1024
_result_cache: dict = {}


def _cache_get(key: str):
    """Return a fresh cached payload for ``key`` or None."""
    hit = _result_cache.get(key)
    if hit and (time.time() - hit[0]) < RESULT_CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key: str, payload: dict) -> None:
    """Cache ``payload`` under ``key``, pruning expired entries and bounding size
    (FIFO) so a flood of distinct IPs cannot grow the cache without limit."""
    now = time.time()
    _result_cache[key] = (now, payload)
    # Drop expired entries first, then oldest-inserted until under the cap.
    for k in [k for k, (ts, _) in _result_cache.items() if now - ts >= RESULT_CACHE_TTL]:
        _result_cache.pop(k, None)
    while len(_result_cache) > RESULT_CACHE_MAX:
        _result_cache.pop(next(iter(_result_cache)), None)

# Bound concurrent ACTIVE measurements (each forks ping/tracert + outbound
# probes) so a flood of /api/locate calls cannot exhaust the host's processes.
ACTIVE_MEASUREMENT_LIMIT = 8
_active_sem = asyncio.Semaphore(ACTIVE_MEASUREMENT_LIMIT)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Initialise the consent store on startup (modern lifespan handler; the old
    # @app.on_event("startup") form is deprecated in current FastAPI).
    store.init_db()
    yield


app = FastAPI(
    title="Multi-signal IP Geolocation (research prototype)",
    lifespan=_lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_valid_ip(ip: str) -> bool:
    """True if ``ip`` parses as a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _is_routable(ip: str) -> bool:
    try:
        obj = ipaddress.ip_address(ip)
        return not (obj.is_private or obj.is_loopback or obj.is_link_local or obj.is_unspecified)
    except ValueError:
        return False


def _valid_coord(lat: float, lon: float) -> bool:
    """True only for a finite, in-range GPS coordinate (blocks NaN/Inf/9999 that
    would otherwise corrupt the fusion math, the map, and reverse-geocoding)."""
    return (
        math.isfinite(lat) and math.isfinite(lon)
        and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
    )


def _is_global_unicast(ip: str) -> bool:
    """True only for a globally-routable UNICAST address — the gate for ACTIVE
    probing (ping/traceroute/Globalping). A positive check (is_global) rather
    than a denylist, also excluding multicast and reserved/NAT64 ranges that
    is_global alone would let through. Prevents the server from being used to
    probe CGNAT/multicast/reserved/internal targets on a caller's behalf.
    """
    try:
        obj = ipaddress.ip_address(ip)
        return obj.is_global and not obj.is_multicast and not obj.is_reserved
    except ValueError:
        return False


# Only honour X-Forwarded-For when explicitly told we sit behind a trusted
# reverse proxy. Otherwise any client could spoof the header to claim — and via
# /api/delete, erase — another IP's contributions.
_TRUST_PROXY = os.environ.get("GEOIP_TRUST_PROXY", "").lower() in ("1", "true", "yes")


def _header_ip(request: Request) -> str:
    """Best-effort public IP of the caller (socket IP; proxy header only if trusted)."""
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            cand = xff.split(",")[0].strip()
            if _is_routable(cand):
                return cand
    if request.client and _is_routable(request.client.host):
        return request.client.host
    return ""


async def _self_public_ip(client: httpx.AsyncClient) -> str:
    """When running locally the caller is 127.0.0.1; ask an HTTPS IP-echo for our
    egress IP (https avoids the plaintext-MITM surface of http://ip-api.com)."""
    try:
        r = await client.get("https://api.ipify.org?format=json", timeout=8.0)
        return r.json().get("ip", "") or ""
    except Exception:
        return ""


async def resolve_public_ip(request: Request, client: httpx.AsyncClient) -> str:
    ip = _header_ip(request)
    if ip:
        return ip
    return await _self_public_ip(client)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/contribute")
def contribute_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "contribute.html"))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/locate")
async def api_locate(request: Request, ip: Optional[str] = None):
    # Reject an explicitly-provided but malformed IP up front: don't waste GeoIP
    # lookups on garbage, and return a clear error instead of a confusing
    # "추정 불가". An empty param means "locate the caller", which is allowed.
    target = (ip or "").strip()
    if target and not _is_valid_ip(target):
        return JSONResponse({"error": "유효하지 않은 IP 주소입니다."}, status_code=400)

    # Serve a fresh cached result for a repeated explicit lookup.
    if target:
        cached = _cache_get(target)
        if cached is not None:
            return JSONResponse(cached)

    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        if not target:
            target = await resolve_public_ip(request, client)

        providers = await geoip.lookup_all(target, client)

        # If we still don't have an IP (empty param + lookup), recover it from
        # providers — then re-validate so a bogus provider echo can't propagate.
        if not target:
            for p in providers:
                q = (p.raw or {}).get("query") or (p.raw or {}).get("ip")
                if q and _is_valid_ip(str(q)):
                    target = str(q)
                    break

        classification = asn.classify(providers)

        estimates = []
        estimates += geoip.to_estimates(providers)
        try:
            estimates += crowdsource.collect(target)
        except Exception:
            pass

        # ACTIVE measurements (ping/tracert/Globalping) — bounded by a semaphore
        # so a flood can't exhaust host processes, and only ever aimed at
        # globally-routable unicast targets (never multicast/CGNAT/reserved/
        # internal — the server must not be a probing proxy).
        if _is_global_unicast(target):
            async with _active_sem:
                try:
                    estimates += await asyncio.to_thread(anchor_latency.collect, target)
                except Exception:
                    pass
                # Mobile/CGNAT: skip the expensive measurements.
                if not asn.should_early_return(classification):
                    tr, la = await asyncio.gather(
                        traceroute.collect(target, client),
                        latency.collect(target, client),
                        return_exceptions=True,
                    )
                    if isinstance(tr, list):
                        estimates += tr
                    if isinstance(la, list):
                        estimates += la

        result = fusion.fuse(target, classification, estimates)

        # Best-effort: turn the fused coordinate into a readable admin name.
        if result.fused_lat is not None and result.fused_lon is not None:
            try:
                result.address = await geocode.reverse_geocode(
                    result.fused_lat, result.fused_lon, client
                )
            except Exception:
                pass

        payload = result.to_dict()
        if target:
            _cache_put(target, payload)
        return JSONResponse(payload)


@app.post("/api/contribute")
async def api_contribute(request: Request):
    body = await request.json()
    if not body.get("consent"):
        return JSONResponse({"ok": False, "error": "동의가 필요합니다."}, status_code=400)
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "좌표가 올바르지 않습니다."}, status_code=400)
    if not _valid_coord(lat, lon):
        return JSONResponse({"ok": False, "error": "좌표 범위가 올바르지 않습니다."}, status_code=400)

    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        ip = await resolve_public_ip(request, client)
    if not ip:
        return JSONResponse({"ok": False, "error": "공인 IP를 확인할 수 없습니다."}, status_code=400)

    ua = request.headers.get("user-agent", "")
    ua_hash = hashlib.sha256(ua.encode("utf-8", "ignore")).hexdigest()[:16]
    consent_text = "GeoIP 정확도 개선 연구를 위한 공인 IP + GPS 좌표 매핑에 동의"

    # Bound free-text fields and sanitise accuracy (unbounded client strings /
    # non-finite numbers must not reach the store).
    def _clip(v):
        return v[:64] if isinstance(v, str) else None

    try:
        acc = float(body.get("accuracy"))
        accuracy_m = acc if (math.isfinite(acc) and acc >= 0) else None
    except (TypeError, ValueError):
        accuracy_m = None

    rid = store.add_contribution(
        ip=ip,
        lat=lat,
        lon=lon,
        accuracy_m=accuracy_m,
        tz=_clip(body.get("tz")),
        lang=_clip(body.get("lang")),
        ua_hash=ua_hash,
        consent=consent_text,
    )
    return JSONResponse({"ok": True, "id": rid, "ip": ip, "total": store.count()})


@app.post("/api/delete")
async def api_delete(request: Request):
    # CSRF guard: erasure is a destructive, IP-authorized action. Require a
    # custom header that a cross-site simple request cannot set (it would force a
    # CORS preflight, which this server does not answer), so a page the user
    # visits cannot silently POST /api/delete on their behalf.
    if request.headers.get("x-geoip-csrf") is None:
        return JSONResponse({"ok": False, "error": "CSRF 보호: 요청 헤더가 필요합니다."}, status_code=403)
    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        ip = await resolve_public_ip(request, client)
    if not ip:
        return JSONResponse({"ok": False, "error": "공인 IP를 확인할 수 없습니다."}, status_code=400)
    deleted = store.delete_for_ip(ip)
    return JSONResponse({"ok": True, "deleted": deleted, "ip": ip})
