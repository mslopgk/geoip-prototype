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

# A household's OWN contributions (exact IP) count more than /24-prefix
# neighbours — but only as a weight multiplier, so a much fresher neighbour
# cluster can still win via time-decay (a stale exact fix must not pin us).
EXACT_IP_BOOST = 3.0

# A single, uncorroborated GPS fix (no cluster) has not earned street-level
# precision; floor its radius so the reported circle matches its low trust.
SPARSE_FLOOR_KM = 2.0

# Privacy floor for /24-prefix-only matches: a caller who has no contribution of
# their OWN must not receive a neighbour household's street-level location, only
# a coarse neighbourhood-level hint.
PREFIX_PRIVACY_FLOOR_KM = 2.0


def collect(ip) -> list[Estimate]:
    """Return at most one crowdsource ``Estimate`` for ``ip`` (or ``[]``)."""
    try:
        pts = store.get_points_for_ip(ip, include_prefix=True)
        if not pts:
            return []

        n = len(pts)
        # (N, 2) array of [lat, lon] for clustering.
        arr = np.array([[float(p["lat"]), float(p["lon"])] for p in pts], dtype=float)

        # Per-point weight = recency (30-day exp decay) x exact-IP boost. The
        # household's own contributions count more, but a much fresher neighbour
        # cluster still wins (time-decay preserved — no hard exact-only filter,
        # which would let a stale exact fix shadow fresh same-household data).
        now = time.time()
        pt_w = np.array(
            [
                math.exp(-((now - float(p["ts"])) / 86400.0) / 30.0)
                * (EXACT_IP_BOOST if str(p.get("ip")) == str(ip) else 1.0)
                for p in pts
            ],
            dtype=float,
        )

        # Tight clusters (1 km eps, >=2 points) represent a stable location.
        labels = geo.dbscan_haversine(arr, eps_km=1.0, min_samples=2)

        # Choose the cluster with the greatest TOTAL weight (recency + exact-IP),
        # not the most raw points — so a noisier neighbour can't outvote us and a
        # stale fix can't outweigh fresh data. Fall back to all points if none.
        non_noise = sorted({int(l) for l in labels if l >= 0})
        cluster = len(non_noise) > 0
        if cluster:
            best_label = max(
                non_noise,
                key=lambda L: float(pt_w[[i for i in range(n) if int(labels[i]) == L]].sum()),
            )
            chosen_idx = [i for i in range(n) if int(labels[i]) == best_label]
        else:
            chosen_idx = list(range(n))

        chosen = [pts[i] for i in chosen_idx]

        # Time/boost-weighted centroid (antimeridian-safe in longitude).
        weights = pt_w[chosen_idx]
        lat, lon = geo.centroid(arr[chosen_idx], weights)

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

        # Honest uncertainty: at least the cluster spread, never tighter than the
        # best device-reported GPS accuracy (accuracy only ADDS uncertainty), and
        # -- for a sparse/uncorroborated fix -- never claiming street-level
        # precision it has not earned.
        radius_km = max(0.05, spread)
        if min_accuracy_km is not None:
            radius_km = max(radius_km, min_accuracy_km)
        if not cluster:
            radius_km = max(radius_km, SPARSE_FLOOR_KM)

        # Real cluster -> high confidence; sparse noise -> low confidence.
        weight = 0.95 if cluster else 0.5

        # Exact contributor match (vs only a /24 prefix neighbour) among chosen.
        exact = any(str(p.get("ip")) == str(ip) for p in chosen)

        # Privacy: if the caller has no contribution of their own and is only a
        # /24-prefix neighbour, never expose the household's street-level radius.
        if not exact:
            radius_km = max(radius_km, PREFIX_PRIVACY_FLOOR_KM)

        label = (
            "동의 기반 GPS "
            + ("정밀 군집" if cluster else "희소 데이터")
            + f"(점 {len(chosen)}개/총 {n}개)"
        )

        est = Estimate(
            signal="crowdsource",
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            weight=weight,
            label=label,
            meta={
                "total_points": n,
                "used": len(chosen),
                "cluster": bool(cluster),
                "exact_ip": exact,
            },
        )
        return [est]
    except Exception:
        # Never raise out of a signal collector.
        return []
