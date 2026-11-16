"""Local authentication must work without a provider or changes to stock storage."""
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth

EMAIL = "owner@example.test"
PASSWORD = "local-test-password-123"


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / "local-auth.db"
    monkeypatch.setenv("LOCAL_AUTH_DB", str(path))
    monkeypatch.setenv("LOCAL_AUTH_HOURS", "12")
    def network_forbidden(*args, **kwargs):
        pytest.fail("Local authentication attempted a network call")
    monkeypatch.setattr(httpx, "AsyncClient", network_forbidden)
    auth.save_user(EMAIL, PASSWORD)
    app = FastAPI()
    app.middleware("http")(auth.guard)
    app.include_router(auth.router)
    @app.get("/api/parts")
    def parts(request: Request):
        return {"email": request.state.auth_user["email"]}
    @app.post("/api/parts")
    def create_part():
        return {"created": True}
    @app.get("/")
    def index():
        return {"local": True}
    return TestClient(app), path, app


def signin(client, password=PASSWORD, **kwargs):
    return client.post("/api/auth/signin", json={"email": EMAIL, "password": password}, **kwargs)


def test_local_signin_and_session(local):
    client, _, _ = local
    response = signin(client)
    assert response.status_code == 200 and response.json()["provider"] == "local"
    cookie = response.headers.get_list("set-cookie")[-1]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=43200" in cookie
    assert "; Secure" not in cookie
    assert client.get("/api/auth/session").json() == {"authenticated": True, "email": EMAIL, "provider": "local"}
    assert client.get("/api/parts").json()["email"] == EMAIL


def test_session_survives_new_app_client(local):
    client, _, app = local
    signin(client)
    token = client.cookies.get(auth.ACCESS_COOKIE)
    restarted = TestClient(app)
    restarted.cookies.set(auth.ACCESS_COOKIE, token)
    assert restarted.get("/api/parts").status_code == 200


def test_signout_revokes_server_session(local):
    client, _, _ = local
    signin(client)
    token = client.cookies.get(auth.ACCESS_COOKIE)
    assert client.post("/api/auth/signout").status_code == 200
    client.cookies.clear()
    client.cookies.set(auth.ACCESS_COOKIE, token)
    assert client.get("/api/parts").status_code == 401


def test_expired_session_is_denied(local):
    client, path, _ = local
    signin(client)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert client.get("/api/parts").status_code == 401


def test_missing_and_forged_sessions_are_denied(local):
    client, _, _ = local
    assert client.get("/api/parts").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    client.cookies.set(auth.ACCESS_COOKIE, "forged-token")
    assert client.get("/api/parts").status_code == 401


def test_old_supabase_cookie_does_not_grant_access(local):
    client, _, _ = local
    client.cookies.set("verifix_access_token", "old-provider-token")
    client.cookies.set("verifix_refresh_token", "old-provider-refresh")
    assert client.get("/api/parts").status_code == 401


def test_no_public_signup(local):
    client, _, _ = local
    assert client.post("/api/auth/signup", json={"email": "attacker@example.test", "password": PASSWORD}).status_code == 403


def test_password_reset_revokes_existing_sessions(local):
    client, _, _ = local
    signin(client)
    auth.save_user(EMAIL, "a-new-local-password-123", reset=True)
    assert client.get("/api/parts").status_code == 401
    assert signin(client).status_code == 401
    assert signin(client, "a-new-local-password-123").status_code == 200


def test_passwords_and_raw_session_tokens_not_stored(local):
    client, path, _ = local
    signin(client)
    token = client.cookies.get(auth.ACCESS_COOKIE)
    raw = path.read_bytes()
    assert PASSWORD.encode() not in raw and token.encode() not in raw
    with sqlite3.connect(path) as connection:
        password_hash = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        token_hash = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert password_hash.startswith("pbkdf2_sha256$600000$")
    assert len(token_hash) == 64 and token_hash != token


