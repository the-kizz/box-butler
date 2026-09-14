"""`Session` wired in front of a sink, driven through the real run loop
(Task 39; spec §3 "Session lifetime").

Uses the `world` fixture from `tests/orchestrator/conftest.py` (Task 21's
FakeSink-backed orchestrator harness) with `world["deps"].sink` swapped for
`SessionGatedSink` below - a **test-only** stand-in for what Task 26's real
`ToniesCloudSink` will do: route `clear()`/`upload()` through `Session`
before touching the real cloud. No `tonie_api` import anywhere in this
file - Task 26 owns wiring the real sink; this proves the run loop's
existing `ClearFailed`/`UploadFailed` -> `DEGRADED` machinery (Task 21/22,
already covered by `tests/orchestrator/test_degraded.py`) does the right
thing when the failure originates from `Session` instead of a bare
`SinkUploadError`/`SinkClearError`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from boxbutler.domain.models import AssignmentState, RunOutcome
from boxbutler.sinks.session import AuthResult, Session

TOKEN_A = "access-token-aaaaaaaa"
REFRESH_A = "refresh-token-aaaaaaaa"
TOKEN_B = "access-token-bbbbbbbb"
REFRESH_B = "refresh-token-bbbbbbbb"


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        return {}


class SessionGatedSink:
    """Routes `clear`/`upload` through `Session`; everything else passes
    straight to the wrapped `FakeSink` untouched. Stands in for the shape
    Task 26's real sink will have (spec §3: "the sink routes every call
    through it") without importing `tonie_api`.
    """

    def __init__(self, inner, session: Session):
        self._inner = inner
        self._session = session

    def clear(self, target):
        self._session.request("POST", f"/tonies/{target.id}/clear")
        self._inner.clear(target)

    def upload(self, target, path, title):
        self._session.request("POST", f"/tonies/{target.id}/upload")
        self._inner.upload(target, path, title)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def make_auth(access=TOKEN_A, refresh=REFRESH_A, expires_in=300.0):
    def authenticate():
        return AuthResult(access, refresh, expires_in)

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


def all_payload_text(store, run_id) -> str:
    """Every run_event's payload for `run_id`, flattened to one string, for
    a substring scan - the same shape a human `grep`-ing the persisted log
    would do."""
    return " ".join(
        json.dumps(e.payload, default=str) for e in store.runs.events(run_id)
    )


# --------------------------------------------------------------------- 3


def _unreachable_transport(*args, **kwargs):
    raise AssertionError("transport must not be called")


def test_refresh_failure_aborts_the_tonie_untouched(world):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = Session(
        make_auth(),
        make_refresh(raises=RuntimeError("connection reset")),
        transport=_unreachable_transport,
        clock=clock,
        sleep=lambda s: None,
    )
    session.ensure_fresh()          # primes a real token, outside the run
    clock.advance(300 - 30)         # now within the 60s refresh margin

    world["deps"].sink = SessionGatedSink(world["sink"], session)

    rep = world["orch"].run(apply=True)

    assert world["sink"].calls_named("clear") == []
    assert world["sink"].calls_named("upload") == []
    a = world["store"].assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert a.staged_json is not None   # verified files kept, nothing half-completed


# --------------------------------------------------------------------- 4


def test_401_between_clear_and_upload_lands_in_degraded_then_repairs(world):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    upload_status = {"code": 401}

    def transport(method, path, *, headers=None, **kwargs):
        if path.endswith("/upload"):
            return FakeResponse(upload_status["code"])
        return FakeResponse(200)

    session = Session(make_auth(), make_refresh(), transport, clock=clock, sleep=lambda s: None)
    world["deps"].sink = SessionGatedSink(world["sink"], session)
    world["deps"].settings.upload_backoff_s = (0, 0, 0)

    rep = world["orch"].run(apply=True)

    # CLEAR went through (the exposure window is *after* clear); upload
    # never landed on the underlying sink because Session raised first.
    assert len(world["sink"].calls_named("clear")) == 1
    assert world["sink"].calls_named("upload") == []
    a = world["store"].assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    staged = json.loads(a.staged_json)
    assert staged and staged[0]["path"]   # the staged file is retained

    # The repair path (Task 22, already covered elsewhere): once the cloud
    # accepts the (now fresh) token, the retained staged file re-uploads.
    upload_status["code"] = 200
    rep2 = world["orch"].run(apply=True)
    assert rep2.reports[0].outcome == RunOutcome.REPAIRED
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.OK


# --------------------------------------------------------------------- 6


def test_no_token_reaches_a_run_event_or_the_database(world):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))

    def transport(method, path, *, headers=None, **kwargs):
        if path.endswith("/upload"):
            return FakeResponse(401)
        return FakeResponse(200)

    session = Session(make_auth(), make_refresh(), transport, clock=clock, sleep=lambda s: None)
    world["deps"].sink = SessionGatedSink(world["sink"], session)
    world["deps"].settings.upload_backoff_s = (0, 0, 0)

    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED   # sanity: the interesting path ran

    text = all_payload_text(world["store"], rep.run_id)
    for secret in (TOKEN_A, REFRESH_A, TOKEN_B, REFRESH_B):
        assert secret not in text

    # And the assignment row itself (staged_json, the only free-text column
    # a sink failure can reach) is equally clean.
    a = world["store"].assignments.get(world["a"].id)
    for secret in (TOKEN_A, REFRESH_A, TOKEN_B, REFRESH_B):
        assert secret not in (a.staged_json or "")
