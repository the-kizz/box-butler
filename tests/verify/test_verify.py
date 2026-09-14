"""Tests for `verify_rendition` — the last gate before a tonie is cleared
(spec §2 step 4, §3.4). See `boxbutler/verify/verify.py` module docstring
for the fail-closed rationale.

No real ffmpeg/ffprobe here: every renderer is `FakeRenderer` (or a small
subclass raising to simulate an unreadable file), per the project-wide rule
that the default suite never invokes real ffmpeg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import math

from boxbutler.sinks.protocol import SinkLimits
from boxbutler.verify.verify import MIN_SECONDS, VerifyResult, verify_rendition
from tests.audio.fakes import FakeRenderer

LIM = SinkLimits(5400, 250, 1 << 30, ("m4a",))


def test_good_rendition(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x" * 10)
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": 5340.4}), LIM)
    assert r.ok and r.seconds == 5340.4
    assert r.reason is None
    assert r.bytes == 10


def test_missing_or_empty(tmp_path):
    r_missing = verify_rendition(tmp_path / "nope.m4a", 5340, FakeRenderer(), LIM)
    assert not r_missing.ok
    assert r_missing.reason == "missing"

    p = tmp_path / "e.m4a"
    p.write_bytes(b"")
    r_empty = verify_rendition(p, 5340, FakeRenderer(), LIM)
    assert not r_empty.ok
    assert r_empty.reason == "empty"


def test_duration_mismatch(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": 5000.0}), LIM)
    assert not r.ok
    assert r.reason == "duration_mismatch"


def test_over_cap_never_ships(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5401, FakeRenderer({"r.m4a": 5401.0}), LIM)
    assert not r.ok
    assert r.reason == "over_cap"


def test_too_short(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 3, FakeRenderer({"r.m4a": 3.0}), LIM)
    assert not r.ok
    assert r.reason == "too_short"


def test_unreadable(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")

    class Boom(FakeRenderer):
        def probe(self, path):
            raise RuntimeError("bad file")

    r = verify_rendition(p, 5340, Boom(), LIM)
    assert not r.ok
    assert r.reason == "unreadable"


# --- Additional tests for the failure family this module is a backstop for ---


def test_zero_duration_from_probe_is_not_a_pass(tmp_path):
    """A probe implementation that (wrongly) returns 0.0 for an unknown
    duration must never be read as "shorter than the cap" and passed. The
    real FfmpegRenderer.probe() now raises instead of returning 0.0 (Task
    14) — but this module must not rely on that alone: if *any* renderer
    hands back a non-positive duration, that is a failed verification, not
    a passed one with a zero. This is the same failure family as
    choose_mode's old `probe.seconds <= cap` bug and fit_fill's zero-length
    truncated-piece bug.
    """
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": 0.0}), LIM)
    assert not r.ok
    assert r.reason == "too_short"


def test_over_bytes(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x" * 100)
    tiny_limit = SinkLimits(5400, 250, 50, ("m4a",))
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": 5340.0}), tiny_limit)
    assert not r.ok
    assert r.reason == "over_bytes"


def test_stray_part_file_is_a_transcoding_artifact(tmp_path):
    """A leftover `.part` file (the exact temp-name shape
    `boxbutler/audio/ffmpeg.py` uses for an in-progress render, e.g.
    `story.part.m4a`) must never be verified as if it were a finished
    rendition, even if its contents happen to probe cleanly. This is the
    "leftover from a previous run" case the brief calls out explicitly."""
    p = tmp_path / "story.part.m4a"
    p.write_bytes(b"x" * 10)
    r = verify_rendition(p, 5340, FakeRenderer({"story.part.m4a": 5340.0}), LIM)
    assert not r.ok
    assert r.reason == "transcoding_artifact"


def test_over_cap_checked_before_duration_mismatch(tmp_path):
    """Order matters (brief step 3): over_cap must win even when the
    mismatch would *also* fail the tolerance check, so the more specific,
    more actionable reason ("this is simply too long to ever ship") is
    reported rather than a generic mismatch."""
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    # expect_seconds also 5401 -> would be a perfect "match" by tolerance,
    # but over_cap must still fire first.
    r = verify_rendition(p, 5401, FakeRenderer({"r.m4a": 5401.0}), LIM)
    assert r.reason == "over_cap"


def test_within_tolerance_boundary_passes(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340.0, FakeRenderer({"r.m4a": 5342.0}), LIM)
    assert r.ok


def test_just_outside_tolerance_boundary_fails(tmp_path):
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340.0, FakeRenderer({"r.m4a": 5342.1}), LIM)
    assert not r.ok
    assert r.reason == "duration_mismatch"


def test_nan_duration_does_not_pass(tmp_path):
    """Every threshold comparison (<, >, abs(...) >) is False against
    NaN, so without an explicit finiteness guard a NaN duration would
    fall through every check in verify_rendition and pass as ok=True.
    This is the same failure family as the 0.0-duration-sentinel bug
    fixed in Task 14 — the "no sentinel for unknown duration, raise" rule
    is enforced at FfmpegRenderer.probe(), but this module is the last
    gate and must not assume that renderer is the only one that will ever
    call it."""
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": math.nan}), LIM)
    assert not r.ok
    assert r.reason == "not_finite"


def test_inf_duration_caught_by_over_cap(tmp_path):
    """inf is already > any real limits.max_seconds, so it is caught by
    over_cap rather than needing its own path — pinned here so a future
    refactor of check order cannot silently change which branch catches
    it without a test noticing."""
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5340, FakeRenderer({"r.m4a": math.inf}), LIM)
    assert not r.ok
    assert r.reason == "over_cap"


def test_part_substring_in_legitimate_title_does_not_false_positive(tmp_path):
    """The `.part` check is anchored to the two real temp-file
    conventions (stem + '.part' + suffix, and suffix + '.part'), not a
    bare substring match — sanitise_title() does not strip periods, so a
    real item title containing the literal text ".part" (e.g. a filename
    derived from "Non-Stop.Part Two") must not be refused as a stray
    render artifact."""
    p = tmp_path / "Non-Stop.Part Two [abc123].m4a"
    p.write_bytes(b"x" * 10)
    r = verify_rendition(
        p, 5340, FakeRenderer({"Non-Stop.Part Two [abc123].m4a": 5340.0}), LIM
    )
    assert r.ok


def test_over_configured_cap_but_under_sink_cap_passes(tmp_path):
    """Distinguishes the configured render cap (~5395s, DEFAULT_CAP_SECONDS
    in boxbutler/domain/fitting.py) from the sink's hard limit (5400s,
    LIM.max_seconds here). verify_rendition only ever checks against
    limits.max_seconds — a duration over the configured cap but still
    under the sink's real limit must pass. (The existing over_cap tests
    use 5401, which cannot distinguish "capped at 5400" from "capped at
    5395"; this pins the distinction the code already gets right.)"""
    p = tmp_path / "r.m4a"
    p.write_bytes(b"x")
    r = verify_rendition(p, 5398, FakeRenderer({"r.m4a": 5398.0}), LIM)
    assert r.ok


def test_min_seconds_constant():
    assert MIN_SECONDS == 30.0


def test_verify_result_is_frozen_dataclass():
    r = VerifyResult(ok=True, seconds=1.0, bytes=1, reason=None)
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]
