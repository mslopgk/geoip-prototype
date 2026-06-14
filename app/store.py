"""SQLite storage for consented crowdsourced GPS contributions.

Privacy posture: this holds personal location data. Every row records the
consent text the contributor agreed to and a timestamp. `delete_for_ip`
supports the contributor's right to erasure.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "contrib.db")
_DB_PATH = os.path.abspath(_DB_PATH)
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS contributions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          TEXT NOT NULL,
                prefix24    TEXT NOT NULL,
                lat         REAL NOT NULL,
                lon         REAL NOT NULL,
                accuracy_m  REAL,
                tz          TEXT,
                lang        TEXT,
                ua_hash     TEXT,
                consent     TEXT NOT NULL,
                ts          REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_ip ON contributions(ip)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prefix ON contributions(prefix24)")


def ipv4_prefix24(ip: str) -> str:
    """Return the /24 prefix string for an IPv4 address, else the ip itself."""
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0/24"
    return ip


def add_contribution(
    ip: str,
    lat: float,
    lon: float,
    accuracy_m: Optional[float],
    tz: Optional[str],
    lang: Optional[str],
    ua_hash: Optional[str],
    consent: str,
    ts: Optional[float] = None,
) -> int:
    if ts is None:
        ts = time.time()
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO contributions
               (ip, prefix24, lat, lon, accuracy_m, tz, lang, ua_hash, consent, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ip, ipv4_prefix24(ip), lat, lon, accuracy_m, tz, lang, ua_hash, consent, ts),
        )
        return int(cur.lastrowid)


def get_points_for_ip(ip: str, include_prefix: bool = True) -> list[dict]:
    """Return stored points for an IP. If include_prefix, also matches its /24."""
    with _lock, _conn() as c:
        if include_prefix:
            rows = c.execute(
                "SELECT * FROM contributions WHERE ip = ? OR prefix24 = ? ORDER BY ts DESC",
                (ip, ipv4_prefix24(ip)),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM contributions WHERE ip = ? ORDER BY ts DESC", (ip,)
            ).fetchall()
        return [dict(r) for r in rows]


def delete_for_ip(ip: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM contributions WHERE ip = ?", (ip,))
        return cur.rowcount


def count() -> int:
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM contributions").fetchone()["n"])
