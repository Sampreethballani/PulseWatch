"""Background monitoring worker.

The scheduler is a daemon thread that wakes up on the interval stored in the
settings table, so changing the interval on the Settings page takes effect on
the next cycle without restarting the application.

It must start exactly once per process. ``start_background_scheduler`` keeps a
process-wide singleton, which is why production runs a single Gunicorn worker
(see README): with several worker processes each one would monitor separately.
"""

from __future__ import annotations

import atexit
import logging
import threading
from datetime import datetime, timedelta, timezone

import database
import monitor

LOG = logging.getLogger(__name__)

MIN_INTERVAL = 5
MAX_INTERVAL = 3600


class MonitorScheduler:
    def __init__(self, db_path: str | None = None, start_delay: float = 2.0) -> None:
        self.db_path = db_path
        self.start_delay = start_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="monitor-scheduler", daemon=True)
        self._thread.start()
        LOG.info("Monitoring scheduler started (database=%s).", self.db_path or "<configured>")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        LOG.info("Monitoring scheduler stopped.")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # -- internals ----------------------------------------------------------
    def interval(self) -> int:
        try:
            settings = database.get_settings(self.db_path)
            value = int(settings["monitoring_interval"])
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Could not read monitoring interval; using 30s.")
            value = 30
        return max(MIN_INTERVAL, min(MAX_INTERVAL, value))

    def _run(self) -> None:
        if self._stop.wait(self.start_delay):
            return
        while not self._stop.is_set():
            try:
                summary = monitor.run_monitoring_cycle(self.db_path)
                if summary.get("skipped"):
                    LOG.info("Cycle skipped: %s", summary.get("reason"))
                else:
                    LOG.info(
                        "Cycle finished: checked=%s up=%s degraded=%s down=%s duration=%sms",
                        summary.get("checked"),
                        summary.get("up"),
                        summary.get("degraded"),
                        summary.get("down"),
                        summary.get("duration_ms"),
                    )
            except Exception:  # pragma: no cover - defensive
                LOG.exception("Unexpected scheduler error.")

            interval = self.interval()
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=interval)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            monitor.set_next_cycle(next_at)
            if self._stop.wait(interval):
                break

        monitor.set_next_cycle(None)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_scheduler_lock = threading.Lock()
_scheduler_instance: MonitorScheduler | None = None


def start_background_scheduler(db_path: str | None = None) -> MonitorScheduler:
    """Start the single scheduler for this process (idempotent)."""
    global _scheduler_instance
    with _scheduler_lock:
        if _scheduler_instance is not None and _scheduler_instance.is_running:
            return _scheduler_instance
        scheduler = MonitorScheduler(db_path)
        scheduler.start()
        _scheduler_instance = scheduler
        atexit.register(_stop_scheduler)
        return scheduler


def stop_background_scheduler() -> None:
    global _scheduler_instance
    with _scheduler_lock:
        if _scheduler_instance is not None:
            _scheduler_instance.stop()
            _scheduler_instance = None


def _stop_scheduler() -> None:  # pragma: no cover - process shutdown
    try:
        stop_background_scheduler()
    except Exception:
        LOG.exception("Could not stop the monitoring scheduler cleanly.")


def scheduler_status() -> dict:
    scheduler = _scheduler_instance
    return {
        "running": bool(scheduler and scheduler.is_running),
        "interval": scheduler.interval() if scheduler else None,
    }
