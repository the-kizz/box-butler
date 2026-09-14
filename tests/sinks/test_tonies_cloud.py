"""Tests for `ToniesCloudSink` (Task 26; spec §3, §3.4, §2 step 8, §10.5).

Every test here runs against `FakeApi` (a duck-typed stand-in for
`tonie_api.TonieAPI`) or a fake transport wired through `Session` — never
the network, the real cloud, ffmpeg, or a real tonie.

Brief's interface sketch calls the read method `snapshot(target)`; the
already-built `boxbutler/sinks/protocol.py` (what the orchestrator
actually calls) names it `read_chapters(target) -> TargetSnapshot`. Every
test line below is transcribed accordingly — no `snapshot` alias exists.
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from boxbutler.sinks.session import SessionError
from boxbutler.sinks.tonies_cloud import ToniesCloudSink


class FakeApi:
    def __init__(self):
        self.calls = []
        self.polls = 0
        self.tonie = NS(
            id="T100",
            householdId="H",
            name="Green Tonie",
            secondsPresent=5340.0,
            chaptersPresent=1,
            transcoding=False,
            lastUpdate=None,
            chapters=[NS(id="c1", title="Old", file="f", seconds=5340.0, transcoding=False)],
        )

    def _get(self, url):
        # The sink reads the raw config dict rather than tonie_api's own
        # pydantic Config model. That model demands fields this project never
        # reads, and when the live endpoint stopped returning one of them the
        # model raised and every run failed. The shape below is the real
        # response as measured on 2026-09-13, including the extra keys we
        # ignore -- a fake that returned only the four fields we use would
        # not prove we tolerate the rest.
        assert url == "config"
        self.calls.append("get_config")
        return {
            "locales": ["de", "en"],
            "maxChapters": 250,
            "maxSeconds": 5400,
            "maxBytes": 1 << 30,
            "accepts": ["aac", "m4a", "mp3"],
            "stageWarning": False,
            "features": ["discs"],
        }

    def get_households(self):
        return [NS(id="H", name="Home")]

    def get_all_creative_tonies_by_household(self, hh):
        self.polls += 1
        if self.polls > 2 and self.tonie.chapters and self.tonie.chapters[0].transcoding:
            self.tonie.chapters[0].transcoding = False
            self.tonie.chapters[0].seconds = 5340.0
        return [self.tonie]

    def clear_all_chapter_of_tonie(self, t):
        self.calls.append("clear")
        self.tonie.chapters = []

    def upload_file_to_tonie(self, t, path, title):
        self.calls.append(("upload", str(path), title))
        self.tonie.chapters = [NS(id="c2", title=title, file="f2", seconds=0.0, transcoding=True)]


def mk():
    api = FakeApi()
    return api, ToniesCloudSink(api, sleep=lambda s: None, clock=iter(range(0, 10_000, 5)).__next__)


def test_limits_come_from_cloud_config_once():
    api, s = mk()
    _ = s.limits
    _ = s.limits
    assert (s.limits.max_seconds, s.limits.max_chapters, s.limits.accepts) == (
        5400,
        250,
        ("aac", "m4a", "mp3"),
    ) and api.calls.count("get_config") == 1


def test_list_and_snapshot():
    api, s = mk()
    [t] = s.list_targets()
    assert (t.id, t.name) == ("T100", "Green Tonie")
    snap = s.read_chapters(t)
    assert snap.chapters[0].id == "c1" and snap.chapters[0].seconds == 5340.0


def test_clear_upload_settle_polls_through_transcoding():
    api, s = mk()
    [t] = s.list_targets()
    s.clear(t)
    s.upload(t, Path("/c/new.m4a"), "New")
    r = s.settle(t, 5340.0, timeout_s=180)
    # Order-relative, not index-absolute: this project's single most
    # important correctness requirement is that a tonie is never cleared
    # before its replacement is ready (PLAN -> FETCH -> RENDER -> VERIFY ->
    # SNAPSHOT -> CLEAR -> UPLOAD -> SETTLE -> COMMIT), so pin *that
    # ordering* and stay green regardless of what else lands in `calls`
    # (e.g. a lazy `.limits` fetch, which may or may not have happened by
    # now depending on what earlier code touched `.limits`).
    assert "clear" in api.calls and ("upload", "/c/new.m4a", "New") in api.calls
    assert api.calls.index("clear") < api.calls.index(("upload", "/c/new.m4a", "New"))
    assert r.settled and api.polls >= 3


def test_settle_rejects_wrong_duration():
    api, s = mk()
    [t] = s.list_targets()
    s.upload(t, Path("/c/new.m4a"), "New")
    api.tonie.chapters[0].transcoding = False
    api.tonie.chapters[0].seconds = 4000.0
    assert s.settle(t, 5340.0, timeout_s=60).settled is False


def test_no_credentials_are_read_here():
    import inspect

    import boxbutler.sinks.tonies_cloud as m

    src = inspect.getsource(m)
    assert "estate" not in src and "os.environ" not in src


# --- Decision 3: the injected session MUST raise on a non-2xx response ---
# A fake transport returning 500 on the clear PATCH must surface as an
# exception out of ToniesCloudSink.clear, never as a quiet return — the
# swallow-to-{} branch in tonie_api.TonieAPI.__request must be unreachable.


class _FakeTransportResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers: dict = {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def _wire_real_tonie_api_with_fake_transport(transport):
    """Build a real `tonie_api.TonieAPI` instance (bypassing its network-
    calling `__init__`) with `.session` replaced by `ToniesCloudSink`'s
    live session adapter over a fake transport, so this test exercises the
    real `tonie_api` request path (including its swallow-to-{} branch) end
    to end, without ever touching the network.
    """
    from tonie_api.api import TonieAPI

    from boxbutler.sinks.session import AuthResult, Session
    from boxbutler.sinks.tonies_cloud import _LiveSessionAdapter

    session = Session(
        authenticate=lambda: AuthResult(access_token="t1", refresh_token="r1", expires_in=300),
        refresh=lambda rt: AuthResult(access_token="t2", refresh_token="r2", expires_in=300),
        transport=transport,
    )
    adapter = _LiveSessionAdapter(session)
    api = object.__new__(TonieAPI)
    api.session = adapter
    return api


def test_clear_raises_on_non2xx_response_never_swallowed():
    household_payload = [{"id": "H", "name": "Home", "ownerName": "K", "access": "owner", "canLeave": False}]
    tonie_payload = [
        {
            "id": "T100",
            "householdId": "H",
            "name": "Green Tonie",
            "imageUrl": "",
            "secondsRemaining": 0.0,
            "secondsPresent": 5340.0,
            "chaptersRemaining": 249,
            "chaptersPresent": 1,
            "transcoding": False,
            "lastUpdate": None,
            "chapters": [],
        }
    ]
    config_payload = {
        "locales": ["en"],
        "unicodeLocales": ["en"],
        "maxChapters": 250,
        "maxSeconds": 5400,
        "maxBytes": 1 << 30,
        "accepts": ["aac", "m4a", "mp3"],
        "stageWarning": False,
        "paypalClientId": "x",
        "ssoEnabled": True,
    }

    def fake_transport(method, url, *, headers=None, json=None, **kwargs):
        if method == "PATCH":
            return _FakeTransportResponse(500, {})
        if url.endswith("/config"):
            return _FakeTransportResponse(200, config_payload)
        if url.endswith("/households"):
            return _FakeTransportResponse(200, household_payload)
        if url.endswith("/creativetonies"):
            return _FakeTransportResponse(200, tonie_payload)
        raise AssertionError(f"unexpected transport call: {method} {url}")

    api = _wire_real_tonie_api_with_fake_transport(fake_transport)
    sink = ToniesCloudSink(api, sleep=lambda s: None, clock=lambda: 0.0)
    [target] = sink.list_targets()

    with pytest.raises(SessionError):
        sink.clear(target)


def test_upload_raises_on_non2xx_response_never_swallowed():
    """Same guarantee on the upload-side small-JSON POST (`add_chapter_to_tonie`
    crosses the session; only the S3 stream bypasses it per decision 4)."""
    household_payload = [{"id": "H", "name": "Home", "ownerName": "K", "access": "owner", "canLeave": False}]
    tonie_payload = [
        {
            "id": "T100",
            "householdId": "H",
            "name": "Green Tonie",
            "imageUrl": "",
            "secondsRemaining": 0.0,
            "secondsPresent": 5340.0,
            "chaptersRemaining": 249,
            "chaptersPresent": 1,
            "transcoding": False,
            "lastUpdate": None,
            "chapters": [],
        }
    ]
    config_payload = {
        "locales": ["en"],
        "unicodeLocales": ["en"],
        "maxChapters": 250,
        "maxSeconds": 5400,
        "maxBytes": 1 << 30,
        "accepts": ["aac", "m4a", "mp3"],
        "stageWarning": False,
        "paypalClientId": "x",
        "ssoEnabled": True,
    }

    def fake_transport(method, url, *, headers=None, json=None, **kwargs):
        if method == "POST" and url.endswith("/file"):
            return _FakeTransportResponse(500, {})
        if url.endswith("/config"):
            return _FakeTransportResponse(200, config_payload)
        if url.endswith("/households"):
            return _FakeTransportResponse(200, household_payload)
        if url.endswith("/creativetonies"):
            return _FakeTransportResponse(200, tonie_payload)
        raise AssertionError(f"unexpected transport call: {method} {url}")

    api = _wire_real_tonie_api_with_fake_transport(fake_transport)
    sink = ToniesCloudSink(api, sleep=lambda s: None, clock=lambda: 0.0)
    [target] = sink.list_targets()

    with pytest.raises(SessionError):
        sink.upload(target, Path("/c/new.m4a"), "New")


# --- Finding 4: the most consequential swallowed failure of all — a
# non-2xx on add_chapter_to_tonie's POST happens *after* the S3 upload has
# already succeeded and *after* clear() has already emptied the tonie, so a
# swallowed failure here leaves a child's tonie blank at bedtime while the
# run believes it succeeded.


def test_add_chapter_raises_on_non2xx_response_never_swallowed(monkeypatch, tmp_path):
    household_payload = [{"id": "H", "name": "Home", "ownerName": "K", "access": "owner", "canLeave": False}]
    tonie_payload = [
        {
            "id": "T100",
            "householdId": "H",
            "name": "Green Tonie",
            "imageUrl": "",
            "secondsRemaining": 0.0,
            "secondsPresent": 5340.0,
            "chaptersRemaining": 249,
            "chaptersPresent": 1,
            "transcoding": False,
            "lastUpdate": None,
            "chapters": [],
        }
    ]
    config_payload = {
        "locales": ["en"],
        "unicodeLocales": ["en"],
        "maxChapters": 250,
        "maxSeconds": 5400,
        "maxBytes": 1 << 30,
        "accepts": ["aac", "m4a", "mp3"],
        "stageWarning": False,
        "paypalClientId": "x",
        "ssoEnabled": True,
    }
    # What `_post("file")` returns on success -- consumed by `tonie_api`
    # itself to build the (bypassed-by-design) S3 upload request.
    file_upload_payload = {
        "request": {"url": "https://s3.example.invalid/bucket", "fields": {"key": "abc"}},
        "fileId": "file-123",
    }

    def fake_transport(method, url, *, headers=None, json=None, **kwargs):
        if method == "POST" and url.endswith("/file"):
            return _FakeTransportResponse(200, file_upload_payload)
        if method == "POST" and url.endswith("/chapters"):
            # add_chapter_to_tonie's POST -- the failure this test targets.
            return _FakeTransportResponse(500, {})
        if url.endswith("/config"):
            return _FakeTransportResponse(200, config_payload)
        if url.endswith("/households"):
            return _FakeTransportResponse(200, household_payload)
        if url.endswith("/creativetonies"):
            return _FakeTransportResponse(200, tonie_payload)
        raise AssertionError(f"unexpected transport call: {method} {url}")

    class _FakeS3Response:
        def raise_for_status(self):
            pass

    # The S3 leg bypasses `_LiveSessionAdapter`/`Session` entirely by
    # design (a bare `requests.post` inside `tonie_api`, per the module
    # docstring's "large payloads still bypass the session" note) -- stub
    # it directly rather than routing it through `fake_transport`, keeping
    # this test network-free without pretending that leg goes through the
    # session.
    import tonie_api.api as tonie_api_module

    monkeypatch.setattr(tonie_api_module.requests, "post", lambda *a, **k: _FakeS3Response())

    api = _wire_real_tonie_api_with_fake_transport(fake_transport)
    sink = ToniesCloudSink(api, sleep=lambda s: None, clock=lambda: 0.0)
    [target] = sink.list_targets()

    upload_path = tmp_path / "new.m4a"
    upload_path.write_bytes(b"fake-audio")

    with pytest.raises(SessionError):
        sink.upload(target, upload_path, "New")


# --- Finding 3: Session.bearer_token() is a public, owned accessor --------


def test_bearer_token_raises_when_no_token_installed():
    from boxbutler.sinks.session import Session

    session = Session(
        authenticate=lambda: (_ for _ in ()).throw(AssertionError("must not authenticate")),
        refresh=lambda rt: (_ for _ in ()).throw(AssertionError("must not refresh")),
        transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    # Simulate a session that was never actually able to install a token
    # (its normal `ensure_fresh` would authenticate-or-raise; here it's
    # stubbed to a no-op so `_token` stays `None`) -- `bearer_token()` must
    # raise rather than return `None`/`""` for that state.
    session.ensure_fresh = lambda: None
    with pytest.raises(SessionError, match="no token available"):
        session.bearer_token()


# --- verify_login (protocol method the brief's sketch omits) ---


def test_verify_login_accepts_good_credentials(monkeypatch):
    import boxbutler.sinks.tonies_cloud as m

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "t"}

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Resp())
    api, s = mk()
    assert s.verify_login("user@example.com", "correct") is True


def test_verify_login_rejects_bad_credentials(monkeypatch):
    import boxbutler.sinks.tonies_cloud as m

    class _Resp:
        status_code = 401

        def raise_for_status(self):
            raise AssertionError("must not be called for a 401")

        def json(self):
            raise AssertionError("must not be called for a 401")

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Resp())
    api, s = mk()
    assert s.verify_login("user@example.com", "wrong") is False


def test_missing_limit_in_cloud_config_raises_rather_than_guessing():
    """Found on the first live deployment (2026-09-13).

    `tonie_api.get_config()` validates the whole response against its own
    pydantic model, which requires fields this project never reads. The live
    endpoint stopped returning one of them and every run died on a
    ValidationError about `paypalClientId` -- a field with no bearing on
    anything Box Butler does.

    Reading the raw dict fixes that, but it must not slide into the opposite
    error: a *missing* limit is unknown, not zero and not a sensible default.
    We clamp uploads against these numbers, so guessing one means guessing
    what a tonie will accept.
    """
    api, s = mk()
    full = api._get("config")

    for dropped in ("maxSeconds", "maxChapters", "maxBytes", "accepts"):
        partial = {k: v for k, v in full.items() if k != dropped}
        sink = ToniesCloudSink(NS(_get=lambda url, p=partial: p))
        with pytest.raises(SessionError) as e:
            _ = sink.limits
        assert dropped in str(e.value)


def test_unknown_extra_config_keys_are_ignored():
    """The live response carries locales, feature flags and billing fields we
    have no interest in. Tolerating them is the whole point of not depending
    on someone else's model of the entire payload.
    """
    api, s = mk()
    assert (s.limits.max_seconds, s.limits.max_chapters) == (5400, 250)
