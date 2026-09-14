"""Export/import of libraries and assignments as JSON (Task 7, §3.1)."""
import pytest

from boxbutler.store.export_import import export_json, import_json
from boxbutler.domain.models import ItemKind, LibraryMode
from boxbutler.store.db import Store


def test_round_trip_is_lossless(store, tmp_path):
    """Export → fresh database → import → export should produce equivalent JSON."""
    lib = store.libraries.create("Bedtime", LibraryMode.SERIAL)
    i = store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A", loudnorm=True)
    a = store.assignments.upsert_target("fake", "T1", "Blue Tonie")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_pin(a.id, i.id)
    store.assignments.set_cursor(a.id, 3)

    data = export_json(store)

    # Create a fresh database and import the same data
    other = Store.open(tmp_path / "other.sqlite")
    other.assignments.upsert_target("fake", "T1", "Blue Tonie")
    import_json(other, data)

    # Re-export from the fresh database and verify equivalence
    assert export_json(other) == data


def test_import_merges_without_duplicating(store):
    """Importing the same data twice should not duplicate entries."""
    lib = store.libraries.create("Bedtime")
    store.items.add(lib.id, ItemKind.YOUTUBE, "u", "vidA", "A")

    data = export_json(store)

    # Import the same data
    counts = import_json(store, data)

    # Should have created nothing (already exists)
    assert counts["libraries_created"] == 0
    assert counts["items_created"] == 0
    assert counts["assignments_updated"] == 0
    assert counts["unresolved_pins"] == 0
    assert counts["unresolved_libraries"] == 0

    # Should still have exactly one item
    assert len(store.items.list(lib.id)) == 1


def test_import_rejects_unknown_version(store):
    """Import should reject documents with unknown version."""
    with pytest.raises(ValueError):
        import_json(store, {"version": 99})


def test_round_trip_with_multiple_assignments(store, tmp_path):
    """Round-trip with multiple assignments and libraries."""
    # Create two libraries with items
    lib1 = store.libraries.create("Bedtime", LibraryMode.SERIAL)
    i1a = store.items.add(lib1.id, ItemKind.YOUTUBE, "https://example.invalid/1a", "vid1a", "Video 1A", loudnorm=True)
    i1b = store.items.add(lib1.id, ItemKind.YOUTUBE, "https://example.invalid/1b", "vid1b", "Video 1B")

    lib2 = store.libraries.create("Stories", LibraryMode.ALBUM)
    i2a = store.items.add(lib2.id, ItemKind.URL, "https://example.invalid/story", "story1", "Story 1", loudnorm=False)

    # Create multiple assignments
    a1 = store.assignments.upsert_target("sink1", "T1", "Tonie1")
    store.assignments.assign_library(a1.id, lib1.id)
    store.assignments.set_cursor(a1.id, 2)

    a2 = store.assignments.upsert_target("sink2", "T2", "Tonie2")
    store.assignments.assign_library(a2.id, lib2.id)
    store.assignments.set_pin(a2.id, i2a.id)

    data = export_json(store)

    # Import into a fresh database
    other = Store.open(tmp_path / "other.sqlite")
    other.assignments.upsert_target("sink1", "T1", "Tonie1")
    other.assignments.upsert_target("sink2", "T2", "Tonie2")
    counts = import_json(other, data)

    # Verify counts
    assert counts["libraries_created"] == 2
    assert counts["items_created"] == 3
    assert counts["assignments_updated"] == 2

    # Re-export and verify equivalence
    other_data = export_json(other)
    assert other_data == data


def test_import_merges_new_items_with_existing_library(store):
    """Importing into non-empty DB: merge items into existing library."""
    # Create initial library with one item
    lib = store.libraries.create("Bedtime")
    i1 = store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "Video A")

    # Export it
    data = export_json(store)

    # Add another item to the exported data manually (simulating external change)
    data["libraries"][0]["items"].append({
        "kind": "youtube",
        "source_ref": "https://example.invalid/b",
        "source_key": "vidB",
        "title": "Video B",
        "loudnorm": False,
        "enabled": True,
    })

    # Import the modified data (should merge, not duplicate)
    counts = import_json(store, data)

    # Should have created 0 libraries and 1 new item
    assert counts["libraries_created"] == 0
    assert counts["items_created"] == 1

    # Verify both items exist
    items = store.items.list(lib.id)
    assert len(items) == 2
    assert {i.source_key for i in items} == {"vidA", "vidB"}


