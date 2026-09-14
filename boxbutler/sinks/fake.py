"""In-memory fake implementing `SinkProtocol` (Task 9; spec §3, §11 P2).

Phase 2 runs entirely against this: nothing on a screen may touch a real
tonie, so every route and test in this phase is wired to `FakeSink`, never
`tonie_api`. No I/O happens here — no HTTP client, no ffmpeg, no real
tonie IDs.

Simulates the cloud's real awkward upload behaviour: a freshly-uploaded
chapter reads `seconds: 0.0, transcoding: True` immediately. `settle()`
(Task 19) delegates to `orchestrator.settle.poll_until_settled`, driven by
an internal fake clock so no test ever sleeps in real time: for the first
`settle_after_polls` polls the pending chapters still read back as
`transcoding=True, seconds=0.0`; from the next poll on they read as
settled, at `settle_seconds_override` if given, else `expect_seconds`
split evenly across the chapters that were pending when `settle()` was
called. `settle_after_polls=0` (the default) resolves on the first poll,
which is enough for Phase 2's fake-only screens and keeps every
pre-Task-19 test passing unchanged.

Every call to `read_chapters` / `clear` / `upload` / `settle` is recorded
to `.calls` (also filterable via `calls_named`) so tests can assert
exactly what was, or was not, done to a target.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from boxbutler.orchestrator.settle import poll_until_settled

from .protocol import LiveChapter, SettleResult, SinkLimits, Target, TargetSnapshot

DEFAULT_LIMITS = SinkLimits(
    max_seconds=5400,
    max_chapters=250,
    max_bytes=1 << 30,
    accepts=("aac", "aif", "aiff", "flac", "mp3", "m4a", "m4b", "wav", "oga", "ogg", "opus", "wma"),
)


class _FakeClock:
    """Drives `settle()`'s internal `poll_until_settled` call without ever
    sleeping in real time (binding constraint, Task 19). `t` only advances
    when `poll_until_settled` calls `sleep`; `__call__` reads it back as
    the clock, so `waited_s` on the returned `SettleResult` reflects
    simulated poll intervals, not wall time.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class SinkUploadError(RuntimeError):
    """Simulated upload failure (FakeSink(fail_upload_times=N))."""


class SinkClearError(RuntimeError):
    """Simulated clear failure (FakeSink(fail_clear=True))."""


class FakeSink:
    """Implements SinkProtocol in memory. Records every call in .calls as
    (method, target_id, *args).
    """

    #: `SinkProtocol.name` — the project-wide discriminator for fake
    #: assignment rows, so a row written against the fake sink is never
    #: mistaken for one against the real cloud account. Every other layer
    #: reads it from here rather than repeating the literal.
    name = "fake"

    def __init__(
        self,
        targets: list[Target] | None = None,
        limits: SinkLimits | None = None,
        settle_after_polls: int = 0,
        fail_upload_times: int = 0,
        fail_clear: bool = False,
        settle_seconds_override: float | None = None,
        valid_username: str | None = None,
        valid_password: str | None = None,
    ):
        self._targets: dict[str, Target] = {}
        self.chapters: dict[str, list[LiveChapter]] = {}
        self._limits = limits or DEFAULT_LIMITS
        self.settle_after_polls = settle_after_polls
        self.fail_upload_times = fail_upload_times
        self.fail_clear = fail_clear
        self.settle_seconds_override = settle_seconds_override
        # Task 36 setup wizard: when neither is set (every other Phase 2
        # test's default `FakeSink()`), verify_login accepts any
        # non-empty credentials — this fake has no real account to check
        # against. A test that needs `verify_login` to reject a specific
        # bad login configures both.
        self.valid_username = valid_username
        self.valid_password = valid_password

        self.calls: list[tuple] = []
        self._next_chapter_n: dict[str, int] = {}
        self._upload_fail_count = 0

        for t in targets or []:
            self.add_target(t.id, t.name)

    def add_target(self, target_id: str, name: str, chapters: list[LiveChapter] = ()) -> Target:
        chapters = list(chapters)
        target = Target(
            id=target_id,
            name=name,
            seconds_present=sum(c.seconds for c in chapters),
            chapters_present=len(chapters),
        )
        self._targets[target_id] = target
        self.chapters[target_id] = chapters
        self._next_chapter_n[target_id] = len(chapters)
        return target

    def list_targets(self) -> list[Target]:
        return list(self._targets.values())

    def verify_login(self, username: str, password: str) -> bool:
        """Non-mutating credential check (Task 36 setup wizard).

        Recorded in `.calls` as `("verify_login", username)` — never
        `clear`/`upload` — so a wizard step that only checks credentials
        can be asserted to have made no destructive sink call. See
        `__init__` for the unconfigured-fake default.
        """
        self.calls.append(("verify_login", username))
        if self.valid_username is None and self.valid_password is None:
            return bool(username) and bool(password)
        return username == self.valid_username and password == self.valid_password

    def read_chapters(self, target: Target) -> TargetSnapshot:
        self.calls.append(("read_chapters", target.id))
        chapters = self.chapters.get(target.id, [])
        return TargetSnapshot(target=target, taken_at=datetime.now(UTC), chapters=list(chapters))

    def clear(self, target: Target) -> None:
        self.calls.append(("clear", target.id))
        if self.fail_clear:
            raise SinkClearError(f"simulated clear failure for {target.id}")
        self.chapters[target.id] = []

    def upload(self, target: Target, path: Path, title: str) -> None:
        self.calls.append(("upload", target.id, str(path), title))
        if self._upload_fail_count < self.fail_upload_times:
            self._upload_fail_count += 1
            raise SinkUploadError(f"simulated upload failure for {target.id}")
        n = self._next_chapter_n.get(target.id, 0)
        self._next_chapter_n[target.id] = n + 1
        chapter = LiveChapter(id=f"ch{n}", title=title, seconds=0.0, transcoding=True)
        self.chapters.setdefault(target.id, []).append(chapter)

    def settle(self, target: Target, expect_seconds: float, timeout_s: int) -> SettleResult:
        self.calls.append(("settle", target.id, expect_seconds, timeout_s))
        pending_ids = [c.id for c in self.chapters.get(target.id, []) if c.transcoding]
        poll_count = {"n": 0}
        fake_clock = _FakeClock()

        def read() -> list[LiveChapter]:
            poll_count["n"] += 1
            current = self.chapters.get(target.id, [])
            if poll_count["n"] <= self.settle_after_polls or not pending_ids:
                return list(current)
            seconds = (
                self.settle_seconds_override
                if self.settle_seconds_override is not None
                else expect_seconds / len(pending_ids)
            )
            updated = [
                LiveChapter(id=c.id, title=c.title, seconds=seconds, transcoding=False)
                if c.id in pending_ids
                else c
                for c in current
            ]
            self.chapters[target.id] = updated
            return updated

        return poll_until_settled(
            read, expect_seconds, timeout_s, sleep=fake_clock.sleep, clock=fake_clock
        )

    @property
    def limits(self) -> SinkLimits:
        return self._limits

    def calls_named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]
