"""Idempotency at run level (Task 21; spec §3.3, §10.3).

Running twice in a night must not re-upload, must not advance the cursor,
and must not be confused with a failure: `already_current() == True` maps to
`SKIPPED_ALREADY_CURRENT`, a success, never to `PLAN_FAILED`. Identity is
`sink_chapter_id`, never the title.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from boxbutler.domain.models import AssignmentState, RunOutcome
from boxbutler.orchestrator.run import rotation_window_key
from tests.orchestrator.conftest import events


def test_second_run_is_skipped_with_zero_sink_writes(world):
    o = world["orch"]
    o.run(apply=True)
    n = len([c for c in world["sink"].calls if c[0] in ("clear", "upload")])
    rep = o.run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT
    assert len([c for c in world["sink"].calls if c[0] in ("clear", "upload")]) == n
    assert world["fetcher"].calls.count(world["items"][0].id) == 1
    # a skip is a success, not a planning failure (cross-task requirement 3)
    ev = events(world["store"], rep.run_id)
    assert "skipped_already_current" in ev and "plan_failed" not in ev
    assert rep.outcome == "OK"


def test_cursor_moves_only_on_verified_settled_upload(world):
    o = world["orch"]
    a = world["a"]
    o.run(apply=True)
    assert world["store"].assignments.get(a.id).cursor_position == 1
    o.run(apply=True)
    assert world["store"].assignments.get(a.id).cursor_position == 1   # skipped -> no move


def test_three_runs_one_upload(world):
    for _ in range(3):
        world["orch"].run(apply=True)
    assert len(world["sink"].calls_named("upload")) == 1


def test_duration_tolerance_respected(world):
    world["orch"].run(apply=True)
    ch = world["sink"].chapters["T1"][0]
    world["sink"].chapters["T1"] = [ch.__class__(ch.id, ch.title, ch.seconds + 1.5, False)]
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT
    world["sink"].chapters["T1"] = [ch.__class__(ch.id, ch.title, ch.seconds - 300, False)]
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED


def test_retitled_upstream_still_matches_on_id(world):
    world["orch"].run(apply=True)
    world["store"].items.set_title(world["items"][0].id, "Story 0 (Remastered)")
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT


def test_a_chapter_still_transcoding_is_not_already_current(world):
    """An ambiguous live state is never read as success: a chapter the cloud
    has not finished with might yet settle to the wrong thing."""
    world["orch"].run(apply=True)
    ch = world["sink"].chapters["T1"][0]
    world["sink"].chapters["T1"] = [ch.__class__(ch.id, ch.title, ch.seconds, True)]
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED


MELBOURNE = "Australia/Melbourne"


def _at(world, when: datetime) -> None:
    """Freeze the orchestrator's clock (and so the rotation window) at `when`."""
    world["deps"].clock = lambda: when


def _melbourne_settings(world) -> None:
    world["deps"].settings.timezone = MELBOURNE
    world["deps"].settings.schedule = "15:00"


def test_rotation_window_key_is_the_local_day_not_the_utc_day():
    """09:00 and 15:00 on one Melbourne day are 23:00Z the day before and
    05:00Z — two UTC dates, one local day, one rotation window."""
    morning = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)      # 09:00 Sun 13, Melbourne
    evening = datetime(2026, 9, 13, 5, 0, tzinfo=UTC)       # 15:00 Sun 13, Melbourne
    next_day = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)      # 15:00 Mon 14, Melbourne
    assert rotation_window_key(morning, "15:00", MELBOURNE) == rotation_window_key(
        evening, "15:00", MELBOURNE
    )
    assert rotation_window_key(evening, "15:00", MELBOURNE) != rotation_window_key(
        next_day, "15:00", MELBOURNE
    )


@pytest.mark.parametrize("bad", [
    lambda: rotation_window_key(datetime(2026, 9, 13, 5, 0), "15:00", MELBOURNE),   # naive
    lambda: rotation_window_key(datetime(2026, 9, 13, 5, 0, tzinfo=UTC), "half past", MELBOURNE),
    lambda: rotation_window_key(datetime(2026, 9, 13, 5, 0, tzinfo=UTC), "15:00", "Mars/Olympus"),
])
def test_rotation_window_key_refuses_rather_than_guesses(bad):
    """A naive timestamp, an unparseable schedule and an unresolvable zone all
    raise, so `_rotation_due` fails open (rotate) instead of comparing windows
    in a zone nobody asked for."""
    with pytest.raises(ValueError):
        bad()


def test_two_runs_in_one_local_day_do_not_rotate_twice(world):
    """The defect this window exists to prevent: a morning manual --apply plus
    the evening scheduled run would destroy the morning's story before bedtime,
    so the child never hears it. Same local day across a UTC midnight → skip."""
    _melbourne_settings(world)
    _at(world, datetime(2026, 9, 12, 23, 0, tzinfo=UTC))         # 09:00 local
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED
    first = list(world["sink"].chapters["T1"])

    _at(world, datetime(2026, 9, 13, 5, 0, tzinfo=UTC))          # 15:00 local, same day
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT
    assert world["sink"].chapters["T1"] == first                  # the morning's story survives
    assert len(world["sink"].calls_named("upload")) == 1
    assert world["store"].assignments.get(world["a"].id).cursor_position == 1


