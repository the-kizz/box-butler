"""DEGRADED is sticky; repair runs first and reuses the staged file (Task 22;
spec §2 "The window between 6 and 8", §10.4; R20).

Departures from the task brief's embedded sketch, all because the real code
(and, for R20, a later review finding) win over the brief
(R2, already ruled in `boxbutler/domain/state.py`):

1. `Assignment` carries one `staged_json` field (a JSON list of
   `{"item_id", "path", "title", "seconds"}`), never the brief's
   `staged_path` / `staged_titles_json` pair. So where the brief's sketch
   reads `a.staged_path` / `json.loads(a.staged_titles_json)`, these tests
   read `json.loads(a.staged_json)[0]["path"]` / `[...]["title"]`.
2. **R20 — repair observes the tonie before it acts; it does not assume
   "cleared" means "still empty".** The original Task-22 pass had `repair()`
   never clear, reasoned from "DEGRADED means the tonie is already empty."
   That premise holds for a single-chapter load but not for a multi-chapter
   one: if chapter 2 of 3 fails, chapters 0-1 are physically on the tonie
   when the assignment goes DEGRADED, so "never clear" meant every retried
   repair re-uploaded the full staged set on top of what was already there
   — unbounded duplication (see
   `test_partial_multichapter_repair_never_duplicates_chapters` below, the
   regression this review caught). Every test in the first pass used a
   `SINGLE`-mode library, where the prefix is always empty, so this never
   surfaced. `repair()` now reads live chapters first and only clears when
   the tonie is neither empty nor already exactly the staged set — see the
   `repair()` docstring for the three-way split and why that still honours
   "never destroy without a verified replacement in hand".
"""
import json
from pathlib import Path

from boxbutler.domain.models import AssignmentState, ItemKind, LibraryMode, RunOutcome
from boxbutler.sinks.protocol import LiveChapter
from tests.orchestrator.conftest import events, source_cache_name


def test_upload_failure_after_clear_becomes_degraded_with_staged_path(world):
    world["sink"].fail_upload_times = 99
    rep = world["orch"].run(apply=True)
    a = world["store"].assignments.get(world["a"].id)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED and a.state == AssignmentState.DEGRADED
    staged = json.loads(a.staged_json)
    assert staged[0]["path"].endswith("[vid0].trim5395.m4a")
    assert staged[0]["title"] == "Story 0"
    assert "degraded" in events(world["store"], rep.run_id)
    assert len(world["sink"].calls_named("upload")) == 3  # the retries happened


def test_settle_failure_is_degraded_too(world):
    world["sink"].settle_seconds_override = 100.0
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.DEGRADED


def test_degraded_is_sticky_across_runs_and_repaired_first(world, store):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    world["sink"].fail_upload_times = 0
    # A second healthy tonie, so we can prove the repair loop runs before any
    # rotation work regardless of target order.
    world["sink"].add_target("T2", "Blue", [])
    b = store.assignments.upsert_target("fake", "T2", "Blue")
    store.assignments.assign_library(b.id, world["lib"].id)
    store.assignments.set_cursor(b.id, 2)

    rep = world["orch"].run(apply=True)
    by = {r.target_name: r.outcome for r in rep.reports}
    assert by["Green Tonie"] == RunOutcome.REPAIRED and by["Blue"] == RunOutcome.SWAPPED

    ev = store.runs.events(rep.run_id)
    first_green = next(i for i, e in enumerate(ev) if e.assignment_id == world["a"].id)
    first_blue = next(i for i, e in enumerate(ev) if e.assignment_id == b.id)
    assert first_green < first_blue  # repair before rotation
    assert store.assignments.get(world["a"].id).state == AssignmentState.OK


def test_repair_reuses_staged_file_without_refetch_or_rerender(world):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    fetches, renders = list(world["fetcher"].calls), list(world["renderer"].calls)

    world["sink"].fail_upload_times = 0
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert world["fetcher"].calls == fetches and world["renderer"].calls == renders

    up = world["sink"].calls_named("upload")[-1]
    assert up[2].endswith("[vid0].trim5395.m4a") and up[3] == "Story 0"