def test_password_policy_and_duplicate_account(local):
    with pytest.raises(ValueError, match="12"):
        auth.save_user("other@example.test", "too-short")
    with pytest.raises(ValueError, match="already exists"):
        auth.save_user(EMAIL.upper(), PASSWORD)


def test_wrong_password_generic_error_and_rate_limit(local):
    client, _, _ = local
    for _ in range(10):
        response = signin(client, "wrong-password")
        assert response.status_code == 401 and response.json()["detail"] == "Email or password is incorrect"
    response = signin(client)
    assert response.status_code == 429 and response.headers["retry-after"] == "600"


def test_cross_origin_requests_rejected(local):
    client, _, _ = local
    assert signin(client, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert signin(client, headers={"Origin": "null"}).status_code == 403
    assert signin(client, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/parts", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/parts", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/parts", headers={"Origin": "http://testserver"}).status_code == 200


def test_https_cookie_secure(local):
    _, _, app = local
    client = TestClient(app, base_url="https://testserver")
    response = signin(client)
    assert "; Secure" in response.headers.get_list("set-cookie")[-1]


def test_missing_store_fails_closed(local, monkeypatch, tmp_path):
    client, _, _ = local
    monkeypatch.setenv("LOCAL_AUTH_DB", str(tmp_path / "missing.db"))
    assert signin(client).status_code == 503
    assert client.get("/api/parts").status_code == 401


def test_corrupt_store_fails_closed(local):
    client, path, _ = local
    path.write_text("not a sqlite file")
    client.cookies.set(auth.ACCESS_COOKIE, "some-token")
    assert client.get("/api/parts").status_code == 503
    assert signin(client).status_code == 503


def test_cannot_use_stock_database_as_auth_store(local, monkeypatch, tmp_path):
    path = tmp_path / "inventory.db"
    path.write_bytes(b"inventory unchanged")
    monkeypatch.setattr(auth.config, "DB_PATH", path)
    monkeypatch.setenv("LOCAL_AUTH_DB", str(path))
    with pytest.raises(ValueError, match="must not"):
        auth.save_user("new@example.test", PASSWORD)
    assert path.read_bytes() == b"inventory unchanged"


def test_login_page_has_no_external_dependencies(local):
    client, _, _ = local
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in locally" in response.text
    assert "https://" not in response.text


def test_installer_backs_up_only_auth_and_preserves_stock(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("local_installer_builder", root / "scripts/build_local_auth_installer.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "OUTPUT", tmp_path / "download")
    output = builder.build()
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    original = b"router = object()\nasync def guard(request, call_next): pass\n"
    (project / "app/auth.py").write_bytes(original)
    (project / "app/main.py").write_text("# existing Windows app")
    (project / "app/config.py").write_text("# existing settings")
    (project / ".env").write_text("DATABASE_URL=unchanged")
    (project / "data").mkdir()
    (project / "data/inventory.db").write_bytes(b"stock unchanged")
    result = subprocess.run([sys.executable, str(output / "install_local_auth.py"), "--project", str(project)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (project / "app/auth.py").read_bytes() == (root / "app/auth.py").read_bytes()
    assert next((project / "app").glob("auth.before-local-*.bak")).read_bytes() == original
    assert (project / ".env").read_text() == "DATABASE_URL=unchanged"
    assert (project / "data/inventory.db").read_bytes() == b"stock unchanged"
    assert (project / "app/main.py").read_text() == "# existing Windows app"


def test_explicit_sqlite_inventory_url_also_protected(local, monkeypatch, tmp_path):
    path = tmp_path / "different-stock.db"
    path.write_bytes(b"stock unchanged")
    monkeypatch.setattr(auth.config.settings, "database_url", "sqlite:///" + str(path))
    monkeypatch.setenv("LOCAL_AUTH_DB", str(path))
    with pytest.raises(ValueError, match="must not"):
        auth.save_user("new@example.test", PASSWORD)
    assert path.read_bytes() == b"stock unchanged"
