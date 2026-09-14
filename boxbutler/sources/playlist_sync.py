"""Playlist re-resolution (Task 18; spec §3.5.1, §10.10).

"Playlist sources re-resolve on every run and auto-append new entries. A
playlist URL is a living source, not a one-time expansion: each run lists
it again and appends entries whose `source_key` is new to the **end** of
the library. Existing order, positions and cursors are untouched, so new
uploads simply queue up behind what is already there. This is the part
that makes the service stop being manual." (spec §3.5)

**Identity, never title (load-bearing).** Re-resolution is keyed on
`source_key`, exactly like `Ingestor` (Task 16). A re-titled upstream
entry must add nothing and duplicate nothing, so an entry already present
by key is left alone — its title is never overwritten, even when upstream
has changed it. Matching on title instead would be wrong twice over: an
edited title would look like a new item, and two different stories
sharing one title would collide.

**Removed, never deleted.** An upstream entry that has vanished is marked
`ItemState.UNAVAILABLE`, never deleted — deleting would shift every
position behind it (corrupting a hand-arranged order) and could walk a
`serial` assignment's cursor onto the wrong chapter. `ItemRepo.delete()`
closes position gaps for exactly this reason; this module never calls it.
A key that reappears in a later resolve (state was `UNAVAILABLE`, now
present again) is restored to `OK` in place, at its existing position.

**Scoped to *this* playlist only.** `Item.playlist_source_id` (added in
the initial migration for exactly this purpose) records which playlist
produced a `PLAYLIST_ENTRY` item. Only items whose `kind ==
ItemKind.PLAYLIST_ENTRY` **and** `playlist_source_id == playlist_ref` are
candidates for `UNAVAILABLE` here — so re-resolving playlist A can never
mark playlist B's vanished entries, a hand-added item, or a folder-scanned
file as unavailable. (The brief sketched encoding this into `source_ref`
as `f"{playlist_ref}#{video_id}"` instead; the dedicated
`playlist_source_id` column already exists for this and is what the store
layer and its docstring say to use, so that is followed instead of the
brief.)

**A failed resolve changes nothing.** `resolve()` can raise `SourceError`
(network down, feed 500ing, extraction broken) — that is "could not
resolve", not "resolved to an empty playlist", and must never be treated
as a mass removal. This module does not catch `SourceError`; it lets it
propagate before touching any item, so the caller (the orchestrator) can
classify the failure and nothing is marked unavailable on its way there.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from boxbutler.domain.models import Item, ItemKind, ItemState
from boxbutler.sources.protocol import SourceProtocol


@dataclass(frozen=True)
class SyncResult:
    appended: int
    unavailable: int
    restored: int
    unchanged: int


@dataclass(frozen=True)
class SyncPreview:
    """The read-only twin of `SyncResult` (R21, Task 23): what a sync
    *would* do, plus the library's item list as it would read immediately
    afterward — for a dry run to plan against without writing anything."""

    result: SyncResult
    items: list[Item]


def playlist_refs(store, library_id: str) -> list[str]:
    """The list of playlist/feed references configured for `library_id`,
    stored under settings key `f"playlists:{library_id}"`."""
    return store.settings.get(f"playlists:{library_id}", [])


def sync_playlist(store, library_id: str, playlist_ref: str, source: SourceProtocol) -> SyncResult:
    """Re-resolve `playlist_ref` against `source` and reconcile it into
    `library_id`.

    Resolution happens first, in full, before any write: if `resolve()`
    raises `SourceError` it propagates immediately and no item is
    touched — see module docstring on "a failed resolve changes nothing".
    Cursors are never read or written here; only `Item` rows change.
    """
    resolved = source.resolve(playlist_ref)

    seen_keys: set[str] = set()
    appended = restored = unchanged = 0

    for entry in resolved:
        seen_keys.add(entry.source_key)
        existing = store.items.find_by_key(library_id, entry.source_key)

        if existing is None:
            # `ItemRepo.add` positions new rows at max(position)+1, i.e.
            # the end of the library — new uploads queue up behind what
            # is already there.
            store.items.add(
                library_id,
                entry.kind,
                entry.source_ref,
                entry.source_key,
                entry.title,
                seconds=entry.seconds,
                local_path=entry.local_path,
                playlist_source_id=playlist_ref,
            )
            appended += 1
            continue

        # Never match/update on title: a re-title upstream must not churn
        # the existing row, so title is deliberately left untouched here.
        if existing.state == ItemState.UNAVAILABLE and existing.playlist_source_id == playlist_ref:
            store.items.set_state(existing.id, ItemState.OK)
            restored += 1
        else:
            unchanged += 1

    unavailable = 0
    for item in store.items.list(library_id):
        if item.kind != ItemKind.PLAYLIST_ENTRY or item.playlist_source_id != playlist_ref:
            continue
        if item.source_key not in seen_keys and item.state != ItemState.UNAVAILABLE:
            store.items.set_state(item.id, ItemState.UNAVAILABLE)
            unavailable += 1

    return SyncResult(appended=appended, unavailable=unavailable, restored=restored, unchanged=unchanged)


def sync_all_playlists(store, library_id: str, source: SourceProtocol) -> list[SyncResult]:
    """Re-resolve every playlist configured for `library_id` (settings key
    `f"playlists:{library_id}"`) against `source`, one `sync_playlist`
    call per reference, in order."""
    return [sync_playlist(store, library_id, ref, source) for ref in playlist_refs(store, library_id)]


# --- read-only preview (R21, Task 23): a dry run must not grow/shrink the
# catalog, but must still plan against what the real run would see — so a
# preview that turns up a new episode is honest about tonight's pick rather
# than reporting last night's stale answer. ---


def _preview_one(
    existing: list[Item], library_id: str, playlist_ref: str, source: SourceProtocol
) -> SyncPreview:
    """Same matching logic as `sync_playlist`, over an in-memory `existing`
    list rather than the store, producing a merged item list instead of
    issuing writes. `resolve()` still runs for real and `SourceError` still
    propagates before anything is inspected (R21 point 4; matches the hard
    requirement in `test_stage_then_swap.py`'s SourceError coverage) —
    only the *write* half of `sync_playlist` is skipped.

    Takes `existing` as a parameter rather than reading `store.items.list`
    itself so `preview_sync_all_playlists` can chain multiple playlists in
    one library, each seeing the previous one's synthetic additions, none
    of it ever touching the store.
    """
    resolved = source.resolve(playlist_ref)
    by_key = {i.source_key: i for i in existing}

    seen_keys: set[str] = set()
    appended = restored = unchanged = 0
    to_add = []
    restored_ids: set[str] = set()

    for entry in resolved:
        seen_keys.add(entry.source_key)
        existing_item = by_key.get(entry.source_key)
        if existing_item is None:
            to_add.append(entry)
            appended += 1
            continue
        if existing_item.state == ItemState.UNAVAILABLE and existing_item.playlist_source_id == playlist_ref:
            restored_ids.add(existing_item.id)
            restored += 1
        else:
            unchanged += 1

    unavailable_ids: set[str] = set()
    for item in existing:
        if item.kind != ItemKind.PLAYLIST_ENTRY or item.playlist_source_id != playlist_ref:
            continue
        if item.source_key not in seen_keys and item.state != ItemState.UNAVAILABLE:
            unavailable_ids.add(item.id)

    merged: list[Item] = []
    for item in existing:
        if item.id in unavailable_ids:
            item = replace(item, state=ItemState.UNAVAILABLE)
        elif item.id in restored_ids:
            item = replace(item, state=ItemState.OK)
        merged.append(item)

    # Synthetic rows for not-yet-persisted appends — a stable, clearly-not-a-
    # real-id id (`preview:<ref>:<key>` can never collide with a real id, and
    # is deterministic across calls in case anything ever needs to spot one),
    # positioned after everything currently in the library, exactly where
    # `ItemRepo.add` would really place it.
    next_pos = max((i.position for i in existing), default=-1) + 1
    for entry in to_add:
        merged.append(
            Item(
                id=f"preview:{playlist_ref}:{entry.source_key}",
                library_id=library_id,
                position=next_pos,
                kind=entry.kind,
                source_ref=entry.source_ref,
                source_key=entry.source_key,
                title=entry.title,
                seconds=entry.seconds,
                local_path=entry.local_path,
                playlist_source_id=playlist_ref,
            )
        )
        next_pos += 1

    result = SyncResult(
        appended=appended, unavailable=len(unavailable_ids), restored=restored, unchanged=unchanged
    )
    return SyncPreview(result=result, items=merged)


def preview_sync_playlist(store, library_id: str, playlist_ref: str, source: SourceProtocol) -> SyncPreview:
    """Read-only twin of `sync_playlist`: no `items.add`/`items.set_state`
    call anywhere on this path."""
    return _preview_one(store.items.list(library_id), library_id, playlist_ref, source)


def preview_sync_all_playlists(
    store, library_id: str, source: SourceProtocol
) -> tuple[list[SyncResult], list[Item]]:
    """Read-only twin of `sync_all_playlists`: every configured playlist's
    preview is folded in, in order, each seeing the last one's synthetic
    merge, and returns `(one SyncResult per ref, the final merged item
    list)` — the item list a real run's `plan()` would build from after
    every write actually landed, without any of them happening."""
    items = store.items.list(library_id)
    results: list[SyncResult] = []
    for ref in playlist_refs(store, library_id):
        preview = _preview_one(items, library_id, ref, source)
        results.append(preview.result)
        items = preview.items
    return results, items
