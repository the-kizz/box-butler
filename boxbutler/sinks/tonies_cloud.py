"""`ToniesCloudSink` — `SinkProtocol` over `tonie_api.TonieAPI` (Task 26;
spec §3 "`ToniesCloudSink` reads real limits from `/config`", §3.4 measured
limits, §2 step 8, §10.5).

This is the first module in the project that can talk to the real Tonies
cloud. Everything before Task 26 runs against fakes (`FakeSink`); until
this module exists, no `--apply` path may be exposed (Task 27).

`ToniesCloudSink` itself stays duck-typed over `api` exactly as the brief's
tests inject it (`FakeApi`) — no test in this module reaches the network,
tonie_api's real session, or a real tonie. The *only* pieces here that ever
touch a wire are `from_env` and the private `_LiveSessionAdapter` /
`_authenticate` / `_refresh` helpers it wires together, used solely by the
composition root (Task 29) once real credentials exist.

Why `TonieAPI` cannot just authenticate itself
-----------------------------------------------
The measured access token lives 300 s; a three-tonie run (~80 MB upload
plus 45-60 s settle each) takes 401s in its back half if nothing refreshes.
`tonie_api.session.TonieCloudSession._acquire_token` throws away
`expires_in` and `refresh_token` (`return
response.json().get("access_token")`) and swallows `Timeout`/
`RequestException` into `None`, making a network failure and a wrong
password indistinguishable. So Box Butler performs its own OpenID token
acquisition (grant_type=password / grant_type=refresh_token), capturing all
three of `access_token`, `refresh_token`, `expires_in` into
`boxbutler.sinks.session.AuthResult`, and drives `Session` (Task 39) --
proactive refresh at 60 s of margin, reactive refresh-and-retry-once on a
401, backoff on 429/503.

The seam that makes this cheap: `TonieAPI.__request` rebuilds
`headers = {"Authorization": f"Bearer {self.session.token}"}` on *every*
call and sends via `self.session.request(...)`. `_LiveSessionAdapter`
below stands in for `tonie_api`'s own `TonieCloudSession` as `TonieAPI`'s
`.session`: its `.token` property forces `Session.ensure_fresh()` before
handing back the live value, and its `.request()` forwards to
`Session.request()` -- so every `TonieAPI` call, including
`add_chapter_to_tonie` immediately after a 180 s S3 upload, picks up a
live token with no change to `tonie_api` itself.

Why the adapter must raise on a non-2xx response
--------------------------------------------------
`TonieAPI.__request` reads `if not resp.ok: log.error(...); return {}` --
every HTTP failure is swallowed into an empty dict nobody inspects. For
`clear_all_chapter_of_tonie` in particular, that means a 500 or a 429 on
CLEAR is indistinguishable from a clear that actually happened: a clear
that silently did nothing, followed by an upload that appends, puts the
tonie over cap. `_LiveSessionAdapter.request()` raises `SessionError` on
any non-2xx (after `Session`'s own 401/429/503 handling has had its turn),
so `__request`'s swallow branch is unreachable no matter what the cloud
returns. `tests/sinks/test_tonies_cloud.py::test_clear_raises_on_non2xx_response_never_swallowed`
proves it against a fake transport returning 500 on the clear PATCH.

Large payloads still bypass the session entirely (deliberately, per
`Session.request`'s refusal of non-retry-safe bodies): `upload_file_to_tonie`
streams the file to S3 with a bare `requests.post`, and only the small JSON
POSTs (`/file`, `add_chapter_to_tonie`) cross `_LiveSessionAdapter`. That
arrangement is `tonie_api`'s own and is left intact here.

No third-party URL is copied into this repo: `TonieCloudSession.OPENID_CONNECT`
is imported as a constant from the installed `tonie_api` package (as is
`TonieAPI.API_URL`, used by `tonie_api` itself to build the paths this
module's `_live_transport` receives already-assembled).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from tonie_api.api import TonieAPI
from tonie_api.session import TonieCloudSession

from boxbutler.orchestrator.settle import poll_until_settled
from boxbutler.sinks.protocol import LiveChapter, SettleResult, SinkLimits, Target, TargetSnapshot
from boxbutler.sinks.session import AuthResult, Session, SessionError

# Imported, not retyped: the real endpoint lives in `tonie_api`, this repo
# never spells it out itself.
_OPENID_CONNECT = TonieCloudSession.OPENID_CONNECT

# `tonie_api.session.TonieCloudSession._acquire_token`'s own login payload
# shape (client id, not a URL or a secret).
_CLIENT_ID = "my-tonies"

# Timeout for every live HTTP call this module makes directly (login,
# verify_login, and the plain `requests` send behind `_live_transport` --
# config, households, creativetonies, clear, add-chapter all cross it), not
# just login.
_HTTP_TIMEOUT_S = 30
_CREDENTIAL_REJECTED_STATUSES = (400, 401)


def _token_request(data: dict[str, str]) -> AuthResult:
    """POST the OpenID token endpoint and turn a success response into an
    `AuthResult`. A definite credential rejection (400/401) is raised as
    `SessionError` with a fixed message -- everything else (timeout,
    connection failure, a 5xx) is an *unknown* outcome, not a "bad
    password", so it propagates as whatever `requests` raised and
    `Session._authenticate_now`/`_refresh_now` turn it into a generic
    `SessionError` rather than a value that could be mistaken for success.
    """
    response = requests.post(_OPENID_CONNECT, data=data, timeout=_HTTP_TIMEOUT_S)
    if response.status_code in _CREDENTIAL_REJECTED_STATUSES:
        raise SessionError("authentication rejected by cloud")
    response.raise_for_status()
    body = response.json()
    return AuthResult(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_in=body["expires_in"],
    )


def _authenticate(username: str, password: str) -> AuthResult:
    return _token_request(
        {
            "grant_type": "password",
            "client_id": _CLIENT_ID,
            "scope": "openid",
            "username": username,
            "password": password,
        }
    )


def _refresh(refresh_token: str) -> AuthResult:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": _CLIENT_ID,
            "refresh_token": refresh_token,
        }
    )


def _live_transport(method: str, url: str, *, headers: dict | None = None, json: Any = None, **kwargs):
    """The real HTTP send, injected into `Session` as its `transport`.
    `url` here is always the full URL `TonieAPI.__request` already built
    (`f"{TonieAPI.API_URL}/{path}"`) -- `Session` treats "path" as opaque.
    """
    return requests.request(method, url, headers=headers, json=json, timeout=_HTTP_TIMEOUT_S, **kwargs)


class _LiveSessionAdapter:
    """Stands in for `tonie_api.session.TonieCloudSession` as `TonieAPI`'s
    `.session`, backed by a Task 39 `Session` instead of a one-shot,
    non-refreshing token. See module docstring for why this is the whole
    seam: `TonieAPI` needs nothing else changed.

    Gets the raw token string through `Session.bearer_token()` -- a public,
    owned-and-versioned accessor, not a reach into `Session`'s private
    `_token` -- so a future refactor inside `session.py` cannot break this
    adapter silently (no test in this module exercises this class directly:
    Task 26's brief tests all inject `FakeApi`, never this adapter's own
    `.token`/`.request`, except the dedicated non-2xx tests which drive it
    end to end).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def token(self) -> str:
        return self._session.bearer_token()

    def request(self, method: str, url: str, *, headers: dict | None = None, json: Any = None, **kwargs):
        response = self._session.request(method, url, headers=headers, json=json, **kwargs)
        if not response.ok:
            # Never let TonieAPI.__request's `if not resp.ok: return {}`
            # branch see this: that is the swallow this project cannot
            # afford on a clear. Message is fixed and carries no header,
            # body or token -- only the status code.
            raise SessionError(f"cloud request failed with status {response.status_code}")
        return response


