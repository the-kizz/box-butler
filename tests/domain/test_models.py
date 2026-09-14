"""Tripwire tests for controller rulings R1 and R2 (task-2 amendments).

These guard against a later task "helpfully" restoring the brief's original
staged_path/staged_titles_json fields, or a merge reintroducing snapshot()
on SinkProtocol — both of which are deliberate, binding deviations from the
brief, not drift to be reconciled.
"""
import dataclasses

from boxbutler.domain.models import Assignment
from boxbutler.sinks.protocol import SinkProtocol


def test_assignment_has_staged_json_not_staged_path_fields():
    # R2: Assignment carries one staged_json field (JSON list of staged files),
    # not staged_path + staged_titles_json.
    field_names = {f.name for f in dataclasses.fields(Assignment)}
    assert "staged_json" in field_names
    assert "staged_path" not in field_names
    assert "staged_titles_json" not in field_names


def test_sink_protocol_has_read_chapters_not_snapshot():
    # R1: SinkProtocol's chapter read is read_chapters(), not snapshot() —
    # kept distinct from the write-once snapshot file written in swap step 5.
    assert hasattr(SinkProtocol, "read_chapters")
    assert not hasattr(SinkProtocol, "snapshot")
