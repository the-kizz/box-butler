"""Cross-tonie duplicate avoidance wired into the run (Task 23; spec §4.2).

single mode only: album loads its whole library by definition and serial
follows its own cursor, so neither can meaningfully avoid an item another
tonie holds (see `choose_next`'s docstring). The default `world` fixture's
library is SINGLE mode, so no test here changes it.

The ladder runs per assignment, and assignments within one `run()` are
processed in target order, each committing its `chapter_record` before the
next assignment plans — that sequencing (not any extra bookkeeping here) is
what lets the second and third tonie in a run see what the first just
picked.
"""
from boxbutler.domain.models import RunOutcome

from tests.orchestrator.conftest import events


def _three_tonies(world, store):
    out = [world["a"]]
    for tid, name in [("T2", "Blue"), ("T3", "Red")]:
        world["sink"].add_target(tid, name, [])
        b = store.assignments.upsert_target("fake", tid, name)
        store.assignments.assign_library(b.id, world["lib"].id)
        out.append(store.assignments.get(b.id))
    return out


def test_item_on_another_tonie_is_skipped(world, store):
    a, b, c = _three_tonies(world, store)
    world["orch"].run(apply=True)
    titles = {t: world["sink"].chapters[t][0].title for t in ("T1", "T2", "T3")}
    assert len(set(titles.values())) == 3   # three tonies, three different stories


def test_two_items_three_tonies_yields_a_repeat_never_empty_and_logs_why(world, store):
    a, b, c = _three_tonies(world, store)
    store.items.set_enabled(world["items"][2].id, False)   # only two items remain
    rep = world["orch"].run(apply=True)
    assert all(r.outcome == RunOutcome.SWAPPED for r in rep.reports)
    assert all(len(world["sink"].chapters[t]) == 1 for t in ("T1", "T2", "T3"))
    assert "RELAXED_UNIQUENESS" in events(store, rep.run_id, c.id)


def test_cooldown_relaxed_before_uniqueness(world, store):
    world["deps"].settings.repeat_cooldown_days = 30
    world["orch"].run(apply=True)   # T1 holds Story 0, held today
    store.items.set_enabled(world["items"][1].id, False)
    store.items.set_enabled(world["items"][2].id, False)
    world["sink"].chapters["T1"] = []   # someone wiped it in the app
    rep = world["orch"].run(apply=True)
    ev = events(store, rep.run_id, world["a"].id)
    assert "RELAXED_COOLDOWN" in ev
    assert "RELAXED_UNIQUENESS" not in ev
    assert rep.reports[0].outcome == RunOutcome.SWAPPED


def test_avoid_duplicates_off_allows_same_story_everywhere(world, store):
    world["deps"].settings.avoid_duplicates = False
    _three_tonies(world, store)
    world["orch"].run(apply=True)
    assert len({world["sink"].chapters[t][0].title for t in ("T1", "T2", "T3")}) == 1


def _new_entry_source(kind_module):
    from boxbutler.sources.protocol import ResolvedItem

    class PL:
        def matches(self, r):
            return True

        def resolve(self, r):
            return [ResolvedItem(kind_module.ItemKind.PLAYLIST_ENTRY, "pl#new1", "new1", "New One")]

    return PL()


def test_playlist_resync_previews_without_persisting_then_persists_on_apply(world, store):
    """R21: a dry run must not mutate the catalog, and must still preview
    accurately (never gate the resolve itself on `apply` — that would let
    the preview plan against a stale catalog and report a pick the real
    run, seeing a newly-appeared episode, would not actually make)."""
    import boxbutler.domain.models as models

    store.settings.set(f"playlists:{world['lib'].id}", ["pl"])
    world["deps"].playlist_source = _new_entry_source(models)

    # Dry run: resolves for real (see the unit-level test below for proof
    # the merged result is what planning sees) but persists nothing.
    rep = world["orch"].run()
    assert rep.reports[0].outcome == RunOutcome.DRY_RUN
    assert store.items.find_by_key(world["lib"].id, "new1") is None

    run_events = [e for e in store.runs.events(rep.run_id) if e.assignment_id == world["a"].id]
    preview = next(e for e in run_events if e.event == "playlist_preview")
    assert preview.payload == {"would_add": 1, "would_mark_unavailable": 0, "would_restore": 0}

    # Real run: the same resolve, this time actually persisted.
    rep2 = world["orch"].run(apply=True)
    assert rep2.reports[0].outcome == RunOutcome.SWAPPED
    new_item = store.items.find_by_key(world["lib"].id, "new1")
    assert new_item is not None
    assert new_item.position == 3   # appended behind what was already there
    assert store.assignments.get(world["a"].id).cursor_position == 1   # single mode's own +1, unrelated to the sync


def test_sync_sources_preview_merges_the_new_entry_without_writing(world, store):
    """The lower-level proof that a dry run's preview is what planning
    would actually see: `_sync_sources(apply=False)` returns a merged item
    list containing the not-yet-persisted entry, at the position a real
    append would give it, while the store itself stays untouched."""
    import boxbutler.domain.models as models

    store.settings.set(f"playlists:{world['lib'].id}", ["pl"])
    world["deps"].playlist_source = _new_entry_source(models)
    run = store.runs.start("cli", dry_run=True)

    items = world["orch"]._sync_sources(run.id, world["a"], apply=False)

    assert store.items.find_by_key(world["lib"].id, "new1") is None
    assert {i.source_key for i in items} == {"vid0", "vid1", "vid2", "new1"}
    assert next(i for i in items if i.source_key == "new1").position == 3
