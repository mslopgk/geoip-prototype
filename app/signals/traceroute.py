"""Traceroute signal: infer a coarse location from router hostnames.

Backbone routers are frequently named after the airport (IATA code) or city
nearest the facility they live in (e.g. ``ae-1.r02.icn01.kr.bb.example.net``).
By running a hostname-resolving traceroute and matching 3-letter tokens against
a curated IATA airport table - plus a handful of bare city names - we can place
the LAST hop that yields a match, which is the hop physically closest to the
target.

This is a low-confidence hint (radius ~150 km, weight 0.3): a router named ICN
is somewhere near Seoul, not at the subscriber's doorstep. Any failure of the
subprocess or parsing degrades to an empty list - this collector never raises.
"""
from __future__ import annotations

import asyncio
import os
import csv
import re

from app.models import Estimate

# Path to the curated airport table, resolved relative to this file:
#   app/signals/traceroute.py -> ../../data/iata.csv
_HERE = os.path.dirname(os.path.abspath(__file__))
_IATA_CSV = os.path.join(_HERE, "..", "..", "data", "iata.csv")

# Bare city-name tokens that also appear in router hostnames. Mapped to a
# representative airport code so we can reuse the IATA coordinate table.
_CITY_TOKENS = {
    "seoul": "ICN",
    "busan": "PUS",
    "incheon": "ICN",
    "daegu": "TAE",
    "tokyo": "HND",
    "osaka": "KIX",
    "singapore": "SIN",
    "sydney": "SYD",
    "london": "LHR",
    "paris": "CDG",
    "frankfurt": "FRA",
    "amsterdam": "AMS",
    "newyork": "JFK",
    "losangeles": "LAX",
    "seattle": "SEA",
    "sanjose": "SJC",
    "hongkong": "HKG",
    "taipei": "TPE",
    "shanghai": "PVG",
    "beijing": "PEK",
}

# A bracketed dotted-quad, e.g. "[203.0.113.1]". The token immediately before it
# (when present) is the resolved hostname for that hop.
_IP_BRACKET_RE = re.compile(r"\[(\d{1,3}(?:\.\d{1,3}){3})\]")
# A bare 3-letter alphabetic token, e.g. "icn".
_IATA_TOKEN_RE = re.compile(r"^[A-Za-z]{3}$")
# A 3-letter IATA code glued to a small numeric suffix, e.g. "icn01", "lax2",
# "fra5" - extremely common in real backbone router hostnames.
_IATA_PREFIX_RE = re.compile(r"^([A-Za-z]{3})\d{1,3}$")

# Router-interface / role abbreviations that collide with real IATA codes and
# would otherwise be mis-located: "gig0" (GigabitEthernet) -> GIG (Rio),
# "tun3" (tunnel) -> TUN (Tunis), "pos1" (Packet-over-SONET) -> POS, etc.
# These are never geographic hints.
_INTERFACE_TOKENS = {
    "gig", "ten", "son", "agg", "lag", "pos", "tun", "irb", "bvi", "eth",
    "fab", "sup", "rsp", "mgt", "oob", "vme", "lan", "wan", "bdl", "lacp",
}

# Two-letter router interface/bundle prefixes (Juniper/Cisco) that collide with
# ISO country codes and would otherwise be mistaken for a country label in the
# hostname (e.g. "ae-1" = aggregated-ethernet, not UAE; "ge-0" = gigabit-ethernet,
# not Georgia; "be-2" = bundle-ethernet, not Belgium).
_INTERFACE_2L = {
    "ae", "be", "et", "ge", "hu", "se", "so", "xe", "te", "gi", "fe",
    "fa", "lo", "po", "tu", "vl", "em", "me", "pp", "br",
}


