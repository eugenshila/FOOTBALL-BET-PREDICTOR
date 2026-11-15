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

# The app defaults to PostgreSQL; the suite runs on SQLite unless a server is
# offered explicitly, so `pytest` needs no database server installed.
#   TEST_DATABASE_URL=postgresql://postgres@localhost:5432/partsid_test pytest
if os.getenv("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.setdefault("DB_ENGINE", "sqlite")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["VISION_PROVIDER"] = "offline"  # never hit a network provider in tests
os.environ["WHATSAPP_NUMBER"] = "+254 700 000 000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app.config import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


# The app sits behind Supabase Auth, which needs the network. Tests drive the
# API as an already-signed-in user, so the session check is replaced here and
# nothing else in the auth layer changes.
async def _assume_signed_in(request, call_next):
    request.state.auth_user = {"email": "tests@example.com", "id": "test-user"}
    return await call_next(request)


app.user_middleware = [
    Middleware(BaseHTTPMiddleware, dispatch=_assume_signed_in)
    if middleware.kwargs.get("dispatch") is auth.guard
    else middleware
    for middleware in app.user_middleware
]

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
