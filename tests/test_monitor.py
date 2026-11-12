"""Monitoring engine tests with fully mocked HTTP responses (no external calls)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

import database
import monitor


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None, url: str = "https://example.com/"):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url


def queue_requests(monkeypatch, responses):
    """Patch the HTTP layer with a scripted list of responses/exceptions."""
    queue = list(responses)
    calls: list[str] = []

    def fake_request(session, url, timeout, address=None):
        calls.append(url)
        if not queue:
            raise AssertionError("Unexpected extra HTTP request")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(monitor, "_request", fake_request)
    return calls


def insert_website(db_path, name="Example", url="https://example.com/"):
    with database.session(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO websites (name, url, created_at) VALUES (?, ?, ?)",
            (name, url, database.utc_now()),
        )
        return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# HTTP classification
# ---------------------------------------------------------------------------

def test_http_200_is_up(monkeypatch):
    queue_requests(monkeypatch, [FakeResponse(200, url="https://example.com/")])
    result = monitor.perform_check("https://example.com")
    assert result.status == "UP"
    assert result.http_status_code == 200
    assert result.response_time_ms is not None
    assert result.error_message is None


def test_http_301_redirect_followed_to_final_status(monkeypatch):
    calls = queue_requests(
        monkeypatch,
        [
            FakeResponse(301, {"Location": "/final"}, url="https://example.com/"),
            FakeResponse(200, url="https://example.com/final"),
        ],
    )
    result = monitor.perform_check("https://example.com")
    assert result.status == "UP"
    assert result.http_status_code == 200
    assert result.final_url == "https://example.com/final"
    assert result.redirects == 1
    assert len(calls) == 2


def test_http_404_is_degraded(monkeypatch):
    queue_requests(monkeypatch, [FakeResponse(404, url="https://example.com/missing")])
    result = monitor.perform_check("https://example.com/missing")
    assert result.status == "DEGRADED"
    assert result.http_status_code == 404
    assert "404" in (result.error_message or "")


def test_http_500_is_degraded(monkeypatch):
    queue_requests(monkeypatch, [FakeResponse(500)])
    result = monitor.perform_check("https://example.com")
    assert result.status == "DEGRADED"
    assert result.http_status_code == 500


def test_unreachable_host_is_down(monkeypatch):
    queue_requests(monkeypatch, [requests.exceptions.ConnectionError("refused")])
    result = monitor.perform_check("https://example.com")
    assert result.status == "DOWN"
    assert "Connection failed" in (result.error_message or "")


def test_timeout_is_down(monkeypatch):
    queue_requests(monkeypatch, [requests.exceptions.Timeout("slow")])
    result = monitor.perform_check("https://example.com", timeout=10)
    assert result.status == "DOWN"
    assert "timed out" in (result.error_message or "").lower()
    assert result.response_time_ms is not None


def test_dns_failure_is_down(monkeypatch):
    def failing_resolve(hostname, port=None):
        raise monitor.DNSResolutionError(f"DNS resolution failed for {hostname}.")

    monkeypatch.setattr(monitor, "resolve_host", failing_resolve)
    result = monitor.perform_check("https://unknown.invalid")
    assert result.status == "DOWN"
    assert "DNS resolution failed" in (result.error_message or "")


def test_slow_response_is_degraded(monkeypatch):
    def slow_request(session, url, timeout, address=None):
        time.sleep(0.32)
        return FakeResponse(200)

    monkeypatch.setattr(monitor, "_request", slow_request)
    result = monitor.perform_check("https://example.com", moderate_threshold=300)
    assert result.status == "DEGRADED"
    assert "Slow response" in result.detail


def test_redirect_loop_is_down(monkeypatch):
    responses = [FakeResponse(301, {"Location": "/loop"}, url="https://example.com/loop")] * 8
    queue_requests(monkeypatch, responses)
    result = monitor.perform_check("https://example.com")
    assert result.status == "DOWN"
    assert "redirect" in (result.error_message or "").lower()


def test_ssl_error_is_down(monkeypatch):
    queue_requests(monkeypatch, [requests.exceptions.SSLError("bad cert")])
    result = monitor.perform_check("https://expired.example")
    assert result.status == "DOWN"
    assert "certificate" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

def test_loopback_literal_blocked(monkeypatch):
    calls = queue_requests(monkeypatch, [])
    result = monitor.perform_check("http://127.0.0.1/")
    assert result.status == "DOWN"
    assert "private" in (result.error_message or "").lower()
    assert calls == []


def test_private_resolution_blocked(monkeypatch):
    monkeypatch.setattr(monitor, "resolve_host", lambda hostname, port=None: ["10.0.0.5"])
    calls = queue_requests(monkeypatch, [])
    result = monitor.perform_check("https://internal.example.com")
    assert result.status == "DOWN"
    assert calls == []


def test_redirect_to_private_address_blocked(monkeypatch):
    calls = queue_requests(
        monkeypatch,
        [FakeResponse(301, {"Location": "http://192.168.1.10/admin"}, url="https://example.com/")],
    )
    result = monitor.perform_check("https://example.com")
    assert result.status == "DOWN"
    assert len(calls) == 1
    assert "private" in (result.error_message or "").lower()


def test_localhost_hostname_blocked():
    with pytest.raises(ValueError):
        monitor.normalize_url("http://localhost:80/")


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)"])
def test_non_http_schemes_rejected(url):
    with pytest.raises(ValueError):
        monitor.normalize_url(url)


def test_urls_with_credentials_rejected():
    with pytest.raises(ValueError):
        monitor.normalize_url("https://user:pass@example.com/")


def test_non_standard_port_rejected():
    with pytest.raises(ValueError):
        monitor.normalize_url("https://example.com:8080/")


def test_url_normalisation():
    assert monitor.normalize_url("example.com") == "https://example.com/"
    assert monitor.normalize_url("HTTPS://Example.COM/Path#frag") == "https://example.com/Path"
    assert monitor.normalize_url("http://example.com:80/a?b=1") == "http://example.com:80/a?b=1"


# ---------------------------------------------------------------------------
# Certificate summaries (no network)
# ---------------------------------------------------------------------------

def make_cert(expires_in_days: int) -> dict:
    now = datetime.now(timezone.utc)

    def stamp(value: datetime) -> str:
        return value.strftime("%b %d %H:%M:%S %Y GMT")

    return {
        "notBefore": stamp(now - timedelta(days=30)),
        "notAfter": stamp(now + timedelta(days=expires_in_days)),
        "issuer": ((("commonName", "Test CA"),),),
        "subject": ((("commonName", "example.com"),),),
        "serialNumber": "ABCD1234",
    }


def test_ssl_valid_certificate_summary():
    summary = monitor.summarize_certificate(make_cert(200))
    assert summary["expiry_status"] == "valid"
    assert summary["warning"] is False
    assert 195 <= summary["days_remaining"] <= 200
    assert summary["issuer"] == "Test CA"
    assert summary["expires_at"].endswith("Z")


def test_ssl_expiring_soon_warns():
    summary = monitor.summarize_certificate(make_cert(12))
    assert summary["expiry_status"] == "warning"
    assert summary["warning"] is True


def test_ssl_expired_certificate():
    summary = monitor.summarize_certificate(make_cert(-3))
    assert summary["expiry_status"] == "expired"
    assert summary["days_remaining"] < 0


def test_ssl_not_applicable_for_http():
    result = monitor.check_ssl("http://example.com")
    assert result["applicable"] is False
    assert result["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# DNS helpers
# ---------------------------------------------------------------------------

def test_lookup_dns_returns_addresses():
    data = monitor.lookup_dns("https://example.com")
    assert data["hostname"] == "example.com"
    assert data["ipv4"] == ["93.184.216.34"]
    assert data["primary_ip"] == "93.184.216.34"


def test_dns_lookup_failure_reported(monkeypatch):
    def failing(hostname, port=None):
        raise monitor.DNSResolutionError("DNS resolution failed.")

    monkeypatch.setattr(monitor, "resolve_host", failing)
    with pytest.raises(monitor.DNSResolutionError):
        monitor.lookup_dns("https://unknown.invalid")


# ---------------------------------------------------------------------------
# Monitoring cycle + incidents
# ---------------------------------------------------------------------------

def test_cycle_monitors_multiple_sites_and_creates_incidents(db_path, monkeypatch):
    insert_website(db_path, "Alpha", "https://alpha.example.com/")
    insert_website(db_path, "Beta", "https://beta.example.com/")
    insert_website(db_path, "Gamma", "https://gamma.example.com/")

    outcomes = {"alpha": "UP", "beta": "DOWN", "gamma": "DEGRADED"}

    def fake_check(url, timeout=None, fast_threshold=None, moderate_threshold=None, session=None):
        key = url.split("//", 1)[1].split(".", 1)[0]
        status = outcomes[key]
        return monitor.CheckResult(
            url=url,
            status=status,
            http_status_code=200 if status == "UP" else (503 if status == "DEGRADED" else None),
            response_time_ms=140.0,
            error_message=None if status == "UP" else f"{status} simulated",
            detail=f"{status} simulated",
            checked_at=database.utc_now(),
        )

    monkeypatch.setattr(monitor, "perform_check", fake_check)
    summary = monitor.run_monitoring_cycle(db_path)

    assert summary["checked"] == 3
    assert summary["up"] == 1
    assert summary["down"] == 1
    assert summary["degraded"] == 1

    with database.session(db_path) as conn:
        websites = conn.execute("SELECT name, current_status, last_checked FROM websites").fetchall()
        incidents = conn.execute("SELECT * FROM incidents").fetchall()
        results = conn.execute("SELECT COUNT(*) AS count FROM monitoring_results").fetchone()["count"]

    assert {row["name"].lower(): row["current_status"] for row in websites} == outcomes
    assert all(row["last_checked"] for row in websites)
    assert results == 3
    assert len(incidents) == 2
    assert {row["incident_status"] for row in incidents} == {"OPEN"}


def test_incident_closes_on_recovery(db_path, monkeypatch):
    website_id = insert_website(db_path, "Flappy", "https://flappy.example.com/")
    state = {"status": "DOWN"}

    def fake_check(url, timeout=None, fast_threshold=None, moderate_threshold=None, session=None):
        return monitor.CheckResult(
            url=url,
            status=state["status"],
            http_status_code=None if state["status"] == "DOWN" else 200,
            response_time_ms=95.0,
            detail="simulated",
            checked_at=database.utc_now(),
        )

    monkeypatch.setattr(monitor, "perform_check", fake_check)

    monitor.run_monitoring_cycle(db_path)
    with database.session(db_path) as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) AS count FROM incidents WHERE incident_status = 'OPEN'"
        ).fetchone()["count"]
    assert open_count == 1

    state["status"] = "UP"
    time.sleep(1.05)
    monitor.run_monitoring_cycle(db_path)

    with database.session(db_path) as conn:
        incident = conn.execute(
            "SELECT * FROM incidents WHERE website_id = ?", (website_id,)
        ).fetchone()
        status = conn.execute(
            "SELECT current_status FROM websites WHERE id = ?", (website_id,)
        ).fetchone()["current_status"]

    assert incident["incident_status"] == "RESOLVED"
    assert incident["resolved_at"] is not None
    assert incident["duration_seconds"] >= 1
    assert status == "UP"


def test_cycle_skips_when_already_running(db_path):
    acquired = monitor._cycle_lock.acquire(blocking=False)
    assert acquired
    try:
        summary = monitor.run_monitoring_cycle(db_path)
    finally:
        monitor._cycle_lock.release()
    assert summary["skipped"] is True
    assert summary["checked"] == 0
