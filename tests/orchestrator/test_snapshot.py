"""Tests for write-once snapshots (Task 20; spec §2 step 5, §10.6).

Step 5 of the swap sequence writes the only surviving record of what a
tonie held immediately before `CLEAR` destroys it. The prototype's bug
(spec handoff brief) was a fixed filename that let the second night's
snapshot silently clobber the first night's. These tests hold the line:
two snapshots taken in the same instant must both survive, and nothing
ever overwrites an existing snapshot file.

Naming: the brief's own interface comment says the filename embeds the
raw `target_id` (`tonie-snapshot-<timestamp>-<target_id>.json`). But
Task 11's History screen (`boxbutler/web/routes/history.py`) only ever
lists/serves files matching `^tonie-snapshot-[0-9TZ-]+\\.json$` -- a
character class of digits, `T`, `Z` and `-` only. Real target ids (see
`boxbutler/sinks/fake.py`, `boxbutler/web/fake_data.py`: `"fake-green-01"`,
and store ids, which are `uuid4().hex` -- lowercase hex including
`a`-`f`) contain characters outside that class. Embedding them verbatim
would write a snapshot that silently never appears in History -- exactly
the "two correct-in-isolation decisions combine into a silent failure"
trap called out in the task instructions. So this module does not put
the target id in the filename; the id is still fully recorded inside the
JSON body. `test_filename_matches_history_screen_regex` pins that
decision directly against the real regex Task 11 uses.
"""
import json
import os
import re
from datetime import UTC, datetime

import pytest

from boxbutler.orchestrator.snapshot import SnapshotError, list_snapshots, write_snapshot
from boxbutler.sinks.protocol import LiveChapter, Target, TargetSnapshot

# Pulled directly from boxbutler/web/routes/history.py so a drift between
# the two modules fails this test rather than silently hiding snapshots.
from boxbutler.web.routes.history import _SNAPSHOT_NAME_RE


def snap(target_id="T1"):
    return TargetSnapshot(
        Target(target_id, "Green Tonie"), datetime.now(UTC), [LiveChapter("c1", "Old", 5340.0, False)]
    )


def test_two_snapshots_in_one_second_produce_two_files(tmp_path):
    fixed = datetime(2026, 9, 11, 8, 14, 35, tzinfo=UTC)
    a = write_snapshot(tmp_path, snap(), clock=lambda: fixed)
    b = write_snapshot(tmp_path, snap(), clock=lambda: fixed)
    assert a != b and a.exists() and b.exists() and len(list_snapshots(tmp_path)) == 2


def test_never_overwrites_even_with_identical_name_collision(tmp_path):
    fixed = datetime(2026, 9, 11, 8, 14, 35, tzinfo=UTC)
    a = write_snapshot(tmp_path, snap(), clock=lambda: fixed)
    before = a.read_bytes()
    write_snapshot(tmp_path, snap(), clock=lambda: fixed)
    assert a.read_bytes() == before


def test_many_snapshots_same_instant_all_survive(tmp_path):
    # Not just two -- the collision-retry counter must keep climbing
    # rather than giving up or wrapping after one bump.
    fixed = datetime(2026, 9, 11, 8, 14, 35, tzinfo=UTC)
    paths = [write_snapshot(tmp_path, snap(), clock=lambda: fixed) for _ in range(5)]
    assert len(set(paths)) == 5
    assert all(p.exists() for p in paths)
    assert len(list_snapshots(tmp_path)) == 5


def test_snapshot_is_read_only_and_complete(tmp_path):
    p = write_snapshot(tmp_path, snap())
    assert not os.access(p, os.W_OK) or (p.stat().st_mode & 0o222) == 0
    d = json.loads(p.read_text())
    assert d["target"]["id"] == "T1" and d["chapters"][0]["title"] == "Old"
    assert d["chapters"][0]["seconds"] == 5340.0


def test_snapshot_content_matches_documented_shape(tmp_path):
    p = write_snapshot(tmp_path, snap("fake-green-01"), clock=lambda: datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    d = json.loads(p.read_text())
    assert set(d.keys()) == {"taken_at", "target", "chapters", "source", "app", "version"}
    assert d["target"] == {"id": "fake-green-01", "name": "Green Tonie"}
    assert d["chapters"] == [{"id": "c1", "title": "Old", "seconds": 5340.0, "transcoding": False}]
    assert d["source"] == "live"
    assert d["app"] and d["version"]
    # taken_at round-trips as a real, timezone-aware UTC instant.
    parsed = datetime.fromisoformat(d["taken_at"])
    assert parsed.tzinfo is not None


def test_creates_dir_and_lists_newest_first(tmp_path):
    d = tmp_path / "snapshots"
    t1 = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    write_snapshot(d, snap(), clock=lambda: t1)
    p2 = write_snapshot(d, snap(), clock=lambda: t2)
    assert list_snapshots(d)[0] == p2


def test_list_snapshots_on_missing_dir_is_empty(tmp_path):
    assert list_snapshots(tmp_path / "does-not-exist") == []


def test_naive_clock_datetime_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_snapshot(tmp_path, snap(), clock=lambda: datetime(2026, 9, 11, 8, 0))


def test_filenames_contain_no_raw_target_id_characters_outside_history_charset(tmp_path):
    # Real target ids are not restricted to History's [0-9TZ-] charset
    # (uuid4().hex ids and sink ids like "fake-green-01" both contain
    # lowercase letters outside T/Z). If the filename embedded the id
    # verbatim it would silently 404 in History. Assert the produced
    # name matches the real regex regardless of how "weird" the id is.
    p = write_snapshot(tmp_path, snap("fake-green-01"), clock=lambda: datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    assert _SNAPSHOT_NAME_RE.match(p.name), p.name


def test_filename_matches_history_screen_regex(tmp_path):
    for i in range(3):
        p = write_snapshot(
            tmp_path,
            snap(f"target-{i}-uuid-abcdef01"),
            clock=lambda i=i: datetime(2026, 9, 11, 8, i, tzinfo=UTC),
        )
        assert _SNAPSHOT_NAME_RE.match(p.name), f"{p.name} does not match History's reader regex"


def test_write_failure_raises_snapshot_error_not_silent_partial_file(tmp_path, monkeypatch):
    # A snapshot that fails to write must abort the swap (spec §2 step 5:
    # "fail -> ABORT. tonie untouched."), not raise some unrelated OSError
    # and not leave a corrupt file that a later reader might mistake for
    # a real snapshot.
    import boxbutler.orchestrator.snapshot as snapshot_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot_mod.os, "open", boom)
    with pytest.raises(SnapshotError):
        write_snapshot(tmp_path, snap())
    assert list(tmp_path.glob("*.json")) == []
