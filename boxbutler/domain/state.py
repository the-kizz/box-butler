"""Assignment state machine: DEGRADED is sticky, and repair comes before
rotation (spec §2, §10.2).

No I/O here: pure transitions over the Assignment dataclass.

R2 (controller ruling): Assignment carries a single `staged_json` field —
a JSON list of {"item_id", "path", "title", "seconds"} — not the brief's
original staged_path + staged_titles_json pair. That single field is what
mark_degraded records and mark_repaired clears, so a repair can re-use
every staged file (and its title) from a partially-completed album or
serial load, not just one path.
"""
from dataclasses import replace
from datetime import datetime

from .models import Assignment, AssignmentState


def can_rotate(assignment: Assignment) -> bool:
    """False when DEGRADED (repair first) or disabled."""
    return assignment.enabled and assignment.state == AssignmentState.OK


def mark_degraded(a: Assignment, staged_json: str) -> Assignment:
    """Tonie was cleared but not successfully filled: go DEGRADED and keep
    the staged files so a repair run can re-upload them without redoing
    download/trim work.
    """
    return replace(a, state=AssignmentState.DEGRADED, staged_json=staged_json)


def mark_repaired(a: Assignment, at: datetime) -> Assignment:
    """Repair succeeded: back to OK, staged files consumed, success recorded."""
    return replace(a, state=AssignmentState.OK, staged_json=None, last_success_at=at)
