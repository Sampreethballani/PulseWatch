"""Monitoring engine: real HTTP probes, DNS resolution, TLS inspection.

Networking notes
----------------
* Requests are made with ``requests`` (TCP + TLS handled by the library) and
  redirects are followed manually so every hop can be re-validated.
* The host name is resolved with ``socket.getaddrinfo`` *before* the request
  and every resolved address is checked, which blocks SSRF attempts that use
  host names pointing at private ranges or redirects into the local network.
* TLS certificates are read with a raw ``ssl`` handshake and then verified
  against the CA bundle shipped with ``certifi``.
"""

from __future__ import annotations

import http
import ipaddress
import logging
import os
import socket
import ssl
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import requests
import urllib3

import config
import database

LOG = logging.getLogger(__name__)
_CONFIG = config.Config

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}
REDIRECT_CODES = (301, 302, 303, 307, 308)


class TargetBlocked(ValueError):
    """The URL points at an address that must never be monitored."""


class DNSResolutionError(ValueError):
    """The host name could not be resolved."""


# ---------------------------------------------------------------------------
# URL handling and SSRF protection
# ---------------------------------------------------------------------------

def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


def normalize_url(raw_url: str | None) -> str:
    """Validate and canonicalise a user supplied URL.

    Raises ``ValueError``/``TargetBlocked`` with a user-friendly message.
    """
    if raw_url is None:
        raise ValueError("A website URL is required.")
    candidate = str(raw_url).strip()
    if not candidate:
        raise ValueError("A website URL is required.")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("The URL must not contain spaces.")
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("The URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// addresses can be monitored.")
    if parts.username or parts.password:
        raise ValueError("URLs containing credentials are not allowed.")

    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise ValueError("The URL must include a host name.")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("The URL contains an invalid port.") from exc

    if port is not None and port not in _CONFIG.ALLOWED_PORTS:
        raise ValueError("Only the standard web ports 80 (HTTP) and 443 (HTTPS) can be monitored.")

    lowered = host.lower()
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_SUFFIXES):
        raise TargetBlocked("Local and internal host names cannot be monitored.")

    netloc = _format_host(host) + (f":{port}" if port is not None else "")
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resolve_host(hostname: str, port: int | None = None) -> list[str]:
    """Resolve a host name to every address, IPv4 first."""
    try:
        infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DNSResolutionError(f"DNS resolution failed for {hostname}.") from exc
    except UnicodeError as exc:
        raise DNSResolutionError("The host name contains invalid characters.") from exc

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise DNSResolutionError(f"DNS resolution returned no addresses for {hostname}.")
    addresses.sort(key=lambda value: 0 if ":" not in value else 1)
    return addresses


