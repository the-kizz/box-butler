"""Stage-then-swap: the tests that matter most (Task 21; spec §2).

The parametrised `test_staging_failure_never_clears_the_tonie` is the single
most important test in Box Butler: it injects a failure at each of the four
points before the line past which destruction is allowed (fetch, render,
verify, snapshot) and asserts that `clear` was **never called** and the
tonie's chapters are byte-identical to what they were. A tonie still holding
last night's story is an acceptable outcome; an empty one is not.

No test here touches a real tonie, the network, or ffmpeg: `FakeSink`,
`FakeFetcher`, `FakeRenderer`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boxbutler.domain.models import AssignmentState, LibraryMode, RunOutcome
from boxbutler.fetch.protocol import ExtractionBroken, ItemUnavailable
from boxbutler.orchestrator import events as E
from boxbutler.orchestrator.run import AssignmentReport, RunReport
from boxbutler.sources.protocol import SourceError
from tests.orchestrator.conftest import (
    events,
    mutating_calls,
    rendition_cache_name,
    source_cache_name,
)


def test_happy_path_follows_the_exact_order(world):
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    ev = [e for e in events(world["store"], rep.run_id) if e in E.STAGE_ORDER + E.SWAP_ORDER]
    assert ev == ["plan", "fetch", "render", "verify", "snapshot", "clear", "upload",
                  "settle", "commit"]
    assert mutating_calls(world["sink"]) == ["clear", "upload", "settle"]
    assert len(list(world["snaps"].glob("*.json"))) == 1
    assert rep.outcome == "OK"


def test_snapshot_is_written_before_clear(world):
    """Not just event order: the file must exist on disk at the moment
    `clear` is entered."""
    sink = world["sink"]
    snaps = world["snaps"]
    original_clear = sink.clear

    def clear_spy(t):
        assert len(list(snaps.glob("*.json"))) == 1, "clear called before snapshot was written"
        original_clear(t)

    sink.clear = clear_spy
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED


def _break_snapshot(w):
    """Make `write_snapshot` fail: the snapshot directory's own path is
    occupied by a regular file, so `mkdir` raises OSError -> SnapshotError."""
    w["snaps"].write_bytes(b"not a directory")


@pytest.mark.parametrize("stage,break_it", [
    ("fetch", lambda w: w["fetcher"].fail.update(
        {i.id: ExtractionBroken("solver broke") for i in w["items"]})),
    ("fetch-unavailable-all", lambda w: w["fetcher"].fail.update(
        {i.id: ItemUnavailable("private") for i in w["items"]})),
    ("render", lambda w: w["renderer"].fail.update(
        {source_cache_name(i) for i in w["items"]})),
    ("verify", lambda w: w["renderer"].durations.update(
        {rendition_cache_name(i): 100.0 for i in w["items"]})),
    ("snapshot", _break_snapshot),
])
def test_staging_failure_never_clears_the_tonie(world, stage, break_it):
    before = list(world["sink"].chapters["T1"])
    break_it(world)
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome in (RunOutcome.ABORTED_STAGING, RunOutcome.PLAN_FAILED)
    assert "clear" not in [c[0] for c in world["sink"].calls], f"clear was called after {stage} failure"
    assert "upload" not in [c[0] for c in world["sink"].calls]
    assert world["sink"].chapters["T1"] == before          # byte-identical LiveChapter tuples
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.OK
    assert world["store"].assignments.get(world["a"].id).cursor_position == 0
    if stage != "snapshot":
        # nothing was destroyed, so there was nothing to snapshot
        assert list(world["snaps"].glob("*.json")) == []
    assert rep.outcome == "FAILED"


def test_snapshot_failure_is_an_abort_not_a_warning(world):
    """A SnapshotError is the one "stage" failure that happens after VERIFY.
    Clearing without a surviving record of what was destroyed is exactly the
    failure write-once snapshots exist to prevent, so it aborts."""
    _break_snapshot(world)
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.ABORTED_STAGING
    assert rep.reports[0].detail == "snapshot"
    assert world["sink"].calls_named("clear") == []
    staging = [
        e for e in world["store"].runs.events(rep.run_id) if e.event == E.STAGING_FAILED
    ]
    assert staging and staging[0].payload["reason"] == "snapshot"


def test_extraction_broken_aborts_without_trying_next_item(world):
    world["fetcher"].fail[world["items"][0].id] = ExtractionBroken("n challenge")
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.ABORTED_STAGING
    assert world["fetcher"].calls == [world["items"][0].id]
    assert "extraction_broken" in events(world["store"], rep.run_id)
    # not marked unavailable: the extractor is broken, not the video
    assert world["store"].items.get(world["items"][0].id).state == "ok"


def test_item_unavailable_skips_to_next_and_marks_it(world):
    world["fetcher"].fail[world["items"][0].id] = ItemUnavailable("private")
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert world["store"].items.get(world["items"][0].id).state == "unavailable"
    assert world["sink"].chapters["T1"][0].title == "Story 1"          # sanitised: no emoji
    assert "item_unavailable" in events(world["store"], rep.run_id)


def test_chapter_title_reaching_the_sink_is_sanitised(world):
    world["orch"].run(apply=True)
    up = world["sink"].calls_named("upload")[0]
    assert up[3] == "Story 0" and "\N{SPAGHETTI}" not in up[3]


def test_dry_run_is_default_and_touches_nothing(world):
    rep = world["orch"].run()
    assert rep.dry_run and rep.reports[0].outcome == RunOutcome.DRY_RUN
    assert mutating_calls(world["sink"]) == [] and world["fetcher"].calls == []
    assert list(world["snaps"].glob("*.json")) == []
    assert list(world["cache"].iterdir()) == []
    assert "dry_run_plan" in events(world["store"], rep.run_id)


def test_upload_retries_same_verified_file_then_succeeds(world):
    world["sink"].fail_upload_times = 2
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    ups = world["sink"].calls_named("upload")
    assert len(ups) == 3 and len({u[2] for u in ups}) == 1             # same path each time
    # the bytes were already known good: no re-fetch, no re-render
    assert world["fetcher"].calls.count(world["items"][0].id) == 1
    assert len(world["renderer"].calls) == 1
    assert events(world["store"], rep.run_id).count("upload_retry") == 2


def test_upload_exhausted_degrades_and_keeps_the_assignment_out_of_rotation(world):
    """Past the point of no return the tonie is cleared and not filled. That
    must be sticky: the next run repairs, it does not rotate."""
    world["sink"].fail_upload_times = 99
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert rep.outcome == "DEGRADED"
    a = world["store"].assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    assert a.cursor_position == 0                          # no commit, no advance
    assert world["store"].chapters.for_assignment(a.id) == []
    assert "degraded" in events(world["store"], rep.run_id)
    # the verified bytes are kept, so Task 22's repair need not re-download
    staged = json.loads(a.staged_json)
    assert len(staged) == 1
    assert staged[0]["item_id"] == world["items"][0].id
    assert staged[0]["title"] == "Story 0"
    assert Path(staged[0]["path"]).exists()


def test_clear_failure_degrades_and_lets_the_other_tonies_continue(world, store):
    """A flaky cloud `clear` is an expected failure, not a bug: a real
    `tonie_api` clear can fail having partially emptied a tonie, so the
    assignment must go DEGRADED with its staged files — not be left `OK` while
    the whole run crashes and the second tonie is never processed."""
    sink = world["sink"]
    sink.add_target("T2", "Blue", [])
    b = store.assignments.upsert_target("fake", "T2", "Blue")
    store.assignments.assign_library(b.id, world["lib"].id)
    store.assignments.set_cursor(b.id, 1)
    sink.fail_clear = True

    rep = world["orch"].run(apply=True)
    by = {r.target_name: r.outcome for r in rep.reports}
    assert by["Green Tonie"] == RunOutcome.DEGRADED
    assert by["Blue"] == RunOutcome.DEGRADED               # its clear fails too
    assert rep.reports[0].detail.startswith("clear:")
    a = store.assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    assert json.loads(a.staged_json)[0]["item_id"] == world["items"][0].id
    assert store.runs.list(1)[0].outcome == "DEGRADED"     # a handled failure, not CRASHED
    assert len(sink.calls_named("clear")) == 2             # both tonies were attempted
    assert sink.calls_named("upload") == []                # nothing uploaded after a failed clear


@pytest.mark.parametrize("setup,reason", [
    (lambda s: setattr(s, "settle_seconds_override", 11.0), "duration_mismatch"),
    (lambda s: setattr(s, "settle_seconds_override", 0.0), "settled_empty"),
    (lambda s: setattr(s, "settle_after_polls", 10**6), "timeout"),
])
def test_settle_reasons_are_distinct_not_collapsed(world, setup, reason):
    """`timeout`, `duration_mismatch` and `settled_empty` mean different
    things to whoever reads the run log; all three are failures, none of
    them is success."""
    setup(world["sink"])
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert rep.reports[0].detail == reason
    assert world["store"].chapters.for_assignment(world["a"].id) == []
    settle_events = [e for e in world["store"].runs.events(rep.run_id) if e.event == E.SETTLE]
    assert settle_events[0].payload["reason"] == reason
    assert settle_events[0].payload["settled"] is False


def test_new_target_appears_unmanaged_and_is_untouched(world):
    world["sink"].add_target("T9", "Brand New", [])
    rep = world["orch"].run(apply=True)
    outcomes = {r.target_name: r.outcome for r in rep.reports}
    assert outcomes["Brand New"] == RunOutcome.UNMANAGED
    assert all(c[1] != "T9" for c in world["sink"].calls if c[0] in ("clear", "upload"))


def test_one_tonie_failing_does_not_stop_the_others(world, store):
    sink = world["sink"]
    sink.add_target("T2", "Blue", [])
    b = store.assignments.upsert_target("fake", "T2", "Blue")
    store.assignments.assign_library(b.id, world["lib"].id)
    # T1 plans item 0 and breaks; T2 (cursor 0) would also plan item 0...
    world["fetcher"].fail[world["items"][0].id] = ExtractionBroken("x")
    store.assignments.set_cursor(b.id, 1)                 # ...so give T2 a different cursor
    rep = world["orch"].run(apply=True)
    by = {r.target_name: r.outcome for r in rep.reports}
    assert by["Green Tonie"] == RunOutcome.ABORTED_STAGING and by["Blue"] == RunOutcome.SWAPPED
    assert sink.chapters["T1"][0].title == "Old Story"     # Green Tonie keeps last night's story


def test_source_error_is_reported_distinctly_never_as_an_empty_library(world, store):
    """"Could not resolve" is not "the playlist is empty" (cross-task
    requirement 6). Task 23 wired `_sync_sources` to call a real
    `Deps.playlist_source`; this drives that seam through its actual
    behaviour (a resolver that raises) rather than a hand-written stub of
    the private method, so it can't drift out of sync with
    `_sync_sources`'s own signature again."""
    orch = world["orch"]

    class BrokenSource:
        def matches(self, ref):
            return True

        def resolve(self, ref):
            raise SourceError("playlist 403")

    store.settings.set(f"playlists:{world['lib'].id}", ["pl"])
    world["deps"].playlist_source = BrokenSource()

    rep = orch.run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.PLAN_FAILED
    assert rep.reports[0].detail == "source_error:playlist 403"
    assert "source_error" in events(world["store"], rep.run_id)
    assert "plan_failed" not in events(world["store"], rep.run_id)     # not a silent "empty"
    assert mutating_calls(world["sink"]) == []


