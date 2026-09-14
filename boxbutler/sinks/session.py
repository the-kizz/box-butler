"""`Session` — owns the ToniesCloud access token so a sink never issues a
request with one it knows, or should know, is dead (Task 39; spec §3
"Session lifetime — the sink must refresh its own token").

Measured at authentication on 2026-09-11: the access token lives 300 s (5
minutes); the refresh token lives 180 days. `tonie_api.TonieAPI.__init__`
acquires its token once and never refreshes it, and a real run — three
tonies, each an ~80 MB upload followed by a 45-60 s settle poll — comfortably
outlasts five minutes. Nothing above `tonie_api` covered this until now.

**No `tonie_api` import here.** This module owns the token/expiry/refresh
policy only; `authenticate`, `refresh` and `transport` are injected
callables, so every test drives a fake and no test reaches the network.
Task 26 wires a real sink against the real cloud by supplying real
callables that use `tonie_api`/`requests` underneath — nothing in this
module needs to know that.

Contract (spec §3):

- Refresh **proactively** when the token is within `refresh_margin_s`
  (default 60 s) of expiry.
- Refresh **reactively** on a 401: re-authenticate and retry the request
  **once**. A second 401 raises `SessionError` rather than looping.
- A refresh failure is a normal sink failure: `SessionError` propagates to
  the caller (the sink method), which is exactly what lets the existing
  orchestrator machinery (`ClearFailed`/`UploadFailed`, see
  `boxbutler/orchestrator/run.py`) turn it into an abort or a `DEGRADED`
  the same way any other sink failure would.
- 429 and 503 are retried with exponential backoff, honouring `Retry-After`
  when present, bounded by `max_rate_limit_attempts`, and surfaced as
  `RateLimited` — its own class, never confusable with `SessionError` or any
  fetch-layer failure class.
- **Tokens are never logged, never written to a run event, never persisted,
  and never appear in an exception message.** `_AccessToken.__repr__` and
  `AuthResult.__repr__` are redacted below, and every message this module
  raises is a fixed string with no interpolated token, header or response
  body — `run.py` freely does `error=str(e)` and persists it, so that
  invariant has to hold here, not there.

Instance eight of the recurring bug family (an *unknown* state read as a
*definite* one): a refresh result with a missing, non-numeric, non-finite or
non-positive `expires_in`, or a missing token string, is not a valid token —
`_validate_auth_result` rejects all of those rather than installing a token
whose expiry can't actually be trusted.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

REFRESH_MARGIN_S = 60.0
DEFAULT_MAX_RATE_LIMIT_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 60.0

_RATE_LIMIT_STATUSES = (429, 503)


class SessionError(Exception):
    """Authentication failed, a refresh failed, or a request was rejected
    even after a fresh token. Always a fixed, static message — never the
    token, a header, or a response body — because callers (`run.py`) log
    `str(exc)` verbatim into persisted run events.
    """


class RateLimited(Exception):
    """429/503 exhausted the retry budget. A distinct class from
    `SessionError` and from `boxbutler.fetch.protocol.ExtractionBroken` (or
    any other failure class): it means "the cloud asked us to slow down",
    never "the extraction is broken" or "the token is bad".
    """

    def __init__(self, status_code: int, attempts: int) -> None:
        super().__init__(f"rate limited (status {status_code}) after {attempts} attempt(s)")
        self.status_code = status_code
        self.attempts = attempts


@dataclass(frozen=True, repr=False)
class AuthResult:
    """What an injected `authenticate`/`refresh` callable returns.

    `__repr__` is redacted so an accidental `log.debug("%r", result)` in a
    caller can't leak a token — this module never logs it either way, but
    the type itself does not trust callers downstream not to.
    """

    access_token: str
    refresh_token: str
    expires_in: float

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "AuthResult(<redacted>)"


@dataclass(frozen=True, repr=False)
class _AccessToken:
    value: str
    refresh_token: str
    expires_at: datetime

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"_AccessToken(expires_at={self.expires_at.isoformat()})"

    def expires_within(self, margin_s: float, *, now: datetime) -> bool:
        return (self.expires_at - now) <= timedelta(seconds=margin_s)


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> dict: ...


Transport = Callable[..., HttpResponse]


_RETRY_SAFE_SCALAR_TYPES = (bytes, bytearray, str)
_RETRY_SAFE_CONTAINER_TYPES = (Mapping, list, tuple)


def _find_unsafe_path(value: object, path: str, seen: set[int]) -> str | None:
    """Walk `value` looking for a non-retry-safe leaf, to arbitrary depth.
    Returns the offending sub-value's path (e.g. "files[0][1]") or `None`
    if everything underneath is safe.

    `seen` guards against a self-referential container (a list containing
    itself): rather than recursing forever on an input that can never
    actually occur from real caller code, we raise immediately - per this
    project's "no sentinel for an unknown state" rule, a cycle here is a
    caller bug to surface loudly, not a shape to silently tolerate as
    "safe" or "unsafe".
    """
    if value is None or isinstance(value, _RETRY_SAFE_SCALAR_TYPES):
        return None
    if isinstance(value, _RETRY_SAFE_CONTAINER_TYPES):
        value_id = id(value)
        if value_id in seen:
            raise SessionError(f"{path} is a self-referential container: cannot check retry-safety")
        seen = seen | {value_id}
        if isinstance(value, Mapping):
            items = value.items()
        else:
            items = enumerate(value)
        for key, item in items:
            found = _find_unsafe_path(item, f"{path}[{key!r}]", seen)
            if found is not None:
                return found
        return None
    # A file object, a generator, or any other single-use iterator exposes
    # `read` or `__next__` without being one of the safe types above - and
    # `request()` may resend this same object on retry, so it must be
    # rejected here rather than silently truncated or emptied on attempt 2.
    if hasattr(value, "read") or hasattr(value, "__next__"):
        return path
    return None


def _check_retry_safe_kwargs(kwargs: dict) -> None:
    for kwarg in ("data", "json", "files"):
        if kwarg not in kwargs:
            continue
        unsafe_path = _find_unsafe_path(kwargs[kwarg], kwarg, set())
        if unsafe_path is not None:
            # Top-level failures read exactly as before (`'files'`); a
            # nested failure names its position too (`files[0][1]`) so the
            # offending value can be found without ever printing it.
            described = repr(kwarg) if unsafe_path == kwarg else unsafe_path
            raise SessionError(
                f"{described} is not retry-safe: Session.request may retry this "
                "request, so its body must be re-readable. Read it into bytes/"
                "str/a Mapping first, or send the payload outside the session "
                "(as the S3 upload does)."
            )


def _validate_auth_result(result: AuthResult) -> None:
    if not isinstance(result, AuthResult):
        raise SessionError("authentication did not return a usable token")
    if not result.access_token or not result.refresh_token:
        raise SessionError("authentication returned an incomplete token")
    try:
        expires_in = float(result.expires_in)
    except (TypeError, ValueError):
        raise SessionError("authentication returned an unparseable expiry") from None
    # NaN/inf must be rejected explicitly: `float("nan") <= 0` is False, so
    # a bare comparison alone would let a NaN expiry through as "valid".
    if not math.isfinite(expires_in) or expires_in <= 0:
        raise SessionError("authentication returned an invalid expiry") from None


class Session:
    """Owns the access token, its expiry and the refresh grant. A sink
    routes every mutating and read call through `request()` instead of
    calling a transport directly.
    """

    def __init__(
        self,
        authenticate: Callable[[], AuthResult],
        refresh: Callable[[str], AuthResult],
        transport: Transport,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        refresh_margin_s: float = REFRESH_MARGIN_S,
        max_rate_limit_attempts: int = DEFAULT_MAX_RATE_LIMIT_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
    ) -> None:
        self._authenticate = authenticate
        self._refresh = refresh
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._refresh_margin_s = refresh_margin_s
        self._max_rate_limit_attempts = max_rate_limit_attempts
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._token: _AccessToken | None = None
        # Test-visible counters only — never the token itself.
        self.authenticate_count = 0
        self.refresh_count = 0

    # ---------------------------------------------------------- lifecycle

    def _install(self, result: AuthResult) -> None:
        _validate_auth_result(result)
        self._token = _AccessToken(
            value=result.access_token,
            refresh_token=result.refresh_token,
            expires_at=self._clock() + timedelta(seconds=float(result.expires_in)),
        )

    def _authenticate_now(self) -> None:
        self.authenticate_count += 1
        try:
            result = self._authenticate()
        except SessionError:
            raise
        except Exception:  # noqa: BLE001 - never let a raw exception's text (which
            # might embed request/response detail) escape as our message.
            raise SessionError("authentication failed") from None
        self._install(result)

    def _refresh_now(self) -> None:
        if self._token is None:
            self._authenticate_now()
            return
        self.refresh_count += 1
        refresh_token = self._token.refresh_token
        try:
            result = self._refresh(refresh_token)
        except SessionError:
            raise
        except Exception:  # noqa: BLE001 - see _authenticate_now
            raise SessionError("token refresh failed") from None
        self._install(result)

    def ensure_fresh(self) -> None:
        """Proactive refresh: call before using the token for anything.
        `request()` already does this; exposed for callers priming a
        session ahead of the first request (e.g. Task 26's `verify_login`).
        """
        now = self._clock()
        if self._token is None:
            self._authenticate_now()
        elif self._token.expires_within(self._refresh_margin_s, now=now):
            self._refresh_now()

    def bearer_token(self) -> str:
        """Public accessor for the raw token string, for callers (e.g.
        `tonies_cloud._LiveSessionAdapter`) that must rebuild an
        `Authorization` header themselves instead of going through
        `request()`. Ensures freshness first, exactly as `request()` does.

        Raises `SessionError` -- never returns `None` or `""` -- when no
        token is installed and none can be acquired: this project's
        standing rule is no sentinel for an unknown state.
        """
        self.ensure_fresh()
        if self._token is None:
            raise SessionError("no token available")
        return self._token.value

    # ----------------------------------------------------------- requests

    def request(self, method: str, path: str, **kwargs) -> HttpResponse:
        _check_retry_safe_kwargs(kwargs)
        self.ensure_fresh()
        reauthenticated = False
        rate_limit_attempt = 0
        while True:
            response = self._send(method, path, **kwargs)
            status = response.status_code

            if status == 401:
                if reauthenticated:
                    raise SessionError("request rejected after a fresh token")
                reauthenticated = True
                self._refresh_now()
                continue

            if status in _RATE_LIMIT_STATUSES:
                rate_limit_attempt += 1
                if rate_limit_attempt > self._max_rate_limit_attempts:
                    raise RateLimited(status, rate_limit_attempt)
                self._sleep(self._backoff_delay(response, rate_limit_attempt))
                continue

            return response

    def _send(self, method: str, path: str, *, headers: dict | None = None, **kwargs) -> HttpResponse:
        assert self._token is not None
        headers = dict(headers or {})
        headers["Authorization"] = f"Bearer {self._token.value}"
        return self._transport(method, path, headers=headers, **kwargs)

    def _backoff_delay(self, response: HttpResponse, attempt: int) -> float:
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None
            else:
                if math.isfinite(retry_after) and retry_after >= 0:
                    return retry_after
        return min(self._backoff_base_s * (2 ** (attempt - 1)), self._backoff_cap_s)


__all__ = [
    "AuthResult",
    "HttpResponse",
    "RateLimited",
    "Session",
    "SessionError",
    "Transport",
]
