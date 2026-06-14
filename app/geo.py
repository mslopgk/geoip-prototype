"""Geospatial math utilities shared by all signals.

Pure functions only - no I/O, no network. Safe to import anywhere.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

EARTH_R_KM = 6371.0088

# Light in fiber travels at ~0.66c. One-way kilometres per millisecond.
_C_KM_PER_S = 299_792.458
FIBER_KM_PER_MS = _C_KM_PER_S * 0.66 / 1000.0  # ~197.9 km/ms one-way


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def rtt_to_max_distance_km(rtt_ms: float) -> float:
    """Upper bound on physical distance implied by a round-trip latency.

    Uses one-way time (rtt/2) times fiber propagation speed. This is a hard
    physical ceiling; real distance is smaller due to routing/queueing.
    """
    if rtt_ms is None or rtt_ms <= 0:
        return float("inf")
    return (rtt_ms / 2.0) * FIBER_KM_PER_MS


def dbscan_haversine(points: np.ndarray, eps_km: float, min_samples: int) -> np.ndarray:
    """DBSCAN with a haversine metric. No scikit-learn dependency.

    points: (N, 2) array of [lat, lon] in degrees.
    Returns an (N,) int array of cluster labels; -1 means noise.
    """
    points = np.asarray(points, dtype=float)
    n = len(points)
    labels = np.full(n, -1, dtype=int)
    if n == 0:
        return labels

    lat = np.radians(points[:, 0])
    lon = np.radians(points[:, 1])
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat)[:, None] * np.cos(lat)[None, :] * np.sin(dlon / 2) ** 2
    dist = 2 * EARTH_R_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))  # (N, N) km

    neighbors = [np.where(dist[i] <= eps_km)[0] for i in range(n)]
    visited = np.zeros(n, dtype=bool)
    cluster = 0
    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        if len(neighbors[i]) < min_samples:
            continue  # leave as noise; may be reclaimed as a border point
        labels[i] = cluster
        seeds = list(neighbors[i])
        k = 0
        while k < len(seeds):
            j = int(seeds[k])
            k += 1
            if not visited[j]:
                visited[j] = True
                if len(neighbors[j]) >= min_samples:
                    for nb in neighbors[j]:
                        if nb not in seeds:
                            seeds.append(int(nb))
            if labels[j] == -1:
                labels[j] = cluster
        cluster += 1
    return labels


def triangulate(constraints: Sequence[tuple]) -> Optional[tuple]:
    """Constraint-based geolocation from latency disks.

    constraints: iterable of (lat, lon, max_radius_km, weight). The target is
    assumed to lie within each probe's disk. Returns
    (lat, lon, radius_km, satisfied_count) or None.

    Coarse global grid search refined locally. Intentionally approximate -
    this resolves to region/continent scale, not precision.
    """
    cons = [c for c in constraints if c is not None and math.isfinite(c[2])]
    if not cons:
        return None

    def worst_margin(la: float, lo: float):
        """(max_i(dist_i - R_i), satisfied_count).

        Each disk is dist <= R. The worst margin is the largest signed overshoot
        across probes; minimizing it yields the Chebyshev centre of the disk
        intersection (the point deepest inside all disks). This pulls the estimate
        toward the smallest disk - i.e. the nearest probe, which for a unicast
        target sits closest to the truth - instead of an arbitrary corner of the
        feasible region.
        """
        worst = -1.0e18
        sat = 0
        for (plat, plon, R, _w) in cons:
            dd = haversine_km(la, lo, plat, plon)
            if dd <= R:
                sat += 1
            margin = dd - R
            if margin > worst:
                worst = margin
        return worst, sat

    best = None
    best_m = None
    for la in np.arange(-60.0, 78.0, 4.0):  # cover all inhabited/probe latitudes
        for lo in np.arange(-180.0, 180.0, 4.0):
            m, _sat = worst_margin(float(la), float(lo))
            if best_m is None or m < best_m:
                best_m = m
                best = (float(la), float(lo))

    cla, clo = best
    for step in (2.0, 1.0, 0.4, 0.15):
        span = step * 5
        for la in np.arange(cla - span, cla + span + 1e-9, step):
            for lo in np.arange(clo - span, clo + span + 1e-9, step):
                m, _sat = worst_margin(float(la), float(lo))
                if m < best_m:
                    best_m = m
                    cla, clo = float(la), float(lo)

    radius = min(c[2] for c in cons)
    _, sat = worst_margin(cla, clo)
    return (float(cla), float(clo), float(radius), int(sat))


def weighted_lon_mean(lons, weights=None) -> float:
    """Circular (antimeridian-safe) weighted mean of longitudes in degrees.

    A plain arithmetic mean of longitudes is wrong across the ±180° dateline
    (170° and -170° are 20° apart, not 340°). We average the unit vectors and
    recover the angle with atan2.
    """
    lons = np.radians(np.asarray(lons, dtype=float))
    if len(lons) == 0:
        return 0.0
    if weights is None:
        weights = np.ones(len(lons))
    weights = np.asarray(weights, dtype=float)
    s = float(np.sum(weights * np.sin(lons)))
    c = float(np.sum(weights * np.cos(lons)))
    if s == 0.0 and c == 0.0:  # antipodal cancellation: no meaningful mean
        return float(np.degrees(lons[0]))
    return math.degrees(math.atan2(s, c))


def inverse_variance_fuse(estimates: Sequence) -> Optional[tuple]:
    """Reliability-weighted, inverse-variance fusion of point estimates.

    Each estimate must expose .lat, .lon, .radius_km, .weight.
    Returns (lat, lon, radius_km) or None.
    """
    items = [e for e in estimates if e is not None]
    if not items:
        return None
    weights = np.array([float(e.weight) / (max(0.5, float(e.radius_km)) ** 2) for e in items])
    sw = float(weights.sum())
    if sw <= 0:
        return None
    lats = np.array([float(e.lat) for e in items])
    lons = np.array([float(e.lon) for e in items])
    lat = float(np.sum(weights * lats) / sw)
    lon = weighted_lon_mean(lons, weights)  # antimeridian-safe
    radius = 1.0 / math.sqrt(sw)
    return (lat, lon, radius)


def centroid(points: np.ndarray, weights: Optional[np.ndarray] = None) -> tuple:
    """Weighted centroid of [lat, lon] points (antimeridian-safe in longitude)."""
    points = np.asarray(points, dtype=float)
    if len(points) == 0:
        return 0.0, 0.0
    if weights is None:
        weights = np.ones(len(points))
    weights = np.asarray(weights, dtype=float)
    wsum = weights.sum()
    if wsum <= 0:
        weights = np.ones(len(points))
        wsum = float(weights.sum())
    lat = float((points[:, 0] * weights).sum() / wsum)
    lon = weighted_lon_mean(points[:, 1], weights)
    return lat, lon
