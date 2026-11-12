from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Importing ``app`` builds the module-level WSGI application. Keep that bootstrap
# from starting a scheduler or touching the developer's database during tests.
os.environ.setdefault("MONITOR_DISABLE_SCHEDULER", "1")
if not os.environ.get("DATABASE_PATH"):
    os.environ["DATABASE_PATH"] = str(
        Path(tempfile.mkdtemp(prefix="pulsewatch-bootstrap-")) / "bootstrap.db"
    )

import config  # noqa: E402
import database  # noqa: E402
import monitor  # noqa: E402
from app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Never touch real DNS from the test suite."""
    monkeypatch.setattr(monitor, "resolve_host", lambda hostname, port=None: ["93.184.216.34"])


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "pulsewatch-test.db")
    monkeypatch.setattr(config.Config, "DATABASE_PATH", path)
    database.init_db(path)
    return path


@pytest.fixture
def flask_app(db_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE_PATH": db_path,
            "ENABLE_SCHEDULER": False,
        }
    )
    yield application


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()
