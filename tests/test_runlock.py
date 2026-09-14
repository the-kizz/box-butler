"""The cross-process run lock (final safety review, M1).

The documented remediation for a degraded tonie is `docker exec box-butler
boxbutler run --apply`, and `monitoring/alerts.yml` says to use it "now if
bedtime is near" — i.e. at the hour the scheduler fires. That CLI invocation
is a *separate process*, so the `threading.Lock` inside `OrchestratorRunner`
could not see it, and the reviewer measured the result: two CLEARs against
one tonie, two UPLOADs, and 10790 s of audio against a 5400 s cap, with the
loser settling to `chapter_count_mismatch` and going DEGRADED.

`flock` is taken on a separate file descriptor per `RunLock.held()` call, so
two `RunLock` objects over the same path exclude each other exactly as two
processes do — which is what these tests drive. One test does use a real
second process, because "the kernel releases it when the holder dies" is the
property that makes a lock file safe without a stale-lock policy, and only a
real process death proves it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from boxbutler.runlock import RunInProgress, RunLock


def test_second_holder_fails_fast_with_a_clear_message(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path).held(timeout=0.0, what="a run"):
        with pytest.raises(RunInProgress) as e:
            with RunLock(path).held(timeout=0.0, what="a run"):
                pytest.fail("two holders at once")
    assert "a run is already in progress in another process" in str(e.value)
    assert str(path) in str(e.value)


def test_the_lock_is_released_when_the_block_ends(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path).held(timeout=0.0):
        pass
    with RunLock(path).held(timeout=0.0):
        pass            # no exception: a finished run never blocks the next one


def test_a_waiting_caller_gets_the_lock_when_it_frees(tmp_path):
    """The scheduled fire waits rather than losing the night (M1)."""
    import threading

    path = tmp_path / "run.lock"
    first = RunLock(path)
    got_it = []

    def waiter():
        with RunLock(path).held(timeout=5.0):
            got_it.append(True)

    with first.held(timeout=0.0):
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.5)
        assert got_it == [], "the waiter must not hold it while the first does"
    t.join(timeout=5.0)
    assert got_it == [True]


def test_waiting_gives_up_loudly_rather_than_waiting_forever(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path).held(timeout=0.0):
        started = time.monotonic()
        with pytest.raises(RunInProgress):
            with RunLock(path).held(timeout=0.5):
                pytest.fail("two holders at once")
        assert time.monotonic() - started >= 0.4


def test_a_killed_holder_does_not_wedge_every_later_run(tmp_path):
    """A real second process, SIGKILLed while holding the lock.

    This is why the lock is an `flock` and not a pid file: the kernel drops
    it when the holder dies, however it dies, so a SIGKILLed run cannot leave
    a lock nobody can clear — no liveness check, no stale-lock policy, no
    `--force` flag for an operator to get wrong at 7pm.
    """
    path = tmp_path / "run.lock"
    script = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from boxbutler.runlock import RunLock\n"
        "with RunLock(%r).held(timeout=0.0):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(60)\n"
    ) % (os.getcwd(), str(path))
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(RunInProgress):
            with RunLock(path).held(timeout=0.0):
                pytest.fail("two holders at once")
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Poll briefly: the kernel releases the lock as the process is reaped.
    deadline = time.monotonic() + 5
    while True:
        try:
            with RunLock(path).held(timeout=0.0):
                break
        except RunInProgress:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