def test_repair_advances_cursor_once_and_records_chapters(world):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    assert world["store"].assignments.get(world["a"].id).cursor_position == 0

    world["sink"].fail_upload_times = 0
    world["orch"].run(apply=True)
    a = world["store"].assignments.get(world["a"].id)
    assert a.cursor_position == 1 and a.last_success_at is not None
    assert [r.item_id for r in world["store"].chapters.for_assignment(a.id)] == [
        world["items"][0].id
    ]


def test_repair_restages_if_staged_file_vanished(world):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    Path(json.loads(world["store"].assignments.get(world["a"].id).staged_json)[0]["path"]).unlink()

    fetches_before = list(world["fetcher"].calls)
    world["sink"].fail_upload_times = 0
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    # A genuine restage: the vanished file forced a fresh fetch/render, not a
    # silent skip and not a silent "success" over a missing file.
    assert len(world["fetcher"].calls) > len(fetches_before)
    a = world["store"].assignments.get(world["a"].id)
    assert a.state == AssignmentState.OK


def test_repair_falls_back_to_fresh_staging_if_file_no_longer_verifies(world):
    """Not just "missing" -- a staged file that still exists but no longer
    passes `verify_rendition` (corrupted, truncated, whatever) must not be
    read as good. R3: when in doubt, stay degraded / re-derive, never assume."""
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    staged_path = Path(
        json.loads(world["store"].assignments.get(world["a"].id).staged_json)[0]["path"]
    )
    staged_path.write_bytes(b"")  # exists, but now empty -> fails verify_rendition

    world["sink"].fail_upload_times = 0
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.OK


def test_repair_dry_run_changes_nothing(world):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    n = len(world["sink"].calls)
    rep = world["orch"].run()
    assert rep.reports[0].outcome == RunOutcome.DRY_RUN
    assert len([c for c in world["sink"].calls[n:] if c[0] in ("clear", "upload")]) == 0
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.DEGRADED


def test_failed_repair_stays_degraded(world):
    world["sink"].fail_upload_times = 999
    world["orch"].run(apply=True)
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert world["store"].assignments.get(world["a"].id).state == AssignmentState.DEGRADED


def test_repair_of_a_still_empty_tonie_never_clears(world):
    """R20's empty-tonie branch: this fixture's library is SINGLE-mode, so a
    fully-failed upload leaves the tonie genuinely empty (the one chapter
    never made it on), and every retried repair finds it still empty. R5's
    concern — never clear an empty tonie for no reason — still holds; it is
    now a *consequence* of R20's observe-first rule rather than an
    unconditional "repair never clears"."""
    world["sink"].fail_upload_times = 999
    world["orch"].run(apply=True)
    n = len(world["sink"].calls)
    snaps_before = len(list(world["snaps"].glob("*.json")))
    world["orch"].run(apply=True)  # repair attempt #2, also fails
    assert [c for c in world["sink"].calls[n:] if c[0] == "clear"] == []
    assert len(list(world["snaps"].glob("*.json"))) == snaps_before  # nothing to snapshot


def test_pinned_assignment_still_repairs(world, store):
    store.assignments.set_pin(world["a"].id, world["items"][1].id)
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)

    world["sink"].fail_upload_times = 0
    assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.REPAIRED
    assert store.assignments.get(world["a"].id).cursor_position == 0  # pin: never advances


# --------------------------------------------------------------- R20: multi-chapter


def _build_multi(world, store, *, mode, target_id, target_name, n=3, seconds=500.0):
    """An assignment with `n` short items that all fit comfortably under the
    cap in one load — the ALBUM/SERIAL coverage the first pass was missing
    (every test above uses the base `world` fixture's SINGLE-mode library,
    which can never exercise a partially-filled tonie: one chapter is always
    all-or-nothing)."""
    sink = world["sink"]
    sink.add_target(target_id, target_name, [])
    lib = store.libraries.create(f"lib-{target_id}", mode)
    items = [
        store.items.add(lib.id, ItemKind.YOUTUBE, f"u/{target_id}{i}", f"{target_id}v{i}",
                         f"Story {i}")
        for i in range(n)
    ]
    a = store.assignments.upsert_target("fake", target_id, target_name)
    store.assignments.assign_library(a.id, lib.id)

    tmp = world["cache"].parent
    for it in items:
        p = tmp / f"src-{target_id}-{it.source_key}.m4a"
        p.write_bytes(b"aac" * 100)
        world["fetcher"].files[it.id] = p
        # Short enough to COPY (no ffmpeg trim needed) and to fit all `n`
        # items under the cap in one load.
        world["renderer"].durations[source_cache_name(it)] = seconds
    return store.assignments.get(a.id), items


