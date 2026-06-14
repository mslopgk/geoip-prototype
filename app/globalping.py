"""Globalping API client (https://globalping.io).

Free, no-auth (rate-limited) global measurement network. We use it for
distributed ping (latency triangulation) and traceroute. Measurements are
async: POST creates one, then we poll until status == "finished".
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

API = "https://api.globalping.io/v1/measurements"


async def measure(
    client: httpx.AsyncClient,
    target: str,
    mtype: str = "ping",
    limit: int = 8,
    locations: Optional[list] = None,
    packets: int = 3,
    poll_timeout_s: float = 30.0,
) -> list[dict]:
    """Run a measurement and return its per-probe results list.

    Each result dict has shape:
      {"probe": {"city","country","continent","latitude","longitude",...},
       "result": {"stats": {"min","avg",...}, "status": ..., "rawOutput": ...}}
    Returns [] on any failure (caller treats the signal as unavailable).
    """
    payload: dict = {
        "type": mtype,
        "target": target,
        "limit": limit,
        "locations": locations or [],
    }
    if mtype == "ping":
        payload["measurementOptions"] = {"packets": packets}

    try:
        r = await client.post(API, json=payload, timeout=15.0)
    except Exception:
        return []
    if r.status_code not in (200, 201, 202):
        return []
    try:
        mid = r.json()["id"]
    except Exception:
        return []

    url = f"{API}/{mid}"
    waited = 0.0
    interval = 1.0
    while waited < poll_timeout_s:
        try:
            rr = await client.get(url, timeout=15.0)
            data = rr.json()
        except Exception:
            await asyncio.sleep(interval)
            waited += interval
            continue
        if data.get("status") == "finished":
            return data.get("results", []) or []
        await asyncio.sleep(interval)
        waited += interval

    # Timed out - return partial results if any.
    try:
        data = (await client.get(url, timeout=15.0)).json()
        return data.get("results", []) or []
    except Exception:
        return []


# A spread of continents so triangulation disks actually intersect from
# multiple directions rather than clustering in one region.
WORLD_SPREAD = [
    {"continent": "NA"},
    {"continent": "SA"},
    {"continent": "EU"},
    {"continent": "AF"},
    {"continent": "AS"},
    {"continent": "OC"},
]
