from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from .config import settings

log = logging.getLogger(__name__)
router = APIRouter()
AUTH_OFF_ALIASES = {"off", "none", "disabled", "false", "open", "no"}
AUTH_SUPABASE_ALIASES = {"supabase", "on", "true", "yes", "auth"}
LOCAL_OPERATOR = {"id": "local", "email": "local@localhost", "local": True}
ACCESS_COOKIE = "verifix_access_token"
REFRESH_COOKIE = "verifix_refresh_token"
REFRESH_MAX_AGE = 30 * 24 * 3600
AUTH_TIMEOUT = 8


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("Enter a valid email address")
        return value


def auth_mode() -> str:
    """``off`` or ``supabase``. Unknown values fall back to the safe one."""
    raw = (settings.auth_mode or "").strip().lower()
    if raw in AUTH_OFF_ALIASES:
        return "off"
    return "supabase" if raw in AUTH_SUPABASE_ALIASES or not raw else "supabase"


def auth_off() -> bool:
    return auth_mode() == "off"


def _url(path: str) -> str:
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(status_code=503, detail="Supabase Auth is not configured")
    return f"{settings.supabase_url}/auth/v1/{path.lstrip('/')}"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"apikey": settings.supabase_publishable_key, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _cookie_policy(request: Request) -> dict[str, Any]:
    """SameSite/Secure for the session cookies.

    ``AUTH_COOKIE_SAMESITE=none`` is for apps embedded in a cross-site iframe
    (preview panes, portals). Browsers drop Lax cookies in that context, which
    looks exactly like being signed out on every click.
    """
    samesite = settings.auth_cookie_samesite
    if samesite not in {"lax", "none", "strict"}:
        samesite = "lax"
    return {"httponly": True, "secure": _secure(request) or samesite == "none", "samesite": samesite, "path": "/"}


def set_cookies(response: Response, session: dict[str, Any], request: Request) -> None:
    """Store the session. The refresh cookie outlives the access cookie by design."""
    shared = _cookie_policy(request)
    expires_in = int(session.get("expires_in") or 3600)
    response.set_cookie(ACCESS_COOKIE, session["access_token"], max_age=max(expires_in, 60), **shared)
    if session.get("refresh_token"):
        response.set_cookie(REFRESH_COOKIE, session["refresh_token"], max_age=REFRESH_MAX_AGE, **shared)


def clear_cookies(response: Response, request: Request | None = None) -> None:
    samesite = settings.auth_cookie_samesite
    if samesite not in {"lax", "none", "strict"}:
        samesite = "lax"
    # A SameSite=None cookie can only be replaced by another Secure one.
    secure = bool(request and _secure(request)) or samesite == "none"
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/", httponly=True, samesite=samesite, secure=secure)


def _refuse_when_off() -> None:
    if auth_off():
        raise HTTPException(
            status_code=409,
            detail="Sign-in is disabled (AUTH_MODE=off). Set AUTH_MODE=supabase in .env to require an account.",
        )


@router.post("/api/auth/signup")
async def signup(payload: Credentials, request: Request) -> Response:
    _refuse_when_off()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            result = await client.post(_url("signup"), headers=_headers(), json={"email": payload.email.strip(), "password": payload.password})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach Supabase Auth") from exc
    if not result.is_success:
        body = result.json()
        raise HTTPException(status_code=result.status_code, detail=body.get("msg") or body.get("message") or "Sign up failed")
    session = result.json()
    has_session = bool(session.get("access_token") and session.get("refresh_token"))
    response = JSONResponse({"authenticated": has_session, "confirmation_required": not has_session})
    if has_session:
        set_cookies(response, session, request)
    return response


@router.post("/api/auth/signin")
async def signin(payload: Credentials, request: Request) -> Response:
    _refuse_when_off()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            result = await client.post(_url("token?grant_type=password"), headers=_headers(), json={"email": str(payload.email), "password": payload.password})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach Supabase Auth") from exc
    if not result.is_success:
        body = result.json()
        raise HTTPException(status_code=result.status_code, detail=body.get("msg") or body.get("message") or "Email or password is incorrect")
    response = JSONResponse({"authenticated": True})
    set_cookies(response, result.json(), request)
    return response


@router.post("/api/auth/signout")
async def signout(request: Request) -> Response:
    token = request.cookies.get(ACCESS_COOKIE)
    if token and not auth_off() and settings.supabase_url and settings.supabase_publishable_key:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(_url("logout"), headers=_headers(token))
        except httpx.HTTPError:
            log.warning("Could not revoke Supabase session during sign out")
    response = JSONResponse({"signed_out": True})
    clear_cookies(response, request)
    return response


@router.get("/api/auth/session")
def session(request: Request) -> dict[str, Any]:
    if auth_off():
        return {"authenticated": True, "email": None, "mode": "off"}
    user = getattr(request.state, "auth_user", None)
    return {"authenticated": bool(user), "email": user.get("email") if user else None, "mode": "supabase"}


# ── session resolution ─────────────────────────────────────────────────────
# Three outcomes, because "no valid token" and "auth server is down" must not be
# treated the same: the second one used to log people out mid-shift.
AUTHENTICATED = "authenticated"
UNAUTHENTICATED = "unauthenticated"
UNREACHABLE = "unreachable"


