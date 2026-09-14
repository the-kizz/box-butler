"""Settle-polling helper (Task 19; spec §2 step 8, §3, §10.5).

`upload_file_to_tonie` returns before the file is usable: a freshly uploaded
chapter reads `seconds: 0.0, transcoding: True` while Boxine transcodes it
server-side, settling to its true duration roughly 45-60 s later. **The
`transcoding` flag is the settle signal; `seconds > 0` is only an
inference and must never be used as one** -- a `seconds > 0` test cannot
tell "still working" from "finished but zero-length", so it would wait out
the timeout on a genuinely empty result instead of failing fast (spec §3).

`poll_until_settled` polls `read_chapters()` until every chapter has
stopped transcoding, then verifies the summed duration against
`expect_seconds` within `tolerance_s`. It never reports `settled=True`
from a read where any chapter is still `transcoding`.

Distinguishing "still transcoding" from "finished but empty":
a single not-transcoding, zero-seconds read is genuinely ambiguous -- it
could be the instant before the cloud flips `transcoding` to `True`, which
is a real, transient state (proven by
`tests/orchestrator/test_settle.py::test_zero_seconds_not_flagged_transcoding_still_waits`,
where the very next poll carries the real duration). So the helper grants
that state exactly one grace poll. If the *same* not-transcoding,
zero-seconds state is read again on the very next poll, it is not
transient -- it is §10.5's "finished but zero-length" failure -- and
`poll_until_settled` returns immediately with `settled=False`,
`reason="settled_empty"`, typically after a single `poll_interval_s`
rather than idling out `timeout_s`.

No real sleeping: `sleep` and `clock` are both injectable (default
`time.sleep` / `time.monotonic`), and tests pass a fake clock whose
`sleep` advances its own `t` so the whole suite runs in well under a
second.
"""
from __future__ import annotations

import time
from typing import Callable

from boxbutler.domain.fitting import DURATION_TOLERANCE_S
from boxbutler.sinks.protocol import LiveChapter, SettleResult


def poll_until_settled(
    read_chapters: Callable[[], list[LiveChapter]],
    expect_seconds: float,
    timeout_s: int,
    *,
    tolerance_s: float = DURATION_TOLERANCE_S,
    poll_interval_s: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SettleResult:
    start = clock()
    consecutive_settled_zero = 0

    while True:
        chapters = list(read_chapters())
        total = sum(c.seconds for c in chapters)
        transcoding = any(c.transcoding for c in chapters)
        waited_s = clock() - start

        if not transcoding and total == 0.0:
            consecutive_settled_zero += 1
            if consecutive_settled_zero >= 2:
                return SettleResult(
                    settled=False,
                    seconds=total,
                    chapters=chapters,
                    waited_s=waited_s,
                    reason="settled_empty",
                )
        else:
            consecutive_settled_zero = 0

        if not transcoding and total != 0.0:
            if abs(total - expect_seconds) <= tolerance_s:
                return SettleResult(
                    settled=True, seconds=total, chapters=chapters, waited_s=waited_s, reason=None
                )
            return SettleResult(
                settled=False,
                seconds=total,
                chapters=chapters,
                waited_s=waited_s,
                reason="duration_mismatch",
            )

        # Still transcoding, or a single grace read of a not-yet-flagged zero.
        if waited_s >= timeout_s:
            return SettleResult(
                settled=False, seconds=total, chapters=chapters, waited_s=waited_s, reason="timeout"
            )
        sleep(poll_interval_s)
