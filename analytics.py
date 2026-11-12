"""Read models: uptime, analytics series, timelines, incidents, snapshots."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from urllib.parse import urlsplit

import config
import database
import monitor

# A check counts as "successful" for uptime when it is UP. DEGRADED responses
# (HTTP 4xx/5xx or very slow) and DOWN results count as unsuccessful. This rule
# is applied everywhere: dashboard, details page, analytics and CSV reports.
SUCCESS_STATUS = "UP"

PERIODS = {24: "24h", 168: "7d", 720: "30d"}
TIMELINE_BUCKET_MINUTES = {24: 30, 168: 240, 720: 1440}


def normalize_hours(hours: int | None) -> int:
    try:
        value = int(hours or 24)
    except (TypeError, ValueError):
        return 24
    return value if value in PERIODS else 24


def cutoff(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct(up: int, total: int) -> float | None:
    if not total:
        return None
    return round((up / total) * 100, 2)


def response_time_class(response_ms: float | None, fast: int, moderate: int) -> str:
    if response_ms is None:
        return "unknown"
    if response_ms < fast:
        return "fast"
    if response_ms <= moderate:
        return "moderate"
    return "slow"


# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------

def uptime_window(website_id: int | None = None, hours: int = 24,
                  db_path: str | None = None) -> dict:
    sql = """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END), 0) AS successful
        FROM monitoring_results
        WHERE checked_at >= ?
    """
    params: list = [cutoff(hours)]
    if website_id is not None:
        sql += " AND website_id = ?"
        params.append(website_id)

    with database.session(db_path) as conn:
        row = conn.execute(sql, params).fetchone()

    total = int(row["total"] or 0)
    successful = int(row["successful"] or 0)
    return {
        "total_checks": total,
        "successful_checks": successful,
        "failed_checks": total - successful,
        "uptime_pct": _pct(successful, total),
        "hours": hours,
    }


# ---------------------------------------------------------------------------
# Website overview (dashboard / websites page)
# ---------------------------------------------------------------------------

def _stats_by_site(conn, hours: int) -> dict[int, dict]:
    rows = conn.execute(
        """
        SELECT website_id,
               COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END), 0) AS up,
               AVG(CASE WHEN status = 'UP' THEN response_time_ms END) AS avg_ms
        FROM monitoring_results
        WHERE checked_at >= ?
        GROUP BY website_id
        """,
        (cutoff(hours),),
    ).fetchall()
    return {
        int(row["website_id"]): {
            "total": int(row["total"] or 0),
            "up": int(row["up"] or 0),
            "avg_ms": row["avg_ms"],
        }
        for row in rows
    }


def site_overview(db_path: str | None = None) -> list[dict]:
    settings = database.get_settings(db_path)
    fast = int(settings["fast_threshold"])
    moderate = int(settings["moderate_threshold"])

    with database.session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.name, w.url, w.created_at, w.current_status, w.last_checked,
                   r.status AS last_status, r.http_status_code, r.response_time_ms,
                   r.error_message AS last_error, r.checked_at AS last_result_at, r.resolved_ip
            FROM websites w
            LEFT JOIN monitoring_results r
                   ON r.id = (SELECT id FROM monitoring_results
                              WHERE website_id = w.id ORDER BY id DESC LIMIT 1)
            ORDER BY w.name COLLATE NOCASE
            """
        ).fetchall()
        stats = {hours: _stats_by_site(conn, hours) for hours in PERIODS}

    sites: list[dict] = []
    for row in rows:
        site_id = int(row["id"])
        status = (row["last_status"] or row["current_status"] or "UNKNOWN").upper()
        hostname = urlsplit(row["url"]).hostname or row["url"]

        windows = {}
        for hours in PERIODS:
            bucket = stats[hours].get(site_id, {})
            windows[hours] = {
                "checks": bucket.get("total", 0),
                "uptime_pct": _pct(bucket.get("up", 0), bucket.get("total", 0)),
                "avg_ms": round(bucket["avg_ms"], 1) if bucket.get("avg_ms") is not None else None,
            }

        sites.append({
            "id": site_id,
            "name": row["name"],
            "url": row["url"],
            "hostname": hostname,
            "created_at": row["created_at"],
            "status": status,
            "http_status_code": row["http_status_code"],
            "response_time_ms": row["response_time_ms"],
            "error_message": row["last_error"],
            "checked_at": row["last_result_at"] or row["last_checked"],
            "resolved_ip": row["resolved_ip"],
            "performance_class": response_time_class(row["response_time_ms"], fast, moderate),
            "uptime_24h": windows[24]["uptime_pct"],
            "uptime_7d": windows[168]["uptime_pct"],
            "uptime_30d": windows[720]["uptime_pct"],
            "checks_24h": windows[24]["checks"],
            "avg_response_24h": windows[24]["avg_ms"],
        })
    return sites