def test_disabled_assignment_is_skipped_entirely(world):
    world["store"].assignments.set_enabled(world["a"].id, False)
    rep = world["orch"].run(apply=True)
    assert rep.reports == []
    assert mutating_calls(world["sink"]) == []


def test_degraded_assignment_never_rotates_via_run_assignment(world):
    """While DEGRADED, `can_rotate` is False — `run()` must route this
    assignment to `repair()`, never to `run_assignment()`/`swap()`. A `[]`
    `staged_json` (no chapters ever recorded, an edge case) has nothing to
    reuse, so Task 22's `repair` falls back to staging fresh content — see
    `tests/orchestrator/test_degraded.py` for the full repair behaviour.
    This test only pins down that the *route* is `repair`, not `swap`: the
    tonie is cleared at most once here, never twice via a stray `swap` call.

    A real DEGRADED assignment's tonie is already empty (that is what
    DEGRADED means — cleared, not filled), so the fixture's target is
    emptied here to match; `repair()` itself must never be the one to do it.
    """
    world["sink"].chapters["T1"] = []
    world["store"].assignments.set_state(
        world["a"].id, AssignmentState.DEGRADED, staged_json="[]"
    )
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert world["sink"].calls_named("clear") == []       # repair() never clears
    assert "repair_start" in events(world["store"], rep.run_id)
    assert "repair_restaged" in events(world["store"], rep.run_id)


