"""Cache retention (Task 24; spec §3.6): LRU eviction, budget 40 GB by
default, with three things it must never delete — see `retention.py`'s
module docstring.

Departure from the task brief's embedded sketch, because the real model
wins: `Assignment` has no `staged_path` attribute (only a single
`staged_json` column, R2 — see `boxbutler/domain/state.py` and
`tests/orchestrator/test_degraded.py`, which already made this same
correction for Task 22). Where the brief's sketch reads
`Path(assignment.staged_path)`, these tests read
`Path(json.loads(assignment.staged_json)[0]["path"])`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from boxbutler.orchestrator.prefetch import upcoming_item_ids
from boxbutler.orchestrator.retention import evict, plan_eviction


def fill(world, store):
    """Run once with prefetch so items 0..2 have sources + renditions;
    then age them and add an orphan."""
    world["deps"].settings.prefetch_depth = 3
    world["orch"].run(apply=True)
    cache = world["deps"].cache_dir
    (cache / "orphan.m4a").write_bytes(b"o" * 500)
    now = datetime.now(UTC)
    for i, r in enumerate(sorted(store.renditions.list(), key=lambda r: r.cache_path)):
        store.renditions.touch(r.id, now - timedelta(days=10 - i))
    for i, sf in enumerate(sorted(store.sources.list(), key=lambda s: s.cache_path)):
        store.sources.touch(sf.item_id, now - timedelta(days=20 - i))
    return cache


def upcoming(store, a):
    return upcoming_item_ids(store, a, 3)


def test_under_budget_evicts_nothing(world, store):
    cache = fill(world, store)
    assert plan_eviction(store, cache, 10**12, 3) == []


def test_orphans_go_first_then_renditions_lru_then_sources(world, store):
    cache = fill(world, store)
    plan = plan_eviction(store, cache, 0, depth=0)                 # depth 0: nothing protected by prefetch
    kinds = [e.kind for e in plan]
    assert kinds[0] == "orphan" and kinds.index("rendition") < kinds.index("source")
    rend = [e for e in plan if e.kind == "rendition"]
    assert rend == sorted(rend, key=lambda e: e.last_used)         # LRU order
    src = [e for e in plan if e.kind == "source"]
    assert src == sorted(src, key=lambda e: e.last_used)


def test_prefetch_window_and_current_chapter_are_protected(world, store):
    cache = fill(world, store)
    plan = plan_eviction(store, cache, 0, depth=3)
    a = store.assignments.get(world["a"].id)
    current = store.chapters.for_assignment(a.id)[0].item_id
    protected_items = set(upcoming(store, a)) | {current}
    for e in plan:
        assert not any(
            f"[{store.items.get(i).source_key}]" in e.path.name for i in protected_items
        )


def test_degraded_staged_file_is_protected(world, store):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    a = store.assignments.get(world["a"].id)
    staged = Path(json.loads(a.staged_json)[0]["path"])
    plan = plan_eviction(store, world["deps"].cache_dir, 0, depth=0)
    assert staged not in [e.path for e in plan]


def test_evict_apply_unlinks_and_deletes_rows(world, store):
    cache = fill(world, store)
    evicted = evict(store, cache, 0, depth=0, apply=True)
    assert evicted, "budget 0 with an orphan present must evict something"
    assert all(not e.path.exists() for e in evicted)
    assert not (cache / "orphan.m4a").exists()
    # Deleted renditions/sources leave no dangling database row.
    remaining_rendition_paths = {Path(r.cache_path) for r in store.renditions.list()}
    remaining_source_paths = {Path(sf.cache_path) for sf in store.sources.list()}
    for e in evicted:
        if e.kind == "rendition":
            assert e.path not in remaining_rendition_paths
        elif e.kind == "source":
            assert e.path not in remaining_source_paths


def test_evict_dry_run_deletes_nothing(world, store):
    cache = fill(world, store)
    before = sorted(p.name for p in cache.iterdir())
    evict(store, cache, 0, depth=0, apply=False)
    assert sorted(p.name for p in cache.iterdir()) == before


def test_evict_never_touches_a_protected_file_even_at_zero_budget(world, store):
    """The currently-loaded chapter's source/rendition must survive even a
    budget-0 eviction pass — this is the property that keeps a degraded
    tonie's repair (and a live tonie's own story) recoverable."""
    cache = fill(world, store)
    a = store.assignments.get(world["a"].id)
    current_item_id = store.chapters.for_assignment(a.id)[0].item_id
    current_rendition_paths = {
        Path(r.cache_path) for r in store.renditions.list() if r.item_id == current_item_id
    }
    evicted = evict(store, cache, 0, depth=0, apply=True)
    evicted_paths = {e.path for e in evicted}
    assert not (current_rendition_paths & evicted_paths)
    for p in current_rendition_paths:
        assert p.exists()


# --------------------------------------------------------- wired into run()

def test_run_evicts_at_end_when_over_budget(world, store):
    world["deps"].settings.cache_budget_bytes = 0
    world["deps"].settings.prefetch_depth = 0
    world["orch"].run(apply=True)
    a = store.assignments.get(world["a"].id)
    current_item_id = store.chapters.for_assignment(a.id)[0].item_id
    remaining = {p.name for p in world["cache"].iterdir()}
    # Only the currently-held chapter's source and rendition may survive a
    # budget-0 pass; nothing else should still be in the cache directory.
    surviving_paths = {
        Path(sf.cache_path).name for sf in store.sources.list() if sf.item_id == current_item_id
    } | {
        Path(r.cache_path).name for r in store.renditions.list() if r.item_id == current_item_id
    }
    assert remaining == surviving_paths


def test_dry_run_never_evicts(world):
    cache = world["deps"].cache_dir
    orphan = cache / "orphan.m4a"
    orphan.write_bytes(b"x" * 10)
    world["deps"].settings.cache_budget_bytes = 0
    world["orch"].run(apply=False)
    assert orphan.exists()


def test_budget_exceeded_by_protected_content_emits_event(world, store):
    """Important 3 of the Task 24 review: legitimately-held content (the
    currently-loaded chapter, here) can exceed a tiny budget on its own --
    that's not an error, eviction correctly leaves it alone -- but a
    budget that quietly stops being enforced must say so rather than
    silently no-op forever."""
    world["deps"].settings.cache_budget_bytes = 0
    world["deps"].settings.prefetch_depth = 0
    rep = world["orch"].run(apply=True)
    evs = [e for e in store.runs.events(rep.run_id) if e.event == "evict"]
    assert any(
        e.payload.get("reason") == "budget_exceeded_by_protected_content" for e in evs
    )


def test_budget_exceeded_event_absent_when_budget_is_reachable(world, store):
    """The event is specific to an unreachable budget, not fired on every
    eviction pass -- with real headroom, no such event appears."""
    world["deps"].settings.cache_budget_bytes = 10**12
    world["deps"].settings.prefetch_depth = 0
    rep = world["orch"].run(apply=True)
    evs = [e for e in store.runs.events(rep.run_id) if e.event == "evict"]
    assert not any(
        e.payload.get("reason") == "budget_exceeded_by_protected_content" for e in evs
    )