def site_detail(website_id: int, db_path: str | None = None) -> dict | None:
    sites = site_overview(db_path)
    site = next((item for item in sites if item["id"] == website_id), None)
    if site is None:
        return None

    with database.session(db_path) as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS checks, MIN(checked_at) AS first_check,
                   MAX(checked_at) AS last_check
            FROM monitoring_results WHERE website_id = ?
            """,
            (website_id,),
        ).fetchone()
        open_incident = conn.execute(
            """
            SELECT * FROM incidents
            WHERE website_id = ? AND incident_status = 'OPEN'
            ORDER BY started_at DESC LIMIT 1
            """,
            (website_id,),
        ).fetchone()
        incident_count = conn.execute(
            "SELECT COUNT(*) AS count FROM incidents WHERE website_id = ?", (website_id,)
        ).fetchone()

    site["total_checks"] = int(totals["checks"] or 0)
    site["first_check"] = totals["first_check"]
    site["last_check"] = totals["last_check"]
    site["open_incident"] = dict(open_incident) if open_incident else None
    site["incident_count"] = int(incident_count["count"] or 0)
    return site


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def response_series(website_id: int, hours: int = 24, max_points: int = 160,
                    db_path: str | None = None) -> dict:
    hours = normalize_hours(hours)
    with database.session(db_path) as conn:
        website = conn.execute(
            "SELECT id, name, url FROM websites WHERE id = ?", (website_id,)
        ).fetchone()
        if website is None:
            return {}
        rows = conn.execute(
            """
            SELECT checked_at, status, response_time_ms, http_status_code
            FROM monitoring_results
            WHERE website_id = ? AND checked_at >= ?
            ORDER BY checked_at ASC, id ASC
            """,
            (website_id, cutoff(hours)),
        ).fetchall()

    total = len(rows)
    step = max(1, ceil(total / max_points)) if total > max_points else 1
    sampled = rows[::step]
    if rows and sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])

    labels: list[str] = []
    values: list[float | None] = []
    statuses: list[str] = []
    codes: list[int | None] = []
    for row in sampled:
        labels.append(row["checked_at"])
        is_up = row["status"] == SUCCESS_STATUS
        values.append(round(row["response_time_ms"], 1) if is_up and row["response_time_ms"] is not None else None)
        statuses.append(row["status"])
        codes.append(row["http_status_code"])

    return {
        "website": {"id": website["id"], "name": website["name"], "url": website["url"]},
        "hours": hours,
        "labels": labels,
        "values": values,
        "statuses": statuses,
        "codes": codes,
        "points": len(sampled),
        "total_checks": total,
        "sampled": step > 1,
    }


def comparison(hours: int = 24, db_path: str | None = None) -> dict:
    hours = normalize_hours(hours)
    with database.session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.name, w.url,
                   COUNT(r.id) AS total,
                   COALESCE(SUM(CASE WHEN r.status = 'UP' THEN 1 ELSE 0 END), 0) AS up,
                   AVG(CASE WHEN r.status = 'UP' THEN r.response_time_ms END) AS avg_ms
            FROM websites w
            LEFT JOIN monitoring_results r
                   ON r.website_id = w.id AND r.checked_at >= ?
            GROUP BY w.id
            ORDER BY w.name COLLATE NOCASE
            """,
            (cutoff(hours),),
        ).fetchall()

    sites = []
    for row in rows:
        total = int(row["total"] or 0)
        up = int(row["up"] or 0)
        sites.append({
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "total_checks": total,
            "uptime_pct": _pct(up, total),
            "avg_response_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
        })

    return {
        "hours": hours,
        "sites": sites,
        "generated_at": database.utc_now(),
        "success_rule": "A check is successful when its status is UP.",
    }


