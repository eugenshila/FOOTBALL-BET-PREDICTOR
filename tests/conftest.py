"""Test bootstrap: isolate every run from the real database and media folders.

Environment is set *before* importing the app because ``app.config`` freezes
paths at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="partsid-tests-"))
os.environ.setdefault("APP_DB_PATH", str(_TMP / "test.db"))
os.environ.setdefault("APP_UPLOAD_DIR", str(_TMP / "uploads"))
os.environ.setdefault("APP_EXPORT_DIR", str(_TMP / "exports"))

# Run the whole suite against PostgreSQL by exporting TEST_DATABASE_URL, e.g.
#   TEST_DATABASE_URL=postgresql://postgres@localhost:5432/partsid_test pytest
if os.getenv("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["VISION_PROVIDER"] = "offline"  # never hit a network provider in tests
os.environ["WHATSAPP_NUMBER"] = "+254 700 000 000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402

SEED_DIR = Path(__file__).resolve().parent.parent / "assets" / "seed"


@pytest.fixture(scope="session", autouse=True)
def _fresh_database():
    from app import storage

    storage.reset_schema()          # drop tables so reruns start clean
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield


@pytest.fixture(scope="session")
def dialect() -> str:
    from app import storage

    return storage.dialect_of(storage.resolve_dsn())


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def sample_parts() -> dict[str, Path]:
    return {path.stem: path for path in sorted(SEED_DIR.glob("*.jpg"))}
