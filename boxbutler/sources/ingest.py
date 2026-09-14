"""`Ingestor` — the seam that makes every ingest method equivalent (spec
§3.5, §10.14).

`add_ref` tries each configured `SourceProtocol` in order and lets the
first that recognises the reference resolve it. Every `ResolvedItem` a
resolver produces, regardless of which one, goes through the exact same
path into the store: title sanitised via `sanitise_title` (same function
the fetchers use for cache/chapter names — never a second implementation),
then `store.items.add`, which dedupes on `(library_id, source_key)`.
Nothing here branches on `kind` except to record playlist provenance for
Task 18's re-resolve.
"""
from __future__ import annotations

from typing import BinaryIO

from boxbutler.domain.cache_name import sanitise_title
from boxbutler.domain.models import Item, ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError, SourceProtocol
from boxbutler.sources.upload import UploadSource


class Ingestor:
    def __init__(self, store, sources: list[SourceProtocol]):
        self._store = store
        self._sources = sources

    def add_ref(self, library_id: str, ref: str) -> list[Item]:
        for source in self._sources:
            if not source.matches(ref):
                continue
            resolved = source.resolve(ref)
            items = [self._store_resolved(library_id, r) for r in resolved]
            if resolved and resolved[0].kind == ItemKind.PLAYLIST_ENTRY:
                self._record_playlist(library_id, ref)
            return items
        raise SourceError("no ingest method recognises this reference")

    def add_upload(self, library_id: str, filename: str, stream: BinaryIO) -> Item:
        upload_source = next(
            (s for s in self._sources if isinstance(s, UploadSource)), None
        )
        if upload_source is None:
            raise SourceError("no upload source configured for this Ingestor")
        resolved = upload_source.save(filename, stream)
        return self._store_resolved(library_id, resolved)

    def _store_resolved(self, library_id: str, r: ResolvedItem) -> Item:
        title = sanitise_title(r.title)
        return self._store.items.add(
            library_id,
            r.kind,
            r.source_ref,
            r.source_key,
            title,
            seconds=r.seconds,
            local_path=r.local_path,
        )

    def _record_playlist(self, library_id: str, ref: str) -> None:
        # Task 18 re-resolves each recorded playlist ref to mark vanished
        # entries unavailable without touching the rest of the library.
        key = f"playlists:{library_id}"
        existing = self._store.settings.get(key, [])
        if ref not in existing:
            self._store.settings.set(key, [*existing, ref])
