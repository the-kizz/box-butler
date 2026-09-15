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

## A source is how media arrives in the library's folder

A library is exactly one folder (`boxbutler/sources/library_folder.py`).
Adding a video, a playlist, a feed or an upload therefore *downloads the
audio into that folder*, the way Sonarr imports into a root folder —
after which it is an ordinary file, indistinguishable from one the
operator copied in by hand. Rotation, ordering, pinning and trimming then
treat every item identically, because every item genuinely is a file in a
folder.

Three rules govern that download, and `_materialise` below is the only
place they are implemented:

- **Never re-download what is already there.** A file in the folder whose
  bracketed source key matches is the same item, whatever its title half
  says — so an operator's rename does not produce a duplicate, and adding
  the same link twice fetches nothing the second time. Identity is the
  content fingerprint / source key, never the filename.
- **Never overwrite.** The bytes land in a staging directory under
  `/cache` first and enter the library folder only through
  `library_folder.adopt_into`, which cannot clobber an existing file.
- **Never lose the item to a failed download.** A dead video or a broken
  extractor at *add* time leaves the row in place with no `local_path`;
  the orchestrator fetches it again at run time. Dropping the row would
  make pasting a link look like it silently did nothing.

`fetcher` is optional so the Phase 2 fake wiring and the store-layer tests
can still build an `Ingestor` that only records rows. A real install
always passes one (`boxbutler/main.py::create_web_app`).
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from boxbutler.domain.cache_name import cache_name, sanitise_title
from boxbutler.domain.models import Item, ItemKind
from boxbutler.sources.library_folder import adopt_into, find_by_source_key
from boxbutler.sources.protocol import ResolvedItem, SourceError, SourceProtocol
from boxbutler.sources.upload import UploadSource

logger = logging.getLogger(__name__)


class Ingestor:
    def __init__(
        self,
        store,
        sources: list[SourceProtocol],
        fetcher=None,
        staging_dir: Path | None = None,
    ):
        self._store = store
        self._sources = sources
        self._fetcher = fetcher
        self._staging_dir = Path(staging_dir) if staging_dir is not None else None

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
        item = self._store.items.add(
            library_id,
            r.kind,
            r.source_ref,
            r.source_key,
            title,
            seconds=r.seconds,
            local_path=r.local_path,
        )
        return self._materialise(library_id, item, r)

    # ------------------------------------------------ into the library folder

    def _materialise(self, library_id: str, item: Item, r: ResolvedItem) -> Item:
        """Put this item's audio in the library's folder and record where
        it landed. Returns the item, with `local_path` updated if it
        moved.

        Failures here are logged, never raised: the item row is the thing
        the operator just asked for, and the orchestrator can fetch the
        audio later. See the module docstring's third rule.
        """
        library = self._store.libraries.get(library_id)
        folder = Path(library.folder_path) if library and library.folder_path else None
        if folder is None:
            # Only reachable for a library written before "a library is a
            # folder"; `ensure_library_folders` fixes those at startup.
            return item

        staging: Path | None = None
        try:
            existing = find_by_source_key(folder, item.source_key)
            if existing is not None:
                return self._record_path(item, existing)

            staged, staging = self._staged_bytes_for(item, r)
            if staged is None:
                return item
            if folder.resolve() in staged.resolve().parents:
                # Already inside the library folder. Adopting it would
                # *move* a file we did not put there, which is exactly
                # what this app must never do -- record it where it is.
                return self._record_path(item, staged)

            landed = adopt_into(
                folder, staged, cache_name(item.title, item.source_key, _ext_of(staged))
            )
            return self._record_path(item, landed)
        except Exception:
            logger.exception(
                "could not put %r into %s; the item was added and will be fetched "
                "at the next run", item.title, folder,
            )
            return item
        finally:
            # Whatever happened, the staging directory never survives the
            # call: a half-downloaded file left there would be a cache hit
            # for nobody and a puzzle for everybody. `adopt_into` has
            # already moved the finished file out by this point.
            if staging is not None:
                self._clear_staging(staging)

    def _staged_bytes_for(self, item: Item, r: ResolvedItem) -> tuple[Path | None, Path | None]:
        """The item's audio, sitting somewhere disposable and ready to be
        adopted into the library folder, plus the staging directory the
        caller must clear afterwards. `(None, ...)` means it could not be
        obtained.

        An upload has already written itself to the upload directory, so
        those bytes *are* the staging copy. Everything else is fetched
        into a private staging directory under `/cache`, never straight
        into the library folder: a fetcher writes whatever name it likes
        and may write partial files while it works, and neither belongs in
        a folder the operator is looking at.
        """
        if r.local_path:
            candidate = Path(r.local_path)
            if candidate.is_file():
                return candidate, None

        if self._fetcher is None or self._staging_dir is None:
            return None, None

        staging = self._staging_dir / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=True)
        try:
            fetched = Path(self._fetcher.fetch(item, staging))
        except Exception:
            logger.warning(
                "could not download %r when it was added; it will be fetched at "
                "the next run instead", item.title, exc_info=True,
            )
            return None, staging
        return (fetched if fetched.is_file() else None), staging

    @staticmethod
    def _clear_staging(staging: Path) -> None:
        try:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass

    def _record_path(self, item: Item, path: Path) -> Item:
        from dataclasses import replace

        self._store.items.set_local_path(item.id, str(path))
        return replace(item, local_path=str(path))

    def _record_playlist(self, library_id: str, ref: str) -> None:
        # Task 18 re-resolves each recorded playlist ref to mark vanished
        # entries unavailable without touching the rest of the library.
        key = f"playlists:{library_id}"
        existing = self._store.settings.get(key, [])
        if ref not in existing:
            self._store.settings.set(key, [*existing, ref])


def _ext_of(path: Path) -> str:
    return path.suffix.lstrip(".") or "bin"
