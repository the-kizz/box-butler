"""The publish-target interface every sink (Tonie cloud, folder, etc.) implements (spec §3).

No I/O here: types and the Protocol only. Implementations live in per-sink modules
and are exercised against fakes in tests — no test may touch the network, a real
tonie, or ffmpeg.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class LiveChapter:
    id: str
    title: str
    seconds: float
    transcoding: bool


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    seconds_present: float = 0.0
    chapters_present: int = 0


@dataclass(frozen=True)
class TargetSnapshot:
    target: Target
    taken_at: datetime
    chapters: list[LiveChapter]


@dataclass(frozen=True)
class SettleResult:
    settled: bool
    seconds: float
    chapters: list[LiveChapter]
    waited_s: float
    reason: str | None = None


@dataclass(frozen=True)
class SinkLimits:
    max_seconds: int
    max_chapters: int
    max_bytes: int
    accepts: tuple[str, ...]


class SinkProtocol(Protocol):
    #: The `assignment.sink` discriminator every assignment row for this
    #: sink is keyed under — `store.assignments.upsert_target(sink.name,
    #: ...)` / `get_by_target(sink.name, ...)`.
    #:
    #: It lives on the sink object because it is the **one** source of
    #: truth for sink identity, read by the web layer, the setup wizard,
    #: the CLI, the orchestrator and the composition root alike. It used
    #: to be a constant in `boxbutler/web/fake_data.py` that the routes
    #: imported (`"fake"`) plus a separate constant built in
    #: `boxbutler/main.py` (`"tonies_cloud"`) and handed to the
    #: orchestrator — two values that had to agree with nothing keeping
    #: them in agreement. They did not agree: with the real sink
    #: configured, the UI wrote assignments under `sink='fake'` while
    #: every run looked them up under `sink='tonies_cloud'`, found
    #: nothing, reported UNMANAGED and loaded no tonie, ever (final
    #: safety review, C1 — the same defect shape as the two diverging
    #: `RotationSettings` copies). Asking the sink object itself removes
    #: the second value rather than trying to keep it in step.
    name: str

    def list_targets(self) -> list[Target]: ...

    def verify_login(self, username: str, password: str) -> bool:
        """Check account credentials without mutating anything (Task 36
        setup wizard). Never touches `tonie_api`, an HTTP client or
        ffmpeg from Phase 2's `FakeSink`; `ToniesCloudSink` (Phase 4)
        implements this against the real cloud login.
        """
        ...

    def read_chapters(self, target: Target) -> TargetSnapshot: ...

    def clear(self, target: Target) -> None: ...

    def upload(self, target: Target, path: Path, title: str) -> None: ...

    def settle(self, target: Target, expect_seconds: float, timeout_s: int) -> SettleResult: ...

    @property
    def limits(self) -> SinkLimits: ...
