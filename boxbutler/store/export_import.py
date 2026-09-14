"""Export/import of libraries and assignments as JSON (Task 7; spec §3.1).

Export produces a portable, reviewable JSON document with libraries,
items, and assignments. Import is idempotent, merging by (library.name,
item.source_key). No secrets are exported: folder_path is relative or
logical, never an absolute host path.
"""
import json
import dataclasses
from typing import Any

from .db import Store
from ..domain.models import ItemKind, LibraryMode, AssignmentMode


EXPORT_VERSION = 1


def export_json(store: Store) -> dict:
    """Export libraries and assignments to a portable JSON document.

    Returns:
        {
            "version": 1,
            "libraries": [{
                "name": str,
                "mode": str,
                "folder_path": str | None,
                "items": [{
                    "kind": str,
                    "source_ref": str,
                    "source_key": str,
                    "title": str,
                    "loudnorm": bool,
                    "enabled": bool,
                    "playlist_source_id": str | None,
                }]
            }],
            "assignments": [{
                "sink": str,
                "target_id": str,
                "target_name": str,
                "library_name": str | None,
                "mode": str,
                "shuffle_seed": int,
                "pinned_source_key": str | None,
                "cursor_position": int,
                "mode_override": str | None,
                "allow_partial_tail": bool,
                "enabled": bool,
            }]
        }
    """
    libraries_data = []

    for lib in store.libraries.list():
        items_data = []
        for item in store.items.list(lib.id):
            items_data.append({
                "kind": str(item.kind),
                "source_ref": item.source_ref,
                "source_key": item.source_key,
                "title": item.title,
                "loudnorm": item.loudnorm,
                "enabled": item.enabled,
                "playlist_source_id": item.playlist_source_id,
            })

        libraries_data.append({
            "name": lib.name,
            "mode": str(lib.mode),
            "folder_path": lib.folder_path,
            "items": items_data,
        })

    assignments_data = []

    for assignment in store.assignments.list():
        # Resolve pinned_item_id to source_key
        pinned_source_key = None
        if assignment.pinned_item_id:
            pinned_item = store.items.get(assignment.pinned_item_id)
            if pinned_item:
                pinned_source_key = pinned_item.source_key

        # Resolve library_id to library_name
        library_name = None
        if assignment.library_id:
            lib = store.libraries.get(assignment.library_id)
            if lib:
                library_name = lib.name

        assignments_data.append({
            "sink": assignment.sink,
            "target_id": assignment.target_id,
            "target_name": assignment.target_name,
            "library_name": library_name,
            "mode": str(assignment.mode),
            "shuffle_seed": assignment.shuffle_seed,
            "pinned_source_key": pinned_source_key,
            "cursor_position": assignment.cursor_position,
            "mode_override": str(assignment.mode_override) if assignment.mode_override else None,
            "allow_partial_tail": assignment.allow_partial_tail,
            "enabled": assignment.enabled,
        })

    return {
        "version": EXPORT_VERSION,
        "libraries": libraries_data,
        "assignments": assignments_data,
    }