def _fail_chapter(sink, title: str):
    """Make every upload of the chapter titled `title` raise, forever —
    unlike `FakeSink.fail_upload_times` (a global call counter that can't
    single out one chapter once earlier chapters have already succeeded).
    Restore with the returned callable."""
    from boxbutler.sinks.fake import SinkUploadError

    original = sink.upload

    def flaky(target, path, up_title):
        if up_title == title:
            raise SinkUploadError(f"simulated permanent failure for {title!r}")
        return original(target, path, up_title)

    sink.upload = flaky
    return lambda: setattr(sink, "upload", original)


def test_partial_multichapter_repair_never_duplicates_chapters(world, store):
    """The regression this review caught: chapter 2 of a 3-chapter ALBUM
    load permanently fails to upload. Chapters 0-1 are physically on the
    tonie when the assignment degrades — R20 requires repair to notice that
    and clear before re-uploading the full set, rather than appending it on
    top of the surviving prefix on every retry."""
    a, items = _build_multi(world, store, mode=LibraryMode.ALBUM, target_id="T4", target_name="Album")
    sink = world["sink"]
    restore = _fail_chapter(sink, "Story 2")

    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert len(sink.chapters["T4"]) == 2                    # Story 0, Story 1 made it on

    rep = world["orch"].run(assignment_ids=[a.id], apply=True)   # repair #1, still fails
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert len(sink.chapters["T4"]) == 2                    # not 4 — the old bug

    rep = world["orch"].run(assignment_ids=[a.id], apply=True)   # repair #2, still fails
    assert rep.reports[0].outcome == RunOutcome.DEGRADED
    assert len(sink.chapters["T4"]) == 2                    # not 6 — unbounded growth

    restore()
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)   # repair #3, now succeeds
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert [c.title for c in sink.chapters["T4"]] == ["Story 0", "Story 1", "Story 2"]
    assert store.assignments.get(a.id).state == AssignmentState.OK


def test_repair_of_an_empty_tonie_uploads_without_clear_or_snapshot(world, store):
    a, items = _build_multi(world, store, mode=LibraryMode.SERIAL, target_id="T5", target_name="Serial")
    sink = world["sink"]
    restore = _fail_chapter(sink, "Story 2")
    world["orch"].run(assignment_ids=[a.id], apply=True)        # DEGRADED, tonie holds 0-1
    restore()
    # Empty it out from underneath, simulating e.g. a manual wipe between
    # runs, so this repair genuinely starts from empty rather than partial.
    sink.chapters["T5"] = []

    n = len(sink.calls)
    snaps_before = len(list(world["snaps"].glob("*.json")))
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert [c[0] for c in sink.calls[n:] if c[0] == "clear"] == []
    assert len(list(world["snaps"].glob("*.json"))) == snaps_before
    assert len(sink.chapters["T5"]) == 3


def test_repair_of_a_partially_filled_tonie_snapshots_and_clears_exactly_once(world, store):
    a, items = _build_multi(world, store, mode=LibraryMode.ALBUM, target_id="T6", target_name="Album2")
    sink = world["sink"]
    restore = _fail_chapter(sink, "Story 2")
    world["orch"].run(assignment_ids=[a.id], apply=True)        # DEGRADED, tonie holds 0-1
    restore()

    snaps_before = len(list(world["snaps"].glob("*.json")))
    clears_before = len(sink.calls_named("clear"))
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    assert len(sink.calls_named("clear")) == clears_before + 1   # the repair cleared exactly once
    assert len(list(world["snaps"].glob("*.json"))) == snaps_before + 1
    assert [c.title for c in sink.chapters["T6"]] == ["Story 0", "Story 1", "Story 2"]


