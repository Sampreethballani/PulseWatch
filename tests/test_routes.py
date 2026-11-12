from __future__ import annotations

import csv
import io

import database
import monitor


def add_site(client, name="Example", url="https://example.com"):
    return client.post(
        "/websites/add",
        data={"name": name, "url": url},
        follow_redirects=True,
    )


def get_site(db_path, name="Example"):
    with database.session(db_path) as conn:
        return conn.execute("SELECT * FROM websites WHERE name = ?", (name,)).fetchone()


def test_add_website_and_dashboard_render(client, db_path):
    response = add_site(client, "Example", "example.com")
    assert response.status_code == 200
    assert b"Example" in response.data

    site = get_site(db_path)
    assert site["url"] == "https://example.com/"
    assert site["current_status"] == "UNKNOWN"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Monitoring overview" in dashboard.data
    assert b"example.com" in dashboard.data


def test_duplicate_urls_rejected(client, db_path):
    add_site(client, "First", "https://example.com/")
    response = add_site(client, "Second", "https://EXAMPLE.com")
    assert b"already being monitored" in response.data

    with database.session(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM websites").fetchone()["c"]
    assert count == 1


def test_invalid_scheme_rejected(client, db_path):
    response = add_site(client, "Bad", "ftp://example.com/file")
    assert b"Only http" in response.data
    with database.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM websites").fetchone()["c"] == 0


def test_private_address_rejected(client, db_path, monkeypatch):
    monkeypatch.setattr(monitor, "resolve_host", lambda hostname, port=None: ["192.168.0.10"])
    response = add_site(client, "Internal", "https://intranet.example.com")
    assert b"private" in response.data.lower()
    with database.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM websites").fetchone()["c"] == 0


def test_websites_page_and_details(client, db_path):
    add_site(client, "Example", "https://example.com")
    sites = client.get("/websites")
    assert sites.status_code == 200
    assert b"All monitors" in sites.data

    site = get_site(db_path)
    details = client.get(f"/websites/{site['id']}")
    assert details.status_code == 200
    assert b"Availability timeline" in details.data
    assert b"DNS" in details.data


def test_edit_website(client, db_path):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)

    response = client.post(
        f"/websites/{site['id']}/edit",
        data={"name": "Renamed", "url": "https://example.org"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    updated = get_site(db_path, "Renamed")
    assert updated["url"] == "https://example.org/"


def test_delete_cascades_history(client, db_path):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)
    with database.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO monitoring_results (website_id, status, checked_at)
            VALUES (?, 'UP', ?)
            """,
            (site["id"], database.utc_now()),
        )
        conn.execute(
            "INSERT INTO incidents (website_id, incident_status, started_at) VALUES (?, 'OPEN', ?)",
            (site["id"], database.utc_now()),
        )

    response = client.post(f"/websites/{site['id']}/delete", follow_redirects=True)
    assert b"removed" in response.data

    with database.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM websites").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM monitoring_results").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM incidents").fetchone()["c"] == 0


def test_manual_check_updates_status(client, db_path, monkeypatch):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)

    def fake_check(url, timeout=None, fast_threshold=None, moderate_threshold=None, session=None):
        return monitor.CheckResult(
            url=url,
            status="UP",
            http_status_code=200,
            response_time_ms=87.5,
            detail="HTTP 200 in 88 ms",
            checked_at=database.utc_now(),
        )

    monkeypatch.setattr(monitor, "perform_check", fake_check)
    response = client.post(f"/websites/{site['id']}/check", follow_redirects=True)
    assert response.status_code == 200
    assert b"UP" in response.data

    updated = get_site(db_path)
    assert updated["current_status"] == "UP"
    assert updated["last_checked"]


def test_settings_page_updates_values(client, db_path):
    response = client.post(
        "/settings",
        data={
            "monitoring_interval": "60",
            "timeout_seconds": "15",
            "fast_threshold": "250",
            "moderate_threshold": "900",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Settings saved" in response.data

    settings = database.get_settings(db_path)
    assert settings["monitoring_interval"] == 60
    assert settings["timeout_seconds"] == 15
    assert settings["fast_threshold"] == 250
    assert settings["moderate_threshold"] == 900


def test_settings_validation_rejects_bad_thresholds(client, db_path):
    response = client.post(
        "/settings",
        data={
            "monitoring_interval": "30",
            "timeout_seconds": "10",
            "fast_threshold": "1500",
            "moderate_threshold": "900",
        },
        follow_redirects=True,
    )
    assert b"fast threshold must be smaller" in response.data
    assert database.get_settings(db_path)["fast_threshold"] == 300


def test_csv_export(client, db_path):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)
    with database.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO monitoring_results
                (website_id, status, http_status_code, response_time_ms, error_message, checked_at, resolved_ip)
            VALUES (?, 'UP', 200, 123.4, NULL, ?, '93.184.216.34')
            """,
            (site["id"], database.utc_now()),
        )

    response = client.get("/export/csv?hours=720")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]

    text = response.data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "Website name"
    assert rows[0][3] == "Availability status"
    assert rows[1][0] == "Example"
    assert rows[1][3] == "UP"
    assert rows[1][4] == "200"


def test_api_dashboard_and_history(client, db_path):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)

    payload = client.get("/api/dashboard")
    assert payload.status_code == 200
    assert payload.headers["Cache-Control"] == "no-store"
    data = payload.get_json()
    assert data["summary"]["total"] == 1
    assert data["sites"][0]["name"] == "Example"

    history = client.get(f"/api/websites/{site['id']}/history?hours=24")
    assert history.status_code == 200
    assert history.get_json()["labels"] == []

    missing = client.get("/api/websites/999/history")
    assert missing.status_code == 404


def test_incidents_page_and_filters(client, db_path):
    add_site(client, "Example", "https://example.com")
    site = get_site(db_path)
    with database.session(db_path) as conn:
        conn.execute(
            "INSERT INTO incidents (website_id, incident_status, started_at, description) VALUES (?, 'RESOLVED', ?, ?)",
            (site["id"], database.utc_now(), "DOWN: timeout"),
        )

    page = client.get("/incidents")
    assert page.status_code == 200
    assert b"DOWN: timeout" in page.data

    filtered = client.get("/incidents?status=OPEN")
    assert b"DOWN: timeout" not in filtered.data


def test_analytics_page(client):
    add_site(client, "Example", "https://example.com")
    response = client.get("/analytics?hours=168")
    assert response.status_code == 200
    assert b"Response time history" in response.data
    assert b"Uptime by website" in response.data


def test_404_renders_error_page(client):
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert b"does not exist" in response.data


def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
