"""Prefetch depth (Task 24; spec §2 "Prefetch depth", §10.9).

> YouTube extraction broke twice on the prototype's first night.

That is the entire reason this module exists: after a successful swap, the
next `prefetch_depth` items for that assignment are fetched, rendered and
verified in the background, so a third-party extraction break costs days
of buffer instead of one silent bedtime. These tests check the buffer gets
built (and stops building the instant the extractor looks broken), never
touches a tonie, and never runs on a dry run.

Two departures from the task brief's embedded sketch, both because the real
model wins over the brief (same convention `conftest.py` and
`test_degraded.py` already establish for this codebase):

1. `test_prefetch_extraction_broken_stops_quietly` starts its own run via
   `store.runs.start(...)` exactly as the brief sketch does — that part was
   fine. Nothing else changed there.
2. The brief's three tests only ever exercise SINGLE mode. Spec §4.1 gives
   `single`/`album`/`serial` genuinely different rotation semantics, and
   this project's own recurring bug family is "a bug in the gap between the
   mode tested and the modes that exist" — so this file also pins down
   `upcoming_item_ids` for SERIAL (next `depth` items from the cursor) and
   ALBUM (the not-yet-loaded delta — ruling R22, see below) explicitly,
   per `prefetch.py`'s own module docstring.

Review round 2 (Task 24 review, three Importants) added:

- SERIAL must start at a pin, same as SINGLE — `order_for` alone is
  pin-agnostic (see its own docstring); only `choose_next`'s
  `_rotate_to` is pin-aware, so `upcoming_item_ids` calls `choose_next`
  for SERIAL too, not a raw `order_for(...)` slice.
- SINGLE's simulated walk must use the same `avoid_duplicates` /
  `repeat_cooldown_days` settings `run.py`'s real `plan()` does, or the
  simulated "next" silently diverges from the real one under a
  configured cooldown.
- ALBUM prefetch is the not-yet-loaded delta (R22), not the whole
  library — the whole library is already protected via `chapter_record`
  once it's loaded, so reporting it as "upcoming" too made retention's
  budget unenforceable for a reason that never applied.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from boxbutler.domain.models import AssignmentMode, ItemKind, ItemState, LibraryMode
from boxbutler.domain.rotation import order_for
from boxbutler.fetch.protocol import ExtractionBroken, ItemUnavailable
from boxbutler.orchestrator.prefetch import prefetch_assignment, ready_depth, upcoming_item_ids
from tests.orchestrator.conftest import source_cache_name


def _set_mode(world, mode):
    lib = world["store"].libraries.get(world["lib"].id)
    world["store"].libraries.update(replace(lib, mode=mode))


# --------------------------------------------------------------- single mode

def test_upcoming_single_is_next_three_after_current(world, store):
    world["orch"].run(apply=True)                       # holds item 0, cursor 1
    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 3) == [
        world["items"][1].id, world["items"][2].id, world["items"][0].id,
    ]


def test_upcoming_single_stops_at_a_pin_instead_of_repeating(world, store):
    """A pin freezes the cursor (spec §4): `Plan.rotates` comes back False,
    so upcoming stops after the one pinned item rather than "predicting" it
    forever — the cursor genuinely never advances past it."""
    store.assignments.set_pin(world["a"].id, world["items"][1].id)
    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 3) == [world["items"][1].id]


def test_prefetch_after_swap_stages_three_without_touching_sink(world):
    world["deps"].settings.prefetch_depth = 3
    world["orch"].run(apply=True)
    # 1 swap fetch (item0) + 2 prefetch fetches (item1, item2) + 1 prefetch
    # fetch that wraps back onto item0 (already cached, still a fetch call —
    # FakeFetcher's idempotency is a cache hit, not a skipped call).
    assert set(world["fetcher"].calls) == {i.id for i in world["items"]}
    assert [c[0] for c in world["sink"].calls if c[0] in ("clear", "upload")] == [
        "clear", "upload",
    ]
    assert ready_depth(world["store"], world["store"].assignments.get(world["a"].id), 3) == 3


def test_prefetch_extraction_broken_stops_quietly(world):
    world["orch"].run(apply=True)
    world["fetcher"].fail[world["items"][2].id] = ExtractionBroken("x")
    run_id = world["store"].runs.start("cli", False).id
    n = prefetch_assignment(
        world["orch"], run_id, world["store"].assignments.get(world["a"].id), 3
    )
    assert n >= 1
    # item1 (before the break) verified; item2's fetch was attempted (and
    # broke) but the break stopped the loop before item0's wrap-around
    # fetch was ever attempted a second time.
    assert n == 1
    assert world["fetcher"].calls == [
        world["items"][0].id, world["items"][1].id, world["items"][2].id,
    ]


def test_prefetch_item_unavailable_marks_and_continues(world, store):
    """`ItemUnavailable` is scoped to one item, unlike `ExtractionBroken` —
    the rest of the prefetch window still gets warmed."""
    world["orch"].run(apply=True)                        # cursor -> 1
    world["fetcher"].fail[world["items"][1].id] = ItemUnavailable("gone")
    a = store.assignments.get(world["a"].id)
    run_id = store.runs.start("cli", False).id
    n = prefetch_assignment(world["orch"], run_id, a, 3)
    # item1 unavailable (skipped); item2 and item0 (cache hit) verified.
    assert n == 2
    assert store.items.get(world["items"][1].id).state == ItemState.UNAVAILABLE


def test_prefetch_never_calls_the_sink(world, store):
    world["orch"].run(apply=True)
    before = len(world["sink"].calls)
    run_id = store.runs.start("cli", False).id
    prefetch_assignment(world["orch"], run_id, store.assignments.get(world["a"].id), 3)
    assert len(world["sink"].calls) == before


def test_dry_run_never_prefetches(world):
    world["deps"].settings.prefetch_depth = 3
    world["orch"].run(apply=False)
    assert world["fetcher"].calls == []


# --------------------------------------------------------------- serial mode

def test_upcoming_serial_is_next_depth_items_from_cursor(world, store):
    _set_mode(world, LibraryMode.SERIAL)
    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 2) == [world["items"][0].id, world["items"][1].id]

    store.assignments.set_cursor(a.id, 1)
    a = store.assignments.get(a.id)
    assert upcoming_item_ids(store, a, 2) == [world["items"][1].id, world["items"][2].id]

    # Depth beyond the library size never repeats — serial has no "wrap
    # within one window" concept the way single's simulated cursor does.
    assert len(upcoming_item_ids(store, a, 5)) == 3


def test_upcoming_serial_starts_at_pin(world, store):
    """`order_for` alone is pin-agnostic (its own docstring says so); only
    `choose_next`'s `_rotate_to` respects a pin. A pinned SERIAL
    assignment's real next fill starts at the pinned item, so prefetch
    must too — Important 1 of the Task 24 review."""
    _set_mode(world, LibraryMode.SERIAL)
    store.assignments.set_pin(world["a"].id, world["items"][2].id)
    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 3) == [
        world["items"][2].id, world["items"][0].id, world["items"][1].id,
    ]


# --------------------------------------------------------------- shuffle

def test_upcoming_single_respects_shuffle_order(world, store):
    """The simulated walk never reimplements ordering — it delegates to
    the real `choose_next`/`order_for`, so a shuffled assignment's
    upcoming window is whatever the real seeded permutation says, not
    library position order."""
    store.assignments.set_mode(world["a"].id, AssignmentMode.SHUFFLE)
    a = store.assignments.get(world["a"].id)
    expected = [i.id for i in order_for(a, store.items.list(world["lib"].id))]
    assert upcoming_item_ids(store, a, 3) == expected
    assert set(expected) == {i.id for i in world["items"]}   # a genuine permutation


# ------------------------------------------------------------- cooldown

def test_upcoming_single_respects_cooldown_settings(world, store):
    """Important 2 of the Task 24 review: the simulated walk must use the
    same `avoid_duplicates` / `repeat_cooldown_days` the real `plan()`
    does, or it predicts (and retention then protects) the wrong item."""
    world["orch"].run(apply=True)                        # holds item0, cursor -> 1
    a = store.assignments.get(world["a"].id)
    now = datetime.now(UTC)

    # No cooldown threaded through: the third simulated pick wraps back
    # onto item0 -- the item this tonie just finished playing.
    assert upcoming_item_ids(store, a, 3) == [
        world["items"][1].id, world["items"][2].id, world["items"][0].id,
    ]
    # With the real 1-day cooldown threaded through, item0 is still within
    # its cooldown window, so the third pick lands on the next unblocked
    # item instead.
    assert upcoming_item_ids(store, a, 3, repeat_cooldown_days=1, now=now) == [
        world["items"][1].id, world["items"][2].id, world["items"][1].id,
    ]


def test_prefetch_assignment_threads_cooldown_settings(world, store):
    """The same threading, exercised through `prefetch_assignment` (which
    reads `orch.deps.settings`), not just the raw function call above."""
    world["orch"].run(apply=True)
    world["deps"].settings.repeat_cooldown_days = 1
    a = store.assignments.get(world["a"].id)
    run_id = store.runs.start("cli", False).id
    prefetch_assignment(world["orch"], run_id, a, 3)
    # item0 is only fetched once (the original swap) -- cooldown kept it
    # out of prefetch's third pick.
    assert world["fetcher"].calls.count(world["items"][0].id) == 1


# ---------------------------------------------------------------- album mode

def test_upcoming_album_is_the_not_yet_loaded_delta(world, store):
    """Ruling R22 (Task 24 review, Important 3): album prefetch is the
    library items not currently loaded, not the whole library. Already-
    loaded album items are already protected via `chapter_record` —
    reporting them as "upcoming" too made retention's cache budget
    unenforceable for a reason that never applied."""
    _set_mode(world, LibraryMode.ALBUM)
    world["renderer"].durations.update({source_cache_name(it): 1000.0 for it in world["items"]})
    world["orch"].run(apply=True)                        # loads the whole (unchanged) library
    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 3) == []

    new_item = store.items.add(world["lib"].id, ItemKind.YOUTUBE, "u/v3", "vid3", "Story 3")
    src = world["deps"].cache_dir.parent / "src-vid3.m4a"
    src.write_bytes(b"aac")
    world["fetcher"].files[new_item.id] = src
    world["renderer"].durations[source_cache_name(new_item)] = 1000.0

    a = store.assignments.get(world["a"].id)
    assert upcoming_item_ids(store, a, 3) == [new_item.id]
    # depth<=0 still means "nothing", album included.
    assert upcoming_item_ids(store, a, 0) == []