def assert_public_addresses(addresses: list[str]) -> None:
    """Reject private, loopback, link-local, reserved and metadata addresses."""
    if not addresses:
        raise DNSResolutionError("The host name did not resolve to any address.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError as exc:
            raise TargetBlocked("The host resolved to an invalid address.") from exc
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise TargetBlocked("The target resolves to a private, loopback or reserved address.")
        if str(ip) in METADATA_IPS:
            raise TargetBlocked("Cloud metadata endpoints cannot be monitored.")


def validate_target(url: str, allow_unresolved: bool = False) -> tuple[str, list[str]]:
    """Normalise a URL and validate every address it resolves to."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)

    if _is_ip_literal(host):
        addresses = [host]
        assert_public_addresses(addresses)
        return normalized, addresses

    try:
        addresses = resolve_host(host, port)
    except DNSResolutionError:
        if allow_unresolved:
            return normalized, []
        raise
    assert_public_addresses(addresses)
    return normalized, addresses


# ---------------------------------------------------------------------------
# HTTP availability checks
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    url: str
    status: str
    http_status_code: int | None = None
    response_time_ms: float | None = None
    error_message: str | None = None
    resolved_ip: str | None = None
    final_url: str | None = None
    redirects: int = 0
    detail: str = ""
    checked_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _http_reason(code: int) -> str:
    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return ""


@dataclass
class HTTPProbeResponse:
    """Minimal response surface used by the probe loop."""

    status_code: int
    headers: dict
    url: str


def _verification_context() -> ssl.SSLContext:
    """TLS context matching the client ``requests``/urllib3 uses.

    Some edges (Wikimedia among them) answer the plain stdlib default context
    with a bot-policy 403, so reuse urllib3's own context and trust certifi.
    """
    try:
        from urllib3.util.ssl_ import create_urllib3_context

        context = create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
        context.load_verify_locations(cafile=certifi.where())
        return context
    except Exception:  # pragma: no cover - urllib3/certifi always installed
        return ssl.create_default_context(cafile=certifi.where())


def _pinned_get(url: str, address: str, timeout: float) -> HTTPProbeResponse:
    """HTTP GET that connects to an already validated IP address.

    The host name is still used for the ``Host`` header, TLS SNI and certificate
    verification, so connecting to the validated IP prevents DNS rebinding
    between validation and the actual request. Redirects are never followed here.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    hostname = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    default_port = 443 if scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    headers = {
        "Host": host_header,
        "User-Agent": _CONFIG.REQUEST_USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
    }
    pool_kwargs = {
        "host": address,
        "port": port,
        "timeout": urllib3.Timeout(connect=timeout, read=timeout),
        "retries": False,
    }

    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            **pool_kwargs,
            ssl_context=_verification_context(),
            assert_hostname=hostname,
            server_hostname=hostname,
        )
    else:
        pool = urllib3.HTTPConnectionPool(**pool_kwargs)

    try:
        raw = pool.urlopen("GET", target, headers=headers, redirect=False, preload_content=False)
        try:
            status_code = int(raw.status)
            response_headers = dict(raw.headers)
        finally:
            raw.close()
        return HTTPProbeResponse(status_code=status_code, headers=response_headers, url=url)
    except urllib3.exceptions.TimeoutError as exc:
        raise requests.exceptions.Timeout(str(exc)) from exc
    except urllib3.exceptions.SSLError as exc:
        raise requests.exceptions.SSLError(str(exc)) from exc
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise requests.exceptions.ConnectionError(str(exc)) from exc
    finally:
        pool.close()


def _request(session: requests.Session, url: str, timeout: float, address: str | None = None):
    """Single HTTP GET without automatic redirects (tests patch this).

    ``address`` is the validated IP for this host name; when supplied the
    connection is pinned to it so a second DNS answer cannot redirect the probe
    into a private network.
    """
    if address:
        return _pinned_get(url, address, timeout)
    return session.get(url, timeout=timeout, allow_redirects=False)