def _jwt_expired(token: str) -> bool:
    """True when the token's own ``exp`` is already in the past.

    Only used to skip a pointless verification round trip; a token is never
    trusted because of this - every unexpired token is still verified with
    Supabase.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        expires_at = int(claims["exp"])
    except (ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
        return False
    return expires_at <= time.time()


async def _verify_token(client: httpx.AsyncClient, token: str) -> tuple[str, dict[str, Any] | None]:
    try:
        result = await client.get(_url("user"), headers=_headers(token))
    except (httpx.HTTPError, ValueError):
        log.warning("Supabase Auth unreachable while checking the session", exc_info=True)
        return UNREACHABLE, None
    if result.is_success:
        return AUTHENTICATED, result.json()
    if result.status_code in (401, 403):
        return UNAUTHENTICATED, None
    log.warning("Supabase Auth returned %s while checking the session", result.status_code)
    return UNREACHABLE, None


async def _refresh_session(client: httpx.AsyncClient, refresh_token: str) -> tuple[str, dict[str, Any] | None]:
    try:
        result = await client.post(
            _url("token?grant_type=refresh_token"),
            headers=_headers(),
            json={"refresh_token": refresh_token},
        )
    except (httpx.HTTPError, ValueError):
        log.warning("Supabase Auth unreachable while refreshing the session", exc_info=True)
        return UNREACHABLE, None
    if result.is_success:
        session = result.json()
        if not session.get("user") and session.get("access_token"):
            # Some projects return the new tokens only; ask who they belong to.
            status, user = await _verify_token(client, session["access_token"])
            if status != AUTHENTICATED:
                return status, None
            session["user"] = user
        return AUTHENTICATED, session
    if result.status_code in (400, 401, 403):
        return UNAUTHENTICATED, None
    log.warning("Supabase Auth returned %s while refreshing the session", result.status_code)
    return UNREACHABLE, None


async def resolve_session(
    access_token: str | None, refresh_token: str | None
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(status, user, refreshed_session)``.

    The access cookie is short lived (Supabase's ``expires_in``, usually an
    hour). When it is gone or stale but a refresh cookie is present, the session
    is renewed instead of dropped - that is what keeps people signed in for the
    life of the 30-day refresh cookie.
    """
    if not auth_configured():
        return UNREACHABLE, None, None

    async with httpx.AsyncClient(timeout=AUTH_TIMEOUT) as client:
        status = UNAUTHENTICATED
        user: dict[str, Any] | None = None

        if access_token and not _jwt_expired(access_token):
            status, user = await _verify_token(client, access_token)
            if status == AUTHENTICATED:
                return AUTHENTICATED, user, None
            if status == UNREACHABLE:
                return UNREACHABLE, None, None

        if refresh_token:
            refreshed_status, session = await _refresh_session(client, refresh_token)
            if refreshed_status == AUTHENTICATED and session is not None:
                return AUTHENTICATED, session.get("user"), session
            if refreshed_status == UNREACHABLE:
                return UNREACHABLE, None, None

        return UNAUTHENTICATED, None, None


def auth_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_publishable_key)


def _service_unavailable(request: Request, *, configured: bool = True) -> Response:
    """Explain the problem without destroying a session that may still be good."""
    if configured:
        detail = "Cannot reach the sign-in service right now. Try again in a moment."
        title = "Sign-in service unreachable"
        body = (
            "<p>Your session is still saved - this is a temporary network problem, "
            "not a sign-out.</p><p><a href=\"\">Try again</a></p>"
        )
        headers = {"Retry-After": "15"}
    else:
        detail = (
            "Supabase Auth is not configured. Set SUPABASE_URL and "
            "SUPABASE_PUBLISHABLE_KEY in .env (see README)."
        )
        title = "Sign-in is not configured"
        body = (
            "<p>Set <code>SUPABASE_URL</code> and <code>SUPABASE_PUBLISHABLE_KEY</code> "
            "in <code>.env</code>, then restart the app. Copying "
            "<code>.env.example</code> gives working values.</p>"
        )
        headers = {}
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail}, status_code=503, headers=headers)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Temporarily unavailable</title>"
        "<body style=\"font-family:system-ui;padding:2rem;max-width:40rem\">"
        f"<h1>{title}</h1>{body}</body>",
        status_code=503,
        headers=headers,
    )


async def guard(request: Request, call_next):
    path = request.url.path
    if auth_off():
        # Single-machine mode: no cookies, no Supabase round trip, no redirects.
        request.state.auth_user = LOCAL_OPERATOR
        return await call_next(request)
    public = (
        path == "/login"
        or path.startswith("/static/")
        or path in {"/api/health", "/api/auth/signup", "/api/auth/signin", "/api/auth/signout"}
    )
    if public or (request.method == "HEAD" and path in {"/", "/api/health"}):
        return await call_next(request)

    token = request.cookies.get(ACCESS_COOKIE)
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not token and not refresh_token:
        status, user, refreshed = UNAUTHENTICATED, None, None
    else:
        status, user, refreshed = await resolve_session(token, refresh_token)

    if status == UNREACHABLE:
        return _service_unavailable(request, configured=auth_configured())

    if status != AUTHENTICATED or user is None:
        if path.startswith("/api/"):
            response: Response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        else:
            response = RedirectResponse("/login?expired=1", status_code=303)
        if token or refresh_token:
            clear_cookies(response, request)
        return response

    request.state.auth_user = user
    response = await call_next(request)
    if refreshed:
        set_cookies(response, refreshed, request)
    return response
