"""`Session` — token refresh and rate-limit backoff (Task 39; spec §3
"Session lifetime — the sink must refresh its own token").

Everything here drives a fake transport and injected `authenticate`/
`refresh` callables — no `tonie_api` import, no network, no real sleep
(`sleep` is always a recorder, `clock` is always the fake `MutableClock`
below).

Integration with the run loop (the §2 exposure window landing in
`DEGRADED`, and the "no token in any run event" proof against the real
persisted event stream) lives in
`tests/orchestrator/test_session_integration.py`, which has the fixtures
for a real `Store` + orchestrator; this file is Session in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from boxbutler.fetch.protocol import ExtractionBroken
from boxbutler.sinks.session import AuthResult, RateLimited, Session, SessionError

TOKEN_A = "access-token-aaaaaaaa"
REFRESH_A = "refresh-token-aaaaaaaa"
TOKEN_B = "access-token-bbbbbbbb"
REFRESH_B = "refresh-token-bbbbbbbb"


class MutableClock:
    """Clock the test advances explicitly. Session never sleeps to pass
    time on its own; only `advance()` moves it."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeResponse:
    status_code: int
    headers: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)

    def json(self) -> dict:
        return self.body


class ScriptedTransport:
    """Returns responses off a fixed script, keyed only by call order.
    Records every call (method, path, headers) so tests can assert on the
    Authorization header actually sent."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, path: str, *, headers: dict | None = None, **kwargs):
        self.calls.append((method, path, dict(headers or {})))
        if not self._responses:
            raise AssertionError("ScriptedTransport script exhausted")
        return self._responses.pop(0)


def make_auth(access=TOKEN_A, refresh=REFRESH_A, expires_in=300.0):
    calls: list[None] = []

    def authenticate():
        calls.append(None)
        return AuthResult(access, refresh, expires_in)

    authenticate.calls = calls
    return authenticate


def make_refresh(access=TOKEN_B, refresh=REFRESH_B, expires_in=300.0, raises: Exception | None = None):
    calls: list[str] = []

    def refresh_fn(refresh_token: str):
        calls.append(refresh_token)
        if raises is not None:
            raise raises
        return AuthResult(access, refresh, expires_in)

    refresh_fn.calls = calls
    return refresh_fn


# --------------------------------------------------------------------- 1


def test_refreshes_proactively_within_60s_of_expiry():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(200), FakeResponse(200)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    session.request("GET", "/a")
    assert authenticate.calls == [None]
    assert refresh.calls == []
    assert transport.calls[0][2]["Authorization"] == f"Bearer {TOKEN_A}"

    # 250s elapsed of a 300s token: 50s left, under the 60s margin.
    clock.advance(250)
    session.request("GET", "/b")

    assert refresh.calls == [REFRESH_A]          # exactly one refresh call
    assert len(refresh.calls) == 1
    assert authenticate.calls == [None]          # not re-authenticated from scratch
    assert transport.calls[1][2]["Authorization"] == f"Bearer {TOKEN_B}"


def test_no_refresh_when_token_is_fresh():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(200), FakeResponse(200)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    session.request("GET", "/a")
    clock.advance(10)   # 290s left: well outside the 60s margin
    session.request("GET", "/b")

    assert refresh.calls == []


# --------------------------------------------------------------------- 2


def test_401_triggers_one_reauth_and_retry():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(401), FakeResponse(200)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    response = session.request("GET", "/x")

    assert response.status_code == 200
    assert len(refresh.calls) == 1
    assert transport.calls[0][2]["Authorization"] == f"Bearer {TOKEN_A}"
    assert transport.calls[1][2]["Authorization"] == f"Bearer {TOKEN_B}"


def test_second_401_raises_instead_of_looping():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(401), FakeResponse(401)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    with pytest.raises(SessionError):
        session.request("GET", "/x")

    # Exactly one reauth attempted, exactly two requests sent - no loop.
    assert len(refresh.calls) == 1
    assert len(transport.calls) == 2


# --------------------------------------------------------------------- 3
# (refresh failure -> a sink never even gets a response to act on) is
# exercised end-to-end, through a sink and the run loop, in
# tests/orchestrator/test_session_integration.py. Here: the failure itself.


def test_refresh_failure_raises_session_error_without_a_request():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh(raises=RuntimeError("connection reset"))
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    session.ensure_fresh()             # primes a real token via authenticate()
    clock.advance(300 - 30)            # now within the refresh margin

    with pytest.raises(SessionError):
        session.request("GET", "/x")
    assert transport.calls == []       # never sent - refresh failed first


# --------------------------------------------------------------------- 5


def test_429_honours_retry_after_then_503_backs_off_exponentially():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport(
        [FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(503), FakeResponse(200)]
    )
    sleeps: list[float] = []
    session = Session(
        authenticate, refresh, transport, clock=clock, sleep=sleeps.append, backoff_base_s=1.0
    )

    response = session.request("GET", "/x")

    assert response.status_code == 200
    assert sleeps[0] == 2.0            # Retry-After honoured verbatim
    assert sleeps[1] == 2.0            # exponential: base(1.0) * 2**(attempt(2)-1)
    assert len(transport.calls) == 3


def test_rate_limit_is_bounded_and_surfaces_as_rate_limited():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(503) for _ in range(20)])
    session = Session(
        authenticate,
        refresh,
        transport,
        clock=clock,
        sleep=lambda s: None,
        max_rate_limit_attempts=3,
    )

    with pytest.raises(RateLimited) as excinfo:
        session.request("GET", "/x")

    assert excinfo.value.status_code == 503
    # Bounded: gave up well before the 20-response script ran out.
    assert len(transport.calls) == 4
    assert not isinstance(excinfo.value, SessionError)
    assert not issubclass(RateLimited, ExtractionBroken)


def test_backoff_delay_is_capped():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([FakeResponse(503) for _ in range(6)] + [FakeResponse(200)])
    sleeps: list[float] = []
    backoff_base_s = 10.0
    backoff_cap_s = 25.0
    session = Session(
        authenticate,
        refresh,
        transport,
        clock=clock,
        sleep=sleeps.append,
        max_rate_limit_attempts=6,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )

    session.request("GET", "/x")

    # Rises exponentially (base * 2**(attempt-1)) while under the cap, then
    # every subsequent delay is pinned to the cap exactly - not merely "<=".
    expected = []
    for attempt in range(1, len(sleeps) + 1):
        expected.append(min(backoff_base_s * (2 ** (attempt - 1)), backoff_cap_s))
    assert sleeps == expected
    assert expected.count(backoff_cap_s) >= 2  # actually reaches and plateaus at the cap


# --------------------------------------------------------------------- 6
# (integration proof against real persisted run events lives in
# test_session_integration.py). Here: the exception messages themselves.


def test_no_token_in_any_exception_message():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh(raises=RuntimeError(f"upstream said {TOKEN_A}"))
    transport = ScriptedTransport([])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    session.ensure_fresh()
    clock.advance(300 - 30)

    with pytest.raises(SessionError) as excinfo:
        session.request("GET", "/x")

    assert TOKEN_A not in str(excinfo.value)
    assert REFRESH_A not in str(excinfo.value)

    # And the repr of the token-carrying types themselves.
    result = AuthResult(TOKEN_A, REFRESH_A, 300.0)
    assert TOKEN_A not in repr(result)
    assert REFRESH_A not in repr(result)


# --------------------------------------------------------------------- instance-eight guards:
# an unparseable/non-finite/non-positive expiry, or a missing token
# string, is not a valid token.


@pytest.mark.parametrize(
    "bad_result",
    [
        AuthResult("", REFRESH_A, 300.0),
        AuthResult(TOKEN_A, "", 300.0),
        AuthResult(TOKEN_A, REFRESH_A, 0.0),
        AuthResult(TOKEN_A, REFRESH_A, -5.0),
        AuthResult(TOKEN_A, REFRESH_A, float("nan")),
        AuthResult(TOKEN_A, REFRESH_A, float("inf")),
    ],
)
def test_unparseable_or_invalid_auth_result_is_not_a_valid_token(bad_result):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))

    def authenticate():
        return bad_result

    refresh = make_refresh()
    transport = ScriptedTransport([])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    with pytest.raises(SessionError):
        session.request("GET", "/x")
    assert transport.calls == []


def test_non_numeric_expires_in_is_not_a_valid_token():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))

    def authenticate():
        return AuthResult(TOKEN_A, REFRESH_A, "soon")  # type: ignore[arg-type]

    refresh = make_refresh()
    transport = ScriptedTransport([])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    with pytest.raises(SessionError):
        session.request("GET", "/x")
    assert transport.calls == []


# --------------------------------------------------------------------- 7
# A retried request resends **kwargs verbatim (see `_send`); a single-use
# body (a file, a generator, ...) would be truncated or emptied on the
# retry with nothing detecting it. `request()` must refuse those bodies
# before making any attempt at all.


class _FileLikeBody:
    """Stands in for `open(path, "rb")`: exposes `read`, consumed once."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read_count = 0

    def read(self, *args, **kwargs) -> bytes:
        self.read_count += 1
        return self._data