def perform_check(url: str, timeout: float | None = None,
                  fast_threshold: int | None = None,
                  moderate_threshold: int | None = None,
                  session: requests.Session | None = None) -> CheckResult:
    """Probe a URL and classify the result as UP / DEGRADED / DOWN."""
    started = time.perf_counter()
    timeout = float(timeout or _CONFIG.DEFAULT_TIMEOUT_SECONDS)
    moderate_threshold = int(
        moderate_threshold if moderate_threshold is not None else _CONFIG.DEFAULT_MODERATE_THRESHOLD
    )
    result = CheckResult(url=str(url), status="DOWN")
    http_session = session or requests.Session()
    owns_session = session is None

    if owns_session:
        http_session.headers.update({
            "User-Agent": _CONFIG.REQUEST_USER_AGENT,
            "Accept": "*/*",
        })

    try:
        current_url, addresses = validate_target(url)
        result.url = current_url
        result.resolved_ip = addresses[0] if addresses else None

        redirects = 0
        status_code: int | None = None
        while True:
            response = _request(http_session, current_url, timeout, addresses[0] if addresses else None)
            status_code = int(response.status_code)
            result.http_status_code = status_code
            result.final_url = getattr(response, "url", None) or current_url

            location = (getattr(response, "headers", None) or {}).get("Location")
            if status_code in REDIRECT_CODES and location:
                if redirects >= _CONFIG.MAX_REDIRECTS:
                    raise requests.exceptions.TooManyRedirects("redirect limit reached")
                next_url, next_addresses = validate_target(urljoin(result.final_url, location))
                redirects += 1
                current_url = next_url
                if result.resolved_ip is None and next_addresses:
                    result.resolved_ip = next_addresses[0]
                continue
            break

        result.redirects = redirects
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.response_time_ms = round(elapsed_ms, 1)

        if status_code is not None and status_code < 400:
            if elapsed_ms > moderate_threshold:
                result.status = "DEGRADED"
                result.detail = (
                    f"Slow response: {result.response_time_ms:.0f} ms "
                    f"(threshold {moderate_threshold} ms)"
                )
            else:
                result.status = "UP"
                result.detail = f"HTTP {status_code} in {result.response_time_ms:.0f} ms"
        else:
            reason = f"{status_code} {_http_reason(status_code)}".strip()
            result.status = "DEGRADED"
            result.detail = f"HTTP {reason}"
            result.error_message = result.detail

    except TargetBlocked as exc:
        result.status = "DOWN"
        result.detail = "Blocked target"
        result.error_message = str(exc)
    except DNSResolutionError as exc:
        result.status = "DOWN"
        result.detail = "DNS resolution failed"
        result.error_message = str(exc)
    except requests.exceptions.Timeout:
        result.status = "DOWN"
        result.detail = "Request timed out"
        result.error_message = f"Request timed out after {timeout:.0f}s."
    except requests.exceptions.SSLError:
        result.status = "DOWN"
        result.detail = "TLS certificate error"
        result.error_message = "The TLS certificate could not be verified."
    except requests.exceptions.TooManyRedirects:
        result.status = "DOWN"
        result.detail = "Too many redirects"
        result.error_message = f"Too many redirects (limit {_CONFIG.MAX_REDIRECTS})."
    except requests.exceptions.ConnectionError:
        result.status = "DOWN"
        result.detail = "Connection failed"
        result.error_message = "Connection failed — the host refused or dropped the request."
    except requests.exceptions.RequestException:
        result.status = "DOWN"
        result.detail = "Request failed"
        result.error_message = "The HTTP request could not be completed."
    except ValueError as exc:
        result.status = "DOWN"
        result.detail = "Invalid target"
        result.error_message = str(exc)
    except Exception:  # pragma: no cover - defensive
        LOG.exception("Unexpected monitoring failure for %s", url)
        result.status = "DOWN"
        result.detail = "Monitoring error"
        result.error_message = "Unexpected error while checking this website."
    finally:
        if owns_session:
            http_session.close()
        if result.response_time_ms is None:
            result.response_time_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if not result.checked_at:
            result.checked_at = database.utc_now()
        if not result.detail:
            result.detail = result.error_message or result.status

    return result


# ---------------------------------------------------------------------------
# DNS lookup
# ---------------------------------------------------------------------------

def lookup_dns(url: str) -> dict:
    """Resolve the host name of a monitored site and return address details."""
    normalized, addresses = validate_target(url)
    host = urlsplit(normalized).hostname or ""
    ipv4 = [address for address in addresses if ":" not in address]
    ipv6 = [address for address in addresses if ":" in address]
    return {
        "hostname": host,
        "addresses": addresses,
        "ipv4": ipv4,
        "ipv6": ipv6,
        "primary_ip": (ipv4 or addresses or [None])[0],
        "checked_at": database.utc_now(),
    }


# ---------------------------------------------------------------------------
# TLS certificate inspection
# ---------------------------------------------------------------------------

_SSL_CACHE: dict[str, tuple[float, dict]] = {}
_SSL_LOCK = threading.Lock()


