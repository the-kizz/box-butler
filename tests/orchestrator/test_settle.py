"""Tests for the settle-polling helper (Task 19; spec §2 step 8, §3, §10.5).

`upload_file_to_tonie` returns before the file is usable: a chapter reads
`seconds: 0.0, transcoding: True` while Boxine transcodes server-side,
settling ~45-60 s later. `transcoding` is the settle signal; `seconds > 0`
is only an inference and must never be used as one (spec §3) -- so these
tests assert the helper polls on the boolean, verifies the settled
duration, and fails *fast* rather than idling out the timeout when a
result settles to something wrong (or to nothing at all, §10.5).

No test sleeps in real time: `Clock` is an injected fake clock whose
`sleep` advances its own `t` instead of blocking, and is passed as both
`sleep=` and `clock=` so the helper's own elapsed-time bookkeeping reads
from the same fake timeline.
"""
from boxbutler.orchestrator.settle import poll_until_settled
from boxbutler.sinks.protocol import LiveChapter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def reader(script):
    it = iter(script)
    last = {}

    def read():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    return read


def test_polls_past_transcoding_then_settles():
    c = Clock()
    script = [[LiveChapter("c", "T", 0.0, True)]] * 3 + [[LiveChapter("c", "T", 5340.0, False)]]
    r = poll_until_settled(reader(script), 5340.0, 180, sleep=c.sleep, clock=c, poll_interval_s=5)
    assert r.settled and r.seconds == 5340.0 and r.waited_s == 15.0


def test_wrong_settled_duration_is_a_failure_not_a_pass():
    c = Clock()
    r = poll_until_settled(reader([[LiveChapter("c", "T", 4000.0, False)]]), 5340.0, 180, sleep=c.sleep, clock=c)
    assert r.settled is False and r.reason == "duration_mismatch"
    # promptly: no polling was needed to know a settled-but-wrong result is wrong.
    assert r.waited_s == 0.0


def test_timeout_while_still_transcoding():
    c = Clock()
    r = poll_until_settled(
        reader([[LiveChapter("c", "T", 0.0, True)]]), 5340.0, 30, sleep=c.sleep, clock=c, poll_interval_s=10
    )
    assert r.settled is False and r.reason == "timeout" and r.waited_s >= 30


def test_zero_seconds_not_flagged_transcoding_still_waits():
    c = Clock()
    script = [[LiveChapter("c", "T", 0.0, False)], [LiveChapter("c", "T", 5340.0, False)]]
    assert poll_until_settled(reader(script), 5340.0, 60, sleep=c.sleep, clock=c).settled


def test_multi_chapter_sums():
    c = Clock()
    script = [[LiveChapter("a", "A", 3000.0, False), LiveChapter("b", "B", 2340.0, False)]]
    assert poll_until_settled(reader(script), 5340.0, 60, sleep=c.sleep, clock=c).settled


def test_settled_but_permanently_empty_fails_fast_not_at_timeout():
    """The exact §10.5 case: transcoding false, seconds 0.0, every read.

    A single not-transcoding zero read is ambiguous (it could be the
    instant before the cloud flags transcoding=True) so the helper grants
    it one grace poll -- proven by test_zero_seconds_not_flagged_transcoding_still_waits
    above, where the very next read carries a real duration. But if the
    *same* empty-and-settled state repeats, it is not transient, it is
    "finished but zero-length" (the failure mode `seconds > 0` cannot see,
    per spec §3), and must be reported well before a 180s timeout.
    """
    c = Clock()
    r = poll_until_settled(
        reader([[LiveChapter("c", "T", 0.0, False)]]), 5340.0, 180, sleep=c.sleep, clock=c, poll_interval_s=5
    )
    assert r.settled is False
    assert r.waited_s <= 10.0
    assert r.reason not in (None, "timeout")
