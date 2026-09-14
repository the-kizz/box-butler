"""Content modes wired into the run (Task 23; spec §4.1, §10.12).

single/album/serial all go through the same `plan()` -> `stage()` ->
`swap()` path; these tests are the coverage the brief calls for across all
three modes, not just SINGLE (Task 22's duplication bug survived review
because every test used SINGLE).

Two corrections to the task brief's embedded test sketch, both because the
real code wins over the brief (see module docstring conventions in
`conftest.py`):

1. Duration injection must key on the *fetched cache filename*
   (`source_cache_name(item)`, i.e. `"Story 0 [vid0].m4a"`), never on the
   tmp_path source name (`"src-vid0.m4a"`) — `FakeRenderer.probe` looks up
   `path.name` of whatever it's actually handed, and the orchestrator never
   hands it the tmp_path original. See `conftest.py`'s own module docstring,
   note 2, for the same correction already made to the Task 21 brief.
2. `test_serial_with_partial_tail_truncates_last_chapter` asserted the
   total against 5340 (an old, superseded cap). `run.py`'s own
   `RotationSettings.cap_seconds` docstring documents that spec §3.4
   revised 5340 up to 5395 (`fitting.DEFAULT_CAP_SECONDS`) and records why;
   5340 here would just be testing the wrong constant.
"""
from dataclasses import replace
from datetime import UTC, datetime

from boxbutler.domain.models import ItemKind, LibraryMode, RenditionMode, RunOutcome
from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS

from tests.orchestrator.conftest import rendition_cache_name, source_cache_name


def _set_mode(world, mode):
    lib = world["store"].libraries.get(world["lib"].id)
    world["store"].libraries.update(replace(lib, mode=mode))


def _seed(world, items, seconds):
    """Seed FakeRenderer with the *fetched* cache filename for each item."""
    world["renderer"].durations.update(
        {source_cache_name(it): seconds for it in items}
    )


def test_single_advances_by_one(world):
    world["orch"].run(apply=True)
    assert world["store"].assignments.get(world["a"].id).cursor_position == 1


def test_album_loads_whole_library_as_titled_chapters_then_skips_forever(world):
    _set_mode(world, LibraryMode.ALBUM)
    _seed(world, world["items"], 1000.0)
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Story 0", "Story 1", "Story 2"]
    assert world["store"].assignments.get(world["a"].id).cursor_position == 0

    for _ in range(3):
        assert world["orch"].run(apply=True).reports[0].outcome == RunOutcome.SKIPPED_ALREADY_CURRENT

    # Library changes underneath: album reloads on the next run.
    new_item = world["store"].items.add(world["lib"].id, ItemKind.YOUTUBE, "u/v3", "vid3", "Story 3")
    src = world["deps"].cache_dir.parent / "src-vid3.m4a"
    src.write_bytes(b"aac")
    world["fetcher"].files[new_item.id] = src
    world["renderer"].durations[source_cache_name(new_item)] = 1000.0

    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert [c.title for c in world["sink"].chapters["T1"]] == [
        "Story 0", "Story 1", "Story 2", "Story 3",
    ]


def test_serial_fills_to_cap_and_advances_past_everything_loaded(world, store):
    """Three separate nights (the rotation window advances each time, as
    every other window-sensitive test in this suite does — see
    `test_idempotency_run.py`'s `_at` helper): calling `run(apply=True)`
    three times in the *same* window would only produce one upload
    (`already_current`'s "three runs in one afternoon" fallback, spec
    §3.3), which is a different property than the one under test here."""
    _set_mode(world, LibraryMode.SERIAL)
    world["deps"].settings.timezone = "UTC"
    new_items = []
    for i in range(3, 5):
        p = world["deps"].cache_dir.parent / f"src-vid{i}.m4a"
        p.write_bytes(b"aac")
        it = store.items.add(world["lib"].id, ItemKind.YOUTUBE, f"u/v{i}", f"vid{i}", f"Story {i}")
        world["fetcher"].files[it.id] = p
        new_items.append(it)
    all_items = world["items"] + new_items
    _seed(world, all_items, 2000.0)
    store.assignments.set_fit(world["a"].id, allow_partial_tail=False)

    world["deps"].clock = lambda: datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    rep = world["orch"].run(apply=True)
    assert rep.reports[0].outcome == RunOutcome.SWAPPED
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Story 0", "Story 1"]
    assert store.assignments.get(world["a"].id).cursor_position == 2

    world["deps"].clock = lambda: datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    world["orch"].run(apply=True)
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Story 2", "Story 3"]
    assert store.assignments.get(world["a"].id).cursor_position == 4

    world["deps"].clock = lambda: datetime(2026, 1, 3, 15, 0, tzinfo=UTC)
    world["orch"].run(apply=True)
    assert [c.title for c in world["sink"].chapters["T1"]] == ["Story 4", "Story 0"]
    assert store.assignments.get(world["a"].id).cursor_position == 1   # wraps


def test_serial_with_partial_tail_truncates_last_chapter(world, store):
    _set_mode(world, LibraryMode.SERIAL)
    _seed(world, world["items"], 2000.0)
    # item2 gets truncated to whatever remains under the (real, 5395s) cap;
    # its rendition is a fresh TRIM, so FakeRenderer needs that filename
    # seeded too (see module docstring, correction 1; conftest.py's own
    # note 1 explains why a rendition needs its own seed).
    tail_take = DEFAULT_CAP_SECONDS - 2000.0 - 2000.0
    world["renderer"].durations[
        rendition_cache_name(world["items"][2], cap=int(tail_take), mode=RenditionMode.TRIM)
    ] = tail_take

    world["orch"].run(apply=True)
    secs = [c.seconds for c in world["sink"].chapters["T1"]]
    assert len(secs) == 3
    assert abs(sum(secs) - DEFAULT_CAP_SECONDS) < 2
    assert store.assignments.get(world["a"].id).cursor_position == 0  # 3 of 3 loaded, wraps


def test_assignment_override_beats_library_mode(world, store):
    store.assignments.set_override(world["a"].id, LibraryMode.ALBUM)
    _seed(world, world["items"], 100.0)
    world["orch"].run(apply=True)
    assert len(world["sink"].chapters["T1"]) == 3


def test_max_chapters_respected(world, store):
    _set_mode(world, LibraryMode.ALBUM)
    limits = world["sink"].limits
    world["sink"]._limits = limits.__class__(limits.max_seconds, 2, limits.max_bytes, limits.accepts)
    _seed(world, world["items"], 100.0)
    world["orch"].run(apply=True)
    assert len(world["sink"].chapters["T1"]) == 2