def _cert_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _name_of(field) -> str | None:
    if not field:
        return None
    for rdn in field:
        for key, value in rdn:
            if key in ("commonName", "organizationName") and value:
                return str(value)
    return None


def decode_der_certificate(der: bytes) -> dict:
    """Decode a DER certificate into a dict using the stdlib getpeercert parser."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    handle, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(handle, "w", encoding="ascii") as file_obj:
            file_obj.write(pem)
        return ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def summarize_certificate(cert: dict, now: datetime | None = None) -> dict:
    """Turn a decoded certificate into display data + expiry classification."""
    now = now or datetime.now(timezone.utc)
    not_before = _cert_time(cert.get("notBefore"))
    not_after = _cert_time(cert.get("notAfter"))

    days_remaining = None
    expiry_status = "unknown"
    if not_after:
        days_remaining = int((not_after - now).total_seconds() // 86400)
        if days_remaining < 0:
            expiry_status = "expired"
        elif days_remaining <= 30:
            expiry_status = "warning"
        else:
            expiry_status = "valid"

    return {
        "not_before": database.format_utc(not_before) if not_before else None,
        "expires_at": database.format_utc(not_after) if not_after else None,
        "days_remaining": days_remaining,
        "expiry_status": expiry_status,
        "issuer": _name_of(cert.get("issuer")),
        "subject": _name_of(cert.get("subject")),
        "serial_number": cert.get("serialNumber"),
        "warning": expiry_status == "warning",
    }


def _verified_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi ships with requests
        return ssl.create_default_context()


def check_ssl(url: str, timeout: float | None = None, use_cache: bool = True) -> dict:
    """Inspect the TLS certificate of an HTTPS website.

    Returns a dict that is always safe to serialise; failures are described in
    ``status``/``error`` instead of raising.
    """
    timeout = float(timeout or _CONFIG.DEFAULT_TIMEOUT_SECONDS)
    result: dict = {
        "applicable": True,
        "status": "error",
        "valid": False,
        "hostname": None,
        "port": 443,
        "resolved_ip": None,
        "error": None,
        "not_before": None,
        "expires_at": None,
        "days_remaining": None,
        "issuer": None,
        "subject": None,
        "serial_number": None,
        "tls_version": None,
        "warning": False,
        "cached": False,
        "checked_at": database.utc_now(),
    }

    try:
        normalized = normalize_url(url)
    except ValueError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    parts = urlsplit(normalized)
    if parts.scheme != "https":
        result.update({
            "applicable": False,
            "status": "not_applicable",
            "error": None,
        })
        return result

    host = parts.hostname or ""
    port = parts.port or 443
    result["hostname"] = host
    result["port"] = port

    cache_key = f"{host}:{port}"
    if use_cache:
        with _SSL_LOCK:
            cached = _SSL_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _CONFIG.SSL_CACHE_TTL:
            snapshot = dict(cached[1])
            snapshot["cached"] = True
            return snapshot

    try:
        _, addresses = validate_target(normalized)
        ip = (addresses or [None])[0]
        result["resolved_ip"] = ip
        if ip is None:
            raise DNSResolutionError("The host name did not resolve to any address.")

        # 1) Unverified handshake: read certificate metadata regardless of validity.
        raw_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        raw_context.check_hostname = False
        raw_context.verify_mode = ssl.CERT_NONE
        der: bytes | None = None
        tls_version = None
        try:
            with socket.create_connection((ip, port), timeout=timeout) as raw_socket:
                with raw_context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
                    der = tls_socket.getpeercert(binary_form=True)
                    tls_version = tls_socket.version()
        except (ssl.SSLError, OSError) as exc:
            result["status"] = "invalid"
            result["error"] = "TLS handshake failed — the server did not present a usable certificate."
            LOG.warning("TLS handshake with %s failed: %s", host, exc)
            return result

        result["tls_version"] = tls_version
        certificate_info: dict = {}
        if der:
            try:
                certificate_info = decode_der_certificate(der)
            except Exception:  # pragma: no cover - parser edge cases
                LOG.exception("Could not decode certificate for %s", host)
        if certificate_info:
            result.update(summarize_certificate(certificate_info))

        # 2) Verified handshake: chain + host name validation.
        try:
            verified_context = _verified_context()
            with socket.create_connection((ip, port), timeout=timeout) as raw_socket:
                with verified_context.wrap_socket(raw_socket, server_hostname=host):
                    pass
        except ssl.SSLCertVerificationError as exc:
            result["valid"] = False
            result["status"] = "expired" if (result.get("days_remaining") or 0) < 0 else "invalid"
            result["error"] = (
                "The certificate has expired." if result["status"] == "expired"
                else "The certificate failed verification."
            )
            LOG.info("Certificate verification failed for %s: %s", host, exc.verify_message)
        except (ssl.SSLError, OSError) as exc:
            result["valid"] = False
            result["status"] = "invalid"
            result["error"] = "The TLS connection could not be verified."
            LOG.info("TLS verification error for %s: %s", host, exc)
        else:
            result["valid"] = True
            if result.get("days_remaining") is not None and result["days_remaining"] < 0:
                result["status"] = "expired"
            elif result.get("warning"):
                result["status"] = "warning"
                result["error"] = f"The certificate expires in {result['days_remaining']} day(s)."
            else:
                result["status"] = "valid"

    except TargetBlocked as exc:
        result["status"] = "invalid"
        result["error"] = str(exc)
    except DNSResolutionError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    except Exception:  # pragma: no cover - defensive
        LOG.exception("SSL inspection failed for %s", url)
        result["status"] = "error"
        result["error"] = "TLS inspection failed."

    result["checked_at"] = database.utc_now()
    if use_cache:
        with _SSL_LOCK:
            _SSL_CACHE[cache_key] = (time.time(), dict(result))
    return result


# ---------------------------------------------------------------------------
# Persistence + incident lifecycle
# ---------------------------------------------------------------------------

def _sync_incidents(conn, website_id: int, result: CheckResult, now_iso: str) -> None:
    open_row = conn.execute(
        """
        SELECT * FROM incidents
        WHERE website_id = ? AND incident_status = 'OPEN'
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (website_id,),
    ).fetchone()

    if result.status == "UP":
        if open_row is not None:
            started = database.parse_utc(open_row["started_at"])
            resolved = database.parse_utc(now_iso)
            duration = None
            if started and resolved:
                duration = max(0, int((resolved - started).total_seconds()))
            conn.execute(
                """
                UPDATE incidents
                SET incident_status = 'RESOLVED', resolved_at = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (now_iso, duration, open_row["id"]),
            )
        return

    description = f"{result.status}: {result.detail or result.error_message or 'Unavailable'}"
    if open_row is None:
        conn.execute(
            """
            INSERT INTO incidents (website_id, incident_status, started_at, description)
            VALUES (?, 'OPEN', ?, ?)
            """,
            (website_id, now_iso, description),
        )
    elif result.status == "DOWN" and (open_row["description"] or "").startswith("DEGRADED"):
        # Escalation: the incident stays open but records the worse condition.
        conn.execute(
            "UPDATE incidents SET description = ? WHERE id = ?",
            (description, open_row["id"]),
        )


def persist_result(website: dict, result: CheckResult, db_path: str | None = None) -> None:
    checked_at = result.checked_at or database.utc_now()
    with database.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO monitoring_results
                (website_id, status, http_status_code, response_time_ms, error_message, checked_at, resolved_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                website["id"],
                result.status,
                result.http_status_code,
                result.response_time_ms,
                result.error_message,
                checked_at,
                result.resolved_ip,
            ),
        )
        conn.execute(
            "UPDATE websites SET current_status = ?, last_checked = ? WHERE id = ?",
            (result.status, checked_at, website["id"]),
        )
        _sync_incidents(conn, website["id"], result, checked_at)


def run_single_check(website_id: int, db_path: str | None = None) -> dict:
    """Check one website synchronously and persist the result."""
    settings = database.get_settings(db_path)
    with database.session(db_path) as conn:
        row = conn.execute("SELECT * FROM websites WHERE id = ?", (website_id,)).fetchone()
    if row is None:
        raise LookupError("Website not found.")
    website = dict(row)
    result = perform_check(
        website["url"],
        timeout=settings["timeout_seconds"],
        fast_threshold=settings["fast_threshold"],
        moderate_threshold=settings["moderate_threshold"],
    )
    persist_result(website, result, db_path)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Monitoring cycle (concurrent, non-overlapping)
# ---------------------------------------------------------------------------

_cycle_lock = threading.Lock()
_state_lock = threading.Lock()
_state: dict = {
    "running": False,
    "last_cycle_started": None,
    "last_cycle_finished": None,
    "next_cycle_at": None,
    "last_summary": None,
    "last_error": None,
}


def monitor_state() -> dict:
    with _state_lock:
        return dict(_state)


def _update_state(**values) -> None:
    with _state_lock:
        _state.update(values)


def set_next_cycle(iso_timestamp: str | None) -> None:
    _update_state(next_cycle_at=iso_timestamp)


def run_monitoring_cycle(db_path: str | None = None) -> dict:
    """Check every registered website once, concurrently.

    A non-blocking lock guarantees cycles never overlap; a second trigger while
    a cycle is running returns a "skipped" summary instead of queueing work.
    """
    if not _cycle_lock.acquire(blocking=False):
        return {
            "skipped": True,
            "reason": "A monitoring cycle is already running.",
            "checked": 0,
            "up": 0,
            "down": 0,
            "degraded": 0,
            "failed": 0,
            "duration_ms": 0,
        }

    started_at = database.utc_now()
    started_perf = time.perf_counter()
    summary = {
        "skipped": False,
        "checked": 0,
        "up": 0,
        "down": 0,
        "degraded": 0,
        "failed": 0,
        "duration_ms": 0,
        "started_at": started_at,
        "finished_at": None,
    }
    _update_state(running=True, last_cycle_started=started_at, last_error=None)

    try:
        settings = database.get_settings(db_path)
        with database.session(db_path) as conn:
            sites = [dict(row) for row in conn.execute("SELECT * FROM websites ORDER BY id").fetchall()]

        if not sites:
            summary["finished_at"] = database.utc_now()
            return summary

        workers = max(1, min(_CONFIG.MAX_WORKERS, len(sites)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            futures = {
                pool.submit(
                    perform_check,
                    site["url"],
                    settings["timeout_seconds"],
                    settings["fast_threshold"],
                    settings["moderate_threshold"],
                ): site
                for site in sites
            }
            for future in as_completed(futures):
                site = futures[future]
                try:
                    result = future.result()
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("Probe crashed for website %s", site["id"])
                    result = CheckResult(
                        url=site["url"],
                        status="DOWN",
                        detail="Monitoring error",
                        error_message="Monitoring task failed unexpectedly.",
                        checked_at=database.utc_now(),
                    )
                try:
                    persist_result(site, result, db_path)
                except Exception:
                    LOG.exception("Could not store monitoring result for website %s", site["id"])
                    summary["failed"] += 1
                    continue
                summary["checked"] += 1
                key = result.status.lower()
                if key in ("up", "down", "degraded"):
                    summary[key] += 1
    except Exception:
        LOG.exception("Monitoring cycle failed")
        summary["error"] = "Monitoring cycle failed."
        _update_state(last_error="Monitoring cycle failed.")
    finally:
        summary["duration_ms"] = round((time.perf_counter() - started_perf) * 1000.0, 1)
        summary["finished_at"] = database.utc_now()
        _update_state(running=False, last_cycle_finished=summary["finished_at"], last_summary=summary)
        _cycle_lock.release()

    return summary
