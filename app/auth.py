"""Local-only login. Drop-in router/guard replacement for the Supabase module.

Create/reset an account on the server: python -m app.auth create-user
Passwords and sessions live in a separate SQLite file, never the stock database.
No provider, email service, internet, signing key or additional packages needed.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

# Load the project's .env as usual, but do not use its Supabase settings.
from . import config

router = APIRouter()
ACCESS_COOKIE = "trailvault_session"


def _cache_control_for(path: str) -> str:
    """Per-path caching policy.

    Photo uploads get date+uuid filenames and their content never changes, so
    the browser can keep them forever — that is what makes the parts table
    fast on a phone over Tailscale. API and HTML responses stay no-store.
    """
    if path.startswith("/media/uploads/") or path.startswith("/api/thumbs/"):
        return "private, max-age=31536000, immutable"
    if path.startswith("/media/exports/"):
        return "private, max-age=3600"
    if path.startswith("/static/"):
        return "public, max-age=3600"
    if path.startswith("/assets/fonts/"):
        return "public, max-age=604800, immutable"
    if path.startswith("/assets/"):
        return "private, max-age=86400"
    return "no-store"

REFRESH_COOKIE = "verifix_refresh_token"  # compatibility; no refresh token is used
_ITERATIONS = 600_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS attempts (
 key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL
);
"""


def database_path() -> Path:
    default = Path(config.DATA_DIR) / "local-auth.db"
    path = Path(os.getenv("LOCAL_AUTH_DB") or default).expanduser().resolve()
    # Never allow an operator's path override to turn this into the stock DB.
    stock_paths = [getattr(config, "DB_PATH", None)]
    stock_url = getattr(config.settings, "database_url", "")
    if stock_url.startswith("sqlite:///"):
        stock_paths.append(stock_url[len("sqlite:///"):])
    elif stock_url.startswith("sqlite://"):
        stock_paths.append(stock_url[len("sqlite://"):])
    if any(stock and path == Path(stock).resolve() for stock in stock_paths):
        raise ValueError("LOCAL_AUTH_DB must not be the inventory database")
    return path


@contextmanager
def _connection(*, create: bool = False):
    path = database_path()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise FileNotFoundError("Create a local account first")
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        if create:
            connection.executescript(_SCHEMA)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if create:
        # POSIX protection; on Windows keep the project under your own account ACL.
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _email(value: str) -> str:
    value = value.strip().casefold()
    if not 3 <= len(value) <= 320 or "@" not in value or value.startswith("@") or value.endswith("@") or any(c.isspace() for c in value):
        raise ValueError("Enter an email address for the local account")
    return value


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str | None) -> bool:
    # Perform the same expensive hash for an unknown account, too.
    salt = bytes(16)
    expected = bytes(32)
    iterations = _ITERATIONS
    if encoded:
        algorithm, count, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(count) != _ITERATIONS:
            raise ValueError("Unsupported local password hash")
        iterations, salt, expected = int(count), bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected) and encoded is not None


def save_user(email: str, password: str, *, reset: bool = False) -> None:
    email = _email(email)
    if not 12 <= len(password) <= 128:
        raise ValueError("Use a unique password of 12–128 characters")
    encoded = _password_hash(password)
    with _connection(create=True) as connection:
        existing = connection.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if reset:
            if existing is None:
                raise ValueError("Local account does not exist")
            connection.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, existing["id"]))
            connection.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
        else:
            if existing is not None:
                raise ValueError("Account already exists; use reset-password")
            connection.execute("INSERT INTO users(email,password_hash) VALUES (?,?)", (email, encoded))
        # A local administrator reset also removes any account login cooldown.
        connection.execute("DELETE FROM attempts WHERE key=?", (_digest("account|" + email),))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hours() -> int:
    hours = int(os.getenv("LOCAL_AUTH_HOURS", "12"))
    if not 1 <= hours <= 168:
        raise ValueError("LOCAL_AUTH_HOURS must be between 1 and 168")
    return hours


