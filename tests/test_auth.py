"""Session handling: refresh instead of sign-out, and survive a network blip.

The bug these cover: the access cookie is short lived (Supabase's ``expires_in``,
usually one hour) while the refresh cookie lasts 30 days. When the access cookie
expired the app used to skip the refresh entirely and bounce the user to
``/login``, and any failed call to Supabase (timeout, restart, Wi-Fi drop) also
cleared the session. Both now behave properly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import auth


# ── helpers ───────────────────────────────────────────────────────────────
def _jwt(expires_in: int) -> str:
    """A JWT-shaped token whose ``exp`` is what the guard looks at."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "exp": int(time.time()) + expires_in}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def _request(path: str = "/", cookies: dict[str, str] | None = None) -> Request:
    raw_headers = []
    if cookies:
        raw_headers.append(
            (b"cookie", "; ".join(f"{name}={value}" for name, value in cookies.items()).encode())
        )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    }
    return Request(scope)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Stands in for ``httpx.AsyncClient`` inside ``app.auth``."""

    def __init__(self, user=None, refresh=None, raises: Exception | None = None) -> None:
        self.user = user
        self.refresh = refresh
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):  # httpx.AsyncClient(timeout=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None):
        self.calls.append("GET user")
        self._maybe_raise()
        return self.user

    async def post(self, url, headers=None, json=None):
        self.calls.append("POST refresh")
        self._maybe_raise()
        return self.refresh

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises


def _guard(cookies: dict[str, str], path: str = "/"):
    """Run the real guard middleware; returns (response, responses let through)."""

    async def call_next(request):
        return JSONResponse({"ok": True})

    return asyncio.run(auth.guard(_request(path, cookies), call_next))


@pytest.fixture(autouse=True)
def _supabase_configured(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(auth.settings, "supabase_publishable_key", "sb_publishable_test")


def _session(expires_in: int = 3600) -> dict:
    return {
        "access_token": _jwt(expires_in),
        "refresh_token": "refresh-token",
        "expires_in": expires_in,
        "user": {"id": "u1", "email": "shop@example.com"},
    }


# ── the regression: an expired access cookie must renew, not sign out ─────
def test_expired_access_cookie_refreshes_instead_of_signing_out(monkeypatch):
    fake = FakeClient(refresh=FakeResponse(200, _session()))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.REFRESH_COOKIE: "refresh-token"})

    assert response.status_code == 200
    assert fake.calls == ["POST refresh"]  # no pointless /user call
    set_cookie = " | ".join(response.headers.getlist("set-cookie"))
    assert auth.ACCESS_COOKIE in set_cookie
    assert "max-age=3600" in set_cookie.lower()


def test_stale_access_token_is_refreshed(monkeypatch):
    fake = FakeClient(user=FakeResponse(401), refresh=FakeResponse(200, _session()))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(-60), auth.REFRESH_COOKIE: "refresh-token"})

    assert response.status_code == 200
    assert fake.calls == ["POST refresh"]


def test_valid_access_token_is_verified_and_reused(monkeypatch):
    fake = FakeClient(user=FakeResponse(200, {"id": "u1", "email": "shop@example.com"}))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(3000), auth.REFRESH_COOKIE: "refresh-token"})

    assert response.status_code == 200
    assert fake.calls == ["GET user"]  # refresh token untouched


def test_expired_access_token_without_refresh_cookie_asks_for_sign_in(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(-60)})

    assert response.status_code == 303
    assert response.headers["location"] == "/login?expired=1"
    assert fake.calls == []


# ── a network blip must not end the session ───────────────────────────────
def test_unreachable_supabase_keeps_the_session(monkeypatch):
    fake = FakeClient(raises=httpx.ConnectTimeout("no route to host"))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(3000), auth.REFRESH_COOKIE: "refresh-token"})

    assert response.status_code == 503
    # crucially: nothing is cleared, so the next attempt still has a session
    assert not response.headers.getlist("set-cookie")


def test_unreachable_supabase_returns_json_for_the_api(monkeypatch):
    fake = FakeClient(raises=httpx.ReadTimeout("slow"))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(3000)}, path="/api/parts")

    assert response.status_code == 503
    assert "sign-in service" in json.loads(response.body)["detail"]


def test_server_error_from_supabase_is_not_a_sign_out(monkeypatch):
    fake = FakeClient(user=FakeResponse(502), refresh=FakeResponse(502))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(3000), auth.REFRESH_COOKIE: "refresh-token"})

    assert response.status_code == 503


# ── a genuinely invalid session still signs out ───────────────────────────
def test_invalid_refresh_token_signs_out(monkeypatch):
    fake = FakeClient(user=FakeResponse(401), refresh=FakeResponse(400, {"error": "invalid_grant"}))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.ACCESS_COOKIE: _jwt(-60), auth.REFRESH_COOKIE: "stale"})

    assert response.status_code == 303
    assert response.headers["location"] == "/login?expired=1"
    cleared = " | ".join(response.headers.getlist("set-cookie"))
    assert auth.ACCESS_COOKIE in cleared and auth.REFRESH_COOKIE in cleared
    assert "max-age=0" in cleared.lower()


def test_api_gets_json_401_when_unauthenticated(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({}, path="/api/parts")

    assert response.status_code == 401


# ── public routes and misconfiguration ────────────────────────────────────
def test_public_routes_skip_the_session_check(monkeypatch):
    fake = FakeClient(raises=AssertionError("must not be called"))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    assert _guard({}, path="/login").status_code == 200
    assert _guard({}, path="/api/health").status_code == 200
    assert _guard({}, path="/static/app.js").status_code == 200


def test_unconfigured_supabase_says_so_instead_of_looping(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "")

    response = _guard({auth.ACCESS_COOKIE: _jwt(3000)})

    assert response.status_code == 503
    assert "not configured" in response.body.decode()
    assert "SUPABASE_PUBLISHABLE_KEY" in response.body.decode()


def test_samesite_none_is_used_for_embedded_previews(monkeypatch):
    """Lax cookies are dropped in a cross-site iframe; strict browsers need None+Secure."""
    monkeypatch.setattr(auth.settings, "auth_cookie_samesite", "none")
    fake = FakeClient(refresh=FakeResponse(200, _session()))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.REFRESH_COOKIE: "refresh-token"})

    set_cookie = " | ".join(response.headers.getlist("set-cookie")).lower()
    assert "samesite=none" in set_cookie
    assert "secure" in set_cookie


def test_default_cookie_policy_is_lax(monkeypatch):
    monkeypatch.setattr(auth.settings, "auth_cookie_samesite", "lax")
    fake = FakeClient(refresh=FakeResponse(200, _session()))
    monkeypatch.setattr(auth.httpx, "AsyncClient", fake)

    response = _guard({auth.REFRESH_COOKIE: "refresh-token"})

    set_cookie = " | ".join(response.headers.getlist("set-cookie")).lower()
    assert "samesite=lax" in set_cookie
    assert "secure" not in set_cookie


def test_jwt_expiry_parser_tolerates_junk():
    assert auth._jwt_expired("not-a-jwt") is False
    assert auth._jwt_expired("a.b.c") is False
    assert auth._jwt_expired(_jwt(-10)) is True
    assert auth._jwt_expired(_jwt(10)) is False