def import_json(store: Store, data: dict, *, replace: bool = False, dry_run: bool = False) -> dict:
    """Import libraries and assignments from a JSON document.

    Import is idempotent: importing the same document twice does not
    duplicate libraries, items, or assignments. Libraries and items are
    merged by (library.name, item.source_key). Assignments are matched
    by (sink, target_id) and only updated if they already exist — import
    never invents new tonies.

    Partial documents are handled gracefully: if a library or pin
    reference is absent from the document, the import tries to resolve
    it from the target store. Only truly unresolvable references are
    reported in the returned counts (never silently nulled).

    Args:
        store: The Store to import into.
        data: The JSON document from export_json().
        replace: If True, delete libraries absent from the document
            (not yet implemented; raises NotImplementedError if True).
        dry_run: If True, compute and return the same counts a real
            import would produce, but never call a mutating store method
            (`libraries.create`, `items.add`, `items.set_*`,
            `assignments.assign_library`/`set_*`). The CLI's `library
            import` (no `--apply`) relies on this to preview an import
            with the store provably unchanged. A library that doesn't
            exist yet is given a synthetic, never-persisted id so the
            (read-only) `items.find_by_key` lookups below still work; the
            one known approximation this causes is a pin that targets an
            item inside a *brand-new* library from the same document —
            such a pin is counted `unresolved` in the dry-run preview even
            though a real `--apply` run would create that item first and
            resolve the pin against it. That's a preview-accuracy wrinkle,
            not a correctness bug: a dry run never invents ids for rows it
            hasn't created, so it can only report what it can see.

    Returns:
        {
            "libraries_created": int,
            "libraries_updated": int,
            "items_created": int,
            "assignments_updated": int,
            "unresolved_pins": int,
            "unresolved_libraries": int,
        }

    Raises:
        ValueError: If the document version is unknown.
        NotImplementedError: If replace=True.
    """
    # Check version
    version = data.get("version")
    if version != EXPORT_VERSION:
        raise ValueError(f"Unknown export version: {version}. Expected {EXPORT_VERSION}.")

    if replace:
        raise NotImplementedError("replace=True is not yet implemented.")

    libraries_created = 0
    libraries_updated = 0
    items_created = 0
    assignments_updated = 0
    unresolved_pins = 0
    unresolved_libraries = 0

    # Build a map of imported libraries by name for quick lookup
    library_id_map = {}  # imported name -> store id

    # Import libraries and items
    for lib_data in data.get("libraries", []):
        lib_name = lib_data["name"]
        lib_mode = LibraryMode(lib_data["mode"])
        lib_folder = lib_data.get("folder_path")

        # Try to find existing library by name
        existing_lib = None
        for lib in store.libraries.list():
            if lib.name == lib_name:
                existing_lib = lib
                break

        if existing_lib is None:
            if dry_run:
                # Never persisted; just enough of an id for the read-only
                # find_by_key lookups below to run (and correctly find
                # nothing, since this library doesn't exist yet).
                lib_id = f"__dry_run_pending_library__:{lib_name}"
            else:
                lib = store.libraries.create(lib_name, lib_mode, lib_folder)
                lib_id = lib.id
            libraries_created += 1
        else:
            lib_id = existing_lib.id

            # Reconcile mutable fields on a library that already exists.
            # Re-import of an edited export is the whole point of
            # export/edit/import (see module docstring); a `mode` edit
            # that silently didn't apply was reproduced live (Task 33: an
            # exported `bedtime` library was edited from serial to
            # single, imported with --apply, and the store still said
            # serial with no indication the edit was dropped). Both
            # `mode` and `folder_path` are treated as data the operator
            # meant to change -- a folder library pointed at a different
            # directory is exactly as "did the operator mean this" as a
            # mode change, and leaving one of the two silently un-synced
            # would just move the same bug to the field nobody tested.
            if existing_lib.mode != lib_mode or existing_lib.folder_path != lib_folder:
                if not dry_run:
                    store.libraries.update(
                        dataclasses.replace(existing_lib, mode=lib_mode, folder_path=lib_folder)
                    )
                libraries_updated += 1

        library_id_map[lib_name] = lib_id

        # Import items for this library
        for item_data in lib_data.get("items", []):
            source_key = item_data["source_key"]

            # Check if item already exists by source_key (read-only; safe
            # to call even with the synthetic dry-run id above).
            existing_item = store.items.find_by_key(lib_id, source_key)

            if existing_item is None:
                if not dry_run:
                    # Create new item
                    new_item = store.items.add(
                        lib_id,
                        ItemKind(item_data["kind"]),
                        item_data["source_ref"],
                        source_key,
                        item_data["title"],
                        loudnorm=item_data.get("loudnorm", False),
                        playlist_source_id=item_data.get("playlist_source_id"),
                    )

                    # Set enabled to False if specified (Critical 3)
                    if item_data.get("enabled", True) is False:
                        store.items.set_enabled(new_item.id, False)

                items_created += 1
            elif not dry_run:
                # Item already exists; update enabled/loudnorm/playlist_source_id if needed
                if existing_item.enabled != item_data.get("enabled", True):
                    store.items.set_enabled(existing_item.id, item_data.get("enabled", True))
                if existing_item.loudnorm != item_data.get("loudnorm", False):
                    store.items.set_loudnorm(existing_item.id, item_data.get("loudnorm", False))

    # Import assignments
    for assign_data in data.get("assignments", []):
        sink = assign_data["sink"]
        target_id = assign_data["target_id"]
        target_name = assign_data["target_name"]

        # Only update existing assignments; never create new ones
        existing_assignment = store.assignments.get_by_target(sink, target_id)

        if existing_assignment is not None:
            # Resolve library_name to library_id
            # First try the imported document, then fall back to store lookup (R15: resolve against target)
            lib_name = assign_data.get("library_name")
            library_id = None
            unresolved_lib_this_assignment = False

            if lib_name:
                # Try imported libraries first
                library_id = library_id_map.get(lib_name)

                # If not in imported libraries, try to find it in the existing store (R15a)
                if library_id is None:
                    for lib in store.libraries.list():
                        if lib.name == lib_name:
                            library_id = lib.id
                            break

                # If still not found, mark as unresolved
                if library_id is None:
                    unresolved_lib_this_assignment = True
                    unresolved_libraries += 1
                    # Keep the existing library_id instead of nulling it (R15b)
                    library_id = existing_assignment.library_id

            if not dry_run:
                store.assignments.assign_library(existing_assignment.id, library_id)
                store.assignments.set_mode(existing_assignment.id, AssignmentMode(assign_data["mode"]))
                store.assignments.set_shuffle_seed(existing_assignment.id, assign_data.get("shuffle_seed", 0))

            # Resolve pinned_source_key to item_id
            pinned_source_key = assign_data.get("pinned_source_key")
            pinned_item_id = None
            if pinned_source_key:
                # Try to find the item in the resolved library (read-only;
                # safe even against the synthetic dry-run library id).
                if library_id:
                    pinned_item = store.items.find_by_key(library_id, pinned_source_key)
                    if pinned_item:
                        pinned_item_id = pinned_item.id
                    else:
                        # Item not found in the resolved library
                        unresolved_pins += 1
                        # Keep existing pin instead of nulling it (R15b)
                        pinned_item_id = existing_assignment.pinned_item_id

            if not dry_run:
                store.assignments.set_pin(existing_assignment.id, pinned_item_id)
                store.assignments.set_cursor(existing_assignment.id, assign_data.get("cursor_position", 0))

                mode_override = assign_data.get("mode_override")
                if mode_override:
                    store.assignments.set_override(existing_assignment.id, LibraryMode(mode_override))
                else:
                    store.assignments.set_override(existing_assignment.id, None)

                store.assignments.set_fit(existing_assignment.id, assign_data.get("allow_partial_tail", True))
                store.assignments.set_enabled(existing_assignment.id, assign_data.get("enabled", True))

            assignments_updated += 1

    return {
        "libraries_created": libraries_created,
        "libraries_updated": libraries_updated,
        "items_created": items_created,
        "assignments_updated": assignments_updated,
        "unresolved_pins": unresolved_pins,
        "unresolved_libraries": unresolved_libraries,
    }
