"""Single-admin auth (Task 8; spec §5 auth, §10.18 web).

argon2id password hashing, a signed (not encrypted) session cookie holding
only the username, an in-memory sliding-window login rate limiter, and the
`require_login` FastAPI dependency every protected route depends on.

Never trusts a proxy header for identity (spec §5 / estate infra §11.4):
this module is the only source of truth for who is logged in.
"""
from __future__ import annotations

import hmac
import logging
from collections import deque
from datetime import UTC, datetime
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from boxbutler.store.db import Store
from boxbutler.web.settings import WebSettings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "bb_session"
SESSION_MAX_AGE_S = 30 * 24 * 60 * 60  # 30 days

_hasher = PasswordHasher()


def hash_password(p: str) -> str:
    return _hasher.hash(p)


def verify_password(hash_: str, p: str) -> bool:
    try:
        return _hasher.verify(hash_, p)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real argon2id hash of a fixed, non-secret value, computed once (not
    per request) and used as the verify target when the submitted username
    doesn't match — so a login attempt against an unknown username still
    pays the same ~200ms argon2 cost as one against a known username with a
    wrong password. Without this, `verify_password` is skipped entirely for
    an unknown username and the resulting latency difference (microseconds
    vs. ~200ms) is a trivial timing oracle for username enumeration, even
    though the HTTP status and message are identical either way.
    """
    return _hasher.hash("bb-timing-safety-dummy-not-a-real-password")


def check_credentials(store: Store, username: str, password: str) -> bool:
    """Verify a login attempt without leaking, via timing, whether the
    username exists.

    `verify_password` (argon2id) runs exactly once regardless of outcome:
    against the real stored hash when the username matches the admin
    account, against `_dummy_hash()` otherwise. The username comparison
    itself uses `hmac.compare_digest` for a constant-time check.
    """
    admin_user = store.settings.get("admin_user")
    admin_hash = store.settings.get("admin_hash")

    username_matches = admin_user is not None and hmac.compare_digest(
        username.encode("utf-8"), admin_user.encode("utf-8")
    )
    target_hash = admin_hash if (username_matches and admin_hash is not None) else _dummy_hash()

    password_ok = verify_password(target_hash, password)
    return username_matches and admin_hash is not None and password_ok


class LoginRateLimiter:
    """In-memory sliding-window limiter over *failed* login attempts.

    Not shared across processes — fine for a single-admin, single-instance
    internal app (spec §5); a multi-worker deployment would need a shared
    store, out of scope for Task 8.

    Final review, Major 2: two problems in the original version.

    1. It charged *every* attempt, successful ones included — 8 correct
       logins in a row produced `[303, 303, ..., 429, 429, 429]`. Only a
       failed attempt should count towards the limit now (`hit()` is
       called by the route only on a bad password), and a successful
       login clears the bucket (`reset()`) so a stray earlier typo can't
       combine with tomorrow's correct password to lock the operator out.
       This keeps the brute-force protection meaningful — genuinely wrong
       passwords still accumulate and still trip the limit — while
       ordinary, all-successful use can never trip it at all.

    2. It used to be keyed on `request.client.host`. Behind this app's
       normal deployment (a containerised reverse proxy in front of it),
       every request's `client.host` is the proxy's own address — so that
       was already a single bucket shared by *every* device on the LAN,
       not per-source protection. And per spec §5 this app must never
       trust a proxy header (`X-Forwarded-For` etc.) for identity, so
       there is no trustworthy per-source key available at all here.
       Given that, the route below keys the bucket on the *submitted
       username* instead: it doesn't pretend to isolate one LAN device
       from another (nothing can, honestly, at this layer), but it does
       throttle guessing *against the admin account* regardless of which
       apparent source IP the proxy reports, and — combined with fix (1)
       above — a shared bucket is safe because normal use never feeds it.
    """

    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = datetime.now(UTC).timestamp()
        hits = self._hits.get(key)
        if hits is None:
            return True
        self._evict(hits, now)
        return len(hits) < self.limit

    def hit(self, key: str) -> None:
        now = datetime.now(UTC).timestamp()
        hits = self._hits.setdefault(key, deque())
        self._evict(hits, now)
        hits.append(now)

    def reset(self, key: str) -> None:
        """Clear a key's bucket — called on a successful login so that a
        prior run of wrong-password attempts can never combine with a
        later correct one to lock the operator out."""
        self._hits.pop(key, None)

    def _evict(self, hits: deque[float], now: float) -> None:
        while hits and now - hits[0] > self.window_s:
            hits.popleft()


def ensure_admin(store: Store, settings: WebSettings) -> None:
    """First start only: store the bootstrap admin_user/admin_hash if absent.

    A no-op when `settings.admin_user`/`admin_password` are both absent
    (Task 36): that's the intentional "empty /data, no env vars" case, and
    it's the setup wizard's job — not this function's — to create the
    admin account, at the end of `/setup`. No-arg deployments (env vars)
    still work exactly as before.

    Important 3 (Task 36 review round 1): that same no-op fires when only
    *one* of the pair is set too — e.g. a typo'd `BOXBUTLER_ADMIN_PASSWORD`
    that never reached the container. Before Task 36 that was a hard
    startup error; now it's silent unless logged, and an operator staring
    at an unexpected setup wizard has no way to tell "I meant to configure
    this" from "this is a fresh install" without one. So: log a warning
    naming exactly which half is missing, whenever exactly one is set.
    """
    have_user = bool(settings.admin_user)
    have_password = bool(settings.admin_password)
    if have_user != have_password:
        present, missing = ("admin_user", "admin_password") if have_user else ("admin_password", "admin_user")
        logger.warning(
            "%s set but %s missing — falling back to the setup wizard.", present, missing
        )
    if store.settings.get("admin_user") is None and have_user and have_password:
        store.settings.set("admin_user", settings.admin_user)
        store.settings.set("admin_hash", hash_password(settings.admin_password))


def _serializer(settings: WebSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="bb_session")


def sign_session(settings: WebSettings, username: str) -> str:
    return _serializer(settings).dumps({"u": username})


def _current_user(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    settings: WebSettings = request.app.state.settings
    try:
        data = _serializer(settings).loads(cookie, max_age=SESSION_MAX_AGE_S)
    except BadSignature:
        return None
    return data.get("u")


def require_login(request: Request) -> str:
    """FastAPI dependency: returns the logged-in username or raises.

    303 -> /login for normal (HTML) requests; 401 when the request is an
    HTMX request (HX-Request header present) or otherwise expects JSON,
    since a redirect is meaningless to an hx-* fetch.
    """
    user = _current_user(request)
    if user is not None:
        return user
    if request.headers.get("HX-Request"):
        raise HTTPException(status_code=401, detail="Login required")
    next_ = request.url.path
    raise HTTPException(status_code=303, headers={"Location": f"/login?next={next_}"})
