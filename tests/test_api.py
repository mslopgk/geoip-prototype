"""API-level tests for input validation (offline — no network for the 400 path).

The happy path (200 with a real lookup) hits the network and is exercised
manually, not here; these tests only assert that bad input is rejected cleanly
and that valid IPv4/IPv6 parse.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def test_is_valid_ip_accepts_v4_and_v6():
    assert main._is_valid_ip("8.8.8.8")
    assert main._is_valid_ip("106.253.34.197")
    assert main._is_valid_ip("2001:4860:4860::8888")  # IPv6 must parse


def test_is_valid_ip_rejects_garbage():
    assert not main._is_valid_ip("not-an-ip")
    assert not main._is_valid_ip("999.999.999.999")
    assert not main._is_valid_ip("8.8.8.8/../admin")
    assert not main._is_valid_ip("")


def test_locate_rejects_invalid_ip_with_400():
    # Garbage must be rejected up front (no wasted GeoIP lookups, clear error),
    # not silently turned into a confusing '추정 불가'.
    r = client.get("/api/locate", params={"ip": "not-an-ip"})
    assert r.status_code == 400
    body = r.json()
    assert "IP" in body.get("error", "") or "유효" in body.get("error", "")


def test_locate_rejects_numeric_garbage_with_400():
    r = client.get("/api/locate", params={"ip": "999.999.999.999"})
    assert r.status_code == 400
