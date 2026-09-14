import io
import json
from datetime import UTC, datetime

from boxbutler.logging import log_event, make_logger


def test_shape_matches_prototype():
    buf = io.StringIO()
    fixed = datetime(2026, 9, 11, 5, 0, 0, tzinfo=UTC)
    log_event("upload", stream=buf, clock=lambda: fixed, tonie="Green Tonie", seconds=5340.0)
    rec = json.loads(buf.getvalue())
    assert list(rec)[:2] == ["ts", "event"]
    assert rec["ts"] == "2026-09-11T05:00:00+00:00"
    assert rec["tonie"] == "Green Tonie"


def test_ensure_ascii_false_keeps_unicode_literal():
    buf = io.StringIO()
    fixed = datetime(2026, 9, 11, 5, 0, 0, tzinfo=UTC)
    log_event("upload", stream=buf, clock=lambda: fixed, title="Story \N{SPAGHETTI}")
    raw = buf.getvalue()
    assert "\\u" not in raw
    assert "\N{SPAGHETTI}" in raw


def test_flushes_immediately():
    class TrackingStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushed = False

        def flush(self):
            self.flushed = True
            return super().flush()

    stream = TrackingStream()
    log_event("x", stream=stream, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    assert stream.flushed


def test_password_and_token_fields_are_redacted():
    buf = io.StringIO()
    log_event(
        "login",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        username="alice",
        password="hunter2",
        api_token="sk-live-abc123",
        nested={"Authorization": "should-not-appear"},
    )
    raw = buf.getvalue()
    rec = json.loads(raw)
    assert rec["username"] == "alice"
    assert "hunter2" not in raw
    assert "sk-live-abc123" not in raw
    assert "should-not-appear" not in raw
    assert rec["password"] == "***REDACTED***"
    assert rec["api_token"] == "***REDACTED***"
    assert rec["nested"]["Authorization"] == "***REDACTED***"


def test_bearer_token_embedded_in_error_string_is_scrubbed():
    buf = io.StringIO()
    log_event(
        "fetch_error",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        error="401 for https://x/api: Authorization: Bearer sk-abcDEF123.token-part",
    )
    raw = buf.getvalue()
    assert "sk-abcDEF123" not in raw
    assert "Bearer ***REDACTED***" in raw


def test_credentialed_url_is_scrubbed():
    buf = io.StringIO()
    log_event(
        "fetch_error",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        error="connect to https://user:s3cr3t@example.com/path failed",
    )
    raw = buf.getvalue()
    assert "s3cr3t" not in raw
    assert "user" not in raw


def test_non_mapping_object_with_password_field_is_redacted():
    """Final review, Minor 3 (bypass): key-based redaction only inspects
    `Mapping` keys, so a non-mapping object such as `Creds("admin",
    "hunter2")` has no keys for it to look at and used to reach the log
    line untouched via its repr — `"Creds(user='admin',
    password='hunter2')"`. The value-shaped scrub must catch the
    `password=...` pattern regardless of what object it came from."""

    class Creds:
        def __init__(self, user, password):
            self.user = user
            self.password = password

        def __repr__(self):
            return f"Creds(user={self.user!r}, password={self.password!r})"

    buf = io.StringIO()
    log_event(
        "x",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        detail=Creds("admin", "hunter2"),
    )
    raw = buf.getvalue()
    assert "hunter2" not in raw
    assert "***REDACTED***" in raw


def test_hostile_mapping_raising_on_iteration_does_not_crash_log_event():
    """Final review, Minor 3 (crash): `_sanitise(fields)` used to sit
    outside the try/except that only wrapped `json.dumps`, so a mapping
    or list whose iteration itself raises propagated straight out of
    `log_event` — and `Orchestrator._event` has no try/except of its own,
    so the logger would take down the run it was describing. The logger
    is the diagnostic channel and must degrade, not raise louder."""

    class HostileMapping(dict):
        def items(self):
            raise RuntimeError("boom")

    buf = io.StringIO()
    log_event(
        "x",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        payload=HostileMapping(a=1),
    )
    raw = buf.getvalue()
    assert raw.count("\n") == 1
    rec = json.loads(raw)
    assert rec["event"] == "x"
    assert rec["ts"] == "2026-01-01T00:00:00+00:00"
    assert "log_error" in rec


def test_make_logger_matches_deps_log_signature():
    buf = io.StringIO()
    logger = make_logger(stream=buf)
    logger("upload", {"tonie": "Green Tonie"})
    rec = json.loads(buf.getvalue())
    assert rec["event"] == "upload"
    assert rec["tonie"] == "Green Tonie"


def test_non_serialisable_field_still_yields_one_valid_json_line():
    """Task 27 review, Critical 3 (confirmed empirically): a field
    json.dumps can't serialise used to crash log_event with an uncaught
    TypeError, taking down the orchestrator run it was logging about."""
    class Weird:
        def __repr__(self):
            return "<Weird thing>"

    buf = io.StringIO()
    log_event(
        "x",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        thing=Weird(),
        tonie="Green Tonie",
    )
    raw = buf.getvalue()
    assert raw.count("\n") == 1
    rec = json.loads(raw)
    assert rec["event"] == "x"
    assert rec["tonie"] == "Green Tonie"
    assert "Weird" in rec["thing"]


def test_field_whose_repr_raises_still_yields_a_line():
    """The harder case: not just an unserialisable value, but one whose
    __repr__ itself raises. The fallback must not raise either."""
    class Cursed:
        def __repr__(self):
            raise RuntimeError("nope")

    buf = io.StringIO()
    log_event(
        "x",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        thing=Cursed(),
    )
    raw = buf.getvalue()
    assert raw.count("\n") == 1
    rec = json.loads(raw)
    assert rec["event"] == "x"
    assert "thing" in rec


def test_non_serialisable_field_is_still_redacted():
    """The fallback repr path must still go through redaction — a value
    that fails serialisation must not slip past _sanitise into the log
    line just because it took the fallback branch."""
    class Weird:
        def __repr__(self):
            return "token=Bearer sk-abcDEF123.token-part"

    buf = io.StringIO()
    log_event(
        "x",
        stream=buf,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        error=Weird(),
    )
    raw = buf.getvalue()
    assert "sk-abcDEF123" not in raw
    assert "Bearer ***REDACTED***" in raw
