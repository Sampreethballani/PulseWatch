"""PulseWatch — website availability & performance monitoring.

Run with:  python app.py
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

import analytics
import config
import database
import monitor
import security
from scheduler import scheduler_status, start_background_scheduler

LOG = logging.getLogger("pulsewatch")

SAMPLE_WEBSITES = [
    ("Google", "https://www.google.com"),
    ("GitHub", "https://www.github.com"),
    ("Wikipedia", "https://www.wikipedia.org"),
    ("Python.org", "https://www.python.org"),
    ("Microsoft", "https://www.microsoft.com"),
]

STATUS_CATEGORIES = {"UP": "success", "DOWN": "danger", "DEGRADED": "warning", "UNKNOWN": "secondary"}

# Endpoints that change state or expose configuration. Public visitors can read
# every dashboard page and API, but only an administrator can call these.
ADMIN_ENDPOINTS = {
    "add_website",
    "add_sample_websites",
    "edit_website",
    "delete_website",
    "check_now",
    "check_one",
    "settings_page",
    "admin_logout",
}


def _configure_logging() -> None:
    """Configure stdout logging once per process (Gunicorn captures it)."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _validate_production_config(app: Flask) -> None:
    """Fail fast instead of running publicly with development defaults."""
    if not app.config.get("PRODUCTION"):
        return
    if app.config.get("SECRET_KEY") == config.Config.DEV_SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY must be set to a long random value in production. "
            "Render Blueprints generate one automatically; set it manually otherwise."
        )
    if not app.config.get("DATABASE_PATH_EXPLICIT"):
        raise RuntimeError(
            "DATABASE_PATH must be set in production (Render free: data/monitor.db; "
            "with a persistent disk: /var/data/monitor.db)."
        )


