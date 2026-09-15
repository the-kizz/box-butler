"""SQLite repositories (Task 6; spec §3.1-§3.3).

Plain sqlite calls, one repo class per table, returning the domain dataclasses
from `boxbutler.domain.models` — never dicts or ad-hoc tuples (except
`ChapterRecordRepo.history()`, which is specified to return dicts for the
History screen). Ids are `uuid4().hex` generated here. Datetimes are stored as
ISO-8601 UTC strings and parsed back with `datetime.fromisoformat`. Booleans
are stored as 0/1.

R2 (controller ruling): `AssignmentRepo.set_state` takes a single
`staged_json` argument (not the brief's `staged_path, staged_titles_json`
pair), matching the single `staged_json` column on `assignment`.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..domain.models import (
    Assignment,
    AssignmentMode,
    AssignmentState,
    ChapterRecord,
    Item,
    ItemKind,
    ItemState,
    Library,
    LibraryMode,
    Rendition,
    RenditionMode,
    Run,
    RunEvent,
    RunTrigger,
    SourceFile,
    SwapMarker,
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    # Review fix round 1 (Important 2): UTC, not the local offset. All rows
    # in a given column must share one offset scheme for ORDER BY on the
    # raw string to stay chronological — UTC never has a DST transition,
    # a fixed local offset does (the operator is in Australia/Melbourne).
    return datetime.now(UTC).isoformat()


def _to_utc_iso(dt: datetime) -> str:
    """Normalise a caller-supplied datetime to a UTC ISO-8601 string.

    Rejects naive datetimes rather than guessing their zone: a silently
    assumed offset is exactly the bug this normalisation exists to prevent.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed here; pass a timezone-aware datetime "
            "(e.g. datetime.now(UTC))"
        )
    return dt.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class LibraryRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> Library:
        return Library(
            id=r["id"],
            name=r["name"],
            mode=LibraryMode(r["mode"]),
            # NULL only for a row written before "a library is a folder"
            # (see `Library`): normalised to "" here so nothing downstream
            # has to handle two spellings of "no folder", and fixed for
            # real by `ensure_library_folders` at the next startup.
            folder_path=r["folder_path"] or "",
            created_at=_parse_dt(r["created_at"]),
        )

    def create(self, name: str, mode: LibraryMode = LibraryMode.SINGLE, folder_path: str | None = None) -> Library:
        """Every production caller supplies `folder_path` — a library is a
        folder (see `boxbutler/sources/library_folder.py`). It stays
        defaulted here rather than becoming a required argument so that a
        store-layer test can still make a bare row; `ensure_library_folders`
        is what guarantees a *running* install has none."""
        id_ = _new_id()
        created_at = _now_iso()
        self._conn.execute(
            "INSERT INTO library (id, name, mode, folder_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (id_, name, str(mode), folder_path, created_at),
        )
        return self.get(id_)

    def get(self, id_: str) -> Library | None:
        r = self._conn.execute("SELECT * FROM library WHERE id = ?", (id_,)).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[Library]:
        rows = self._conn.execute("SELECT * FROM library ORDER BY name").fetchall()
        return [self._row(r) for r in rows]

    def update(self, lib: Library) -> Library:
        self._conn.execute(
            "UPDATE library SET name = ?, mode = ?, folder_path = ? WHERE id = ?",
            (lib.name, str(lib.mode), lib.folder_path, lib.id),
        )
        return self.get(lib.id)

    def delete(self, id_: str) -> None:
        self._conn.execute("DELETE FROM library WHERE id = ?", (id_,))


class ItemRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> Item:
        return Item(
            id=r["id"],
            library_id=r["library_id"],
            position=r["position"],
            kind=ItemKind(r["kind"]),
            source_ref=r["source_ref"],
            source_key=r["source_key"],
            title=r["title"],
            loudnorm=bool(r["loudnorm"]),
            state=ItemState(r["state"]),
            enabled=bool(r["enabled"]),
            seconds=r["seconds"],
            local_path=r["local_path"],
            added_at=_parse_dt(r["added_at"]),
            playlist_source_id=r["playlist_source_id"],
        )

    def add(
        self,
        library_id: str,
        kind: ItemKind,
        source_ref: str,
        source_key: str,
        title: str,
        seconds: float | None = None,
        local_path: str | None = None,
        loudnorm: bool = False,
        playlist_source_id: str | None = None,
    ) -> Item:
        id_ = _new_id()
        added_at = _now_iso()
        row = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM item WHERE library_id = ?", (library_id,)
        ).fetchone()
        next_position = row[0]
        self._conn.execute(
            """INSERT INTO item
               (id, library_id, position, kind, source_ref, source_key, title,
                loudnorm, seconds, local_path, added_at, playlist_source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(library_id, source_key) DO NOTHING""",
            (
                id_, library_id, next_position, str(kind), source_ref, source_key, title,
                int(loudnorm), seconds, local_path, added_at, playlist_source_id,
            ),
        )
        return self.find_by_key(library_id, source_key)

    def get(self, id_: str) -> Item | None:
        r = self._conn.execute("SELECT * FROM item WHERE id = ?", (id_,)).fetchone()
        return self._row(r) if r else None

    def list(self, library_id: str) -> list[Item]:
        rows = self._conn.execute(
            "SELECT * FROM item WHERE library_id = ? ORDER BY position", (library_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def find_by_key(self, library_id: str, source_key: str) -> Item | None:
        r = self._conn.execute(
            "SELECT * FROM item WHERE library_id = ? AND source_key = ?", (library_id, source_key)
        ).fetchone()
        return self._row(r) if r else None

    def reorder(self, library_id: str, ordered_ids: list[str], start_position: int = 0) -> None:
        """Assign `ordered_ids` consecutive positions starting at
        `start_position` (default 0, the whole-library case).

        Task 10 review, Critical 2: the library screen paginates at 50
        items and SortableJS is wired to the current page's `<ol>` only,
        so a drag on page 2 posts only page-2 ids. Calling this with
        `start_position=0` for those ids would collide with page 1's real
        positions (confirmed: reordering within page 2 of a 60-item
        library produced duplicate positions `0,0,1,1,2,...`, corrupting
        the whole library's order). `boxbutler/web/routes/library.py`
        passes `start_position = (page - 1) * PAGE_SIZE` so a page-local
        drag only ever rewrites that page's own contiguous position
        range.

        The `position != ?` guard makes a same-order reorder (dropping an
        item back where it started) write nothing — the review asked
        explicitly that this be a real no-op, not merely idempotent.

        Review round 2, Critical 2 reopened: the caller-supplied
        `start_position` was only ever bounded from below
        (`max(0, page - 1) * PAGE_SIZE` in the route) — nothing bounded it
        from *above*. A stale or out-of-range `page` (confirmed: `page=99`
        on a 6-item library) produced positions like `4900..4905`,
        breaking the dense-0-based invariant `delete()` establishes and
        reintroducing the same dead-end-button bug (`item.position == 0`
        / `== total - 1` never matching again). The route now also
        clamps `page` server-side (layer 1), but this method no longer
        trusts that alone: every call ends with `_normalize_positions`,
        which re-numbers the *whole* library to a dense `0..n-1` in
        current position order. That makes the invariant self-healing —
        owned by the operation that can break it, the same principle
        `delete()` already applies — so no caller's arithmetic, however
        wrong, can leave positions non-dense. Already-dense input costs
        nothing extra: normalisation only issues an UPDATE for a row
        whose position actually needs to change.
        """
        self._conn.execute("BEGIN")
        try:
            for offset, id_ in enumerate(ordered_ids):
                position = start_position + offset
                self._conn.execute(
                    "UPDATE item SET position = ? WHERE id = ? AND library_id = ? AND position != ?",
                    (position, id_, library_id, position),
                )
            self._normalize_positions(library_id)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _normalize_positions(self, library_id: str) -> None:
        """Re-number every item in `library_id` to a dense `0..n-1` range,
        preserving current relative order (ties broken by id for
        determinism). Self-healing safety net for `reorder()` (see its
        docstring) — called inside the same transaction, so it's part of
        one atomic operation from the caller's point of view.
        """
        rows = self._conn.execute(
            "SELECT id, position FROM item WHERE library_id = ? ORDER BY position, id",
            (library_id,),
        ).fetchall()
        for rank, row in enumerate(rows):
            if row["position"] != rank:
                self._conn.execute("UPDATE item SET position = ? WHERE id = ?", (rank, row["id"]))

    def move(self, id_: str, delta: int) -> None:
        self._conn.execute("BEGIN")
        try:
            current = self._conn.execute(
                "SELECT library_id, position FROM item WHERE id = ?", (id_,)
            ).fetchone()
            if current is None:
                self._conn.execute("ROLLBACK")
                return
            library_id, position = current["library_id"], current["position"]
            target_position = position + delta
            neighbour = self._conn.execute(
                "SELECT id, position FROM item WHERE library_id = ? AND position = ?",
                (library_id, target_position),
            ).fetchone()
            if neighbour is None:
                self._conn.execute("ROLLBACK")
                return
            self._conn.execute("UPDATE item SET position = ? WHERE id = ?", (target_position, id_))
            self._conn.execute("UPDATE item SET position = ? WHERE id = ?", (position, neighbour["id"]))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def set_enabled(self, id_: str, enabled: bool) -> None:
        self._conn.execute("UPDATE item SET enabled = ? WHERE id = ?", (int(enabled), id_))

    def set_loudnorm(self, id_: str, loudnorm: bool) -> None:
        self._conn.execute("UPDATE item SET loudnorm = ? WHERE id = ?", (int(loudnorm), id_))

    def set_state(self, id_: str, state: ItemState) -> None:
        self._conn.execute("UPDATE item SET state = ? WHERE id = ?", (str(state), id_))

    def set_title(self, id_: str, title: str) -> None:
        self._conn.execute("UPDATE item SET title = ? WHERE id = ?", (title, id_))

    def set_local_path(self, id_: str, local_path: str, source_ref: str | None = None) -> None:
        # Task 17 (folder scanner): a renamed/moved file keeps its Item
        # (same fingerprint-based source_key) but its on-disk location
        # changes. `source_ref` is updated alongside when given (the
        # scanner's relative path), never `source_key` — identity must
        # stay pinned to content, not location.
        if source_ref is None:
            self._conn.execute("UPDATE item SET local_path = ? WHERE id = ?", (local_path, id_))
        else:
            self._conn.execute(
                "UPDATE item SET local_path = ?, source_ref = ? WHERE id = ?",
                (local_path, source_ref, id_),
            )

    def delete(self, id_: str) -> None:
        """Delete an item and close the position gap it leaves behind.

        Task 10 review, Critical 1: leaving a gap (e.g. deleting position 2
        of 5 leaves 0,1,3,4) broke three things that all assumed positions
        are dense and 0-based: the displayed position numbers
        (`item.position + 1`) skip a number; the "Move down" disabled
        check (`item.position == total - 1`, comparing against the
        whole-library *count*) stops matching the true last item once a
        gap exists, so a dead-end button renders enabled; and `move()`'s
        neighbour lookup at exactly `position ± delta` silently no-ops
        across a gap. Renumbering here — in the same transaction as the
        delete — keeps positions dense so all three read correctly with
        no special-casing anywhere else.
        """
        self._conn.execute("BEGIN")
        try:
            row = self._conn.execute(
                "SELECT library_id, position FROM item WHERE id = ?", (id_,)
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                return
            library_id, position = row["library_id"], row["position"]
            self._conn.execute("DELETE FROM item WHERE id = ?", (id_,))
            self._conn.execute(
                "UPDATE item SET position = position - 1 WHERE library_id = ? AND position > ?",
                (library_id, position),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise


class AssignmentRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> Assignment:
        return Assignment(
            id=r["id"],
            sink=r["sink"],
            target_id=r["target_id"],
            target_name=r["target_name"],
            library_id=r["library_id"],
            mode=AssignmentMode(r["mode"]),
            shuffle_seed=r["shuffle_seed"],
            pinned_item_id=r["pinned_item_id"],
            cursor_position=r["cursor_position"],
            enabled=bool(r["enabled"]),
            state=AssignmentState(r["state"]),
            staged_json=r["staged_json"],
            mode_override=LibraryMode(r["mode_override"]) if r["mode_override"] else None,
            allow_partial_tail=bool(r["allow_partial_tail"]),
            last_success_at=_parse_dt(r["last_success_at"]),
        )

    def upsert_target(self, sink: str, target_id: str, target_name: str) -> Assignment:
        existing = self.get_by_target(sink, target_id)
        if existing is None:
            id_ = _new_id()
            self._conn.execute(
                "INSERT INTO assignment (id, sink, target_id, target_name) VALUES (?, ?, ?, ?)",
                (id_, sink, target_id, target_name),
            )
            return self.get(id_)
        self._conn.execute(
            "UPDATE assignment SET target_name = ? WHERE id = ?", (target_name, existing.id)
        )
        return self.get(existing.id)

    def get(self, id_: str) -> Assignment | None:
        r = self._conn.execute("SELECT * FROM assignment WHERE id = ?", (id_,)).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[Assignment]:
        rows = self._conn.execute("SELECT * FROM assignment ORDER BY target_name").fetchall()
        return [self._row(r) for r in rows]

    def get_by_target(self, sink: str, target_id: str) -> Assignment | None:
        r = self._conn.execute(
            "SELECT * FROM assignment WHERE sink = ? AND target_id = ?", (sink, target_id)
        ).fetchone()
        return self._row(r) if r else None

    def assign_library(self, id_: str, library_id: str | None) -> None:
        self._conn.execute("UPDATE assignment SET library_id = ? WHERE id = ?", (library_id, id_))

    def set_cursor(self, id_: str, position: int) -> None:
        self._conn.execute("UPDATE assignment SET cursor_position = ? WHERE id = ?", (position, id_))

    def set_pin(self, id_: str, item_id: str | None) -> None:
        self._conn.execute("UPDATE assignment SET pinned_item_id = ? WHERE id = ?", (item_id, id_))

    def set_mode(self, id_: str, mode: AssignmentMode) -> None:
        self._conn.execute("UPDATE assignment SET mode = ? WHERE id = ?", (str(mode), id_))

    def set_shuffle_seed(self, id_: str, shuffle_seed: int) -> None:
        self._conn.execute("UPDATE assignment SET shuffle_seed = ? WHERE id = ?", (shuffle_seed, id_))

    def set_state(self, id_: str, state: AssignmentState, staged_json: str | None = None) -> None:
        # R2: single staged_json column, not staged_path/staged_titles_json.
        self._conn.execute(
            "UPDATE assignment SET state = ?, staged_json = ? WHERE id = ?",
            (str(state), staged_json, id_),
        )

    def set_enabled(self, id_: str, enabled: bool) -> None:
        self._conn.execute("UPDATE assignment SET enabled = ? WHERE id = ?", (int(enabled), id_))

    def set_last_success(self, id_: str, at: datetime) -> None:
        self._conn.execute(
            "UPDATE assignment SET last_success_at = ? WHERE id = ?", (_to_utc_iso(at), id_)
        )

    def set_override(self, id_: str, mode_override: LibraryMode | None) -> None:
        self._conn.execute(
            "UPDATE assignment SET mode_override = ? WHERE id = ?",
            (str(mode_override) if mode_override else None, id_),
        )

    def set_fit(self, id_: str, allow_partial_tail: bool) -> None:
        self._conn.execute(
            "UPDATE assignment SET allow_partial_tail = ? WHERE id = ?", (int(allow_partial_tail), id_)
        )


class RenditionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> Rendition:
        return Rendition(
            id=r["id"],
            item_id=r["item_id"],
            cache_path=r["cache_path"],
            seconds=r["seconds"],
            bytes=r["bytes"],
            cap_seconds=r["cap_seconds"],
            mode=RenditionMode(r["mode"]),
            verified_at=_parse_dt(r["verified_at"]),
            last_used_at=_parse_dt(r["last_used_at"]),
        )

    def upsert(
        self,
        item_id: str,
        cache_path: str,
        seconds: float,
        bytes: int,
        cap_seconds: int,
        mode: RenditionMode,
        verified_at: datetime | None = None,
    ) -> Rendition:
        existing = self.for_item(item_id, cap_seconds, mode)
        verified_iso = _to_utc_iso(verified_at) if verified_at else None
        if existing is None:
            id_ = _new_id()
            self._conn.execute(
                """INSERT INTO rendition
                   (id, item_id, cache_path, seconds, bytes, cap_seconds, mode, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (id_, item_id, cache_path, seconds, bytes, cap_seconds, str(mode), verified_iso),
            )
            return self._get(id_)
        self._conn.execute(
            """UPDATE rendition SET cache_path = ?, seconds = ?, bytes = ?, verified_at = ?
               WHERE id = ?""",
            (cache_path, seconds, bytes, verified_iso, existing.id),
        )
        return self._get(existing.id)

    def _get(self, id_: str) -> Rendition | None:
        r = self._conn.execute("SELECT * FROM rendition WHERE id = ?", (id_,)).fetchone()
        return self._row(r) if r else None

    def for_item(self, item_id: str, cap_seconds: int, mode: RenditionMode) -> Rendition | None:
        r = self._conn.execute(
            "SELECT * FROM rendition WHERE item_id = ? AND cap_seconds = ? AND mode = ?",
            (item_id, cap_seconds, str(mode)),
        ).fetchone()
        return self._row(r) if r else None

    def touch(self, id_: str, at: datetime) -> None:
        self._conn.execute("UPDATE rendition SET last_used_at = ? WHERE id = ?", (_to_utc_iso(at), id_))

    def list(self) -> list[Rendition]:
        rows = self._conn.execute("SELECT * FROM rendition").fetchall()
        return [self._row(r) for r in rows]

    def delete(self, id_: str) -> None:
        self._conn.execute("DELETE FROM rendition WHERE id = ?", (id_,))


class SourceFileRepo:
    """The cache index for fetched *source* files (migration 0002, Task 21).

    `record()` is an upsert keyed by item, because a re-fetch of the same
    item legitimately replaces its source file (a different extension after
    a format change, say). It deliberately measures `bytes` from the file on
    disk rather than trusting a caller-supplied number: the size of the
    thing that actually exists is the only size worth recording, and a
    missing file raising here is better than a row that claims a cached
    file Box Butler does not have.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> SourceFile:
        return SourceFile(
            item_id=r["item_id"],
            cache_path=r["cache_path"],
            bytes=r["bytes"],
            fetched_at=_parse_dt(r["fetched_at"]),
            last_used_at=_parse_dt(r["last_used_at"]),
        )

    def record(self, item_id: str, path: str | Path, at: datetime) -> SourceFile:
        path = Path(path)
        self._conn.execute(
            """INSERT INTO source_file (item_id, cache_path, bytes, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                 cache_path = excluded.cache_path,
                 bytes = excluded.bytes,
                 fetched_at = excluded.fetched_at""",
            (item_id, str(path), path.stat().st_size, _to_utc_iso(at)),
        )
        return self.get(item_id)

    def get(self, item_id: str) -> SourceFile | None:
        r = self._conn.execute(
            "SELECT * FROM source_file WHERE item_id = ?", (item_id,)
        ).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[SourceFile]:
        rows = self._conn.execute("SELECT * FROM source_file ORDER BY fetched_at").fetchall()
        return [self._row(r) for r in rows]

    def touch(self, item_id: str, at: datetime) -> None:
        self._conn.execute(
            "UPDATE source_file SET last_used_at = ? WHERE item_id = ?",
            (_to_utc_iso(at), item_id),
        )

    def delete(self, item_id: str) -> None:
        self._conn.execute("DELETE FROM source_file WHERE item_id = ?", (item_id,))


class RunRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> Run:
        return Run(
            id=r["id"],
            started_at=_parse_dt(r["started_at"]),
            finished_at=_parse_dt(r["finished_at"]),
            trigger=RunTrigger(r["trigger"]),
            dry_run=bool(r["dry_run"]),
            outcome=r["outcome"],
        )

    def start(self, trigger: str, dry_run: bool = False) -> Run:
        id_ = _new_id()
        started_at = _now_iso()
        self._conn.execute(
            "INSERT INTO run (id, started_at, trigger, dry_run) VALUES (?, ?, ?, ?)",
            (id_, started_at, trigger, int(dry_run)),
        )
        return self.get(id_)

    def finish(self, run_id: str, outcome: str) -> None:
        self._conn.execute(
            "UPDATE run SET finished_at = ?, outcome = ? WHERE id = ?",
            (_now_iso(), outcome, run_id),
        )

    def get(self, id_: str) -> Run | None:
        r = self._conn.execute("SELECT * FROM run WHERE id = ?", (id_,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 50) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def event(self, run_id: str, event: str, assignment_id: str | None = None, **payload) -> RunEvent:
        ts = _now_iso()
        payload_json = json.dumps(payload)
        cur = self._conn.execute(
            "INSERT INTO run_event (run_id, ts, assignment_id, event, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, ts, assignment_id, event, payload_json),
        )
        return RunEvent(
            id=cur.lastrowid,
            run_id=run_id,
            ts=_parse_dt(ts),
            assignment_id=assignment_id,
            event=event,
            payload=payload,
        )

    def events(self, run_id: str) -> list[RunEvent]:
        rows = self._conn.execute(
            "SELECT * FROM run_event WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            RunEvent(
                id=r["id"],
                run_id=r["run_id"],
                ts=_parse_dt(r["ts"]),
                assignment_id=r["assignment_id"],
                event=r["event"],
                payload=json.loads(r["payload_json"]),
            )
            for r in rows
        ]


class ChapterRecordRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> ChapterRecord:
        return ChapterRecord(
            id=r["id"],
            assignment_id=r["assignment_id"],
            item_id=r["item_id"],
            sink_chapter_id=r["sink_chapter_id"],
            title=r["title"],
            seconds=r["seconds"],
            uploaded_at=_parse_dt(r["uploaded_at"]),
            position=r["position"],
        )

    def replace_for_assignment(
        self,
        assignment_id: str,
        records: list[tuple[str, str | None, str, float]],
        at: datetime,
    ) -> list[ChapterRecord]:
        at_iso = _to_utc_iso(at)
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM chapter_record WHERE assignment_id = ?", (assignment_id,))
            new_ids = []
            for position, (item_id, sink_chapter_id, title, seconds) in enumerate(records):
                id_ = _new_id()
                new_ids.append(id_)
                self._conn.execute(
                    """INSERT INTO chapter_record
                       (id, assignment_id, item_id, sink_chapter_id, title, seconds, uploaded_at, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (id_, assignment_id, item_id, sink_chapter_id, title, seconds, at_iso, position),
                )
            self._conn.execute(
                "UPDATE chapter_history SET until = ? WHERE assignment_id = ? AND until IS NULL",
                (at_iso, assignment_id),
            )
            for item_id, sink_chapter_id, title, seconds in records:
                self._conn.execute(
                    """INSERT INTO chapter_history (assignment_id, item_id, title, seconds, since, until)
                       VALUES (?, ?, ?, ?, ?, NULL)""",
                    (assignment_id, item_id, title, seconds, at_iso),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        rows = self._conn.execute(
            "SELECT * FROM chapter_record WHERE id IN ({}) ORDER BY position".format(
                ",".join("?" * len(new_ids))
            ),
            new_ids,
        ).fetchall() if new_ids else []
        return [self._row(r) for r in rows]

    def for_assignment(self, assignment_id: str) -> list[ChapterRecord]:
        rows = self._conn.execute(
            "SELECT * FROM chapter_record WHERE assignment_id = ? ORDER BY position", (assignment_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def loaded_item_ids_except(self, assignment_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT item_id FROM chapter_record WHERE assignment_id != ?", (assignment_id,)
        ).fetchall()
        return {r["item_id"] for r in rows}

    def held_since(self, assignment_id: str, since: datetime) -> set[str]:
        """Item ids this assignment held at any point on or after `since`
        (a cutoff, e.g. `now - repeat_cooldown_days`) — i.e. every hold
        interval `[since_ts, until_ts)` that overlaps `[since, now]`.

        A hold interval's `since_ts` is always <= now (it can't start in
        the future) and the window we're testing against is unbounded
        above (it runs through "now"), so the only condition that can
        exclude an interval is that it *ended* before the cutoff:
        `until IS NOT NULL AND until <= since`. The previous form of this
        query additionally required `since_ts <= since` (the hold must have
        *started* before the cutoff) — backwards: it excluded exactly the
        holds that started most recently, so a longer cooldown window
        returned *fewer* held items instead of more, silently defeating
        the whole ladder (`RELAXED_COOLDOWN` would never fire because
        nothing was ever found "recently held").
        """
        since_iso = _to_utc_iso(since)
        rows = self._conn.execute(
            """SELECT DISTINCT item_id FROM chapter_history
               WHERE assignment_id = ? AND (until IS NULL OR until > ?)""",
            (assignment_id, since_iso),
        ).fetchall()
        return {r["item_id"] for r in rows}

    def history(self, assignment_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT item_id, title, seconds, since, until FROM chapter_history
               WHERE assignment_id = ? ORDER BY since DESC, id DESC""",
            (assignment_id,),
        ).fetchall()
        return [
            {
                "item_id": r["item_id"],
                "title": r["title"],
                "seconds": r["seconds"],
                "since": r["since"],
                "until": r["until"],
            }
            for r in rows
        ]


class SwapMarkerRepo:
    """The write-ahead swap markers (migration 0003; final safety review,
    C3).

    `put` is called — and, because the connection is in autocommit mode,
    durably committed — immediately before `sink.clear()`. `clear` is
    called from `_commit` on success and from `_degrade` on a caught
    failure. Anything still in this table is evidence that a process died
    between those two points, which is what
    `boxbutler.orchestrator.reconcile.reconcile_interrupted_swaps` reads at
    startup.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _row(self, r: sqlite3.Row) -> SwapMarker:
        return SwapMarker(
            assignment_id=r["assignment_id"],
            run_id=r["run_id"],
            staged_json=r["staged_json"],
            written_at=_parse_dt(r["written_at"]),
        )

    def put(self, assignment_id: str, run_id: str, staged_json: str, at: datetime) -> SwapMarker:
        self._conn.execute(
            """INSERT INTO swap_marker (assignment_id, run_id, staged_json, written_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(assignment_id) DO UPDATE SET
                 run_id = excluded.run_id,
                 staged_json = excluded.staged_json,
                 written_at = excluded.written_at""",
            (assignment_id, run_id, staged_json, _to_utc_iso(at)),
        )
        return self.get(assignment_id)

    def get(self, assignment_id: str) -> SwapMarker | None:
        r = self._conn.execute(
            "SELECT * FROM swap_marker WHERE assignment_id = ?", (assignment_id,)
        ).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[SwapMarker]:
        rows = self._conn.execute("SELECT * FROM swap_marker ORDER BY written_at").fetchall()
        return [self._row(r) for r in rows]

    def clear(self, assignment_id: str) -> None:
        self._conn.execute("DELETE FROM swap_marker WHERE assignment_id = ?", (assignment_id,))


class SettingsRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, key: str, default=None):
        r = self._conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if r is None:
            return default
        return json.loads(r["value_json"])

    def set(self, key: str, value) -> None:
        self._conn.execute(
            """INSERT INTO settings (key, value_json) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json""",
            (key, json.dumps(value)),
        )
