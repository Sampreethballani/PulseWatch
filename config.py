"""Application-wide configuration for PulseWatch.

Every value can be supplied through environment variables so the same code runs
on Windows, Linux, macOS and Render. Nothing here points at a machine-specific
path: the development database lives next to the code, while production sets
``DATABASE_PATH`` to a persistent disk.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"

DEV_SECRET_KEY = "pulsewatch-local-development-key"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _database_path() -> str:
    raw = _env_str("DATABASE_PATH", "MONITOR_DB_PATH")
    if raw:
        return str(Path(raw).expanduser().resolve())
    return str((DEFAULT_DATA_DIR / "monitor.db").resolve())


class Config:
    # --- environment -------------------------------------------------------
    PRODUCTION = (
        _env_bool("RENDER")
        or _env_bool("PULSEWATCH_PRODUCTION")
        or _env_str("FLASK_ENV").lower() == "production"
    )
    TRUST_PROXY = _env_bool("TRUST_PROXY", default=PRODUCTION)

    # --- storage -----------------------------------------------------------
    DATABASE_PATH_EXPLICIT = bool(_env_str("DATABASE_PATH", "MONITOR_DB_PATH"))
    DATABASE_PATH = _database_path()
    PERSISTENT_DB_DIR = _env_str("PULSEWATCH_PERSISTENT_DB_DIR", default="/var/data")
    REQUIRE_PERSISTENT_DB = _env_bool("PULSEWATCH_REQUIRE_PERSISTENT_DB")

    # --- Flask -------------------------------------------------------------
    SECRET_KEY = _env_str("SECRET_KEY", "MONITOR_SECRET_KEY", default=DEV_SECRET_KEY)
    DEV_SECRET_KEY = DEV_SECRET_KEY
    ENABLE_SCHEDULER = not (
        _env_bool("MONITOR_DISABLE_SCHEDULER") or _env_bool("PULSEWATCH_DISABLE_SCHEDULER")
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Defaults to secure cookies in production; override for plain-HTTP smoke tests.
    SESSION_COOKIE_SECURE = _env_bool("PULSEWATCH_SECURE_COOKIES", default=PRODUCTION)
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 days
    MAX_CONTENT_LENGTH = 64 * 1024

    # --- administration ----------------------------------------------------
    # When set, add/edit/delete/check/settings require signing in with this token.
    # When empty, management actions are limited to requests from the local machine
    # (development) and everybody else gets a read-only dashboard.
    ADMIN_TOKEN = _env_str("ADMIN_TOKEN", "PULSEWATCH_ADMIN_TOKEN")

    # --- rate limiting (per client IP) ------------------------------------
    RATE_LIMIT_GENERAL_PER_MINUTE = _env_int("RATE_LIMIT_GENERAL_PER_MINUTE", 240)
    RATE_LIMIT_MUTATIONS_PER_MINUTE = _env_int("RATE_LIMIT_MUTATIONS_PER_MINUTE", 30)
    RATE_LIMIT_LOGIN_PER_MINUTE = _env_int("RATE_LIMIT_LOGIN_PER_MINUTE", 10)

    # --- monitoring defaults (overridable from the Settings page) ----------
    DEFAULT_MONITORING_INTERVAL = 30       # seconds
    DEFAULT_TIMEOUT_SECONDS = 10           # seconds
    DEFAULT_FAST_THRESHOLD = 300           # ms
    DEFAULT_MODERATE_THRESHOLD = 1000      # ms

    # --- network safety ----------------------------------------------------
    MAX_REDIRECTS = 5
    MAX_WORKERS = 8
    ALLOWED_PORTS = {80, 443}
    REQUEST_USER_AGENT = "PulseWatch/1.0 (website availability monitor)"

    # --- misc --------------------------------------------------------------
    SSL_CACHE_TTL = 600                    # seconds
    HISTORY_EXPORT_LIMIT = 25_000

    # --- server binding ----------------------------------------------------
    # Binds all interfaces by default; PORT is provided by Render.
    HOST = _env_str("PULSEWATCH_HOST", "MONITOR_HOST", "HOST", default="0.0.0.0")
    PORT = _env_int("PORT", _env_int("MONITOR_PORT", 5000))
    if not 0 < PORT < 65536:  # ignore PORT=0 / garbage from the environment
        PORT = 5000
    # Backwards-compatible aliases used by the development entry point.
    FLASK_HOST = HOST
    FLASK_PORT = PORT
