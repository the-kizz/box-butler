"""Fetcher for folder-backed items (spec §3.5.2, Task 17/38) — closes the
"no configured fetcher supports item kind <ItemKind.FOLDER_FILE>" gap found
on the first live deployment against a real `/media` mount. `scan_folder`
(Task 17) creates `FOLDER_FILE` items and `sync_media_root` (Task 38) wires
`/media` subfolders into libraries, but nothing in `boxbutler/fetch/` ever
claimed the kind — the composite fetcher fell straight to
`ExtractionBroken` on the very first prefetch of a real folder library,
even though the dry run (which never calls a fetcher) looked fine.

## No copy into the cache

A folder item's bytes are already on disk — `scan_folder` recorded the
absolute path at `item.local_path` when it scanned the library. `fetch()`
is not "download the bytes"; it is "hand back a path the renderer can
read". `boxbutler.audio.ffmpeg.FfmpegRenderer` stream-copy-trims whatever
path it's given straight into its own render, so copying the source into
`cache_dir` first would mean holding two full copies of every file on
disk (a 700 MB library) for a copy nothing downstream needs — the file
is already exactly as local as a cached download would be, just without
the download. So `fetch()` returns `item.local_path` itself, unchanged,
after confirming it is still there and readable. Nothing is ever written
under `cache_dir` for this kind, and nothing under the source tree is
ever opened for anything but a stat — nothing under the read-only
`/media` mount is touched.

## Failure classification (spec §3.5.1)

A single file that vanished since the last scan (deleted mid-library, a
dangling symlink) is `ItemUnavailable` — skip it, the rest of the
library is unaffected. An absent or unreadable *library root* is a
different shape of failure: it will fail every item under it identically
in the same fetch pass, the exact case `scan_folder` already treats
specially (`root.is_dir()` / `PermissionError` -> mark-unavailable, never
"the operator deleted everything"). Here that same distinction is drawn
by raising `ExtractionBroken` instead — this fetcher has no orchestrator
loop to quietly skip every sibling one at a time, and reporting a
one-off deleted file identically to a whole unmounted NFS share would
hide the more urgent failure behind a wall of per-item skips. The root
is derived from `item.local_path`/`item.source_ref` (the same
`local_path == root / source_ref` invariant `scan_folder` establishes)
since the fetcher never receives the `Library` row itself.
"""
from __future__ import annotations

from pathlib import Path

from boxbutler.domain.models import Item, ItemKind
from boxbutler.fetch.protocol import ExtractionBroken, ItemUnavailable


class FolderFetcher:
    """`FetcherProtocol` for `ItemKind.FOLDER_FILE` — reads the file where
    `scan_folder` found it; never copies it into `cache_dir`."""

    def supports(self, kind: ItemKind) -> bool:
        return kind is ItemKind.FOLDER_FILE

    def fetch(self, item: Item, cache_dir: Path) -> Path:
        if not item.local_path:
            # Never recorded (or cleared) by the scanner -- there is
            # nothing to read. Skip this one item; it says nothing about
            # any other item in the library.
            raise ItemUnavailable(f"no local_path recorded for item {item.id!r}")

        path = Path(item.local_path)
        root = self._root_for(item, path)

        try:
            root_ok = root.is_dir()
        except PermissionError:
            # Same treatment `scan_folder` gives a `root` whose parent
            # lost search permission -- not distinguishable from "gone"
            # from here, and must not be read as one file's problem.
            root_ok = False
        if not root_ok:
            raise ExtractionBroken(
                f"folder library root {root} is missing or unreadable "
                f"(unmounted share or locked-down permissions) -- will "
                f"fail every item under it, not just {item.id!r}"
            )

        try:
            st = path.stat()
        except OSError as exc:
            # The root is fine but this one file is gone/unreadable --
            # a deletion or permission change on a single file, not a
            # mount problem. Skip it and carry on.
            raise ItemUnavailable(f"file unreadable: {path}: {exc}") from exc
        if not path.is_file():
            raise ItemUnavailable(f"not a regular file: {path}")
        # `st` is only used to force the stat above to happen (and thus
        # raise on an unreadable file) before returning the path -- never
        # to derive a size or duration. Those come only from a real
        # probe (spec: never invent a duration or size).
        del st

        return path

    @staticmethod
    def _root_for(item: Item, path: Path) -> Path:
        """The library folder root implied by `local_path`/`source_ref`:
        `scan_folder` always constructs `local_path` as
        `root / source_ref`, so stripping `source_ref`'s components off
        the end of `local_path` recovers `root` without needing the
        `Library` row here. Falls back to the file's immediate parent if
        that invariant doesn't hold -- defensive only; nothing in the
        scan path ever produces an item that violates it.
        """
        rel_parts = Path(item.source_ref).parts
        if rel_parts and path.parts[-len(rel_parts):] == rel_parts and len(path.parts) > len(rel_parts):
            return Path(*path.parts[: -len(rel_parts)])
        return path.parent
