"""Shared domain models for Box Butler (spec §3.2).

No I/O here: plain frozen dataclasses and enums shared by every later task.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LibraryMode(StrEnum):
    SINGLE = "single"
    ALBUM = "album"
    SERIAL = "serial"


class ItemKind(StrEnum):
    YOUTUBE = "youtube"
    PLAYLIST_ENTRY = "playlist_entry"
    RSS = "rss"
    UPLOAD = "upload"
    FOLDER_FILE = "folder_file"
    URL = "url"


class ItemState(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class AssignmentMode(StrEnum):
    ORDERED = "ordered"
    SHUFFLE = "shuffle"


class AssignmentState(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    CLI = "cli"


class RunOutcome(StrEnum):
    SWAPPED = "SWAPPED"
    SKIPPED_ALREADY_CURRENT = "SKIPPED_ALREADY_CURRENT"
    NO_CANDIDATE = "NO_CANDIDATE"
    PLAN_FAILED = "PLAN_FAILED"
    ABORTED_STAGING = "ABORTED_STAGING"
    DEGRADED = "DEGRADED"
    REPAIRED = "REPAIRED"
    DRY_RUN = "DRY_RUN"
    UNMANAGED = "UNMANAGED"
    # One assignment hit an exception this module does not classify, with no
    # swap marker — so the tonie was provably never touched, but something is
    # genuinely broken (final safety review, C2/M3). Reported per assignment
    # so every other tonie still gets its night; makes the run FAILED.
    CRASHED = "CRASHED"


class RenditionMode(StrEnum):
    TRIM = "trim"
    LOUDNORM = "loudnorm"
    TRANSCODE = "transcode"
    COPY = "copy"


@dataclass(frozen=True)
class Library:
    """A library is exactly one folder on disk.

    `folder_path` is a `str`, not `str | None`: sources are only *how*
    media arrives in the folder, so there is no such thing as a library
    without one any more (see `boxbutler/sources/library_folder.py`). The
    empty string is the one transient exception — a row written before
    that rule existed, which `ensure_library_folders` gives a real folder
    at the next startup — and never a state any code path may create.
    """

    id: str
    name: str
    mode: LibraryMode = LibraryMode.SINGLE
    folder_path: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class Item:
    id: str
    library_id: str
    position: int
    kind: ItemKind
    source_ref: str
    source_key: str
    title: str
    loudnorm: bool = False
    state: ItemState = ItemState.OK
    enabled: bool = True
    seconds: float | None = None
    local_path: str | None = None
    added_at: datetime | None = None
    # R5 (controller ruling, Task 6): records which playlist/feed produced this
    # entry, so a re-resolve marks only that playlist's vanished entries
    # unavailable. Created in the Task 6 initial migration, not added later.
    playlist_source_id: str | None = None


@dataclass(frozen=True)
class Assignment:
    id: str
    sink: str
    target_id: str
    target_name: str
    library_id: str | None
    mode: AssignmentMode = AssignmentMode.ORDERED
    shuffle_seed: int = 0
    pinned_item_id: str | None = None
    cursor_position: int = 0
    enabled: bool = True
    state: AssignmentState = AssignmentState.OK
    staged_json: str | None = None   # JSON list of {"item_id","path","title","seconds"}
    mode_override: LibraryMode | None = None
    allow_partial_tail: bool = True
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class Rendition:
    id: str
    item_id: str
    cache_path: str
    seconds: float
    bytes: int
    cap_seconds: int
    mode: RenditionMode
    verified_at: datetime | None
    last_used_at: datetime | None


@dataclass(frozen=True)
class SourceFile:
    """A downloaded source file in the cache (migration 0002, Task 21).

    The input side of the render step, as `Rendition` is the output side.
    One row per item: its presence plus an existing `cache_path` is what
    makes spec §2 step 2's "cache hit" a fact rather than a guess.
    """
    item_id: str
    cache_path: str
    bytes: int
    fetched_at: datetime | None
    last_used_at: datetime | None = None


@dataclass(frozen=True)
class SwapMarker:
    """The write-ahead record that a tonie is about to be cleared
    (migration 0003; final safety review, C3).

    Written and committed immediately *before* `sink.clear()`, deleted on a
    successful COMMIT (and by `_degrade`, which has written the same truth
    into `assignment` instead). A marker found at startup therefore means
    the process died mid-swap, which `boxbutler.orchestrator.reconcile`
    turns into DEGRADED — the state the design already handles.

    `staged_json` is the same shape `assignment.staged_json` carries
    (`staged_to_json`), so a repair can be driven straight from it.
    """
    assignment_id: str
    run_id: str
    staged_json: str
    written_at: datetime | None


@dataclass(frozen=True)
class Run:
    id: str
    started_at: datetime
    finished_at: datetime | None
    trigger: RunTrigger
    dry_run: bool
    outcome: str | None


@dataclass(frozen=True)
class RunEvent:
    id: int | None
    run_id: str
    ts: datetime
    assignment_id: str | None
    event: str
    payload: dict


@dataclass(frozen=True)
class ChapterRecord:
    id: str
    assignment_id: str
    item_id: str
    sink_chapter_id: str | None
    title: str
    seconds: float
    uploaded_at: datetime
    position: int = 0