def timeline(website_id: int, hours: int = 24, db_path: str | None = None) -> dict:
    hours = normalize_hours(hours)
    bucket_minutes = TIMELINE_BUCKET_MINUTES[hours]
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)
    bucket_count = int(ceil(hours * 60 / bucket_minutes))
    bucket_seconds = bucket_minutes * 60

    buckets = [
        {
            "start_dt": window_start + timedelta(seconds=bucket_seconds * index),
            "total": 0,
            "up": 0,
            "down": 0,
            "degraded": 0,
        }
        for index in range(bucket_count)
    ]

    with database.session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT checked_at, status FROM monitoring_results
            WHERE website_id = ? AND checked_at >= ?
            ORDER BY checked_at ASC
            """,
            (website_id, database.format_utc(window_start)),
        ).fetchall()

    for row in rows:
        checked = database.parse_utc(row["checked_at"])
        if checked is None:
            continue
        index = int((checked - window_start).total_seconds() // bucket_seconds)
        index = max(0, min(bucket_count - 1, index))
        bucket = buckets[index]
        bucket["total"] += 1
        status = (row["status"] or "").upper()
        if status == "UP":
            bucket["up"] += 1
        elif status == "DOWN":
            bucket["down"] += 1
        elif status == "DEGRADED":
            bucket["degraded"] += 1

    output = []
    for bucket in buckets:
        if bucket["total"] == 0:
            visual = "empty"
        elif bucket["down"]:
            visual = "down"
        elif bucket["degraded"]:
            visual = "degraded"
        else:
            visual = "up"
        start = bucket["start_dt"]
        end = start + timedelta(seconds=bucket_seconds)
        output.append({
            "start": database.format_utc(start),
            "end": database.format_utc(end),
            "status": visual,
            "total": bucket["total"],
            "up": bucket["up"],
            "down": bucket["down"],
            "degraded": bucket["degraded"],
            "uptime_pct": _pct(bucket["up"], bucket["total"]),
        })

    window_checks = sum(bucket["total"] for bucket in buckets)
    window_up = sum(bucket["up"] for bucket in buckets)
    return {
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "window_start": database.format_utc(window_start),
        "window_end": database.format_utc(now),
        "buckets": output,
        "total_checks": window_checks,
        "uptime_pct": _pct(window_up, window_checks),
    }


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def recent_checks(website_id: int, limit: int = 25, db_path: str | None = None) -> list[dict]:
    with database.session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, status, http_status_code, response_time_ms, error_message, checked_at, resolved_ip
            FROM monitoring_results
            WHERE website_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (website_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def incident_list(limit: int = 200, website_id: int | None = None,
                  status: str | None = None, db_path: str | None = None) -> list[dict]:
    sql = """
        SELECT i.*, w.name AS website_name, w.url AS website_url
        FROM incidents i
        JOIN websites w ON w.id = i.website_id
        WHERE 1 = 1
    """
    params: list = []
    if website_id:
        sql += " AND i.website_id = ?"
        params.append(website_id)
    if status in ("OPEN", "RESOLVED"):
        sql += " AND i.incident_status = ?"
        params.append(status)
    sql += " ORDER BY COALESCE(i.resolved_at, i.started_at) DESC, i.id DESC LIMIT ?"
    params.append(limit)

    with database.session(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def incident_summary(db_path: str | None = None) -> dict:
    with database.session(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN incident_status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_count,
                   COALESCE(SUM(CASE WHEN incident_status = 'RESOLVED' THEN 1 ELSE 0 END), 0) AS resolved_count,
                   AVG(CASE WHEN incident_status = 'RESOLVED' THEN duration_seconds END) AS avg_duration
            FROM incidents
            """
        ).fetchone()
    total = int(row["total"] or 0)
    average = float(row["avg_duration"]) if row["avg_duration"] is not None else None
    return {
        "total": total,
        "open": int(row["open_count"] or 0),
        "resolved": int(row["resolved_count"] or 0),
        "avg_duration_seconds": int(average) if average is not None else None,
    }


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------

