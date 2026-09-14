from datetime import datetime, UTC
from boxbutler.domain.models import AssignmentState
from boxbutler.domain.state import can_rotate, mark_degraded, mark_repaired
from tests.domain.test_rotation import mk_asg

# R2: Assignment carries one staged_json field (a JSON list of
# {"item_id", "path", "title", "seconds"}), not staged_path + staged_titles_json.
STAGED_JSON = '[{"item_id": "i0", "path": "/cache/x.m4a", "title": "Story", "seconds": 100.0}]'


def test_degraded_is_sticky_and_blocks_rotation():
    d = mark_degraded(mk_asg(), STAGED_JSON)
    assert d.state == AssignmentState.DEGRADED and d.staged_json == STAGED_JSON
    assert can_rotate(d) is False


def test_repair_clears_staged_and_restores_ok():
    d = mark_degraded(mk_asg(), STAGED_JSON)
    r = mark_repaired(d, datetime.now(UTC))
    assert r.state == AssignmentState.OK and r.staged_json is None and r.last_success_at is not None
    assert can_rotate(r) is True


def test_disabled_cannot_rotate():
    from dataclasses import replace
    assert can_rotate(replace(mk_asg(), enabled=False)) is False
