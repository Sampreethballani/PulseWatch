from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import analytics
import database


def add_site(db_path, name="Example", url="https://example.com/"):
    with database.session(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO websites (name, url, created_at) VALUES (?, ?, ?)",
            (name, url, database.utc_now()),
        )
        return int(cursor.lastrowid)


def add_result(db_path, website_id, status, response_ms=120.0, hours_ago=0.0,
               code=200, error=None):
    checked_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with database.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO monitoring_results
                (website_id, status, http_status_code, response_time_ms, error_message, checked_at, resolved_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (website_id, status, code, response_ms, error, database.format_utc(checked_at), "93.184.216.34"),
        )


def test_uptime_calculation_and_success_rule(db_path):
    site_id = add_site(db_path)
    add_result(db_path, site_id, "UP", 100)
    add_result(db_path, site_id, "DEGRADED", 4000, code=503, error="HTTP 503")
    add_result(db_path, site_id, "UP", 150)

    result = analytics.uptime_window(site_id, 24, db_path)
    assert result["total_checks"] == 3
    assert result["successful_checks"] == 2
    assert result["uptime_pct"] == pytest.approx(66.67, abs=0.01)


def test_uptime_is_none_without_history(db_path):
    site_id = add_site(db_path)
    result = analytics.uptime_window(site_id, 24, db_path)
    assert result["total_checks"] == 0
    assert result["uptime_pct"] is None

    site = analytics.site_overview(db_path)[0]
    assert site["uptime_24h"] is None
    assert site["uptime_7d"] is None
    assert site["status"] == "UNKNOWN"


def test_uptime_window_filters_old_results(db_path):
    site_id = add_site(db_path)
    add_result(db_path, site_id, "UP")
    add_result(db_path, site_id, "DOWN", hours_ago=72, code=None)

    assert analytics.uptime_window(site_id, 24, db_path)["total_checks"] == 1
    assert analytics.uptime_window(site_id, 168, db_path)["total_checks"] == 2
    assert analytics.uptime_window(site_id, 168, db_path)["uptime_pct"] == 50.0


def test_overall_uptime_across_sites(db_path):
    first = add_site(db_path, "One", "https://one.example.com/")
    second = add_site(db_path, "Two", "https://two.example.com/")
    add_result(db_path, first, "UP")
    add_result(db_path, second, "UP")
    add_result(db_path, second, "DOWN", code=None)

    result = analytics.uptime_window(None, 24, db_path)
    assert result["total_checks"] == 3
    assert result["uptime_pct"] == pytest.approx(66.67, abs=0.01)


def test_dashboard_snapshot_summary(db_path):
    first = add_site(db_path, "One", "https://one.example.com/")
    second = add_site(db_path, "Two", "https://two.example.com/")
    with database.session(db_path) as conn:
        conn.execute("UPDATE websites SET current_status = 'UP' WHERE id = ?", (first,))
        conn.execute("UPDATE websites SET current_status = 'DOWN' WHERE id = ?", (second,))
    add_result(db_path, first, "UP", 200)
    add_result(db_path, second, "DOWN", 30, code=None)

    snapshot = analytics.dashboard_snapshot(db_path)
    summary = snapshot["summary"]
    assert summary["total"] == 2
    assert summary["up"] == 1
    assert summary["down"] == 1
    assert summary["uptime_24h"] == 50.0
    assert summary["avg_response_ms"] == 200.0
    assert snapshot["monitor"]["interval"] == 30
    assert len(snapshot["sites"]) == 2


def test_comparison_aggregates(db_path):
    site_id = add_site(db_path, "One", "https://one.example.com/")
    add_result(db_path, site_id, "UP", 100)
    add_result(db_path, site_id, "UP", 300)

    data = analytics.comparison(24, db_path)
    site = data["sites"][0]
    assert site["total_checks"] == 2
    assert site["uptime_pct"] == 100.0
    assert site["avg_response_ms"] == 200.0


def test_response_series_marks_failures_as_gaps(db_path):
    site_id = add_site(db_path)
    add_result(db_path, site_id, "UP", 100, hours_ago=0.02)
    add_result(db_path, site_id, "DOWN", 40, hours_ago=0.01, code=None)

    series = analytics.response_series(site_id, 24, db_path=db_path)
    assert len(series["labels"]) == 2
    assert series["values"][0] == 100.0
    assert series["values"][1] is None
    assert series["statuses"] == ["UP", "DOWN"]


def test_timeline_buckets(db_path):
    site_id = add_site(db_path)
    add_result(db_path, site_id, "UP", 100)
    add_result(db_path, site_id, "DEGRADED", 2500, code=500)

    data = analytics.timeline(site_id, 24, db_path)
    assert data["bucket_minutes"] == 30
    assert len(data["buckets"]) == 48
    assert data["total_checks"] == 2
    assert data["buckets"][-1]["status"] == "degraded"
    assert data["buckets"][-1]["total"] == 2
    assert data["uptime_pct"] == 50.0


def test_export_rows_filters_website_and_period(db_path):
    first = add_site(db_path, "One", "https://one.example.com/")
    second = add_site(db_path, "Two", "https://two.example.com/")
    add_result(db_path, first, "UP", 120)
    add_result(db_path, second, "UP", 130)
    add_result(db_path, first, "DOWN", 30, hours_ago=800, code=None)

    rows = analytics.export_rows(first, 720, db_path)
    assert len(rows) == 1
    assert rows[0]["website_name"] == "One"

    all_rows = analytics.export_rows(None, 720, db_path)
    assert len(all_rows) == 2


def test_period_summary_includes_incident_count(db_path):
    site_id = add_site(db_path)
    add_result(db_path, site_id, "DOWN", code=None)
    with database.session(db_path) as conn:
        conn.execute(
            "INSERT INTO incidents (website_id, incident_status, started_at, description) VALUES (?, 'OPEN', ?, ?)",
            (site_id, database.utc_now(), "DOWN: simulated"),
        )

    summary = analytics.period_summary(24, db_path)
    assert summary["checks"] == 1
    assert summary["uptime_pct"] == 0.0
    assert summary["incidents"] == 1
