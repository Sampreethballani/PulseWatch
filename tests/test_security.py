"""Production-hardening tests: access control, rate limits and storage guards."""

from __future__ import annotations

import pytest

import config
import database
import monitor
from app import create_app

REMOTE = {"REMOTE_ADDR": "203.0.113.9"}
STRONG_TOKEN = "test-admin-token-please-change"


def build_app(db_path, **overrides):
    settings = {
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "DATABASE_PATH": db_path,
        "ENABLE_SCHEDULER": False,
    }
    settings.update(overrides)
    return create_app(settings)


def website_count(db_path) -> int:
    with database.session(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) AS count FROM websites").fetchone()["count"])


def test_remote_visitor_gets_read_only_dashboard(db_path):
    app = build_app(db_path)
    client = app.test_client()

    page = client.get("/", environ_base=REMOTE)
    assert page.status_code == 200
    assert b"Local management only" in page.data

    response = client.post(
        "/websites/add",
        data={"name": "Blocked", "url": "https://example.com"},
        environ_base=REMOTE,
    )
    assert response.status_code == 403
    assert website_count(db_path) == 0


def test_admin_login_unlocks_management_and_logout_locks_it(db_path):
    app = build_app(db_path, ADMIN_TOKEN=STRONG_TOKEN)
    client = app.test_client()

    assert client.post(
        "/websites/add",
        data={"name": "Blocked", "url": "https://example.com"},
        environ_base=REMOTE,
    ).status_code == 403

    wrong = client.post(
        "/admin/login", data={"token": "wrong-token"}, environ_base=REMOTE, follow_redirects=True
    )
    assert b"not valid" in wrong.data
    assert website_count(db_path) == 0

    signed_in = client.post(
        "/admin/login", data={"token": STRONG_TOKEN}, environ_base=REMOTE
    )
    assert signed_in.status_code == 302

    added = client.post(
        "/websites/add",
        data={"name": "Example", "url": "https://example.com"},
        environ_base=REMOTE,
        follow_redirects=True,
    )
    assert added.status_code == 200
    assert website_count(db_path) == 1

    client.post("/admin/logout", environ_base=REMOTE)
    assert client.post(
        "/websites/add",
        data={"name": "After logout", "url": "https://example.org"},
        environ_base=REMOTE,
    ).status_code == 403
    assert website_count(db_path) == 1


def test_settings_requires_administration(db_path):
    app = build_app(db_path)
    client = app.test_client()

    response = client.get("/settings", environ_base=REMOTE)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_cross_origin_form_post_is_rejected(db_path):
    app = build_app(db_path, ADMIN_TOKEN=STRONG_TOKEN)
    client = app.test_client()

    response = client.post(
        "/admin/login",
        data={"token": STRONG_TOKEN},
        headers={"Origin": "https://evil.example"},
        environ_base=REMOTE,
    )
    assert response.status_code == 403


def test_mutation_rate_limit_returns_429(db_path):
    app = build_app(
        db_path,
        ADMIN_TOKEN=STRONG_TOKEN,
        RATE_LIMIT_MUTATIONS_PER_MINUTE=3,
        RATE_LIMIT_GENERAL_PER_MINUTE=100,
    )
    client = app.test_client()
    client.post("/admin/login", data={"token": STRONG_TOKEN}, environ_base=REMOTE)

    first = client.post(
        "/websites/add",
        data={"name": "One", "url": "https://one.example.com"},
        environ_base=REMOTE,
    )
    second = client.post(
        "/websites/add",
        data={"name": "Two", "url": "https://two.example.com"},
        environ_base=REMOTE,
    )
    third = client.post(
        "/websites/add",
        data={"name": "Three", "url": "https://three.example.com"},
        environ_base=REMOTE,
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "60"


def test_health_endpoint_reports_database_and_scheduler(db_path):
    app = build_app(db_path)
    payload = app.test_client().get("/healthz").get_json()
    assert payload["status"] == "ok"
    assert payload["database"]["ok"] is True
    assert payload["database"]["writable"] is True
    assert "scheduler" in payload
    assert payload["admin_configured"] is False


def test_database_directory_creation_failure_is_reported(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(RuntimeError):
        database.ensure_database_ready(str(blocker / "monitor.db"), production=False)


def test_production_requires_explicit_database_path(db_path):
    with pytest.raises(RuntimeError, match="DATABASE_PATH"):
        build_app(
            db_path,
            PRODUCTION=True,
            SECRET_KEY="a" * 48,
            DATABASE_PATH_EXPLICIT=False,
        )


def test_production_requires_non_default_secret(db_path):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        build_app(
            db_path,
            PRODUCTION=True,
            SECRET_KEY=config.Config.DEV_SECRET_KEY,
            DATABASE_PATH_EXPLICIT=True,
        )


def test_persistent_disk_requirement_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Config, "DATABASE_PATH_EXPLICIT", True)
    monkeypatch.setattr(config.Config, "PERSISTENT_DB_DIR", str(tmp_path / "persistent"))

    outside = tmp_path / "ephemeral" / "monitor.db"
    with pytest.raises(RuntimeError, match="persistent"):
        database.ensure_database_ready(str(outside), production=True, require_persistent=True)

    inside = tmp_path / "persistent" / "monitor.db"
    resolved = database.ensure_database_ready(
        str(inside), production=True, require_persistent=True
    )
    assert resolved == inside.resolve()


def test_probe_connects_to_the_validated_ip(monkeypatch):
    captured = {}

    def fake_pinned_get(url, address, timeout):
        captured.update(url=url, address=address)
        return monitor.HTTPProbeResponse(200, {}, url)

    monkeypatch.setattr(monitor, "_pinned_get", fake_pinned_get)
    response = monitor._request(None, "https://example.com/", 5, "93.184.216.34")
    assert captured == {"url": "https://example.com/", "address": "93.184.216.34"}
    assert response.status_code == 200


def test_perform_check_passes_resolved_address_to_the_transport(monkeypatch):
    seen = []

    class FakeResponse:
        status_code = 200
        headers = {}
        url = "https://example.com/"

    def fake_request(session, url, timeout, address=None):
        seen.append(address)
        return FakeResponse()

    monkeypatch.setattr(monitor, "_request", fake_request)
    result = monitor.perform_check("https://example.com")
    assert result.status == "UP"
    assert seen == ["93.184.216.34"]