def test_run_report_outcome_aggregates(world):
    """An empty run is NOT OK (final safety review, M4).

    `OK` has to mean "something happened and it worked". It used to be what a
    run with zero assignment reports returned, which is how both of the
    review's probes — one where every assignment row was keyed under a sink
    name the run never looked up, one where the run crashed before touching a
    thing — recorded a clean success while doing nothing at all.
    """
    assert RunReport("r", False, []).outcome == "NOTHING_TO_DO"
    assert RunReport("r", False, [], no_targets_but_managed=True).outcome == "FAILED"
    ok = AssignmentReport("a", "Green Tonie", RunOutcome.SWAPPED)
    assert RunReport("r", False, [ok]).outcome == "OK"
    crashed = AssignmentReport("a", "Green Tonie", RunOutcome.CRASHED, "crashed:KeyError")
    assert RunReport("r", False, [ok, crashed]).outcome == "FAILED"


def test_cursor_advances_by_n_not_by_one(store, world):
    """Cross-task requirement 1: an 8-item serial load from cursor 3 of 10
    items must land on cursor 1. Advancing by one would replay chapter 2
    every night and make an audiobook take eight times as long to finish."""
    from boxbutler.domain.models import ItemKind

    sink = world["sink"]
    sink.add_target("T3", "Serial", [])
    lib = store.libraries.create("Audiobook", LibraryMode.SERIAL)
    items = [
        store.items.add(lib.id, ItemKind.YOUTUBE, f"u/s{i}", f"ser{i}", f"Chapter {i}")
        for i in range(10)
    ]
    a = store.assignments.upsert_target("fake", "T3", "Serial")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_cursor(a.id, 3)
    store.assignments.set_fit(a.id, False)      # no partial tail: the 9th is dropped, not cut

    # 600 s each: 8 fit under the 5340 cap (4800 s), the 9th would overflow.
    tmp = world["cache"].parent
    for it in items:
        p = tmp / f"ser-{it.source_key}.m4a"
        p.write_bytes(b"aac" * 100)
        world["fetcher"].files[it.id] = p
        world["renderer"].durations[source_cache_name(it)] = 600.0

    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert len(sink.chapters["T3"]) == 8
    assert store.assignments.get(a.id).cursor_position == 1          # (3 + 8) % 10
