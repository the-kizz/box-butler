"""Tests for FakeSink (Task 9; spec §3, §11 P2).

R9 (deviation from the task-9 brief, see task-9-report.md): the brief's
sketch calls the chapter-read method `snapshot()`, but `SinkProtocol`
(boxbutler/sinks/protocol.py) defines it as `read_chapters()` — `snapshot`
in this codebase means the write-once file written before a tonie is
cleared (Task 19). The task-9 instructions explicitly flag this exact
discrepancy and say the real protocol wins, so these tests exercise
`read_chapters()`, not `snapshot()`.
"""
from pathlib import Path

from boxbutler.sinks.fake import FakeSink
from boxbutler.sinks.protocol import LiveChapter


def test_records_calls_and_state():
    s = FakeSink()
    t = s.add_target("T1", "Adventure Tonie", [LiveChapter("c1", "Old", 5340.0, False)])

    snap = s.read_chapters(t)
    assert [c.id for c in snap.chapters] == ["c1"]

    s.clear(t)
    s.upload(t, Path("/cache/x.m4a"), "New")

    assert s.calls[:3] == [
        ("read_chapters", "T1"),
        ("clear", "T1"),
        ("upload", "T1", "/cache/x.m4a", "New"),
    ]
    assert s.chapters["T1"][0].transcoding is True
    assert s.chapters["T1"][0].seconds == 0.0

    r = s.settle(t, 5340.0, timeout_s=1)
    assert r.settled and s.chapters["T1"][0].seconds == 5340.0


def test_limits_default_to_measured_cloud_values():
    assert FakeSink().limits.max_seconds == 5400
    assert FakeSink().limits.max_chapters == 250


def test_upload_appends_transcoding_chapter_with_zero_seconds():
    s = FakeSink()
    t = s.add_target("T2", "Green Tonie")
    s.upload(t, Path("/cache/a.m4a"), "A")
    s.upload(t, Path("/cache/b.m4a"), "B")
    ids = [c.id for c in s.chapters["T2"]]
    assert ids == ["ch0", "ch1"]
    assert all(c.transcoding and c.seconds == 0.0 for c in s.chapters["T2"])


def test_calls_named_filters_by_method():
    s = FakeSink()
    t = s.add_target("T3", "Music Tonie")
    s.read_chapters(t)
    s.upload(t, Path("/cache/c.m4a"), "C")
    s.upload(t, Path("/cache/d.m4a"), "D")
    assert len(s.calls_named("upload")) == 2
    assert len(s.calls_named("read_chapters")) == 1


def test_fail_clear_raises_and_leaves_chapters_untouched():
    s = FakeSink(fail_clear=True)
    t = s.add_target("T4", "Red Tonie", [LiveChapter("c1", "Old", 300.0, False)])
    try:
        s.clear(t)
        assert False, "expected clear to raise"
    except Exception:
        pass
    assert len(s.chapters["T4"]) == 1


def test_fail_upload_times_then_recovers():
    s = FakeSink(fail_upload_times=1)
    t = s.add_target("T5", "Blue Tonie")
    try:
        s.upload(t, Path("/cache/e.m4a"), "E")
        assert False, "expected first upload to raise"
    except Exception:
        pass
    assert s.chapters["T5"] == []
    s.upload(t, Path("/cache/e.m4a"), "E")
    assert len(s.chapters["T5"]) == 1


def test_settle_seconds_override():
    """Task 19 note: `settle()` now actually verifies the settled duration
    against `expect_seconds` (spec §3, §10.5) rather than always reporting
    success, so this asserts the override is honoured when it *does*
    match what was expected -- the mismatching case is covered by
    test_fake_sink_wrong_duration_fails_settle below.
    """
    s = FakeSink(settle_seconds_override=42.0)
    t = s.add_target("T6", "Blue Tonie")
    s.upload(t, Path("/cache/f.m4a"), "F")
    r = s.settle(t, expect_seconds=42.0, timeout_s=1)
    assert r.settled and r.seconds == 42.0
    assert s.chapters["T6"][0].seconds == 42.0


def test_fake_sink_simulates_transcoding_then_settles():
    """Task 19: settle() now polls, rather than resolving on the first call."""
    s = FakeSink(settle_after_polls=2)
    t = s.add_target("T1", "x")
    s.upload(t, Path("/c/a.m4a"), "A")
    r = s.settle(t, 5340.0, timeout_s=180)
    assert r.settled and r.waited_s > 0 and s.chapters["T1"][0].seconds == 5340.0


def test_fake_sink_wrong_duration_fails_settle():
    s = FakeSink(settle_seconds_override=4000.0)
    t = s.add_target("T1", "x")
    s.upload(t, Path("/c/a.m4a"), "A")
    assert s.settle(t, 5340.0, timeout_s=60).settled is False


def test_fake_sink_settled_but_empty_fails_fast_not_at_timeout():
    """spec §10.5: transcoding false, seconds 0.0 must fail promptly, not
    idle out a 180s timeout -- proves poll_until_settled's fail-fast path
    is reachable through FakeSink, not only in the helper's own unit tests.
    """
    s = FakeSink(settle_seconds_override=0.0)
    t = s.add_target("T1", "x")
    s.upload(t, Path("/c/a.m4a"), "A")
    r = s.settle(t, 5340.0, timeout_s=180)
    assert r.settled is False
    assert r.waited_s <= 10.0
    assert r.reason not in (None, "timeout")


def test_fake_sink_settle_splits_expected_seconds_across_pending_chapters():
    s = FakeSink()
    t = s.add_target("T1", "x")
    s.upload(t, Path("/c/a.m4a"), "A")
    s.upload(t, Path("/c/b.m4a"), "B")
    r = s.settle(t, 5340.0, timeout_s=60)
    assert r.settled
    assert [c.seconds for c in s.chapters["T1"]] == [2670.0, 2670.0]


def test_fake_sink_settle_only_uses_fake_time():
    """No test may sleep in real time (binding constraint) -- 180 s worth of
    simulated polling must still run near-instantly."""
    import time

    s = FakeSink(settle_after_polls=30)
    t = s.add_target("T1", "x")
    s.upload(t, Path("/c/a.m4a"), "A")
    started = time.monotonic()
    r = s.settle(t, 5340.0, timeout_s=200)
    assert time.monotonic() - started < 1.0
    assert r.settled
