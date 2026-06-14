"""FastAPI application wiring all signals into the locate/contribute pipeline.

Run:  uvicorn app.main:app --reload   (from the project root)
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app import store
from app.signals import geoip, asn, traceroute, latency, crowdsource
from app import fusion

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

app = FastAPI(title="Multi-signal IP Geolocation (research prototype)")


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_routable(ip: str) -> bool:
    try:
        obj = ipaddress.ip_address(ip)
        return not (obj.is_private or obj.is_loopback or obj.is_link_local or obj.is_unspecified)
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
    """When running locally the caller is 127.0.0.1; ask ip-api for our egress IP."""
    try:
        r = await client.get("http://ip-api.com/json/?fields=query", timeout=8.0)
        return r.json().get("query", "") or ""
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
    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        # Resolve target IP: explicit query param, else the caller's public IP.
        target = (ip or "").strip()
        if not target:
            target = await resolve_public_ip(request, client)

        providers = await geoip.lookup_all(target, client)

        # If we still don't have an IP (empty param + lookup), recover it from providers.
        if not target:
            for p in providers:
                q = (p.raw or {}).get("query") or (p.raw or {}).get("ip")
                if q:
                    target = q
                    break

        classification = asn.classify(providers)

        estimates = []
        estimates += geoip.to_estimates(providers)
        try:
            estimates += crowdsource.collect(target)
        except Exception:
            pass

        # Mobile/CGNAT: early return — skip the expensive active measurements.
        if not asn.should_early_return(classification) and _is_routable(target):
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
        return JSONResponse(result.to_dict())


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

    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        ip = await resolve_public_ip(request, client)
    if not ip:
        return JSONResponse({"ok": False, "error": "공인 IP를 확인할 수 없습니다."}, status_code=400)

    ua = request.headers.get("user-agent", "")
    ua_hash = hashlib.sha256(ua.encode("utf-8", "ignore")).hexdigest()[:16]
    consent_text = "GeoIP 정확도 개선 연구를 위한 공인 IP + GPS 좌표 매핑에 동의"

    rid = store.add_contribution(
        ip=ip,
        lat=lat,
        lon=lon,
        accuracy_m=body.get("accuracy"),
        tz=body.get("tz"),
        lang=body.get("lang"),
        ua_hash=ua_hash,
        consent=consent_text,
    )
    return JSONResponse({"ok": True, "id": rid, "ip": ip, "total": store.count()})


@app.post("/api/delete")
async def api_delete(request: Request):
    async with httpx.AsyncClient(headers={"User-Agent": "geoip-prototype/0.1"}) as client:
        ip = await resolve_public_ip(request, client)
    if not ip:
        return JSONResponse({"ok": False, "error": "공인 IP를 확인할 수 없습니다."}, status_code=400)
    deleted = store.delete_for_ip(ip)
    return JSONResponse({"ok": True, "deleted": deleted, "ip": ip})
