"""Repository tests (Task 6, brief step 2), with R2/R5/R9 applied.

R2: assignment carries a single staged_json column (JSON list of staged
files), not staged_path + staged_titles_json — set_state takes staged_json.
"""
from datetime import datetime, timedelta, timezone, UTC

import pytest

from boxbutler.domain.models import ItemKind, AssignmentState, ItemState


def add(store, lib, key, title="T"):
    return store.items.add(lib.id, ItemKind.YOUTUBE, f"https://example.invalid/{key}", key, title)


def test_item_append_positions_and_unique_key(store):
    lib = store.libraries.create("Bedtime")
    a = add(store, lib, "v1")
    b = add(store, lib, "v2")
    again = add(store, lib, "v1", title="renamed")
    assert (a.position, b.position) == (0, 1)
    assert again.id == a.id                      # source_key is the idempotency key, not the title
    assert [i.id for i in store.items.list(lib.id)] == [a.id, b.id]


def test_reorder_and_move(store):
    lib = store.libraries.create("L")
    ids = [add(store, lib, f"v{i}").id for i in range(3)]
    store.items.reorder(lib.id, [ids[2], ids[0], ids[1]])
    assert [i.id for i in store.items.list(lib.id)] == [ids[2], ids[0], ids[1]]
    store.items.move(ids[1], -1)
    assert [i.id for i in store.items.list(lib.id)] == [ids[2], ids[1], ids[0]]
    store.items.move(ids[2], -1)                 # already first: no-op, no exception
    assert store.items.list(lib.id)[0].id == ids[2]


def test_unavailable_keeps_position(store):
    lib = store.libraries.create("L")
    ids = [add(store, lib, f"v{i}").id for i in range(3)]
    store.items.set_state(ids[1], ItemState.UNAVAILABLE)
    assert [i.position for i in store.items.list(lib.id)] == [0, 1, 2]


def test_upsert_target_creates_unmanaged_then_renames(store):
    a = store.assignments.upsert_target("fake", "T1", "Creative-Tonie")
    assert a.library_id is None and a.state == AssignmentState.OK
    b = store.assignments.upsert_target("fake", "T1", "Green Tonie")
    assert b.id == a.id and b.target_name == "Green Tonie"


def test_degraded_round_trip(store):
    # R2: set_state takes staged_json (a JSON list of staged-file dicts), not
    # staged_path/staged_titles_json.
    a = store.assignments.upsert_target("fake", "T1", "x")
    staged = '[{"item_id":"i0","path":"/cache/s.m4a","title":"S","seconds":5340.0}]'
    store.assignments.set_state(a.id, AssignmentState.DEGRADED, staged)
    got = store.assignments.get(a.id)
    assert got.state == AssignmentState.DEGRADED and got.staged_json == staged


def test_chapter_records_and_loaded_elsewhere(store):
    lib = store.libraries.create("L")
    i0 = add(store, lib, "v0")
    i1 = add(store, lib, "v1")
    a = store.assignments.upsert_target("fake", "T1", "A")
    b = store.assignments.upsert_target("fake", "T2", "B")
    now = datetime.now(UTC)
    store.chapters.replace_for_assignment(a.id, [(i0.id, "c1", "S0", 5340.0)], now)
    store.chapters.replace_for_assignment(b.id, [(i1.id, "c2", "S1", 5000.0)], now)
    assert store.chapters.loaded_item_ids_except(a.id) == {i1.id}
    assert store.chapters.held_since(a.id, now) == {i0.id}
    store.chapters.replace_for_assignment(a.id, [(i1.id, "c3", "S1", 5000.0)], now)
    hist = store.chapters.history(a.id)
    assert [h["item_id"] for h in hist][:2] == [i1.id, i0.id] and hist[1]["until"] is not None


def test_runs_and_events(store):
    r = store.runs.start("cli", dry_run=True)
    store.runs.event(r.id, "plan", assignment_id=None, item="x")
    store.runs.finish(r.id, "DRY_RUN")
    assert store.runs.get(r.id).outcome == "DRY_RUN"
    assert store.runs.events(r.id)[0].payload == {"item": "x"}


def test_settings_json(store):
    store.settings.set("schedule", "15:00")
    store.settings.set("cap_seconds", 5340)
    assert store.settings.get("schedule") == "15:00" and store.settings.get("cap_seconds") == 5340
    assert store.settings.get("missing", 7) == 7


def test_delete_library_cascades_items(store):
    lib = store.libraries.create("L")
    add(store, lib, "v0")
    store.libraries.delete(lib.id)
    assert store.items.list(lib.id) == []


def test_timestamps_normalize_to_utc_so_offset_changes_dont_break_ordering(store):
    # Review fix round 1 (Important 2): everything must be stored normalized to
    # UTC, not the caller's local offset — Australia/Melbourne moves between
    # AEST (UTC+10) and AEDT (UTC+11), so a plain string ORDER BY over
    # differently-offset strings can silently misorder rows spanning a DST
    # transition. Simulate that: two instants an hour apart in real (UTC)
    # time, but with the SAME local wall-clock reading because the offset
    # itself shifted by an hour in between (exactly what a DST transition
    # does). If the store still stored local offsets, these two rows would
    # compare equal or backwards as strings; normalized to UTC they sort
    # correctly.
    aedt = timezone(timedelta(hours=11))  # Melbourne daylight time
    aest = timezone(timedelta(hours=10))  # Melbourne standard time
    earlier_local = datetime(2026, 4, 5, 2, 30, tzinfo=aedt)  # 2026-04-04T15:30:00+00:00
    later_local = datetime(2026, 4, 5, 2, 30, tzinfo=aest)    # 2026-04-04T16:30:00+00:00 (an hour later)

    a = store.assignments.upsert_target("fake", "T1", "x")
    store.assignments.set_last_success(a.id, earlier_local)
    first = store.conn.execute(
        "SELECT last_success_at FROM assignment WHERE id = ?", (a.id,)
    ).fetchone()[0]
    store.assignments.set_last_success(a.id, later_local)
    second = store.conn.execute(
        "SELECT last_success_at FROM assignment WHERE id = ?", (a.id,)
    ).fetchone()[0]

    assert first.endswith("+00:00") and second.endswith("+00:00")  # normalized to UTC
    assert first < second   # plain string comparison still sorts chronologically


def test_naive_datetime_rejected(store):
    a = store.assignments.upsert_target("fake", "T1", "x")
    with pytest.raises(ValueError):
        store.assignments.set_last_success(a.id, datetime(2026, 4, 5, 2, 30))  # no tzinfo


def test_r5_playlist_source_id_round_trips(store):
    lib = store.libraries.create("L")
    item = store.items.add(
        lib.id, ItemKind.PLAYLIST_ENTRY, "https://example.invalid/p1", "p1", "T",
        playlist_source_id="playlist-abc",
    )
    assert item.playlist_source_id == "playlist-abc"
    assert store.items.get(item.id).playlist_source_id == "playlist-abc"
