"""`verify_rendition` — is this rendered file fit to ship to a tonie?
(spec §2 step 4, §3.4)

## Why this module is biased to refuse

Spec §2 draws the line as "stage, verify, then swap": a tonie is never
cleared until its replacement is downloaded, trimmed, *and verified*.
Everything upstream of this module can fail safely — a bad fetch or a
killed render just leaves last night's story on the tonie untouched. This
module is the last thing standing before the orchestrator (Task 21) clears
a real tonie and uploads what this module approved. There is no "fail
safely" available to it: a false positive here puts silence, or half a
story, on a child's tonie at bedtime. So the posture is **refuse unless
demonstrably correct**, not "accept unless clearly broken".

## The failure family this is the backstop for

This project has already caught four variants of one bug at four different
layers: content of unknown or partial length accepted as finished, and the
run reporting success anyway.

1. A `.part` download accepted as a complete cache hit (Task 13).
2. A killed ffmpeg leaving a usable-looking output (Task 14).
3. `probe()` returning `seconds=0.0` for an unknown duration, read by
   `choose_mode` as "shorter than the cap" (Task 14).
4. `fit_fill` emitting a zero-length "truncated" piece (Task 2).

The rule established across this build is: **there is no sentinel for
unknown duration — raise.** `FfmpegRenderer.probe()` (Task 14) now raises
`RenderError` rather than returning `0.0`. This module relies on that (it
never re-derives duration itself — `renderer.probe()` is the only source
of truth for `seconds`), but does not *only* rely on it: a renderer that
misbehaves and hands back `0.0` (or any non-positive value) still fails
here, via the `too_short` check below, rather than being read as "shorter
than the cap, therefore fine".

## What is checked, and in what order

Order matters — see `test_over_cap_checked_before_duration_mismatch` for
why a specific, actionable reason should win over a generic one when both
would fire:

1. `missing` — the path does not exist at all.
2. `empty` — the path exists but is zero bytes.
3. `transcoding_artifact` — the file is a stray `.part` fragment (the
   exact temp-name shape `boxbutler/audio/ffmpeg.py` uses for an
   in-progress render, e.g. `story.part.m4a`) rather than a finished
   rendition. A killed render's temp file must never be verified as if it
   were the real output, even if its partial contents happen to probe
   cleanly — this is the "leftover from a previous run" case the task
   brief calls out explicitly, and it is cheap to catch before ever
   invoking the renderer.
4. `unreadable` — `renderer.probe()` raised. A file that exists and is
   non-empty but can't be decoded is exactly as dangerous as a missing
   one, and this is the only place "is this actually decodable, not just
   present" gets checked.
5. `not_finite` — the probed duration is NaN specifically (not +/-inf:
   those are left to the ordinary threshold checks below, which already
   classify them correctly — `+inf` as `over_cap`, `-inf` as `too_short`).
   Every threshold comparison below (`<`, `>`, `abs(...) >`) is `False`
   against NaN, so without this explicit guard a NaN duration would fall
   through every remaining check and pass as `ok=True` — nothing else in
   this function catches it.
6. `too_short` — the probed duration is below `MIN_SECONDS`. Deliberately
   compares against the *probed* duration, not `expect_seconds`, so a
   renderer that regresses to returning `0.0` (or any trivial length) for
   unknown-duration content cannot pass no matter what the caller expected.
7. `over_cap` — probed duration exceeds `limits.max_seconds`. Checked
   before `duration_mismatch` on purpose: a rendition that blows the sink's
   hard cap must never ship, even in the case where `expect_seconds` was
   equally wrong and the two would otherwise "agree".
8. `over_bytes` — file size exceeds `limits.max_bytes`.
9. `duration_mismatch` — probed duration is not within `tolerance_s` of
   `expect_seconds`. This is the last check: only a file that is present,
   non-empty, not a stray temp artifact, decodable, non-trivial, under both
   sink caps, and *still* doesn't match what the caller intended, gets this
   most-generic reason.

Anything that isn't one of the above is `ok=True`.

## What this function cannot check, and who checks it instead

`expect_seconds` is **not an independent expectation**, and this module must
not be read as if it were. On the staging path every number in the chain
descends from one `renderer.probe()` of one fetched file: `_stage_plan`
measures the source, `fit_fill` derives `take_seconds` from that
measurement, and that same number arrives here as `expect_seconds`. The
comparison below is therefore self-consistent by construction — it catches a
*render* that lost time, and it can never catch a *fetch* that arrived
short. A 3600 s episode that downloaded as 900 s passes every check in this
function, correctly, because 900 s is exactly what the caller asked for
(final safety review, M2).

Two independent checks sit outside this module and are the ones that catch
that case:

1. `boxbutler.fetch.http.HttpFetcher.fetch` compares bytes written against
   the server's stated `Content-Length` and refuses a short body.
2. `Orchestrator._probe_source` compares the probed source duration against
   `item.seconds` — the feed's own `<itunes:duration>`, recorded before the
   file was ever downloaded — and raises rather than clearing a tonie for a
   file that is materially shorter than the feed says.

Where a feed published no duration and a server stated no length, there is
no independent expectation at all, and the honest statement is that this
file's *completeness* is unverified even though its fitness to ship is not.
Saying so here is the point: self-consistency must not be allowed to
masquerade as verification.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from boxbutler.audio.protocol import RendererProtocol
from boxbutler.domain.fitting import DURATION_TOLERANCE_S
from boxbutler.sinks.protocol import SinkLimits

# A rendition shorter than this is never fit to ship, regardless of what
# the caller expected — see the module docstring's failure-family note #3.
# There is no sentinel for "duration unknown"; a renderer that produces one
# anyway lands here, not in a passing result.
MIN_SECONDS = 30.0


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    seconds: float
    bytes: int
    reason: str | None


def _is_stray_part_file(path: Path) -> bool:
    # Anchored to the two real conventions rather than a bare ".part in
    # name" substring check, so a sanitised title that happens to contain
    # the literal text ".part" (sanitise_title does not strip periods,
    # e.g. an item titled "Non-Stop.Part Two") can never false-positive:
    #   - boxbutler/audio/ffmpeg.py's in-progress-render temp name:
    #     `dst.stem + ".part" + dst.suffix`, e.g. "story.part.m4a" — the
    #     second-to-last suffix is literally ".part".
    #   - boxbutler/fetch/http.py's in-progress-download temp name:
    #     `target.suffix + ".part"`, e.g. "story.mp3.part" — the whole
    #     name ends in ".part".
    suffixes = path.suffixes
    return path.name.endswith(".part") or (len(suffixes) >= 2 and suffixes[-2] == ".part")


def verify_rendition(
    path: Path,
    expect_seconds: float,
    renderer: RendererProtocol,
    limits: SinkLimits,
    tolerance_s: float = DURATION_TOLERANCE_S,
) -> VerifyResult:
    if not path.exists():
        return VerifyResult(False, 0.0, 0, "missing")

    size = path.stat().st_size
    if size == 0:
        return VerifyResult(False, 0.0, 0, "empty")

    if _is_stray_part_file(path):
        return VerifyResult(False, 0.0, size, "transcoding_artifact")

    try:
        probe = renderer.probe(path)
    except Exception:
        return VerifyResult(False, 0.0, size, "unreadable")

    seconds = probe.seconds

    # NaN compares False against every threshold below (seconds < X,
    # seconds > X, abs(...) > X are all False for NaN), so without this
    # guard a NaN duration would fall through every check and pass as
    # ok=True. This is the same failure family as the 0.0-sentinel bug
    # fixed in Task 14: "no sentinel for unknown duration, raise" is
    # enforced at the source (FfmpegRenderer.probe()), but this is the
    # last gate before a real tonie is cleared — it must not assume the
    # layer below is reliable, only verify that this file is fine.
    #
    # Checked as `isnan` specifically, not the broader `not isfinite`:
    # +/-inf are already handled correctly by the ordinary threshold
    # checks below (+inf > limits.max_seconds -> over_cap; -inf <
    # MIN_SECONDS -> too_short), and those are the *right* reasons for
    # them — a blanket finiteness guard placed ahead of those checks would
    # steal inf's classification into a less specific "not_finite" for no
    # benefit. NaN alone survives every comparison and needs its own
    # reason, both because nothing else below will catch it and because a
    # garbage measurement reads differently in a run log than an
    # oversized one.
    if math.isnan(seconds):
        return VerifyResult(False, seconds, size, "not_finite")

    if seconds < MIN_SECONDS:
        return VerifyResult(False, seconds, size, "too_short")

    if seconds > limits.max_seconds:
        return VerifyResult(False, seconds, size, "over_cap")

    if size > limits.max_bytes:
        return VerifyResult(False, seconds, size, "over_bytes")

    if abs(seconds - expect_seconds) > tolerance_s:
        return VerifyResult(False, seconds, size, "duration_mismatch")

    return VerifyResult(True, seconds, size, None)