def dashboard_snapshot(db_path: str | None = None, incident_limit: int = 6) -> dict:
    sites = site_overview(db_path)
    settings = database.get_settings(db_path)
    state = monitor.monitor_state()

    counts = defaultdict(int)
    for site in sites:
        counts[site["status"]] += 1

    with database.session(db_path) as conn:
        aggregate = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END), 0) AS up,
                   AVG(CASE WHEN status = 'UP' THEN response_time_ms END) AS avg_ms
            FROM monitoring_results WHERE checked_at >= ?
            """,
            (cutoff(24),),
        ).fetchone()
        open_incidents = conn.execute(
            "SELECT COUNT(*) AS count FROM incidents WHERE incident_status = 'OPEN'"
        ).fetchone()

    total = int(aggregate["total"] or 0)
    up = int(aggregate["up"] or 0)
    avg_ms = aggregate["avg_ms"]

    summary = {
        "total": len(sites),
        "up": counts.get("UP", 0),
        "down": counts.get("DOWN", 0),
        "degraded": counts.get("DEGRADED", 0),
        "unknown": counts.get("UNKNOWN", 0),
        "avg_response_ms": round(avg_ms, 1) if avg_ms is not None else None,
        "uptime_24h": _pct(up, total),
        "checks_24h": total,
        "open_incidents": int(open_incidents["count"] or 0),
    }

    return {
        "summary": summary,
        "sites": sites,
        "recent_incidents": incident_list(limit=incident_limit, db_path=db_path),
        "monitor": {
            "running": state.get("running", False),
            "last_cycle_started": state.get("last_cycle_started"),
            "last_cycle_finished": state.get("last_cycle_finished"),
            "next_cycle_at": state.get("next_cycle_at"),
            "last_error": state.get("last_error"),
            "interval": int(settings["monitoring_interval"]),
            "timeout": int(settings["timeout_seconds"]),
            "fast_threshold": int(settings["fast_threshold"]),
            "moderate_threshold": int(settings["moderate_threshold"]),
        },
        "generated_at": database.utc_now(),
    }


def period_summary(hours: int = 24, db_path: str | None = None) -> dict:
    hours = normalize_hours(hours)
    with database.session(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END), 0) AS up,
                   AVG(CASE WHEN status = 'UP' THEN response_time_ms END) AS avg_ms
            FROM monitoring_results WHERE checked_at >= ?
            """,
            (cutoff(hours),),
        ).fetchone()
        incidents = conn.execute(
            "SELECT COUNT(*) AS count FROM incidents WHERE started_at >= ?", (cutoff(hours),)
        ).fetchone()
        sites = conn.execute("SELECT COUNT(*) AS count FROM websites").fetchone()

    total = int(row["total"] or 0)
    return {
        "hours": hours,
        "checks": total,
        "uptime_pct": _pct(int(row["up"] or 0), total),
        "avg_response_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
        "incidents": int(incidents["count"] or 0),
        "websites": int(sites["count"] or 0),
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_rows(website_id: int | None = None, hours: int = 720,
                db_path: str | None = None) -> list[dict]:
    sql = """
        SELECT w.name AS website_name, w.url AS website_url, r.checked_at,
               r.status, r.http_status_code, r.response_time_ms, r.error_message, r.resolved_ip
        FROM monitoring_results r
        JOIN websites w ON w.id = r.website_id
        WHERE r.checked_at >= ?
    """
    params: list = [cutoff(hours)]
    if website_id is not None:
        sql += " AND r.website_id = ?"
        params.append(website_id)
    sql += " ORDER BY r.checked_at DESC LIMIT ?"
    params.append(config.Config.HISTORY_EXPORT_LIMIT)

    with database.session(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