def _load_iata() -> dict:
    """Load iata.csv into ``{CODE: (lat, lon, city, country)}``.

    Returns an empty dict if the file is missing or unreadable so that a
    deployment without the data file simply produces no hints.
    """
    table: dict[str, tuple[float, float, str, str]] = {}
    try:
        with open(_IATA_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    code = (row["code"] or "").strip().upper()
                    if not code:
                        continue
                    table[code] = (
                        float(row["lat"]),
                        float(row["lon"]),
                        (row.get("city") or "").strip(),
                        (row.get("country") or "").strip(),
                    )
                except (KeyError, ValueError, TypeError):
                    continue  # skip malformed row, keep going
    except Exception:
        return {}
    return table


def _decode(raw: bytes) -> str:
    """Decode tracert output robustly across Windows locales.

    Korean Windows emits the OEM codepage (cp949); English emits ~utf-8/ascii.
    Try utf-8 first, fall back to cp949, finally replace undecodable bytes so we
    never raise on garbage.
    """
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_hostname(line: str) -> str:
    """Return the resolved hostname token on a tracert hop line, or "".

    tracert hop lines look like one of::

        3    12 ms    11 ms    10 ms  host.name.icn01.kr [203.0.113.1]
        4     *        *        *     Request timed out.
        5    20 ms    19 ms    21 ms  203.0.113.5

    The hostname is the whitespace token sitting just before the bracketed IP.
    When the host did not resolve, that slot holds the bare IP (which has no
    alpha tokens, so it harmlessly yields no hint).
    """
    m = _IP_BRACKET_RE.search(line)
    if m:
        before = line[: m.start()].rstrip()
        token = before.rsplit(None, 1)[-1] if before else ""
        return token
    # No bracketed IP: take the trailing token if it looks like a hostname
    # (contains a dot and at least one letter), otherwise ignore the line.
    parts = line.split()
    if parts:
        cand = parts[-1]
        if "." in cand and any(c.isalpha() for c in cand):
            return cand
    return ""


def _hints_from_hostname(host: str, iata: dict):
    """Yield (code, lat, lon, city) matches found in a single hostname.

    The hostname is split on '.' and '-'; each 3-letter alpha token is probed
    against the IATA table, and the whole label set is scanned for bare
    city-name tokens.
    """
    if not host:
        return
    tokens = re.split(r"[.\-]", host)

    # Countries explicitly named in the hostname (2-letter ISO tokens matching a
    # country present in the IATA table). When a host names a country, an IATA
    # candidate from a *different* country is almost always an interface/role
    # token coincidence (e.g. "sea01" inside a ".kr" host), not a real location.
    known_countries = {c for (_la, _lo, _ci, c) in iata.values() if c}
    host_countries = {
        tok.upper() for tok in tokens
        if len(tok) == 2 and tok.isalpha()
        and tok.lower() not in _INTERFACE_2L
        and tok.upper() in known_countries
    }

    def _country_ok(country: str) -> bool:
        return not (host_countries and country and country.upper() not in host_countries)

    seen: set[str] = set()
    for tok in tokens:
        if not tok:
            continue
        # IATA airport code: bare "icn" or glued-to-digits "icn01"/"lax2".
        code = None
        if _IATA_TOKEN_RE.match(tok):
            code = tok.upper()
        else:
            m = _IATA_PREFIX_RE.match(tok)
            if m:
                code = m.group(1).upper()
        if (
            code
            and code.lower() not in _INTERFACE_TOKENS
            and code in iata
            and code not in seen
        ):
            lat, lon, city, country = iata[code]
            if _country_ok(country):
                seen.add(code)
                yield (code, lat, lon, city)
        # Bare city-name token (e.g. "seoul", "hongkong")
        low = tok.lower()
        if low in _CITY_TOKENS:
            code = _CITY_TOKENS[low]
            if code in iata and code not in seen:
                lat, lon, city, country = iata[code]
                if _country_ok(country):
                    seen.add(code)
                    yield (code, lat, lon, city)


async def collect(ip, client) -> list[Estimate]:
    """Run a hostname-resolving tracert and locate via router hostnames.

    Returns a single low-confidence ``Estimate`` from the highest-index hop that
    matched an airport/city token (closest to the target), or ``[]`` on any
    failure. ``client`` is accepted for signature parity but unused.
    """
    iata = _load_iata()
    if not iata:
        return []

    # tracert -h 15 -w 800 <ip>  (no -d: we WANT hostnames resolved)
    try:
        proc = await asyncio.create_subprocess_exec(
            "tracert",
            "-h",
            "15",
            "-w",
            "800",
            str(ip),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, NotImplementedError, OSError):
        return []  # tracert missing or subprocess unsupported
    except Exception:
        return []

    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=40.0)
    except (asyncio.TimeoutError, TimeoutError):
        # Hung traceroute: kill the child and give up gracefully.
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return []
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return []

    try:
        text = _decode(out or b"")
    except Exception:
        return []

    # Walk hops in order. Each output line with a leading hop number is one hop;
    # we track an incrementing hop index for matched lines. We keep the match
    # from the HIGHEST hop index (closest to the target).
    best = None  # (hop, code, lat, lon, city)
    hops_parsed = 0
    try:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # A hop line begins with the hop number.
            m = re.match(r"^(\d+)\b", stripped)
            if not m:
                continue
            hop = int(m.group(1))
            hops_parsed += 1
            host = _extract_hostname(stripped)
            for (code, lat, lon, city) in _hints_from_hostname(host, iata):
                if best is None or hop >= best[0]:
                    best = (hop, code, lat, lon, city)
    except Exception:
        return []

    if best is None:
        return []

    hop, code, lat, lon, city = best
    return [
        Estimate(
            signal="traceroute",
            lat=float(lat),
            lon=float(lon),
            radius_km=150.0,
            weight=0.3,
            label=f"traceroute 호스트명 힌트: {code} ({city}) @hop{hop}",
            meta={"hops_parsed": hops_parsed, "hint_code": code},
        )
    ]
