"""The swap's failure surface: unclassified exceptions, process kills, and
the one independent expectation (final safety review, C2, C3, M2, M3, M4).

Everything here is about the layer *around* the central invariant. That
invariant held: a tonie is never cleared before its replacement is
downloaded, trimmed and verified. What did not hold is what happens when
something fails anyway — the system believed it was fine and said nothing.
"""
from __future__ import annotations

import json

import pytest

from boxbutler.domain.models import (
    AssignmentState,
    ItemKind,
    LibraryMode,
    RunOutcome,
)
from boxbutler.domain.cache_name import cache_name
from boxbutler.orchestrator.reconcile import reconcile_interrupted_swaps
from boxbutler.sinks.protocol import LiveChapter
from tests.conftest import events


def _feed_item(world, store, key, title, feed_seconds, on_disk_seconds):
    """One RSS item whose feed says `feed_seconds` and whose file probes at
    `on_disk_seconds`, assigned to the tonie. The duration is keyed by the
    *cache* name because that is the file the orchestrator probes (what
    `FakeFetcher` copies the source to)."""
    lib = store.libraries.create(f"Podcast {key}", LibraryMode.SINGLE)
    item = store.items.add(
        lib.id, ItemKind.RSS, f"https://feed.invalid/{key}.mp3", key, title,
        seconds=feed_seconds,
    )
    src = world["cache"].parent / f"{key}.mp3"
    src.write_bytes(b"mp3" * 100)
    world["fetcher"].files[item.id] = src
    world["renderer"].durations[cache_name(title, key, "mp3")] = on_disk_seconds
    store.assignments.assign_library(world["a"].id, lib.id)
    return item


def _second_tonie(world, store, name="Panda", target_id="T2"):
    """A second managed tonie on the same library, so "one tonie's failure
    must not cost the others their night" can be asserted rather than
    assumed."""
    world["sink"].add_target(target_id, name, [LiveChapter("old2", "Other Story", 5340.0, False)])
    a = store.assignments.upsert_target(world["sink"].name, target_id, name)
    store.assignments.assign_library(a.id, world["lib"].id)
    return store.assignments.get(a.id)


# ------------------------------------------------------------------ C2 / M3


def test_unclassified_settle_failure_degrades_and_the_next_tonie_still_runs(world, store):
    """C2: `settle` raising anything at all must be a DEGRADED, not a crash.

    The reviewer's probe: a `ConnectionError` from the settle poll (live HTTP
    in the real sink) escaped `swap()` entirely. The run was recorded
    `CRASHED`, the assignment was left `state=OK, staged_json=NULL` despite
    having been cleared and holding one still-transcoding chapter — so no
    DEGRADED, no repair next run, no failure notification, no
    `BoxButlerTonieDegraded` — and tonies #2 and #3 were never processed.
    """
    second = _second_tonie(world, store)
    sink = world["sink"]
    real_settle = sink.settle
    boom = {"done": False}

    def settle_once_then_work(target, expect_seconds, timeout_s):
        if not boom["done"]:
            boom["done"] = True
            raise ConnectionError("connection reset by peer while polling chapters")
        return real_settle(target, expect_seconds, timeout_s)

    sink.settle = settle_once_then_work

    rep = world["orch"].run(apply=True)

    outcomes = {r.target_name: r.outcome for r in rep.reports}
    assert outcomes["Green Tonie"] == RunOutcome.DEGRADED
    assert outcomes[second.target_name] in (RunOutcome.SWAPPED, RunOutcome.DEGRADED)
    assert rep.outcome == "DEGRADED"          # not CRASHED, and never OK

    a = store.assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    staged = json.loads(a.staged_json)
    assert staged and staged[0]["path"], "the verified staged files must survive for the repair"

    # The other tonie got its night (spec §4).
    assert sink.calls_named("upload"), "no upload happened at all"
    assert "T2" in {c[1] for c in sink.calls_named("clear")}


def test_unclassified_failure_before_any_clear_is_reported_not_fatal(world, store):
    """M3: a pre-CLEAR sink read that raises must cost one tonie, not the run.

    The tonie is untouched, which is correct — but the run used to be marked
    CRASHED and every later tonie silently skipped. Now it is one `CRASHED`
    assignment report (which makes the run FAILED, i.e. loud) while the rest
    of the run continues.
    """
    second = _second_tonie(world, store)
    sink = world["sink"]
    real_read = sink.read_chapters

    def read_boom(target):
        if target.id == "T1":
            raise KeyError("unknown target T1")
        return real_read(target)

    sink.read_chapters = read_boom

    rep = world["orch"].run(apply=True)
    outcomes = {r.target_name: r.outcome for r in rep.reports}
    assert outcomes["Green Tonie"] == RunOutcome.CRASHED
    assert outcomes[second.target_name] == RunOutcome.SWAPPED
    assert rep.outcome == "FAILED"

    # Untouched, and not mislabelled as degraded: nothing was destroyed.
    assert store.assignments.get(world["a"].id).state == AssignmentState.OK
    assert {c[1] for c in sink.calls_named("clear")} == {"T2"}
    assert "crashed" in events(store, rep.run_id, world["a"].id)


# ------------------------------------------------------------------ C3


def test_marker_is_committed_before_the_clear_and_gone_after_commit(world, store):
    """C3: the write-ahead marker exists exactly during the danger window."""
    sink = world["sink"]
    seen: dict[str, object] = {}
    real_clear = sink.clear

    def clear_and_look(target):
        # Read the marker at the instant the tonie is empty — the state a
        # SIGKILL here would leave committed on disk.
        seen["marker"] = store.swap_markers.get(world["a"].id)
        return real_clear(target)

    sink.clear = clear_and_look

    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED

    marker = seen["marker"]
    assert marker is not None, "nothing was persisted before the destructive act"
    assert json.loads(marker.staged_json)[0]["path"]
    assert marker.run_id == rep.run_id
    # Cleared by COMMIT: a successful swap leaves no marker to reconcile.
    assert store.swap_markers.get(world["a"].id) is None


