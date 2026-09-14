"""Playlist re-resolution (Task 18; spec §3.5.1, §10.10).

A playlist source is re-resolved on every run and new entries are appended
to the end, keyed on `source_key` (never title). Vanished entries are
marked `ItemState.UNAVAILABLE`, never deleted, and a re-resolve of one
playlist must never touch another playlist's items, a hand-added item, or
a folder-scanned file. A failed resolve (`SourceError`) must change
nothing — that is the difference between "resolved, and these are gone"
and "could not resolve".
"""
import pytest

from boxbutler.domain.models import ItemKind, ItemState
from boxbutler.sources.protocol import ResolvedItem, SourceError
from boxbutler.sources.playlist_sync import sync_playlist


class PL:
    def __init__(self, entries):
        self.entries = entries

    def matches(self, ref):
        return True

    def resolve(self, ref):
        return [ResolvedItem(ItemKind.PLAYLIST_ENTRY, f"u/{k}", k, t) for k, t in self.entries]


def seed(store):
    lib = store.libraries.create("Bedtime")
    sync_playlist(store, lib.id, "pl", PL([("a", "A"), ("b", "B"), ("c", "C")]))
    a = store.assignments.upsert_target("fake", "T", "T")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_cursor(a.id, 2)
    return lib, a


def test_unchanged_playlist_adds_nothing(store):
    lib, a = seed(store)
    r = sync_playlist(store, lib.id, "pl", PL([("a", "A"), ("b", "B"), ("c", "C")]))
    assert (r.appended, r.unavailable) == (0, 0) and len(store.items.list(lib.id)) == 3


def test_new_entry_appends_at_end_and_moves_no_cursor(store):
    lib, a = seed(store)
    sync_playlist(store, lib.id, "pl", PL([("d", "D"), ("a", "A"), ("b", "B"), ("c", "C")]))  # upstream put it first
    assert [i.source_key for i in store.items.list(lib.id)] == ["a", "b", "c", "d"]
    assert store.assignments.get(a.id).cursor_position == 2


def test_retitled_entry_matches_on_id_and_does_not_duplicate(store):
    lib, a = seed(store)
    sync_playlist(store, lib.id, "pl", PL([("a", "A (remastered)"), ("b", "B"), ("c", "C")]))
    items = store.items.list(lib.id)
    assert len(items) == 3 and items[0].title == "A"


def test_reordered_upstream_changes_nothing(store):
    lib, a = seed(store)
    sync_playlist(store, lib.id, "pl", PL([("c", "C"), ("b", "B"), ("a", "A")]))
    assert [i.source_key for i in store.items.list(lib.id)] == ["a", "b", "c"]


def test_removed_entry_marked_unavailable_positions_kept(store):
    lib, a = seed(store)
    r = sync_playlist(store, lib.id, "pl", PL([("a", "A"), ("c", "C")]))
    items = store.items.list(lib.id)
    assert r.unavailable == 1 and items[1].state == ItemState.UNAVAILABLE and [i.position for i in items] == [0, 1, 2]
    r2 = sync_playlist(store, lib.id, "pl", PL([("a", "A"), ("b", "B"), ("c", "C")]))
    assert r2.restored == 1 and store.items.list(lib.id)[1].state == ItemState.OK


def test_failed_resolve_marks_nothing(store):
    lib, a = seed(store)

    class Broken(PL):
        def resolve(self, ref):
            raise SourceError("extraction broke")

    with pytest.raises(SourceError):
        sync_playlist(store, lib.id, "pl", Broken([]))
    assert all(i.state == ItemState.OK for i in store.items.list(lib.id))


def test_manually_added_youtube_items_are_not_touched_by_playlist_sync(store):
    lib, a = seed(store)
    store.items.add(lib.id, ItemKind.YOUTUBE, "u/x", "x", "X")
    sync_playlist(store, lib.id, "pl", PL([("a", "A")]))
    assert store.items.find_by_key(lib.id, "x").state == ItemState.OK


def test_second_playlist_in_same_library_does_not_touch_first_playlists_vanished_entry(store):
    """item.playlist_source_id scopes UNAVAILABLE marking to the playlist
    that produced the entry — the whole reason the column exists."""
    lib = store.libraries.create("Mixed")
    sync_playlist(store, lib.id, "pl-1", PL([("a", "A"), ("b", "B")]))
    sync_playlist(store, lib.id, "pl-2", PL([("x", "X"), ("y", "Y")]))

    # pl-1 loses "b" upstream; pl-2 is untouched and must not be affected,
    # and pl-2's re-resolve must not affect pl-1's remaining/removed items.
    r1 = sync_playlist(store, lib.id, "pl-1", PL([("a", "A")]))
    assert r1.unavailable == 1

    items = {i.source_key: i for i in store.items.list(lib.id)}
    assert items["b"].state == ItemState.UNAVAILABLE
    assert items["x"].state == ItemState.OK
    assert items["y"].state == ItemState.OK

    r2 = sync_playlist(store, lib.id, "pl-2", PL([("x", "X"), ("y", "Y")]))
    assert r2.unavailable == 0
    assert store.items.find_by_key(lib.id, "b").state == ItemState.UNAVAILABLE


def test_playlist_refs_reads_settings_key_and_sync_all_playlists_runs_each(store):
    from boxbutler.sources.playlist_sync import playlist_refs, sync_all_playlists

    lib = store.libraries.create("Multi")
    store.settings.set(f"playlists:{lib.id}", ["pl-1", "pl-2"])
    assert playlist_refs(store, lib.id) == ["pl-1", "pl-2"]

    class Multi:
        def matches(self, ref):
            return True

        def resolve(self, ref):
            if ref == "pl-1":
                return [ResolvedItem(ItemKind.PLAYLIST_ENTRY, "u/a", "a", "A")]
            return [ResolvedItem(ItemKind.PLAYLIST_ENTRY, "u/x", "x", "X")]

    results = sync_all_playlists(store, lib.id, Multi())
    assert [r.appended for r in results] == [1, 1]
    assert {i.source_key for i in store.items.list(lib.id)} == {"a", "x"}