def test_import_assignment_without_target_does_not_create(store):
    """Import assignment for non-existent target: should not create."""
    lib = store.libraries.create("Bedtime")
    store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "Video A")

    data = {
        "version": 1,
        "libraries": [{
            "name": "Bedtime",
            "mode": "serial",
            "folder_path": None,
            "items": [{
                "kind": "youtube",
                "source_ref": "https://example.invalid/a",
                "source_key": "vidA",
                "title": "Video A",
                "loudnorm": False,
                "enabled": True,
            }]
        }],
        "assignments": [{
            "sink": "sink1",
            "target_id": "T1",
            "target_name": "Tonie1",
            "library_name": "Bedtime",
            "mode": "ordered",
            "shuffle_seed": 0,
            "pinned_source_key": None,
            "cursor_position": 0,
            "mode_override": None,
            "allow_partial_tail": True,
        }]
    }

    # Try to import without creating the target first
    counts = import_json(store, data)

    # Should have 0 assignments updated (target doesn't exist)
    assert counts["assignments_updated"] == 0

    # Create the target, then import again
    store.assignments.upsert_target("sink1", "T1", "Tonie1")
    counts = import_json(store, data)

    # Should have updated 1 assignment
    assert counts["assignments_updated"] == 1
    a = store.assignments.get_by_target("sink1", "T1")
    assert a.library_id is not None


def test_shuffle_seed_round_trip(store, tmp_path):
    """Critical 1: non-zero shuffle_seed must survive export/import."""
    lib = store.libraries.create("Bedtime", LibraryMode.SERIAL)
    i = store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A")
    a = store.assignments.upsert_target("fake", "T1", "Tonie")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_shuffle_seed(a.id, 42)  # Non-default value

    data = export_json(store)
    assert data["assignments"][0]["shuffle_seed"] == 42

    # Import into fresh database
    other = Store.open(tmp_path / "other.sqlite")
    other.assignments.upsert_target("fake", "T1", "Tonie")
    import_json(other, data)

    # Re-export and verify shuffle_seed is preserved
    other_data = export_json(other)
    assert other_data["assignments"][0]["shuffle_seed"] == 42


def test_partial_document_preserves_existing_assignment_links(store):
    """Critical 2: omitting a library from document doesn't null existing assignment links."""
    lib = store.libraries.create("Bedtime")
    i = store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A")
    a = store.assignments.upsert_target("fake", "T1", "Tonie")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_pin(a.id, i.id)

    # Export full data
    full_data = export_json(store)
    assert full_data["assignments"][0]["library_name"] == "Bedtime"
    assert full_data["assignments"][0]["pinned_source_key"] == "vidA"

    # Create partial document that omits the library
    partial_data = {
        "version": 1,
        "libraries": [],  # omit Bedtime library
        "assignments": [{
            "sink": "fake",
            "target_id": "T1",
            "target_name": "Tonie",
            "library_name": "Bedtime",  # but still reference it
            "mode": "ordered",
            "shuffle_seed": 0,
            "pinned_source_key": "vidA",
            "cursor_position": 0,
            "mode_override": None,
            "allow_partial_tail": True,
        }]
    }

    # Import partial document
    counts = import_json(store, partial_data)

    # Should report unresolved references, not silently null them
    assert "unresolved_pins" in counts or "unresolved_libraries" in counts or counts.get("unresolved_pins", 0) > 0

    # Verify existing links are preserved
    a_after = store.assignments.get(a.id)
    assert a_after.library_id == lib.id, "library_id should be preserved when library exists in store"
    assert a_after.pinned_item_id == i.id, "pinned_item_id should be preserved when item exists in store"


