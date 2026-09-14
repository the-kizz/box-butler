"""Startup reconciliation of interrupted swaps (final safety review, C3).

`swap()` persists a write-ahead `swap_marker` row immediately before it
calls `sink.clear()`, and deletes it on a successful COMMIT (or in
`_degrade`, which writes the same truth into `assignment` instead). So a
marker that is still in the database when a process starts can only mean
one thing: a previous process was killed — SIGKILL, OOM, `docker stop`
past its grace period, a host reboot — somewhere between CLEAR and the
end of the swap. The tonie is, right now, empty or half-filled, while the
assignment row still reads `state=OK`.

That is the one crash window in the whole run loop that costs a child
tonight's story and says nothing: there is no catch-up fire (the
scheduler's `next_run_at` is strictly after `now`), so without this pass
the tonie stays empty until tomorrow's scheduled run, roughly 24 hours,
invisibly.

This module closes it by converting the marker into the state the design
already handles well: `DEGRADED`, carrying the verified staged files. From
there everything else is already built — repair runs before any rotation
(`can_rotate`), `/metrics` exports `assignment_state{state="DEGRADED"}`,
`BoxButlerTonieDegraded` fires at once, the dashboard shows the tonie as
possibly empty at bedtime, and the next run repairs it from the staged
bytes without re-downloading anything.

It is deliberately **not** a run: it touches no sink, reads no network and
clears nothing. It only records what must already be true.
"""
from __future__ import annotations

from collections.abc import Callable

from boxbutler.domain.state import mark_degraded
from boxbutler.store.db import Store


def reconcile_interrupted_swaps(
    store: Store, *, log: Callable[[str, dict], None] | None = None
) -> list[str]:
    """Mark every assignment with a surviving swap marker DEGRADED.

    Returns the assignment ids reconciled (empty on the normal path, which
    is every start after a clean shutdown). Idempotent: the marker is
    deleted once the DEGRADED row is written, so a second call does
    nothing.

    The marker's `staged_json` wins over whatever the assignment carries:
    it is the set of files that were verified immediately before the clear,
    which is exactly what a repair needs. `mark_degraded` is the domain's
    own rule for the transition rather than a second copy of it spelled out
    here.
    """
    reconciled: list[str] = []
    for marker in store.swap_markers.list():
        a = store.assignments.get(marker.assignment_id)
        if a is None:
            # The assignment was deleted while the marker outlived it
            # (ON DELETE CASCADE makes this unreachable through the repos,
            # but a stale row must never wedge startup).
            store.swap_markers.clear(marker.assignment_id)
            continue
        degraded = mark_degraded(a, marker.staged_json or a.staged_json or "[]")
        store.assignments.set_state(a.id, degraded.state, staged_json=degraded.staged_json)
        # Only after the DEGRADED row is committed: if this process dies in
        # between, the next start finds the marker again and redoes the
        # same harmless write.
        store.swap_markers.clear(a.id)
        reconciled.append(a.id)
        if log is not None:
            log(
                "swap_marker_reconciled",
                {
                    "assignment_id": a.id,
                    "target": a.target_name,
                    "run_id": marker.run_id,
                    "written_at": marker.written_at.isoformat() if marker.written_at else None,
                    "detail": (
                        "a previous process died between CLEAR and COMMIT; this tonie may be "
                        "empty and is now DEGRADED, to be repaired before any rotation"
                    ),
                },
            )
    return reconciled


__all__ = ["reconcile_interrupted_swaps"]
