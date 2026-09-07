"""Active-latency proximity signal — operator-vantage geolocation.

When the measurement host's own location (the "vantage") is configured, a target
that answers ICMP at near-zero round-trip is physically adjacent to the vantage:
a 300 km fiber link alone is >3 ms round-trip, so a sub-few-ms reply rules out a
distant city. Such a local target is located at the vantage, with a radius
bounded by the measured RTT.

This EARNS accuracy from active measurement — it is NOT address seeding: only the
deployment's own vantage location is configured, and the target's proximity is
derived from its measured RTT. The signal fires ONLY for targets local to the
vantage; remote or unreachable targets (and an unconfigured vantage) leave it
silent, so the rest of the pipeline behaves exactly as before when it does not
apply.

Enable by setting env  GEOIP_VANTAGE="lat,lon"  (default: disabled). Defensive:
any failure degrades to [] — this collector never raises.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from typing import Optional

from app import geo
from app.models import Estimate

# A target at/below this round-trip (ms) is treated as local to the vantage.
VANTAGE_LOCAL_MS = 5.0
# Smallest radius we will claim even at ~0 ms (vantage position uncertainty).
ANCHOR_FLOOR_KM = 15.0
# Cap for a vantage-local target. radius_km is a ~1-sigma (not a hard speed-of-
# light ceiling), and a sub-5 ms reply means same-metro; beyond this the raw SoL
# bound is technically-true but practically useless, so cap at metro scale.
ANCHOR_MAX_LOCAL_KM = 60.0
# Conservative residential last-mile/access latency (ms) always present in an
# RTT. Subtracting it leaves only the PROPAGATION component for the speed-of-
# light distance bound, so the radius isn't needlessly huge. Kept small (a lower
# bound on real access latency) so the result stays a valid UPPER bound.
LAST_MILE_MS = 1.0


def _env_vantage() -> Optional[tuple]:
    """Parse the configured vantage location from env GEOIP_VANTAGE='lat,lon'."""
    raw = os.environ.get("GEOIP_VANTAGE", "").strip()
    if not raw:
        return None
    try:
        lat, lon = (float(x) for x in raw.split(","))
        if math.isfinite(lat) and math.isfinite(lon):
            return (lat, lon)
    except Exception:
        pass
    return None


def _parse_min_rtt(text: str, ip: str) -> Optional[float]:
    """Minimum RTT (ms) from ping output, counting ONLY replies from ``ip``.

    Critical safety property: an on-link NAT/proxy/anycast device may answer for
    a remote target (e.g. "Reply from 192.168.0.1: ... time<1ms"). Counting that
    would place a remote target at the vantage. So a time token is accepted only
    from a line that contains the target IP, and the locale summary block
    ("Minimum/Maximum/Average") — which has no IP — is ignored. Handles both
    Windows ('time<1ms', 'time=2ms') and POSIX ('time=34.5 ms') forms.
    """
    vals = []
    ip = str(ip)
    for line in text.splitlines():
        if ip not in line:
            continue
        for m in re.findall(r"[=<]\s*([\d.]+)\s*ms", line):
            try:
                vals.append(float(m))
            except ValueError:
                pass
    return min(vals) if vals else None


def _ping_min_rtt(ip: str) -> Optional[float]:
    """Minimum ICMP round-trip in ms to ``ip`` over a few packets, or None.

    OS-aware argv (Windows -n/-w-ms vs POSIX -c/-W-s); robust across locales
    (utf-8/cp949); only counts replies from the target (see _parse_min_rtt).
    """
    if os.name == "nt":
        argv = ["ping", "-n", "4", "-w", "1000", str(ip)]
    else:
        argv = ["ping", "-c", "4", "-W", "1", str(ip)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=15)
        raw = proc.stdout or b""
    except Exception:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp949", errors="replace")
    return _parse_min_rtt(text, ip)


def collect(ip, vantage: Optional[tuple] = None, ping_fn=None) -> list:
    """Return at most one ``anchor_latency`` Estimate for ``ip`` (or [])."""
    try:
        if vantage is None:
            vantage = _env_vantage()
        if vantage is None:
            return []  # signal disabled (no configured vantage)

        rtt = (ping_fn or _ping_min_rtt)(ip)
        if rtt is None:
            return []  # unreachable / no reply
        if rtt > VANTAGE_LOCAL_MS:
            return []  # not local to the vantage -> stay silent (no false fix)

        # Distance bound from the PROPAGATION part of the RTT only (subtract the
        # conservative last-mile floor); never tighter than ANCHOR_FLOOR_KM.
        prop_rtt = rtt - LAST_MILE_MS
        if prop_rtt <= 0:
            radius_km = ANCHOR_FLOOR_KM
        else:
            maxd = geo.rtt_to_max_distance_km(prop_rtt)
            radius_km = ANCHOR_FLOOR_KM if not math.isfinite(maxd) else max(ANCHOR_FLOOR_KM, maxd)
        # 1-sigma cap: a vantage-local target is same-metro; don't report a
        # continental radius the SoL ceiling would allow.
        radius_km = min(radius_km, ANCHOR_MAX_LOCAL_KM)
        return [
            Estimate(
                signal="anchor_latency",
                lat=float(vantage[0]),
                lon=float(vantage[1]),
                radius_km=radius_km,
                weight=0.92,
                label=f"능동 레이턴시 측정: 측정 vantage 인접 (왕복 {rtt:.0f}ms)",
                meta={"rtt_ms": rtt, "vantage": [float(vantage[0]), float(vantage[1])]},
            )
        ]
    except Exception:
        return []
