"""Regression tests for the user's separately supplied Supabase auth module.

The replacement is a downloadable patch, NOT wired into this checkout's app,
which does not have the user's Supabase configuration or login middleware.
All provider traffic is mocked; no credentials or live Supabase are used.
"""
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture
def repaired(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://auth.example.test", raising=False)
    monkeypatch.setattr(settings, "supabase_publishable_key", "test-public-key", raising=False)
    path = Path(__file__).resolve().parents[1] / "docs/patches/auth_fixed.py"
    spec = importlib.util.spec_from_file_location("app._tested_auth_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client_for(module, monkeypatch, handler, *, https=False):
    original = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    app = FastAPI()
    app.middleware("http")(module.guard)
    app.include_router(module.router)

    @app.get("/api/config")
    def config(request: Request):
        return {"user_id": request.state.auth_user["id"]}

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return JSONResponse({"protected": True})

    return TestClient(app, base_url="https://testserver" if https else "http://testserver")


def refreshed():
    return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 60, "user": {"id": "test-user", "email": "user@example.test"}})


def test_refresh_without_access_cookie(repaired, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.params["grant_type"] == "refresh_token"
        return refreshed()
    client = client_for(repaired, monkeypatch, handler)
    client.cookies.set(repaired.REFRESH_COOKIE, "old-refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 200 and response.json()["user_id"] == "test-user"
    assert len(calls) == 1
    assert client.cookies.get(repaired.ACCESS_COOKIE) == "new-access"
    assert client.cookies.get(repaired.REFRESH_COOKIE) == "new-refresh"
    assert response.headers["cache-control"] == "no-store"
    cookies = response.headers.get_list("set-cookie")
    assert all("HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie for cookie in cookies)
    assert all("; Secure" not in cookie for cookie in cookies)


def test_invalid_access_token_can_refresh(repaired, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(401) if request.method == "GET" else refreshed()
    client = client_for(repaired, monkeypatch, handler)
    client.cookies.set(repaired.ACCESS_COOKIE, "expired-access", domain="testserver.local")
    client.cookies.set(repaired.REFRESH_COOKIE, "refresh", domain="testserver.local")
    assert client.get("/api/auth/session").json()["authenticated"] is True
    assert calls == ["GET", "POST"]


def test_valid_access_not_refreshed(repaired, monkeypatch):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "test-user"})
    client = client_for(repaired, monkeypatch, handler)
    client.cookies.set(repaired.ACCESS_COOKIE, "valid-access", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 200 and "set-cookie" not in response.headers


@pytest.mark.parametrize("access", [True, False])
def test_network_timeout_preserves_cookies(repaired, monkeypatch, access):
    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)
    client = client_for(repaired, monkeypatch, handler)
    if access:
        client.cookies.set(repaired.ACCESS_COOKIE, "access", domain="testserver.local")
    client.cookies.set(repaired.REFRESH_COOKIE, "refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert response.headers["retry-after"] == "5"
    assert client.cookies.get(repaired.REFRESH_COOKIE) == "refresh"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_refresh_provider_failure_preserves_cookies(repaired, monkeypatch, status):
    client = client_for(repaired, monkeypatch, lambda request: httpx.Response(status))
    client.cookies.set(repaired.REFRESH_COOKIE, "refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 503 and "set-cookie" not in response.headers


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_refresh_requires_login(repaired, monkeypatch, status):
    client = client_for(repaired, monkeypatch, lambda request: httpx.Response(status))
    client.cookies.set(repaired.REFRESH_COOKIE, "bad-refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 401
    assert all("Max-Age=0" in cookie for cookie in response.headers.get_list("set-cookie"))


def test_missing_cookies_remain_unauthorized(repaired, monkeypatch):
    def handler(request):
        pytest.fail("Anonymous requests must not call Supabase")
    client = client_for(repaired, monkeypatch, handler)
    assert client.get("/api/config").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/health").status_code == 200


def test_https_refresh_sets_secure_cookies(repaired, monkeypatch):
    client = client_for(repaired, monkeypatch, lambda request: refreshed(), https=True)
    client.cookies.set(repaired.REFRESH_COOKIE, "refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 200
    assert all("; Secure" in cookie for cookie in response.headers.get_list("set-cookie"))


def test_invalid_access_without_refresh_is_denied(repaired, monkeypatch):
    client = client_for(repaired, monkeypatch, lambda request: httpx.Response(401))
    client.cookies.set(repaired.ACCESS_COOKIE, "invalid", domain="testserver.local")
    assert client.get("/api/config").status_code == 401


def test_malformed_refresh_does_not_grant_access_or_destroy_session(repaired, monkeypatch):
    client = client_for(repaired, monkeypatch, lambda request: httpx.Response(200, json={"user": {"id": "test-user"}}))
    client.cookies.set(repaired.REFRESH_COOKIE, "refresh", domain="testserver.local")
    response = client.get("/api/config")
    assert response.status_code == 503 and "set-cookie" not in response.headers
