"""Write-once snapshots (Task 20; spec §2 step 5, §10.6).

Step 5 of the swap sequence (spec §2) is the last thing that happens
before `CLEAR`: everything up to VERIFY is side-effect-free on the tonie,
so the snapshot written here is the *only* surviving record of what is
about to be destroyed. The Phase 0 prototype wrote this record to a fixed
filename, so the second night's run silently clobbered the first night's
-- exactly the defect this module exists to prevent by construction, not
by convention. **A snapshot must never be overwritten, by any code path,
under any collision**, and a snapshot that fails to write must abort the
swap rather than let `CLEAR` proceed without one (spec §2 step 5: "fail
-> ABORT. tonie untouched.").

Write-once is enforced with `open(path, "x")`-equivalent semantics
(`O_CREAT | O_EXCL`), which atomically fails with `FileExistsError` if the
path already exists -- there is no "check then write" race window. On a
collision the write is retried against `<name>-<n>.json` for increasing
`n`, so two snapshots taken in the very same instant (the same
microsecond, under a frozen/fake clock) both land on disk rather than one
silently replacing the other. Once written, the file is chmod'd `0o444`
(read-only) as a second, belt-and-braces guard against later mutation --
belt-and-braces because O_EXCL already made the create atomic; the
permission bit protects against a later *open-for-write* on the same
path, which O_EXCL alone would not stop.

Filename note -- a deliberate departure from the task brief: the brief's
sketch names the file `tonie-snapshot-<timestamp>-<target_id>.json`. But
Task 11's History screen (`boxbutler/web/routes/history.py`) validates
snapshot filenames against `^tonie-snapshot-[0-9TZ-]+\\.json$` *before*
touching the filesystem -- a character class of digits, `T`, `Z` and `-`
only. Real target ids are not restricted to that alphabet (sink ids like
`"fake-green-01"`, and store ids, which are `uuid4().hex` -- lowercase
hex including `a`-`f`, both fail that regex). Embedding the raw id in the
filename, as the brief sketches, would write a snapshot that is correct
on disk and simply never listed or servable by History -- silently, with
no error anywhere. That is exactly the "two correct-in-isolation
decisions combine into a silent failure" trap this task was warned about,
so the code (History's real regex) wins over the brief: the target id is
recorded in full inside the JSON body, but the filename carries only the
timestamp (plus a purely numeric collision suffix), which always matches
History's regex.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import APP_NAME, __version__
from ..sinks.protocol import TargetSnapshot


class SnapshotError(Exception):
    """Raised when a snapshot cannot be written.

    Per spec §2 step 5 this must abort the swap: clearing a tonie without
    a surviving record of what it held is exactly the failure this module
    exists to prevent.
    """


def utcnow() -> datetime:
    return datetime.now(UTC)


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed here; pass a timezone-aware datetime "
            "(e.g. datetime.now(UTC))"
        )


def _timestamp(dt: datetime) -> str:
    # Fixed-width YYYYMMDDTHHMMSSffffffZ -- sorts chronologically as a
    # plain string (both this module's list_snapshots and the History
    # route rely on that), and uses only digits, "T" and trailing "Z" so
    # it matches History's `[0-9TZ-]+` filename charset. No "." separator
    # before the microseconds: a literal dot is outside that charset.
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y%m%dT%H%M%S%f") + "Z"


def write_snapshot(
    snapshot_dir: Path,
    snap: TargetSnapshot,
    *,
    clock: Callable[[], datetime] = utcnow,
    source: str = "live",
) -> Path:
    """Write `snap` write-once. `source` records *how* `snap.chapters` was
    obtained -- `"live"` for a `sink.read_chapters` call taken at
    `taken_at`, anything else for a caller that built `snap` from
    something other than the tonie's live state. Every call site in this
    project currently passes live data (the default), but the field is
    always written explicitly rather than implied, so a reader of the
    file on disk never has to guess or assume: a snapshot that cannot say
    where its data came from is one that gets over-trusted later."""
    taken_at = clock()
    _require_aware(taken_at)

    snapshot_dir = Path(snapshot_dir)
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(f"could not create snapshot directory {snapshot_dir}: {exc}") from exc

    body = {
        "taken_at": taken_at.astimezone(UTC).isoformat(),
        "target": {"id": snap.target.id, "name": snap.target.name},
        "chapters": [
            {"id": c.id, "title": c.title, "seconds": c.seconds, "transcoding": c.transcoding}
            for c in snap.chapters
        ],
        "source": source,
        "app": APP_NAME,
        "version": __version__,
    }
    content = json.dumps(body, indent=2).encode("utf-8")

    base = f"tonie-snapshot-{_timestamp(taken_at)}"
    attempt = 0
    while True:
        name = f"{base}.json" if attempt == 0 else f"{base}-{attempt}.json"
        path = snapshot_dir / name
        try:
            # O_EXCL makes creation atomic: no window between "check" and
            # "write" for a second writer to race through.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            attempt += 1
            continue
        except OSError as exc:
            raise SnapshotError(f"could not create snapshot file {path}: {exc}") from exc

        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        except OSError as exc:
            # A partially written file is worse than none: it can be
            # mistaken for a real snapshot later. Best-effort clean it up,
            # then abort -- this path only exists to create the file, so
            # nothing else on disk depends on it yet.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SnapshotError(f"could not write snapshot file {path}: {exc}") from exc

        os.chmod(path, 0o444)
        return path


def list_snapshots(snapshot_dir: Path) -> list[Path]:
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.is_dir():
        return []
    return sorted(snapshot_dir.glob("tonie-snapshot-*.json"), key=lambda p: p.name, reverse=True)
