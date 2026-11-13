from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from .config import settings

log = logging.getLogger(__name__)
router = APIRouter()
ACCESS_COOKIE = "verifix_access_token"
REFRESH_COOKIE = "verifix_refresh_token"


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


def set_cookies(response: Response, session: dict[str, Any], request: Request) -> None:
    shared = {"httponly": True, "secure": _secure(request), "samesite": "lax", "path": "/"}
    response.set_cookie(ACCESS_COOKIE, session["access_token"], max_age=int(session.get("expires_in", 3600)), **shared)
    response.set_cookie(REFRESH_COOKIE, session["refresh_token"], max_age=30 * 24 * 3600, **shared)


def clear_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, samesite="lax")
    response.delete_cookie(REFRESH_COOKIE, path="/", httponly=True, samesite="lax")


@router.post("/api/auth/signup")
async def signup(payload: Credentials, request: Request) -> Response:
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
    if token and settings.supabase_url and settings.supabase_publishable_key:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(_url("logout"), headers=_headers(token))
        except httpx.HTTPError:
            log.warning("Could not revoke Supabase session during sign out")
    response = JSONResponse({"signed_out": True})
    clear_cookies(response)
    return response


@router.get("/api/auth/session")
def session(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "auth_user", None)
    return {"authenticated": bool(user), "email": user.get("email") if user else None}


async def guard(request: Request, call_next):
    path = request.url.path
    public = path == "/login" or path.startswith("/static/") or path in {"/api/health", "/api/auth/signup", "/api/auth/signin", "/api/auth/signout"}
    if public or (request.method == "HEAD" and path in {"/", "/api/health"}):
        return await call_next(request)
    token = request.cookies.get(ACCESS_COOKIE)
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    user = None
    refreshed = None
    if token and settings.supabase_url and settings.supabase_publishable_key:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                result = await client.get(_url("user"), headers=_headers(token))
                if result.is_success:
                    user = result.json()
                elif result.status_code == 401 and refresh_token:
                    result = await client.post(_url("token?grant_type=refresh_token"), headers=_headers(), json={"refresh_token": refresh_token})
                    if result.is_success:
                        refreshed = result.json()
                        user = refreshed.get("user")
        except (httpx.HTTPError, ValueError):
            log.warning("Supabase Auth session check failed", exc_info=True)
    if not user:
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        elif path == "/login":
            return await call_next(request)
        else:
            response = RedirectResponse("/login", status_code=303)
        if token or refresh_token:
            clear_cookies(response)
        return response
    request.state.auth_user = user
    response = await call_next(request)
    if refreshed:
        set_cookies(response, refreshed, request)
    return response