def test_repair_of_an_already_correct_tonie_uploads_nothing(world, store):
    """A prior repair attempt evidently finished the upload even though this
    assignment's own record never saw it settle (e.g. a crash between
    SETTLE and COMMIT). The next repair must recognise the tonie already
    matches the staged set and not re-upload anything."""
    a, items = _build_multi(world, store, mode=LibraryMode.ALBUM, target_id="T7", target_name="Album3")
    sink = world["sink"]
    restore = _fail_chapter(sink, "Story 2")
    world["orch"].run(assignment_ids=[a.id], apply=True)        # DEGRADED, tonie holds 0-1
    restore()

    # Simulate "actually finished, never recorded": upload the missing
    # chapter directly against the sink, bypassing the orchestrator, so the
    # tonie now holds the full staged set but the assignment is still
    # DEGRADED.
    staged = json.loads(store.assignments.get(a.id).staged_json)
    target = next(t for t in sink.list_targets() if t.id == "T7")
    sink.upload(target, staged[2]["path"], staged[2]["title"])
    # settle it so it isn't left mid-transcode
    sink.settle(target, sum(c["seconds"] for c in staged), 30)

    n_calls = len(sink.calls)
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    uploads_this_run = [c for c in sink.calls[n_calls:] if c[0] == "upload"]
    assert uploads_this_run == []
    assert [c[0] for c in sink.calls[n_calls:] if c[0] == "clear"] == []
    assert store.assignments.get(a.id).state == AssignmentState.OK


def test_live_chapters_in_wrong_order_are_not_read_as_already_correct(world, store):
    """Review finding: matching only on (count, duration) lets two
    equal-duration chapters that are transposed on the tonie read as
    "already correct" and skip the fix. In `album`/`serial`, order *is* the
    content — chapters 4 and 5 swapped in an audiobook is a broken tonie a
    parent will notice. `_live_matches_staged` must also compare title,
    position-wise: the live title is our own output from our own staged
    set, not upstream-mutable, so — unlike item identity elsewhere in this
    project — it is the right signal here, and the only one available (a
    partial upload never reached COMMIT, so there is no `chapter_record` /
    `sink_chapter_id` to anchor against instead)."""
    a, items = _build_multi(
        world, store, mode=LibraryMode.ALBUM, target_id="T8", target_name="Album4", n=2
    )
    sink = world["sink"]
    # Get a real DEGRADED record with a real two-chapter staged_json, without
    # caring what (if anything) actually landed on the tonie.
    restore = _fail_chapter(sink, "Story 0")
    world["orch"].run(assignment_ids=[a.id], apply=True)
    restore()
    a = store.assignments.get(a.id)
    assert a.state == AssignmentState.DEGRADED
    staged = json.loads(a.staged_json)
    assert len(staged) == 2

    # The live tonie holds exactly the staged titles and durations, but
    # transposed.
    sink.chapters["T8"] = [
        LiveChapter(id="live1", title=staged[1]["title"], seconds=staged[1]["seconds"],
                    transcoding=False),
        LiveChapter(id="live0", title=staged[0]["title"], seconds=staged[0]["seconds"],
                    transcoding=False),
    ]

    snaps_before = len(list(world["snaps"].glob("*.json")))
    clears_before = len(sink.calls_named("clear"))
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
    # Not read as "already correct": the transposition forced a real
    # snapshot + clear + reupload, not a silent commit over swapped chapters.
    assert len(sink.calls_named("clear")) == clears_before + 1
    assert len(list(world["snaps"].glob("*.json"))) == snaps_before + 1
    assert [c.title for c in sink.chapters["T8"]] == [staged[0]["title"], staged[1]["title"]]


def test_repair_snapshot_is_written_before_clear(world, store):
    """Same proof `test_snapshot_is_written_before_clear` gives `swap()` —
    the file is on disk at the moment `clear()` is entered — now that R20
    lets `repair()` clear too, on its partially-filled branch."""
    a, items = _build_multi(
        world, store, mode=LibraryMode.ALBUM, target_id="T9", target_name="Album5"
    )
    sink = world["sink"]
    restore = _fail_chapter(sink, "Story 2")
    world["orch"].run(assignment_ids=[a.id], apply=True)   # DEGRADED, tonie holds 0-1
    restore()

    snaps = world["snaps"]
    snaps_before = len(list(snaps.glob("*.json")))
    original_clear = sink.clear

    def clear_spy(t):
        assert len(list(snaps.glob("*.json"))) == snaps_before + 1, (
            "repair's clear was called before its snapshot was written"
        )
        original_clear(t)

    sink.clear = clear_spy
    rep = world["orch"].run(assignment_ids=[a.id], apply=True)
    assert rep.reports[0].outcome == RunOutcome.REPAIRED
