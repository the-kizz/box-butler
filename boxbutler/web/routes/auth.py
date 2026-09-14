"""Login, logout and password-change routes (Task 8; spec §5, §10.18).

Deliberately vague on failure (WCAG 2.2 accessible-authentication /
design-system/box-butler/pages/login.md): never distinguishes "no such
user" from "wrong password".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from boxbutler.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    check_credentials,
    hash_password,
    require_login,
    sign_session,
    verify_password,
)
from boxbutler.web.settings import WebSettings

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    templates = _templates(request)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": next or "/"}
    )


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    templates = _templates(request)
    store = request.app.state.store
    settings: WebSettings = request.app.state.settings
    limiter = request.app.state.login_rate_limiter

    # Keyed on the submitted username, not `request.client.host` (final
    # review, Major 2): behind this app's normal reverse-proxy deployment
    # every request's client.host is the proxy's own address — a single
    # bucket shared by the whole LAN either way — and spec §5 forbids
    # trusting a proxy header for identity, so there's no better per-source
    # key available. Keying on the username at least throttles guessing
    # against the admin account specifically. See LoginRateLimiter's
    # docstring for the full rationale.
    key = username.strip().lower() or "unknown"
    if not limiter.allow(key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Try again shortly.", "next": next},
            status_code=429,
        )

    # check_credentials always runs one argon2 verify, win or lose, against
    # either the real stored hash or a dummy of the same parameters — never
    # short-circuited on the username, which would otherwise make response
    # latency a timing oracle for username enumeration (~200ms vs ~microsecs).
    if not check_credentials(store, username, password):
        # Only failed attempts count towards the limit (final review,
        # Major 2) — a run of correct logins must never trip it.
        limiter.hit(key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password.", "next": next},
            status_code=401,
        )

    # A successful login clears this key's failure count, so an earlier
    # mistyped password can never combine with today's correct one to
    # eventually lock the operator out.
    limiter.reset(key)

    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(settings, username),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return response


@router.post("/logout")
def logout(request: Request, user: str = Depends(require_login)):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/settings/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    user: str = Depends(require_login),
):
    store = request.app.state.store
    admin_hash = store.settings.get("admin_hash")
    if admin_hash is None or not verify_password(admin_hash, current):
        return HTMLResponse("Current password is incorrect.", status_code=400)
    if new != confirm or len(new) < 8:
        return HTMLResponse("New password and confirmation must match (min 8 chars).", status_code=400)
    store.settings.set("admin_hash", hash_password(new))
    return RedirectResponse(url="/settings", status_code=303)