def _too_many_requests():
    if request.path.startswith("/api/"):
        response = jsonify({"error": "Too many requests. Please slow down."})
        response.status_code = 429
    else:
        response = Response(
            render_template(
                "error.html",
                code=429,
                message="Too many requests. Please try again shortly.",
                active="",
            ),
            status=429,
            mimetype="text/html",
        )
    response.headers["Retry-After"] = "60"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_next(target: str | None, fallback: str) -> str:
    """Only allow same-site relative redirect targets."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _duration_text(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _utc_label(value: str | None) -> str:
    parsed = database.parse_utc(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%d %b %Y, %H:%M UTC")


def _insert_website(name: str, raw_url: str, db_path: str | None = None) -> tuple[bool, str]:
    """Validate and store a website. Returns (ok, message)."""
    name = (name or "").strip()[:80]
    try:
        normalized = monitor.normalize_url(raw_url)
    except (ValueError, monitor.TargetBlocked) as exc:
        return False, str(exc)

    # Best effort SSRF validation: a host that does not resolve yet is allowed
    # (it will simply be reported DOWN), but any private/internal resolution is
    # refused.
    warning = ""
    try:
        _, addresses = monitor.validate_target(normalized, allow_unresolved=True)
        if not addresses:
            warning = " The host name does not resolve yet — it will be retried on every check."
    except (ValueError, monitor.TargetBlocked) as exc:
        return False, str(exc)

    if not name:
        name = normalized.split("://", 1)[-1].split("/", 1)[0]

    try:
        with database.session(db_path) as conn:
            conn.execute(
                "INSERT INTO websites (name, url, created_at, current_status) VALUES (?, ?, ?, 'UNKNOWN')",
                (name, normalized, database.utc_now()),
            )
    except sqlite3.IntegrityError:
        return False, "That website is already being monitored."

    return True, f"{name} was added.{warning}"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config.Config)
    if overrides:
        app.config.update(overrides)

    app.config.setdefault("ENABLE_SCHEDULER", config.Config.ENABLE_SCHEDULER)
    app.config.setdefault("DATABASE_PATH", config.Config.DATABASE_PATH)

    _configure_logging()
    _validate_production_config(app)

    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=app.config.get("PERMANENT_SESSION_LIFETIME", 60 * 60 * 24 * 7),
    )

    db_path = app.config["DATABASE_PATH"]
    database.init_db(db_path)
    LOG.info(
        "Database ready at %s (persistent=%s, production=%s)",
        database.resolve_path(db_path),
        database.is_persistent_path(db_path),
        bool(app.config.get("PRODUCTION")),
    )

    # -- Jinja helpers ------------------------------------------------------
    app.jinja_env.filters["utc_dt"] = _utc_label
    app.jinja_env.filters["duration"] = _duration_text

    @app.context_processor
    def inject_globals():
        try:
            settings = database.get_settings(db_path)
            state = monitor.monitor_state()
            open_incidents = analytics.incident_summary(db_path)["open"]
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Could not build template context")
            settings = database.get_settings(db_path) if db_path else {}
            state = monitor.monitor_state()
            open_incidents = 0
        return {
            "settings": settings,
            "monitor_state": state,
            "open_incident_count": open_incidents,
            "status_categories": STATUS_CATEGORIES,
            "can_manage": security.can_manage(),
            "admin_configured": security.admin_configured(),
            "is_admin": security.is_signed_in(),
        }

    # -- request protection -------------------------------------------------
    app.extensions["rate_general"] = security.SlidingWindowRateLimiter(
        int(app.config["RATE_LIMIT_GENERAL_PER_MINUTE"]), 60
    )
    app.extensions["rate_mutations"] = security.SlidingWindowRateLimiter(
        int(app.config["RATE_LIMIT_MUTATIONS_PER_MINUTE"]), 60
    )
    app.extensions["rate_login"] = security.SlidingWindowRateLimiter(
        int(app.config["RATE_LIMIT_LOGIN_PER_MINUTE"]), 60
    )

    @app.before_request
    def _protect_request():
        if request.endpoint in (None, "static") or request.path.startswith("/static/"):
            return None

        identity = security.client_ip()
        if not app.extensions["rate_general"].allow(identity):
            return _too_many_requests()

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not security.same_origin_request():
                abort(403)
            if not app.extensions["rate_mutations"].allow(identity):
                return _too_many_requests()

        if request.endpoint in ADMIN_ENDPOINTS and not security.can_manage():
            if request.method == "GET":
                return redirect(url_for("admin_login", next=request.full_path.rstrip("?")))
            abort(403)
        return None

    # -- lifecycle ----------------------------------------------------------
    if app.config.get("ENABLE_SCHEDULER") and not app.config.get("TESTING"):
        start_background_scheduler(db_path)
    app.extensions["scheduler"] = scheduler_status()
    LOG.info(
        "Scheduler %s; admin token %s",
        "started" if app.extensions["scheduler"].get("running") else "not started",
        "configured" if str(app.config.get("ADMIN_TOKEN") or "").strip() else "not configured",
    )

    # -- administration ------------------------------------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not security.admin_configured():
            flash(
                "Remote management is disabled. Set ADMIN_TOKEN on the server to enable sign-in.",
                "info",
            )
            return redirect(url_for("dashboard"))
        if security.is_signed_in():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            if not app.extensions["rate_login"].allow(security.client_ip()):
                return _too_many_requests()
            if security.verify_admin_token(request.form.get("token", "")):
                session.clear()
                session["pw_admin"] = True
                session.permanent = True
                flash("Signed in. You can now manage monitored websites.", "success")
                return redirect(_safe_next(request.form.get("next"), url_for("dashboard")))
            flash("That access token is not valid.", "danger")
        return render_template("admin_login.html", active="")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("pw_admin", None)
        flash("Signed out of administration.", "info")
        return redirect(url_for("dashboard"))

    # -- pages --------------------------------------------------------------
    @app.get("/")
    def dashboard():
        snapshot = analytics.dashboard_snapshot(db_path)
        return render_template("dashboard.html", snapshot=snapshot, active="dashboard")

    @app.get("/websites")
    def websites_page():
        sites = analytics.site_overview(db_path)
        return render_template("websites.html", sites=sites, active="websites")

    @app.get("/websites/<int:website_id>")
    def website_details(website_id: int):
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        site = analytics.site_detail(website_id, db_path)
        if site is None:
            abort(404)
        return render_template(
            "website_details.html",
            site=site,
            hours=hours,
            checks=analytics.recent_checks(website_id, 20, db_path),
            incidents=analytics.incident_list(limit=10, website_id=website_id, db_path=db_path),
            active="websites",
        )

    @app.get("/incidents")
    def incidents_page():
        website_id = request.args.get("website_id", type=int)
        status = (request.args.get("status") or "").upper()
        incidents = analytics.incident_list(
            limit=250, website_id=website_id, status=status if status in ("OPEN", "RESOLVED") else None,
            db_path=db_path,
        )
        return render_template(
            "incidents.html",
            incidents=incidents,
            summary=analytics.incident_summary(db_path),
            sites=analytics.site_overview(db_path),
            filters={"website_id": website_id, "status": status},
            active="incidents",
        )

    @app.get("/analytics")
    def analytics_page():
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        data = analytics.comparison(hours, db_path)
        return render_template(
            "analytics.html",
            comparison=data,
            summary=analytics.period_summary(hours, db_path),
            hours=hours,
            active="analytics",
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        if request.method == "POST":
            errors = []

            def read_int(field: str, label: str, minimum: int, maximum: int) -> int | None:
                raw = (request.form.get(field) or "").strip()
                try:
                    value = int(raw)
                except ValueError:
                    errors.append(f"{label} must be a whole number.")
                    return None
                if not minimum <= value <= maximum:
                    errors.append(f"{label} must be between {minimum} and {maximum}.")
                    return None
                return value

            interval = read_int("monitoring_interval", "Monitoring interval", 5, 3600)
            timeout = read_int("timeout_seconds", "Request timeout", 1, 60)
            fast = read_int("fast_threshold", "Fast threshold", 50, 5000)
            moderate = read_int("moderate_threshold", "Moderate threshold", 100, 30000)
            if fast is not None and moderate is not None and fast >= moderate:
                errors.append("The fast threshold must be smaller than the moderate threshold.")

            if errors:
                for message in errors:
                    flash(message, "danger")
            else:
                database.save_settings(interval, timeout, fast, moderate, db_path)
                flash("Settings saved. New values apply from the next monitoring cycle.", "success")
            return redirect(url_for("settings_page"))

        return render_template(
            "settings.html",
            settings=database.get_settings(db_path),
            db_path=db_path,
            active="settings",
        )

    # -- website management --------------------------------------------------
    @app.post("/websites/add")
    def add_website():
        ok, message = _insert_website(request.form.get("name", ""), request.form.get("url", ""), db_path)
        flash(message, "success" if ok else "danger")
        return redirect(_safe_next(request.form.get("next"), url_for("websites_page")))

    @app.post("/websites/samples")
    def add_sample_websites():
        added = 0
        for name, url in SAMPLE_WEBSITES:
            ok, _ = _insert_website(name, url, db_path)
            if ok:
                added += 1
        skipped = len(SAMPLE_WEBSITES) - added
        if added:
            flash(f"Added {added} sample website(s)." + (f" {skipped} already existed." if skipped else ""), "success")
        else:
            flash("All sample websites are already being monitored.", "info")
        return redirect(url_for("websites_page"))

    @app.post("/websites/<int:website_id>/edit")
    def edit_website(website_id: int):
        site = analytics.site_detail(website_id, db_path)
        if site is None:
            abort(404)

        name = (request.form.get("name") or "").strip()[:80]
        try:
            normalized = monitor.normalize_url(request.form.get("url", ""))
        except (ValueError, monitor.TargetBlocked) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("websites_page"))

        try:
            _, addresses = monitor.validate_target(normalized, allow_unresolved=True)
            if not addresses:
                flash("Website updated. The host name does not resolve yet — it will be retried.", "warning")
        except (ValueError, monitor.TargetBlocked) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("websites_page"))

        try:
            with database.session(db_path) as conn:
                conn.execute(
                    "UPDATE websites SET name = ?, url = ? WHERE id = ?",
                    (name or normalized.split("://", 1)[-1].split("/", 1)[0], normalized, website_id),
                )
        except sqlite3.IntegrityError:
            flash("Another monitored website already uses that URL.", "danger")
            return redirect(url_for("websites_page"))

        flash("Website updated.", "success")
        if site["url"] != normalized:
            flash("The URL changed — monitoring continues with the new address.", "info")
        return redirect(url_for("websites_page"))

    @app.post("/websites/<int:website_id>/delete")
    def delete_website(website_id: int):
        with database.session(db_path) as conn:
            result = conn.execute("DELETE FROM websites WHERE id = ?", (website_id,))
        if result.rowcount:
            flash("Website removed together with its monitoring history.", "success")
        else:
            flash("Website not found.", "warning")
        return redirect(_safe_next(request.form.get("next"), url_for("websites_page")))

    # -- monitoring triggers -------------------------------------------------
    @app.post("/check-now")
    def check_now():
        state = monitor.monitor_state()
        if state.get("running"):
            flash("A monitoring cycle is already running.", "info")
        else:
            thread = threading.Thread(target=monitor.run_monitoring_cycle, args=(db_path,), daemon=True)
            thread.start()
            flash("Monitoring cycle started for all websites.", "info")
        return redirect(_safe_next(request.form.get("next"), url_for("dashboard")))

    @app.post("/websites/<int:website_id>/check")
    def check_one(website_id: int):
        site = analytics.site_detail(website_id, db_path)
        if site is None:
            abort(404)
        try:
            result = monitor.run_single_check(website_id, db_path)
        except Exception:
            LOG.exception("Manual check failed for website %s", website_id)
            flash("The check could not be completed. Please try again.", "danger")
        else:
            category = STATUS_CATEGORIES.get(result["status"], "secondary")
            extra = result["detail"] or result["error_message"] or ""
            flash(f"{site['name']}: {result['status']} — {extra}", category)
        return redirect(_safe_next(request.form.get("next"), url_for("website_details", website_id=website_id)))

    # -- JSON API ------------------------------------------------------------
    @app.get("/api/dashboard")
    def api_dashboard():
        return jsonify(analytics.dashboard_snapshot(db_path))

    @app.get("/api/status")
    def api_status():
        return jsonify({
            "monitor": monitor.monitor_state(),
            "settings": database.get_settings(db_path),
            "generated_at": database.utc_now(),
        })

    @app.get("/api/analytics/comparison")
    def api_comparison():
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        return jsonify(analytics.comparison(hours, db_path))

    @app.get("/api/analytics/summary")
    def api_period_summary():
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        return jsonify(analytics.period_summary(hours, db_path))

    @app.get("/api/websites/<int:website_id>/history")
    def api_history(website_id: int):
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        data = analytics.response_series(website_id, hours, db_path=db_path)
        if not data:
            return jsonify({"error": "Website not found."}), 404
        return jsonify(data)

    @app.get("/api/websites/<int:website_id>/timeline")
    def api_timeline(website_id: int):
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        if analytics.site_detail(website_id, db_path) is None:
            return jsonify({"error": "Website not found."}), 404
        return jsonify(analytics.timeline(website_id, hours, db_path))

    @app.get("/api/websites/<int:website_id>/dns")
    def api_dns(website_id: int):
        site = analytics.site_detail(website_id, db_path)
        if site is None:
            return jsonify({"error": "Website not found."}), 404
        try:
            return jsonify(monitor.lookup_dns(site["url"]))
        except monitor.TargetBlocked as exc:
            return jsonify({"error": str(exc)}), 400
        except monitor.DNSResolutionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception:
            LOG.exception("DNS lookup failed for %s", site["url"])
            return jsonify({"error": "DNS lookup failed."}), 500

    @app.get("/api/websites/<int:website_id>/ssl")
    def api_ssl(website_id: int):
        site = analytics.site_detail(website_id, db_path)
        if site is None:
            return jsonify({"error": "Website not found."}), 404
        refresh = request.args.get("refresh") == "1"
        settings = database.get_settings(db_path)
        try:
            return jsonify(monitor.check_ssl(site["url"], settings["timeout_seconds"], use_cache=not refresh))
        except monitor.TargetBlocked as exc:
            return jsonify({"error": str(exc), "status": "invalid", "applicable": True}), 400
        except Exception:
            LOG.exception("TLS inspection failed for %s", site["url"])
            return jsonify({"error": "TLS inspection failed.", "status": "error", "applicable": True}), 500

    # -- CSV export ----------------------------------------------------------
    @app.get("/export/csv")
    def export_csv():
        website_id = request.args.get("website_id", type=int)
        hours = analytics.normalize_hours(request.args.get("hours", type=int))
        rows = analytics.export_rows(website_id, hours, db_path)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "Website name", "Website URL", "Timestamp (UTC)", "Availability status",
            "HTTP status code", "Response time (ms)", "Error message", "Resolved IP",
        ])
        for row in rows:
            writer.writerow([
                row["website_name"],
                row["website_url"],
                row["checked_at"],
                row["status"],
                "" if row["http_status_code"] is None else row["http_status_code"],
                "" if row["response_time_ms"] is None else row["response_time_ms"],
                row["error_message"] or "",
                row["resolved_ip"] or "",
            ])

        filename = f"pulsewatch-monitoring-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.csv"
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # -- errors --------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found."}), 404
        return render_template("error.html", code=404, message="That page does not exist.", active=""), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Method not allowed."}), 405
        return render_template("error.html", code=405, message="That action is not allowed.", active=""), 405

    @app.errorhandler(403)
    def forbidden(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "You do not have permission to do that."}), 403
        return (
            render_template(
                "error.html",
                code=403,
                message="You do not have permission to perform that action.",
                active="",
            ),
            403,
        )

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover - defensive
        LOG.exception("Unhandled application error")
        if request.path.startswith("/api/"):
            return jsonify({"error": "Something went wrong."}), 500
        return render_template("error.html", code=500, message="Something went wrong on our side.", active=""), 500

    @app.after_request
    def secure_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'",
        )
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if app.config.get("PRODUCTION") and request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/api/") or request.path.startswith("/export/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz():
        db_health = database.health(db_path)
        state = monitor.monitor_state()
        scheduler_running = bool(app.extensions.get("scheduler", {}).get("running"))
        payload = {
            "status": "ok" if db_health["ok"] else "degraded",
            "time": database.utc_now(),
            "environment": "production" if app.config.get("PRODUCTION") else "development",
            "request_scheme": request.scheme,
            "database": db_health,
            "scheduler": {
                "running": bool(state.get("running")) or scheduler_running,
                "last_cycle_finished": state.get("last_cycle_finished"),
                "next_cycle_at": state.get("next_cycle_at"),
            },
            "admin_configured": security.admin_configured(),
        }
        return jsonify(payload), (200 if db_health["ok"] else 503)

    return app


# Module-level WSGI application for Gunicorn/Waitress and the Flask CLI
# (`app:app`). Creating it here starts exactly one monitoring scheduler per
# process; production therefore runs a single Gunicorn worker.
app = create_app()


if __name__ == "__main__":
    print(
        f"\n  PulseWatch running at http://{config.Config.HOST}:{config.Config.PORT}"
        "  (Ctrl+C to stop)\n"
    )
    app.run(
        host=config.Config.HOST,
        port=config.Config.PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