def test_the_next_local_day_does_rotate(world):
    _melbourne_settings(world)
    _at(world, datetime(2026, 9, 13, 5, 0, tzinfo=UTC))          # 15:00 Sun, local
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED
    _at(world, datetime(2026, 9, 14, 5, 0, tzinfo=UTC))          # 15:00 Mon, local
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert world["store"].assignments.get(world["a"].id).cursor_position == 2
    assert len(world["sink"].calls_named("upload")) == 2


def test_a_future_last_success_does_not_stop_rotation_forever(world):
    """Fail OPEN, not closed. A `last_success_at` 30 days ahead — an NTP step,
    a restored backup, a mis-written row — must not silently park the service
    on SKIPPED_ALREADY_CURRENT while the library changes underneath."""
    _melbourne_settings(world)
    _at(world, datetime(2026, 9, 13, 5, 0, tzinfo=UTC))
    world["orch"].run(apply=True)
    store = world["store"]
    store.assignments.set_last_success(world["a"].id, datetime(2026, 10, 13, 5, 0, tzinfo=UTC))
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED           # a different window is due
    assert len(world["sink"].calls_named("upload")) == 2


def test_a_naive_last_success_is_treated_as_due(world):
    """`naive.astimezone()` does not raise — it reinterprets in the host zone.
    An unknown offset is never read as a definite one."""
    _melbourne_settings(world)
    _at(world, datetime(2026, 9, 13, 5, 0, tzinfo=UTC))
    world["orch"].run(apply=True)
    world["store"].conn.execute(
        "UPDATE assignment SET last_success_at = '2026-09-13T15:00:00' WHERE id = ?",
        (world["a"].id,),
    )
    assert world["store"].assignments.get(world["a"].id).last_success_at.tzinfo is None
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SWAPPED


def test_crashed_run_does_not_skip_a_story(world):
    """Cross-task requirement 7: a crash between CLEAR and COMMIT leaves no
    chapter_record, so the story is never silently skipped.

    What "loud" means here changed with the final safety review (C2/C3). The
    exception used to propagate out of `run()`, which recorded the run CRASHED
    and abandoned every *other* tonie in the same run. Now a crash past the
    point where the tonie was touched lands in DEGRADED carrying the verified
    staged files -- loud in the way that actually reaches a human (the
    dashboard, `on_failure`, `BoxButlerTonieDegraded`) rather than only in the
    run log -- and the rest of the night continues. The requirement this test
    exists for is unchanged and still asserted: the same item is still loaded,
    the cursor has not moved, and nothing was skipped.
    """
    store = world["store"]
    o = world["orch"]
    original_commit = o._commit

    def boom(*a, **k):
        raise RuntimeError("crash")

    o._commit = boom
    rep = o.run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED       # never OK, never silent
    o._commit = original_commit

    assert store.assignments.get(world["a"].id).cursor_position == 0
    assert store.runs.list(1)[0].outcome == "DEGRADED"
    assert store.assignments.get(world["a"].id).state == AssignmentState.DEGRADED

    rep = o.run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED       # same item, now recorded
    assert store.chapters.for_assignment(world["a"].id)[0].item_id == world["items"][0].id


def test_dry_run_after_a_swap_reports_skipped_not_dry_run(world):
    """The idempotency check runs before the dry-run branch, so a dry run
    tells the truth about a tonie that is already correct."""
    world["orch"].run(apply=True)
    rep = world["orch"].run()
    assert rep.reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT


def test_source_file_is_recorded_and_reused_as_a_cache_hit(world):
    """The cache index (migration 0002) is what makes "cache hit" a fact
    rather than a guess."""
    store = world["store"]
    rep = world["orch"].run(apply=True)
    sf = store.sources.get(world["items"][0].id)
    assert sf is not None and sf.bytes > 0 and sf.last_used_at is not None
    fetch_events = [e for e in store.runs.events(rep.run_id) if e.event == "fetch"]
    assert fetch_events[0].payload["cache_hit"] is False

    # force a re-stage of the same item: the source is already on disk
    store.chapters.replace_for_assignment(world["a"].id, [], world["deps"].clock())
    store.assignments.set_cursor(world["a"].id, 0)
    rep2 = world["orch"].run(apply=True)
    fetch2 = [e for e in store.runs.events(rep2.run_id) if e.event == "fetch"]
    assert fetch2[0].payload["cache_hit"] is True
    render2 = [e for e in store.runs.events(rep2.run_id) if e.event == "render"]
    assert render2[0].payload["cached"] is True              # verified rendition reused
    assert len(world["renderer"].calls) == 1                 # rendered once, ever