def test_process_killed_between_clear_and_upload_is_reconciled_to_degraded(world, store):
    """C3: the kill window, end to end.

    Simulates the SIGKILL by letting `clear()` happen and then abandoning the
    process (a raised `BaseException` the orchestrator cannot catch), exactly
    as an OOM kill would: nothing after the clear runs. The committed
    database must then be reconciled to DEGRADED at the next start instead of
    reading `state=OK, staged_json=NULL, records=0` over an empty tonie for
    ~24 hours.
    """
    sink = world["sink"]
    real_clear = sink.clear

    class Killed(BaseException):
        """Not an `Exception`: nothing in the run loop may catch it."""

    def clear_then_die(target):
        real_clear(target)
        raise Killed()

    sink.clear = clear_then_die

    with pytest.raises(Killed):
        world["orch"].run(apply=True)

    # The state a restarting process finds: the tonie is empty *right now*.
    assert sink.chapters["T1"] == []
    killed = store.assignments.get(world["a"].id)
    assert killed.state == AssignmentState.OK     # nothing got the chance to say otherwise
    assert store.swap_markers.get(killed.id) is not None

    # --- restart ---
    logged: list[tuple[str, dict]] = []
    assert reconcile_interrupted_swaps(store, log=lambda e, p: logged.append((e, p))) == [
        killed.id
    ]
    after = store.assignments.get(killed.id)
    assert after.state == AssignmentState.DEGRADED
    assert json.loads(after.staged_json)[0]["path"]
    assert store.swap_markers.get(killed.id) is None      # idempotent
    assert reconcile_interrupted_swaps(store) == []
    assert logged and logged[0][0] == "swap_marker_reconciled"

    # And the next run repairs rather than rotating, from the staged bytes.
    sink.clear = real_clear
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert store.assignments.get(killed.id).state == AssignmentState.OK
    assert [c.title for c in sink.chapters["T1"]] == [
        c["title"] for c in json.loads(after.staged_json)
    ]


def test_reconciliation_uses_the_markers_staged_files(world, store):
    """The marker's `staged_json` is what a repair needs, so it wins."""
    store.swap_markers.put(
        world["a"].id, "run-x",
        json.dumps([{"item_id": "i1", "path": "/cache/x.m4a", "title": "X", "seconds": 10.0}]),
        world["deps"].clock(),
    )
    reconcile_interrupted_swaps(store)
    a = store.assignments.get(world["a"].id)
    assert a.state == AssignmentState.DEGRADED
    assert json.loads(a.staged_json)[0]["path"] == "/cache/x.m4a"


# ------------------------------------------------------------------ M2


def test_source_materially_shorter_than_the_feed_says_never_clears_a_tonie(world, store):
    """M2: the independent expectation, and the governing rule.

    `item.seconds` comes from `<itunes:duration>` — written before the file
    was ever downloaded, by something other than the thing being checked. It
    was read by nothing in the orchestrator, verify or audio layers, so a
    download that arrived at 900 s for a 3600 s episode verified perfectly
    against its own probe and the run reported `SWAPPED` with "now has
    Episode One (15 min)".
    """
    # The feed says 3600 s; 900 s arrived on disk -- a truncated download.
    _feed_item(world, store, "ep1", "Episode One", 3600.0, 900.0)

    rep = world["orch"].run(apply=True)

    assert rep.reports[0].outcome == RunOutcome.ABORTED_STAGING
    assert rep.reports[0].detail == "source_duration_mismatch"
    assert rep.outcome == "FAILED"
    # When in doubt, the tonie keeps last night's story.
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Old Story"]
    assert world["sink"].calls_named("clear") == []
    assert "source_duration_mismatch" in events(store, rep.run_id)


def test_source_within_tolerance_or_longer_than_the_feed_says_is_allowed(world, store):
    """Rounding and ad insertion are not truncation.

    A published duration is rounded, and dynamic ad insertion legitimately
    makes an episode *longer* than the feed says. Nothing is missing in
    either case, and `fit_fill` caps what is actually used — so aborting
    would cost a child the story for a mismatch that carries no risk. The
    dangerous direction, and only that one, raises.
    """
    # 10 minutes of inserted ads past the published duration.
    _feed_item(world, store, "ep2", "Episode Two", 3600.0, 4200.0)

    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Episode Two"]
    # Reported rather than silently accepted.
    assert "source_duration_mismatch" in events(store, rep.run_id)


def test_no_feed_duration_is_recorded_as_having_no_independent_expectation(world, store):
    """The honest unknown: nothing independent to check against, said so."""
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    payloads = [
        e.payload for e in store.runs.events(rep.run_id) if e.event == "source_duration"
    ]
    assert payloads and all(p["independent"] is False for p in payloads)


# ------------------------------------------------------------------ M4


def test_a_run_that_processed_nothing_is_not_ok(world, store):
    """M4: `NOTHING_TO_DO`, not `OK`."""
    rep = world["orch"].run(apply=True, assignment_ids=["no-such-assignment"])
    assert rep.reports == []
    assert rep.outcome == "NOTHING_TO_DO"
    assert store.runs.get(rep.run_id).outcome == "NOTHING_TO_DO"


def test_an_empty_target_listing_with_managed_tonies_is_a_failure(world, store):
    """M4: "the cloud listed nothing" is never "nothing to do"."""
    world["sink"].list_targets = lambda: []
    rep = world["orch"].run(apply=True)
    assert rep.outcome == "FAILED"
    assert "no_targets" in events(store, rep.run_id)