class UnknownTargetError(Exception):
    """Raised by `_tonie_for` when a `Target` isn't present in the latest
    cloud listing -- a specific type rather than a bare `KeyError`, in
    keeping with this module's otherwise disciplined exception types."""


class ToniesCloudSink:
    """SinkProtocol over tonie_api.TonieAPI. `api` is duck-typed so tests
    inject a fake; the network/session machinery lives only in `from_env`
    and the private helpers above.
    """

    #: `SinkProtocol.name` — the `assignment.sink` discriminator every
    #: assignment row for the real cloud account is keyed under. This is
    #: the only place the string exists: the web layer, the setup wizard,
    #: the CLI and the orchestrator all read `sink.name`, so there is no
    #: second copy to drift out of step with it (final safety review, C1).
    name = "tonies_cloud"

    def __init__(
        self,
        api,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._api = api
        self._sleep = sleep
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._tonies_by_id: dict[str, Any] = {}
        # Not fetched here: `__init__` must stay side-effect-free (no
        # network, no login) so `from_env` can construct a sink to call
        # `verify_login` on candidate credentials, and so construction is
        # safe wherever the network isn't (a dry run, `--help`, a unit
        # test of unrelated code). See `limits` below.
        self._limits: SinkLimits | None = None

    @classmethod
    def from_env(cls, user: str, password: str) -> "ToniesCloudSink":
        """The ONLY place credentials are read: `user`/`password` arrive as
        plain arguments from `config.py`'s composition root (Task 29) --
        nothing here reads an environment variable or a secrets store.
        """
        session = Session(
            authenticate=lambda: _authenticate(user, password),
            refresh=_refresh,
            transport=_live_transport,
        )
        adapter = _LiveSessionAdapter(session)
        api = object.__new__(TonieAPI)  # bypass TonieAPI.__init__'s own one-shot acquire_token
        api.session = adapter
        return cls(api)

    def verify_login(self, username: str, password: str) -> bool:
        """Non-mutating credential check (Task 36 setup wizard) against the
        real cloud login -- deliberately independent of `self._api`'s own
        session, since the wizard checks arbitrary candidate credentials,
        not necessarily the account this sink already authenticated as.

        A rejected password (400/401) is a definite "no". Anything else --
        timeout, DNS failure, a 5xx -- is an *unknown* outcome, not a "no",
        so it is raised rather than folded into `False` (this project's
        recurring "unknown state read as definite" bug family).
        """
        response = requests.post(
            _OPENID_CONNECT,
            data={
                "grant_type": "password",
                "client_id": _CLIENT_ID,
                "scope": "openid",
                "username": username,
                "password": password,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        if response.status_code in _CREDENTIAL_REJECTED_STATUSES:
            return False
        response.raise_for_status()
        return "access_token" in response.json()

    def _refresh_and_cache_tonies(self) -> None:
        tonies_by_id: dict[str, Any] = {}
        for household in self._api.get_households():
            for tonie in self._api.get_all_creative_tonies_by_household(household):
                tonies_by_id[tonie.id] = tonie
        self._tonies_by_id = tonies_by_id

    def _tonie_for(self, target: Target) -> Any:
        tonie = self._tonies_by_id.get(target.id)
        if tonie is None:
            self._refresh_and_cache_tonies()
            tonie = self._tonies_by_id.get(target.id)
        if tonie is None:
            raise UnknownTargetError(f"unknown target {target.id!r}: not present in latest cloud listing")
        return tonie

    def list_targets(self) -> list[Target]:
        self._refresh_and_cache_tonies()
        return [
            Target(id=t.id, name=t.name, seconds_present=t.secondsPresent, chapters_present=t.chaptersPresent)
            for t in self._tonies_by_id.values()
        ]

    def read_chapters(self, target: Target) -> TargetSnapshot:
        # Always re-lists: this is the live source of truth, never a stale
        # cache read back as present-tense fact.
        self._refresh_and_cache_tonies()
        tonie = self._tonies_by_id.get(target.id)
        if tonie is None:
            raise KeyError(f"unknown target {target.id!r}: not present in latest cloud listing")
        chapters = [
            LiveChapter(id=c.id, title=c.title, seconds=c.seconds, transcoding=c.transcoding)
            for c in tonie.chapters
        ]
        return TargetSnapshot(target=target, taken_at=datetime.now(UTC), chapters=chapters)

    def clear(self, target: Target) -> None:
        self._api.clear_all_chapter_of_tonie(self._tonie_for(target))

    def upload(self, target: Target, path: Path, title: str) -> None:
        self._api.upload_file_to_tonie(self._tonie_for(target), path, title)

    def settle(self, target: Target, expect_seconds: float, timeout_s: int) -> SettleResult:
        return poll_until_settled(
            lambda: self.read_chapters(target).chapters,
            expect_seconds,
            timeout_s,
            poll_interval_s=self._poll_interval_s,
            sleep=self._sleep,
            clock=self._clock,
        )

    @property
    def limits(self) -> SinkLimits:
        # Fetched lazily, on first access, and cached for the process
        # lifetime: spec §3 "reads real limits from /config" plus the
        # requirement that the config endpoint is read exactly once across
        # repeated `.limits` reads. Lazy so construction itself (`from_env`
        # included) stays side-effect-free -- see `__init__`.
        #
        # Deliberately reads the raw dict rather than `tonie_api.get_config()`,
        # which validates the response against its own pydantic `Config`
        # model. That model requires fields this project never looks at, and
        # on 2026-09-13 the live endpoint stopped returning one of them
        # (`paypalClientId`) -- so `get_config()` raised a ValidationError and
        # every run failed, on a field nobody uses. A pinned third-party model
        # is a promise about the WHOLE response; we only need four fields, so
        # we only depend on four fields.
        if self._limits is None:
            cfg = self._api._get("config")
            missing = [k for k in ("maxSeconds", "maxChapters", "maxBytes", "accepts") if k not in cfg]
            if missing:
                # An absent limit is unknown, not zero and not a default: we
                # clamp uploads against these, so guessing would mean guessing
                # about what a tonie will accept.
                raise SessionError(f"cloud config is missing required limits: {', '.join(missing)}")
            self._limits = SinkLimits(
                max_seconds=int(cfg["maxSeconds"]),
                max_chapters=int(cfg["maxChapters"]),
                max_bytes=int(cfg["maxBytes"]),
                accepts=tuple(cfg["accepts"]),
            )
        return self._limits


__all__ = ["ToniesCloudSink", "UnknownTargetError"]
