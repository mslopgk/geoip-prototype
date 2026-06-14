"""Crowdsource signal: consented GPS contributions stored in SQLite.

Reads the points a contributor (or their /24 prefix) consented to share, finds
the dominant spatial cluster with a haversine DBSCAN, and collapses it to a
single time-weighted centroid ``Estimate``. Recent points dominate (30-day
half-life-ish exponential decay) so a stale GPS fix does not pin the result.

This is the highest-precision signal available (street/GPS scale) and is
weighted accordingly when a real cluster is found.

Defensive: any failure (DB I/O, malformed rows, math) degrades to ``[]``.
"""
from __future__ import annotations

import math
import time

import numpy as np

from app import store, geo
from app.models import Estimate


def collect(ip) -> list[Estimate]:
    """Return at most one crowdsource ``Estimate`` for ``ip`` (or ``[]``)."""
    try:
        pts = store.get_points_for_ip(ip, include_prefix=True)
        if not pts:
            return []

        # (N, 2) array of [lat, lon] for clustering.
        arr = np.array([[float(p["lat"]), float(p["lon"])] for p in pts], dtype=float)

        # Tight clusters (1 km eps, >=2 points) represent a stable location.
        labels = geo.dbscan_haversine(arr, eps_km=1.0, min_samples=2)

        # Pick the largest cluster if one exists, else fall back to all points.
        non_noise = [int(l) for l in labels if l >= 0]
        cluster = len(non_noise) > 0
        if cluster:
            # Mode of the non-negative labels = the most populated cluster id.
            best_label = max(set(non_noise), key=non_noise.count)
            chosen_idx = [i for i, l in enumerate(labels) if int(l) == best_label]
        else:
            # All noise: sparse/scattered data, use everything but be cautious.
            chosen_idx = list(range(len(pts)))

        chosen = [pts[i] for i in chosen_idx]

        # Time-weighted centroid: exponential decay with ~30-day scale.
        now = time.time()
        sw = slat = slon = 0.0
        for p in chosen:
            age_days = (now - float(p["ts"])) / 86400.0
            w = math.exp(-age_days / 30.0)
            sw += w
            slat += w * float(p["lat"])
            slon += w * float(p["lon"])
        if sw <= 0:
            # Degenerate weights (shouldn't happen); fall back to plain mean.
            lat = float(np.mean([float(p["lat"]) for p in chosen]))
            lon = float(np.mean([float(p["lon"]) for p in chosen]))
        else:
            lat = slat / sw
            lon = slon / sw

        # Spread: farthest chosen point from the centroid (km).
        spread = 0.0
        for p in chosen:
            d = geo.haversine_km(lat, lon, float(p["lat"]), float(p["lon"]))
            if d > spread:
                spread = d

        # Best reported GPS accuracy among chosen points, if any (km).
        acc_km = [
            float(p["accuracy_m"]) / 1000.0
            for p in chosen
            if p.get("accuracy_m") is not None
        ]
        min_accuracy_km = min(acc_km) if acc_km else None

        # These are precise GPS points: keep the radius small. Bound the spread
        # by the best device accuracy when we have it.
        base = spread if spread > 0 else 0.1
        if min_accuracy_km is not None:
            base = min(base, min_accuracy_km) if base > 0 else min_accuracy_km
        radius_km = max(0.05, base)

        # Real cluster -> high confidence; sparse noise -> low confidence.
        weight = 0.95 if cluster else 0.5

        # Exact contributor match (vs only a /24 prefix neighbour) among chosen.
        exact = any(str(p.get("ip")) == str(ip) for p in chosen)

        label = (
            "동의 기반 GPS "
            + ("정밀 군집" if cluster else "희소 데이터")
            + f"(점 {len(chosen)}개/총 {len(pts)})"
        )

        est = Estimate(
            signal="crowdsource",
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            weight=weight,
            label=label,
            meta={
                "total_points": len(pts),
                "used": len(chosen),
                "cluster": bool(cluster),
                "exact_ip": exact,
            },
        )
        return [est]
    except Exception:
        # Never raise out of a signal collector.
        return []
