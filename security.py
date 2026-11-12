"""Access control, client identity and request rate limiting.

Design goals
------------
* The dashboard is public and read-only by default.
* Management actions (add/edit/delete/check/settings) require either:
  - the ``ADMIN_TOKEN`` session after signing in at ``/admin/login``, or
  - a request coming from the local machine when no token is configured
    (development convenience).
* A small in-memory sliding-window limiter protects all routes and gives
  mutating endpoints a tighter budget.
* Cross-site form posts are rejected using the ``Origin``/``Referer`` headers
  (browsers always send one for cross-site POSTs).
"""

from __future__ import annotations

import hmac
import ipaddress
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from flask import current_app, request, session


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------

def _parse_ip(value: str | None):
    if not value:
        return None
    try:
        return ipaddress.ip_address(str(value).split("%")[0].strip())
    except ValueError:
        return None


def client_ip() -> str:
    """Best-effort client address, honouring the proxy header Render sends."""
    if current_app.config.get("TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        parsed = _parse_ip(first)
        if parsed is not None:
            return str(parsed)
    return request.remote_addr or "unknown"


def is_loopback_ip(value: str | None) -> bool:
    parsed = _parse_ip(value)
    return bool(parsed and parsed.is_loopback)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

def admin_configured() -> bool:
    return bool(str(current_app.config.get("ADMIN_TOKEN") or "").strip())


def verify_admin_token(token: str | None) -> bool:
    expected = str(current_app.config.get("ADMIN_TOKEN") or "")
    if not expected or not token:
        return False
    return hmac.compare_digest(str(token).strip(), expected)


def is_signed_in() -> bool:
    return session.get("pw_admin") is True


def can_manage() -> bool:
    """Whether the current request may perform management operations."""
    if is_signed_in():
        return True
    if admin_configured():
        return False
    return is_loopback_ip(client_ip())


# ---------------------------------------------------------------------------
# Same-origin protection (CSRF defence for browser form posts)
# ---------------------------------------------------------------------------

def same_origin_request() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        origin = request.headers.get("Referer")
    if not origin:
        # Non-browser clients (curl, tests, uptime checkers) do not send these
        # headers; browsers always do for cross-site form submissions.
        return True
    if origin == "null":
        return False
    parsed = urlsplit(origin)
    if not parsed.netloc:
        return True
    return parsed.netloc.lower() == request.host.lower()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiter:
    """Thread-safe, per-key sliding window counter."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            if now - self._last_sweep > 300:
                self._sweep(cutoff)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _sweep(self, cutoff: float) -> None:
        self._last_sweep = time.monotonic()
        for key in list(self._hits):
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                del self._hits[key]
