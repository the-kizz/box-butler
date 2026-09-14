"""The cross-process run lock (final safety review, M1).

`OrchestratorRunner` already held a `threading.Lock`, which correctly stops
two UI actions or a UI action and the scheduled fire from overlapping — they
are threads in one process. It cannot see a *second process*, and the
documented remediation for a degraded tonie is exactly that:
`monitoring/alerts.yml` tells the operator to "run `boxbutler run --apply`
now if bedtime is near", and the commissioning steps use `docker exec
box-butler boxbutler run --assignment … --apply`. Do that at 15:00 while the
scheduler fires and both processes clear the same tonie: the review measured
two CLEARs, two UPLOADs and 10790 s of audio against a 5400 s cap, with the
loser settling to `chapter_count_mismatch` and going DEGRADED.

So there is one lock file under the data directory, and every entry point
that can reach a mutating sink call takes it: the UI's "Run now"/"Repair",
the scheduler's fired job, and the CLI — which now goes through the same
`OrchestratorRunner` as everything else rather than calling
`orchestrator.run` directly, so it inherits this automatically instead of
needing someone to remember.

`fcntl.flock` is the mechanism: it is advisory, per-open-file-description,
and — the property that matters here — released by the kernel when the
holding process dies, however it dies. A lock file containing a pid would
need a liveness check and a stale-lock policy; this needs neither, so a
SIGKILLed run cannot wedge every later one.

Both wait modes are supported because the callers genuinely want different
things, for the reasons already written into `OrchestratorRunner`:

* `timeout=0` — fail fast. Right for an HTTP request (the UI turns it into a
  409) and for the CLI, where an operator wants to be told "a run is already
  in progress" rather than watch a hung terminal.
* `timeout=N` — wait, then give up loudly. Right for the scheduled fire, the
  one run that must not be dropped just because something else held the lock
  for a moment.

The file is never deleted: removing it would let a second process create a
fresh inode and lock *that* instead, which is the classic way a lock file
stops being a lock. It is created once and kept, zero bytes.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: How long to wait between attempts while honouring a non-zero timeout.
#: `flock` has no timed variant, so waiting is a poll; 0.2 s is far below any
#: run's duration and far above the cost of the syscall.
POLL_INTERVAL_S = 0.2


class RunInProgress(RuntimeError):
    """Another run already holds the lock.

    Raised instead of `fastapi.HTTPException` so that nothing below the web
    layer has to import FastAPI to report contention: `boxbutler.web.app`
    registers a handler that turns this into the same HTTP 409 the UI has
    always produced, and `boxbutler.cli.main` prints it and exits non-zero.
    """


class RunLock:
    """An `flock`-based mutual exclusion over one lock file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def held(self, *, timeout: float = 0.0, what: str = "a run") -> Iterator[None]:
        """Hold the lock for the duration of the block.

        `timeout=0` tries once and raises `RunInProgress` immediately;
        a positive `timeout` polls until it elapses and then raises. The
        caller decides which, because the right answer differs per entry
        point (see the module docstring).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise RunInProgress(
                            f"{what} is already in progress in another process "
                            f"(lock: {self.path})"
                        ) from e
                    time.sleep(POLL_INTERVAL_S)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = ["POLL_INTERVAL_S", "RunInProgress", "RunLock"]