def _authenticate(email: str, password: str, address: str) -> str | None:
    now = time.time()
    keys = [(_digest("ip|" + address), 50), (_digest("account|" + email), 10)]
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM attempts WHERE expires_at<=?", (now,))
        for key, limit in keys:
            record = connection.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()
            if record and record["count"] >= limit:
                raise HTTPException(429, "Too many login attempts. Wait 10 minutes and retry.", headers={"Retry-After": "600"})
        for key, _ in keys:
            connection.execute("INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now + 600))
        user = connection.execute("SELECT id,password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not _password_matches(password, user["password_hash"] if user else None):
        return None
    token = secrets.token_urlsafe(32)
    with _connection() as connection:
        # Recheck the hash: a simultaneous password reset must invalidate this login.
        current = connection.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not current or current["password_hash"] != user["password_hash"]:
            return None
        connection.execute("DELETE FROM attempts WHERE key=?", (keys[1][0],))
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        connection.execute("INSERT INTO sessions VALUES (?,?,?)", (_digest(token), user["id"], now + _hours() * 3600))
    return token


def _user(token: str | None) -> dict | None:
    if not token or len(token) > 256 or not database_path().exists():
        return None
    with _connection() as connection:
        row = connection.execute(
            "SELECT users.id,users.email FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?",
            (_digest(token), time.time()),
        ).fetchone()
    return dict(row) if row else None


def _revoke(token: str | None) -> None:
    if token and database_path().exists():
        with _connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))


def clear_cookies(response: Response) -> None:
    for cookie in (ACCESS_COOKIE, "verifix_access_token", REFRESH_COOKIE):
        response.delete_cookie(cookie, path="/", httponly=True, samesite="lax")


def _same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True  # non-browser CLI clients; browser cross-site requests are checked
    parsed = urlsplit(origin)
    return parsed.scheme == request.url.scheme and parsed.netloc.lower() == request.url.netloc.lower()


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


@router.post("/api/auth/signin")
async def signin(payload: Credentials, request: Request) -> Response:
    if not _same_origin(request):
        raise HTTPException(403, "Cross-site login is not allowed")
    try:
        email = _email(payload.email)
    except ValueError:
        raise HTTPException(401, "Email or password is incorrect") from None
    try:
        token = await run_in_threadpool(_authenticate, email, payload.password, request.client.host if request.client else "local")
    except (sqlite3.Error, OSError, ValueError):
        raise HTTPException(503, "Local login unavailable. Create an account with python -m app.auth create-user, or check the local auth database.") from None
    if token is None:
        raise HTTPException(401, "Email or password is incorrect")
    response = JSONResponse({"authenticated": True, "provider": "local"}, headers={"Cache-Control": "no-store"})
    clear_cookies(response)
    response.set_cookie(ACCESS_COOKIE, token, max_age=_hours() * 3600, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
    return response


@router.post("/api/auth/signup")
def signup() -> Response:
    raise HTTPException(403, "Public registration is disabled. Create local accounts on the server with python -m app.auth create-user.")


@router.post("/api/auth/signout")
async def signout(request: Request) -> Response:
    if not _same_origin(request):
        raise HTTPException(403, "Cross-site logout is not allowed")
    await run_in_threadpool(_revoke, request.cookies.get(ACCESS_COOKIE))
    response = JSONResponse({"signed_out": True}, headers={"Cache-Control": "no-store"})
    clear_cookies(response)
    return response


@router.get("/api/auth/session")
def session(request: Request) -> dict:
    user = getattr(request.state, "auth_user", None)
    return {"authenticated": bool(user), "email": user.get("email") if user else None, "provider": "local"}


async def guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
        return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
    path = request.url.path
    public = path in {"/login", "/api/health", "/api/auth/signin", "/api/auth/signout", "/api/auth/signup"} or path.startswith(("/static/", "/assets/fonts/"))
    if public or (request.method == "HEAD" and path == "/"):
        response = await call_next(request)
        response.headers["Cache-Control"] = _cache_control_for(path)
        return response
    try:
        user = await run_in_threadpool(_user, request.cookies.get(ACCESS_COOKIE))
    except (sqlite3.Error, OSError, ValueError):
        return JSONResponse({"detail": "Local session storage unavailable. Check the server terminal."}, status_code=503)
    if not user:
        response = JSONResponse({"detail": "Authentication required"}, status_code=401) if path.startswith("/api/") else RedirectResponse("/login", status_code=303)
        clear_cookies(response)
        response.headers["Cache-Control"] = "no-store"
        return response
    request.state.auth_user = user
    response = await call_next(request)
    response.headers["Cache-Control"] = _cache_control_for(path)
    return response


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_PAGE, headers={"Cache-Control": "no-store"})


_LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Verifix · Local login</title>
<meta name="theme-color" content="#101216">
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;min-height:100svh;display:grid;place-items:center;padding:28px 20px;background:radial-gradient(ellipse at 75% 0,#edc66b0c,transparent 60%),#101216;color:#f1f2ed;font:15px/1.6 system-ui,sans-serif;-webkit-font-smoothing:antialiased}main{width:min(100%,440px);padding:36px;border:1px solid #ffffff18;border-radius:24px;background:#1b1e24;box-shadow:0 24px 80px #0004}.brand{display:flex;align-items:center;gap:12px;margin-bottom:30px}.brand svg{width:42px;height:42px;flex-shrink:0}h1{font-size:29px;font-weight:600;margin:0;letter-spacing:-.06em}h2{font-size:28px;line-height:1.2;font-weight:500;letter-spacing:-.035em;margin:12px 0}p{color:#aeb6c3;font-size:13px}.badge{color:#edc66b;font-size:10px;letter-spacing:.1em}label{display:block;margin:20px 0 7px;font-size:12px;color:#d4d9e1}input,button{width:100%;padding:13px 14px;min-height:48px;border-radius:10px;font:inherit}input{background:#12151a;color:#f1f2ed;border:1px solid #ffffff25}input:hover{border-color:#ffffff45}button{margin-top:24px;background:#edc66b;color:#202015;border:1px solid #edc66b;cursor:pointer;font-weight:600}button:hover{background:#f8d88f}button:disabled{opacity:.5;cursor:wait}:focus-visible{outline:2px solid #edc66b;outline-offset:3px}#status{min-height:22px;color:#edc66b}footer{font-size:11px;color:#99a3b2;border-top:1px solid #ffffff15;padding-top:20px}footer p{font-size:11px;margin-bottom:0}code{overflow-wrap:anywhere;color:#cbd2dd}@media(max-width:440px){main{padding:26px 24px;border-radius:18px}body{padding:20px 14px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
</style></head><body><main><div class="brand"><svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#edc66b"/><g fill="none" stroke="#191a1c" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"><path d="m8 8 8 16L27 6M5 17l5 10"/></g></svg><h1>Verifix</h1></div><div class="badge">YOUR PARTS. YOUR WORKSPACE.</div><h2>Welcome back.</h2><p>Sign in to your parts workspace. Local login, without a cloud connection.</p>
<form id="login"><label for="email">Local account email</label><input id="email" type="email" autocomplete="username" required maxlength="320"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" required maxlength="128"><button id="submit">Sign in locally</button><p id="status" role="status" aria-live="polite"></p></form>
<footer>First use? On the server computer, run:<br><code>python -m app.auth create-user</code><p>Existing Supabase passwords are not migrated. Inventory stays in your configured database.</p></footer></main>
<script>document.querySelector('#login').addEventListener('submit',async(event)=>{event.preventDefault();const button=document.querySelector('#submit'),status=document.querySelector('#status');button.disabled=true;status.textContent='Signing in locally…';try{const response=await fetch('/api/auth/signin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.querySelector('#email').value,password:document.querySelector('#password').value})});const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Unable to sign in. Check your details.');location.replace('/');}catch(error){status.textContent=error.message;}finally{button.disabled=false;}});</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local Verifix login accounts; never modifies inventory.")
    parser.add_argument("command", choices=["create-user", "reset-password"])
    parser.add_argument("--email", help="Local account email (otherwise prompted)")
    args = parser.parse_args()
    try:
        email = args.email or input("Local account email: ")
        password = getpass.getpass("New local password (12–128 characters): ")
        if password != getpass.getpass("Confirm local password: "):
            raise ValueError("Passwords did not match")
        save_user(email, password, reset=args.command == "reset-password")
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Local account not changed: {exc}\n")
    print(f"Local account {'updated' if args.command == 'reset-password' else 'created'}. Login storage: {database_path()}")
    print("Inventory/database connection unchanged. Restart the app if necessary, then sign in.")


if __name__ == "__main__":
    main()
