"""SQLite access helpers.

The monitoring worker runs outside a Flask request context, so every function
opens its own short-lived connection. WAL mode keeps readers from blocking the
background writer.

The database location always comes from configuration (``DATABASE_PATH`` in
production). At startup the parent directory is created and write-tested so a
hosting mistake fails loudly instead of quietly serving an empty database.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import config

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """Current UTC time in the canonical storage format."""
    return datetime.now(timezone.utc).strftime(UTC_FORMAT)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(UTC_FORMAT)


def parse_utc(value: str | None) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(str(value), UTC_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_path(db_path: str | None = None) -> Path:
    """Absolute path of the SQLite file for this process."""
    return Path(db_path or config.Config.DATABASE_PATH).expanduser().resolve()


def is_persistent_path(db_path: str | None = None) -> bool:
    """True when the file lives inside the configured persistent disk."""
    persistent_root = Path(config.Config.PERSISTENT_DB_DIR).expanduser()
    try:
        persistent_root = persistent_root.resolve()
    except OSError:  # pragma: no cover - unusual filesystem
        return False
    return persistent_root in resolve_path(db_path).parents


def ensure_database_ready(
    db_path: str | None = None,
    *,
    production: bool | None = None,
    require_persistent: bool | None = None,
) -> Path:
    """Validate the database location, creating its directory if needed.

    Raises ``RuntimeError`` with an actionable message when the location cannot
    be used, so production never silently falls back to ephemeral storage.
    """
    path = resolve_path(db_path)
    production = config.Config.PRODUCTION if production is None else production
    require_persistent = (
        config.Config.REQUIRE_PERSISTENT_DB if require_persistent is None else require_persistent
    )

    if production and not config.Config.DATABASE_PATH_EXPLICIT:
        raise RuntimeError(
            "DATABASE_PATH is not set. In production, configure DATABASE_PATH to the "
            "persistent disk location (for example /var/data/monitor.db) before starting "
            "the application, so monitoring history survives restarts."
        )

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"The database directory '{parent}' could not be created: {exc}. "
            "Check that the persistent disk is mounted and writable."
        ) from exc

    probe = parent / f".pulsewatch-write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"The database directory '{parent}' is not writable: {exc}. "
            "Check the mount path and permissions of the persistent disk."
        ) from exc

    if production and require_persistent and not is_persistent_path(str(path)):
        raise RuntimeError(
            f"DATABASE_PATH '{path}' is not inside the persistent disk "
            f"'{config.Config.PERSISTENT_DB_DIR}'. Set PULSEWATCH_PERSISTENT_DB_DIR if the "
            "mount point differs, or disable PULSEWATCH_REQUIRE_PERSISTENT_DB for a "
            "deliberately ephemeral deployment."
        )
    return path


def health(db_path: str | None = None) -> dict:
    """Small, non-sensitive database health payload for /healthz."""
    path = resolve_path(db_path)
    directory = path.parent
    result = {
        "ok": False,
        "writable": False,
        "persistent": is_persistent_path(str(path)),
    }
    try:
        result["writable"] = os.access(directory, os.W_OK)
        with session(db_path) as connection:
            connection.execute("SELECT 1").fetchone()
        result["ok"] = True
    except Exception:  # pragma: no cover - defensive
        result["ok"] = False
    return result


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(db_path: str | None = None) -> None:
    """Create tables/indexes and seed the single settings row."""
    ensure_database_ready(db_path)
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    with session(db_path) as connection:
        connection.executescript(schema)
        connection.execute(
            """
            INSERT INTO settings (id, monitoring_interval, timeout_seconds, fast_threshold, moderate_threshold)
            SELECT 1, ?, ?, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM settings WHERE id = 1)
            """,
            (
                config.Config.DEFAULT_MONITORING_INTERVAL,
                config.Config.DEFAULT_TIMEOUT_SECONDS,
                config.Config.DEFAULT_FAST_THRESHOLD,
                config.Config.DEFAULT_MODERATE_THRESHOLD,
            ),
        )


def get_settings(db_path: str | None = None) -> dict:
    with session(db_path) as connection:
        row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO settings (id, monitoring_interval, timeout_seconds, fast_threshold, moderate_threshold) VALUES (1, ?, ?, ?, ?)",
                (
                    config.Config.DEFAULT_MONITORING_INTERVAL,
                    config.Config.DEFAULT_TIMEOUT_SECONDS,
                    config.Config.DEFAULT_FAST_THRESHOLD,
                    config.Config.DEFAULT_MODERATE_THRESHOLD,
                ),
            )
            return {
                "id": 1,
                "monitoring_interval": config.Config.DEFAULT_MONITORING_INTERVAL,
                "timeout_seconds": config.Config.DEFAULT_TIMEOUT_SECONDS,
                "fast_threshold": config.Config.DEFAULT_FAST_THRESHOLD,
                "moderate_threshold": config.Config.DEFAULT_MODERATE_THRESHOLD,
            }
        return dict(row)


def save_settings(monitoring_interval: int, timeout_seconds: int,
                  fast_threshold: int, moderate_threshold: int,
                  db_path: str | None = None) -> None:
    with session(db_path) as connection:
        connection.execute(
            """
            INSERT INTO settings (id, monitoring_interval, timeout_seconds, fast_threshold, moderate_threshold)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                monitoring_interval = excluded.monitoring_interval,
                timeout_seconds = excluded.timeout_seconds,
                fast_threshold = excluded.fast_threshold,
                moderate_threshold = excluded.moderate_threshold
            """,
            (monitoring_interval, timeout_seconds, fast_threshold, moderate_threshold),
        )
