"""Latency triangulation signal.

Uses the Globalping global probe network to ping the target IP from a spread of
continents. Each probe's minimum RTT bounds the maximum physical distance to the
target (speed-of-light-in-fiber ceiling), giving a disk constraint. Intersecting
these disks via ``geo.triangulate`` yields a coarse, region/continent-scale fix.

Defensive: any failure degrades to an empty list - never raises.
"""
from __future__ import annotations

from app import geo
from app.globalping import measure, WORLD_SPREAD
from app.models import Estimate


async def collect(ip, client) -> list[Estimate]:
    """Locate ``ip`` by multi-vantage latency triangulation.

    Returns a single coarse ``Estimate`` (signal="latency") or [] if there are
    too few usable probes or anything goes wrong.
    """
    try:
        results = await measure(
            client, ip, mtype="ping", limit=12, locations=WORLD_SPREAD, packets=3
        )
        if not results:
            return []

        # Turn each probe's min RTT into a (lat, lon, max_radius_km, weight) disk.
        constraints: list[tuple] = []
        for r in results:
            probe = r.get("probe") or {}
            lat = probe.get("latitude")
            lon = probe.get("longitude")

            rr = r.get("result") or {}
            stats = rr.get("stats") or {}
            min_rtt = stats.get("min")

            if lat is None or lon is None or min_rtt is None or min_rtt <= 0:
                continue

            maxR = geo.rtt_to_max_distance_km(min_rtt)
            if not _is_finite(maxR):
                continue

            weight = 1.0
            constraints.append((float(lat), float(lon), maxR, weight))

        # Need at least two disks for an intersection to mean anything.
        if len(constraints) < 2:
            return []

        tri = geo.triangulate(constraints)
        if tri is None:
            return []

        # Each disk is an UPPER bound on distance, so for a genuine unicast
        # target every probe's disk should contain the true location -> nearly
        # all constraints satisfied. When only a minority are satisfiable the
        # disks disagree, which is the classic signature of an anycast IP
        # (each probe reaches a different nearby PoP) or very noisy RTT. In that
        # case triangulation is meaningless, so suppress the signal.
        import math as _math

        n = len(constraints)
        satisfied = tri[3]
        need = max(2, _math.ceil(0.6 * n))
        if satisfied < need:
            return []

        est = Estimate(
            signal="latency",
            lat=tri[0],
            lon=tri[1],
            # Floor the radius: latency triangulation is inherently coarse.
            radius_km=max(tri[2], 150.0),
            weight=0.35,
            label=f"다지점 레이턴시 삼각측량(프로브 {n}개 중 {satisfied}개 제약만족)",
            meta={"probes": n, "satisfied": satisfied},
        )
        return [est]
    except Exception:
        return []


def _is_finite(x) -> bool:
    """True only for a real, finite number (rejects inf/nan/None)."""
    import math

    try:
        return math.isfinite(x)
    except (TypeError, ValueError):
        return False