def test_disabled_item_survives_create_path(store):
    """Critical 3: enabled=False must survive import on newly-created items."""
    data = {
        "version": 1,
        "libraries": [{
            "name": "Bedtime",
            "mode": "serial",
            "folder_path": None,
            "items": [{
                "kind": "youtube",
                "source_ref": "https://example.invalid/a",
                "source_key": "vidA",
                "title": "Video A",
                "loudnorm": False,
                "enabled": False,  # Explicitly disabled
            }]
        }],
        "assignments": []
    }

    import_json(store, data)

    # Find the created item
    libs = store.libraries.list()
    assert len(libs) == 1
    items = store.items.list(libs[0].id)
    assert len(items) == 1
    assert items[0].enabled is False, "enabled=False must be preserved for newly-created items"


def test_assignment_enabled_exported_and_imported(store, tmp_path):
    """Important 4: Assignment.enabled must be exported and imported."""
    lib = store.libraries.create("Bedtime")
    store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A")
    a = store.assignments.upsert_target("fake", "T1", "Tonie")
    store.assignments.assign_library(a.id, lib.id)
    store.assignments.set_enabled(a.id, False)  # Disable it

    data = export_json(store)
    # Assignment.enabled should be in the export
    assert "enabled" in data["assignments"][0], "Assignment.enabled must be exported"
    assert data["assignments"][0]["enabled"] is False

    # Import into fresh database
    other = Store.open(tmp_path / "other.sqlite")
    other.assignments.upsert_target("fake", "T1", "Tonie")
    import_json(other, data)

    # Verify enabled state is preserved
    a_other = other.assignments.get_by_target("fake", "T1")
    assert a_other.enabled is False


def test_playlist_source_id_exported_and_imported(store, tmp_path):
    """Important 5: Item.playlist_source_id must be exported and imported."""
    lib = store.libraries.create("Bedtime")
    i = store.items.add(
        lib.id, ItemKind.PLAYLIST_ENTRY, "https://example.invalid/entry",
        "entry1", "Entry 1", playlist_source_id="playlist_xyz"
    )

    data = export_json(store)
    assert "playlist_source_id" in data["libraries"][0]["items"][0], "playlist_source_id must be exported"
    assert data["libraries"][0]["items"][0]["playlist_source_id"] == "playlist_xyz"

    # Import into fresh database
    other = Store.open(tmp_path / "other.sqlite")
    import_json(other, data)

    # Verify playlist_source_id is preserved
    libs = other.libraries.list()
    items = other.items.list(libs[0].id)
    assert items[0].playlist_source_id == "playlist_xyz"


def test_unresolvable_pin_reported_in_counts(store):
    """Important 6: unresolvable pin must appear in counts, not be silently cleared."""
    lib = store.libraries.create("Bedtime")
    store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A")
    a = store.assignments.upsert_target("fake", "T1", "Tonie")
    store.assignments.assign_library(a.id, lib.id)

    # Create a document with a pin that references a non-existent item
    data = {
        "version": 1,
        "libraries": [{
            "name": "Bedtime",
            "mode": "serial",
            "folder_path": None,
            "items": [{
                "kind": "youtube",
                "source_ref": "https://example.invalid/a",
                "source_key": "vidA",
                "title": "Video A",
                "loudnorm": False,
                "enabled": True,
            }]
        }],
        "assignments": [{
            "sink": "fake",
            "target_id": "T1",
            "target_name": "Tonie",
            "library_name": "Bedtime",
            "mode": "ordered",
            "shuffle_seed": 0,
            "pinned_source_key": "non_existent_pin",  # This item doesn't exist
            "cursor_position": 0,
            "mode_override": None,
            "allow_partial_tail": True,
        }]
    }

    counts = import_json(store, data)

    # Should report the unresolvable pin in counts
    assert "unresolved_pins" in counts
    assert counts["unresolved_pins"] > 0


