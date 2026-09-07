"""Live accuracy harness for the multi-signal geolocation engine.

Queries the running server (http://127.0.0.1:8000) for a set of IPs, and for
those with a known ground-truth address scores:
  * center error  = great-circle distance from fused center to the truth (km)
  * inside        = does the confidence circle contain the truth?  (honesty)
  * radius        = confidence radius (km)  (tightness)

Run with the server up:  python scripts/accuracy_report.py
Writes a UTF-8 report to scripts/_accuracy_report.txt (console-safe).
"""
from __future__ import annotations

import json
import math
import urllib.request

import os as _os
BASE = _os.environ.get("GEOIP_BASE", "http://127.0.0.1:8000")

# Ground-truth residential IPs (provided by the operator for calibration).
# Coordinates are dong-level approximations of the stated street addresses.
GROUND_TRUTH = {
    "106.253.34.195": ("부산 금정구 구서동 420-20", 35.2476, 129.0892),
    "106.253.34.197": ("부산 금정구 구서동 420-20", 35.2476, 129.0892),
    "182.216.201.180": ("부산 강서구 명지동 3250-13", 35.0940, 128.9120),
}

# Exploratory IPs without a known precise truth (sanity / behaviour checks).
EXPLORATORY = {
    "8.8.8.8": "Google DNS (hosting, providers disagree)",
    "1.1.1.1": "Cloudflare (anycast)",
    "168.126.63.1": "KT DNS (Korea infra)",
}


def haversine(a, b, c, d):
    R = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    dphi = math.radians(c - a)
    dl = math.radians(d - b)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def locate(ip: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/api/locate?ip={ip}", timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    lines = []
    scored = []  # (inside, center_err, radius)

    lines.append("################ GROUND TRUTH ################")
    for ip, (addr, tlat, tlon) in GROUND_TRUTH.items():
        try:
            d = locate(ip)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{ip}: ERROR {e}")
            continue
        fl, fo, fr = d["fused_lat"], d["fused_lon"], d["confidence_radius_km"]
        lines.append(f"\n{ip}  (truth: {addr})")
        lines.append(f"  class : {d['classification']['note']}  early_return={d['early_return']}")
        for e in d["estimates"]:
            lines.append(
                f"  sig {e['signal']:18s} ({e['lat']:.3f},{e['lon']:.3f}) r={e['radius_km']:.0f} w={e['weight']} | {e['label']}"
            )
        if fl is None:
            lines.append(f"  FUSED : (none) label={d['confidence_label']}")
            continue
        err = haversine(tlat, tlon, fl, fo)
        inside = err <= fr
        scored.append((inside, err, fr))
        lines.append(
            f"  FUSED : ({fl:.4f},{fo:.4f}) r={fr:.1f}km label={d['confidence_label']}"
        )
        lines.append(
            f"  SCORE : center_err={err:.1f}km  inside_circle={inside}  radius={fr:.1f}km"
        )

    lines.append("\n################ EXPLORATORY ################")
    for ip, desc in EXPLORATORY.items():
        try:
            d = locate(ip)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{ip}: ERROR {e}")
            continue
        fl, fo, fr = d["fused_lat"], d["fused_lon"], d["confidence_radius_km"]
        center = f"({fl:.3f},{fo:.3f})" if fl is not None else "(none)"
        lines.append(f"\n{ip}  ({desc})")
        lines.append(f"  class : {d['classification']['note']}")
        lines.append(f"  FUSED : {center} r={fr if fr else 0:.0f}km label={d['confidence_label']}")

    lines.append("\n################ AGGREGATE (ground truth) ################")
    if scored:
        n = len(scored)
        inside_n = sum(1 for s in scored if s[0])
        mean_err = sum(s[1] for s in scored) / n
        mean_r = sum(s[2] for s in scored) / n
        lines.append(f"  honesty (inside circle): {inside_n}/{n}")
        lines.append(f"  mean center error      : {mean_err:.1f} km")
        lines.append(f"  mean confidence radius : {mean_r:.1f} km")
        # A simple combined score: reward honesty, penalize loose radius & error.
        lines.append(f"  efficiency (err/radius): " +
                     ", ".join(f"{s[1]/s[2]:.2f}" for s in scored) +
                     "   (lower = truth well inside; >1 = OUTSIDE)")
    else:
        lines.append("  no scored cases")

    report = "\n".join(lines)
    with open("scripts/_accuracy_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print("wrote scripts/_accuracy_report.txt")


if __name__ == "__main__":
    main()