def _generator_body():
    yield b"chunk-1"
    yield b"chunk-2"


@pytest.mark.parametrize("kwarg", ["data", "json", "files"])
def test_file_like_body_is_refused_before_any_transport_call(kwarg):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    body = _FileLikeBody(b"payload")

    with pytest.raises(SessionError):
        session.request("POST", "/x", **{kwarg: body})

    assert transport.calls == []           # refused pre-flight, not after a partial send
    assert authenticate.calls == []        # not even a token was fetched
    assert body.read_count == 0            # never touched


@pytest.mark.parametrize("kwarg", ["data", "json", "files"])
def test_generator_body_is_refused_before_any_transport_call(kwarg):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    with pytest.raises(SessionError):
        session.request("POST", "/x", **{kwarg: _generator_body()})

    assert transport.calls == []
    assert authenticate.calls == []


class _BodyRecordingTransport:
    """Like `ScriptedTransport`, but also records the full kwargs (the
    body included) each attempt actually sent, so a retry-safe body can be
    proven identical across attempts - not just "no exception raised"."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.sent_bodies: list[dict] = []

    def __call__(self, method: str, path: str, *, headers: dict | None = None, **kwargs):
        self.sent_bodies.append(kwargs)
        if not self._responses:
            raise AssertionError("_BodyRecordingTransport script exhausted")
        return self._responses.pop(0)


@pytest.mark.parametrize(
    "kwarg, body",
    [
        ("data", b"raw-bytes-payload"),
        ("data", "a plain string payload"),
        ("json", {"title": "some tonie", "count": 3}),
        ("files", [("file", ("clip.opus", b"opus-bytes", "audio/opus"))]),
    ],
)
def test_retry_safe_body_is_accepted_and_resent_identically_on_retry(kwarg, body):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = _BodyRecordingTransport([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)

    response = session.request("POST", "/x", **{kwarg: body})

    assert response.status_code == 200
    assert len(transport.sent_bodies) == 2
    # The retry sent the identical body - not truncated, not empty.
    assert transport.sent_bodies[0][kwarg] == body
    assert transport.sent_bodies[1][kwarg] == body


# --- nested bodies: the check must not stop at the top-level type ---------
#
# A `files=[("field", open(...))]` list, or a `data={"payload": open(...)}`
# mapping, passed the old top-level-only check: the outer object is a
# `list`/`Mapping`, so it was judged safe while a file handle sat inside it,
# still ready to be resent consumed on a retry.


def test_file_like_body_nested_in_files_list_is_refused_before_any_transport_call():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    body = _FileLikeBody(b"payload")
    files = [("field", ("clip.opus", body, "audio/opus"))]

    with pytest.raises(SessionError):
        session.request("POST", "/x", files=files)

    assert transport.calls == []
    assert authenticate.calls == []
    assert body.read_count == 0


def test_file_like_body_nested_as_mapping_value_in_data_is_refused_before_any_transport_call():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    body = _FileLikeBody(b"payload")

    with pytest.raises(SessionError):
        session.request("POST", "/x", data={"payload": body})

    assert transport.calls == []
    assert authenticate.calls == []
    assert body.read_count == 0


def test_deeply_nested_safe_body_is_accepted_and_resent_identically_on_retry():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = _BodyRecordingTransport([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200)])
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    # Lists of dicts of bytes/str, several levels deep - every leaf is a
    # safe scalar, so the whole structure must be accepted.
    body = [
        {"name": "tonie-1", "chunks": [b"chunk-a", b"chunk-b"]},
        {"name": "tonie-2", "chunks": (b"chunk-c", "trailer")},
    ]

    response = session.request("POST", "/x", data=body)

    assert response.status_code == 200
    assert len(transport.sent_bodies) == 2
    # Not just "nothing raised" - the retry sent the identical structure.
    assert transport.sent_bodies[0]["data"] == body
    assert transport.sent_bodies[1]["data"] == body


def test_self_referential_body_raises_instead_of_hanging():
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    authenticate = make_auth()
    refresh = make_refresh()
    transport = ScriptedTransport([])  # must never be reached
    session = Session(authenticate, refresh, transport, clock=clock, sleep=lambda s: None)
    cyclic: list = []
    cyclic.append(cyclic)  # a list containing itself

    # An unknown-safety shape is a caller bug, not a state to tolerate as
    # "safe" or "unsafe" by guessing (per the project's "no sentinel for an
    # unknown state, raise instead" rule) - and it must not hang forever.
    with pytest.raises(SessionError):
        session.request("POST", "/x", data=cyclic)

    assert transport.calls == []
