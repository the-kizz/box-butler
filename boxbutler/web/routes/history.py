"""History screen — what each tonie has held, and for how long (Task 11;
spec §5 screen 3; design-system/box-butler/pages/history.md).

Reads only from `store.chapters.history(assignment_id)`, which is backed
by the append-only `chapter_history` table (companion of `chapter_record`)
and the write-once snapshot files under `settings.data_dir / "snapshots"`.
This screen must never be lossy — the snapshot written just before a
clear is the only record of what a tonie held before a swap destroyed it
(orchestrator/snapshot.py, Task 20), so this route only ever reads that
directory, it never writes, renames or deletes anything in it.

`nights` follows the brief's formula exactly: `max(1, ceil((until or now
- since) / 1 day))` — a story held for any part of a day still reads as
"1 night" rather than "0 nights", which would look like a bug.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from starlette.templating import Jinja2Templates

from ...store.db import Store
from ..auth import require_login

router = APIRouter()

# Snapshot filenames are written by orchestrator/snapshot.py as
# tonie-snapshot-<YYYYMMDDTHHMMSS.ffffffZ>-<target_id>.json. This route
# never trusts the path param beyond this pattern — no slashes, no dots
# outside the fixed ".json" suffix — before touching the filesystem, so a
# crafted name (path traversal, a file elsewhere in data_dir) 404s before
# any `Path` join happens.
_SNAPSHOT_NAME_RE = re.compile(r"^tonie-snapshot-[0-9TZ-]+\.json$")


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _nights(since: datetime, until: datetime | None, now: datetime) -> int:
    end = until or now
    return max(1, math.ceil((end - since).total_seconds() / 86400))


def _history_rows(store: Store) -> list[dict]:
    now = datetime.now(UTC)
    rows = []
    for a in store.assignments.list():
        for h in store.chapters.history(a.id):
            since = datetime.fromisoformat(h["since"])
            until = datetime.fromisoformat(h["until"]) if h["until"] else None
            rows.append(
                {
                    "target_name": a.target_name,
                    "title": h["title"],
                    "nights": _nights(since, until, now),
                    "since": since,
                    "since_display": _fmt(since),
                    "until_display": _fmt(until) if until else "current",
                }
            )
    return rows


@router.get("/history")
def history_index(
    request: Request,
    direction: str = Query("desc", alias="dir"),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    settings = request.app.state.settings
    templates = _templates(request)

    sort_dir = "asc" if direction == "asc" else "desc"
    rows = _history_rows(store)
    rows.sort(key=lambda r: r["since"], reverse=(sort_dir == "desc"))

    snapshots_dir: Path = settings.data_dir / "snapshots"
    snapshots: list[str] = []
    if snapshots_dir.is_dir():
        # Filenames sort lexically newest-first — the embedded timestamp
        # is a fixed-width YYYYMMDDTHHMMSS.ffffffZ, so string order is
        # chronological order.
        snapshots = sorted((p.name for p in snapshots_dir.glob("*.json")), reverse=True)

    return templates.TemplateResponse(
        request,
        "history.html",
        {"rows": rows, "sort_dir": sort_dir, "snapshots": snapshots},
    )


@router.get("/history/snapshots/{name}")
def download_snapshot(request: Request, name: str, user: str = Depends(require_login)):
    if not _SNAPSHOT_NAME_RE.match(name):
        return Response(status_code=404)
    settings = request.app.state.settings
    path: Path = settings.data_dir / "snapshots" / name
    if not path.is_file():
        return Response(status_code=404)
    return Response(
        content=path.read_bytes(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