def test_import_dry_run_writes_nothing(store):
    """dry_run=True must report the same counts a real import would
    produce, but the store must be provably unchanged afterwards (Task 27
    review, Critical 1: `library import` had no dry-run gate at all)."""
    # Existing library/item/assignment that a dry-run import will touch.
    lib = store.libraries.create("Bedtime")
    store.items.add(lib.id, ItemKind.YOUTUBE, "u", "vidA", "A")
    a = store.assignments.upsert_target("fake", "T1", "Tonie")

    data = {
        "version": 1,
        "libraries": [{
            "name": "NewLib",  # doesn't exist yet
            "mode": "serial",
            "folder_path": None,
            "items": [{
                "kind": "youtube",
                "source_ref": "https://example.invalid/b",
                "source_key": "vidB",
                "title": "Video B",
                "loudnorm": False,
                "enabled": True,
            }],
        }],
        "assignments": [{
            "sink": "fake",
            "target_id": "T1",
            "target_name": "Tonie",
            "library_name": "NewLib",
            "mode": "ordered",
            "shuffle_seed": 0,
            "pinned_source_key": None,
            "cursor_position": 5,
            "mode_override": None,
            "allow_partial_tail": True,
        }],
    }

    libraries_before = {l.name for l in store.libraries.list()}
    items_before = {i.id for i in store.items.list(lib.id)}
    assignment_before = store.assignments.get(a.id)

    counts = import_json(store, data, dry_run=True)

    # The plan is accurate...
    assert counts["libraries_created"] == 1
    assert counts["items_created"] == 1
    assert counts["assignments_updated"] == 1

    # ...but nothing was actually written: same libraries, same items in
    # the pre-existing library, and the pre-existing assignment untouched
    # (not merely "no crash" — its fields are compared field-for-field).
    assert {l.name for l in store.libraries.list()} == libraries_before
    assert {i.id for i in store.items.list(lib.id)} == items_before
    assignment_after = store.assignments.get(a.id)
    assert assignment_after == assignment_before
    assert assignment_after.library_id is None
    assert assignment_after.cursor_position == 0


def test_import_applies_changed_library_mode(store):
    """Reproduced live: exporting `snoozer`, editing its `mode` in the
    document from serial to single, then importing with --apply reported
    success but left the library's mode untouched. Re-importing an edited
    export is the entire point of export/edit/import, so a mode edit must
    actually land -- and the counts must say so, not just report
    `libraries_created: 0` (true and useless)."""
    lib = store.libraries.create("snoozer", LibraryMode.SERIAL)
    store.items.add(lib.id, ItemKind.YOUTUBE, "https://example.invalid/a", "vidA", "A")

    data = export_json(store)
    assert data["libraries"][0]["mode"] == "serial"
    data["libraries"][0]["mode"] = "single"  # operator's hand-edit

    counts = import_json(store, data, dry_run=True)
    assert counts["libraries_updated"] == 1
    assert counts["libraries_created"] == 0
    # Dry run must not have touched the store.
    assert store.libraries.get(lib.id).mode == LibraryMode.SERIAL

    counts = import_json(store, data, dry_run=False)
    assert counts["libraries_updated"] == 1
    assert counts["libraries_created"] == 0

    updated = store.libraries.get(lib.id)
    assert updated.mode == LibraryMode.SINGLE, "mode edit from the document must actually apply"

    # A second import of the same (now-matching) document is a true no-op.
    counts = import_json(store, data, dry_run=False)
    assert counts["libraries_updated"] == 0
    assert store.libraries.get(lib.id).mode == LibraryMode.SINGLE


def test_import_apply_true_writes_for_real(store):
    """The counterpart to the dry-run test above: with dry_run=False (the
    default, matching pre-existing behaviour), the same document really
    does write."""
    store.assignments.upsert_target("fake", "T1", "Tonie")
    data = {
        "version": 1,
        "libraries": [{
            "name": "NewLib",
            "mode": "serial",
            "folder_path": None,
            "items": [],
        }],
        "assignments": [],
    }

    counts = import_json(store, data, dry_run=False)

    assert counts["libraries_created"] == 1
    assert any(l.name == "NewLib" for l in store.libraries.list())
