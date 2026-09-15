"""Folder-backed libraries — scan a directory the way a media server scans
a library (spec §3.5.2, §10.15).

"Should also be files added to a volume like a Plex library works. Yeah?
Doesn't matter how it gets there." (operator, brainstorming). Files arrive
however the household already moves them — SMB, NFS, `rsync`, a download
client, dragging them from a laptop — and Box Butler neither knows nor
cares. A scan just reflects what is on disk right now.

**Ownership distinction (load-bearing):** `/media` is read-write now —
`Ingestor` downloads a source's audio into the library folder — but a
*scan* is still purely a read. `scan_folder` must never write, move,
rename, or delete anything under `root`, and a scan of a `chmod`-ed
read-only fixture directory is still expected to succeed
(`test_scan_against_read_only_media_root_succeeds`): an operator who
mounts their library read-only can still use Box Butler as a cataloguer,
they just cannot add sources to it. The broader guarantee that replaced
the read-only mount — the app only ever creates new files and never
touches one it did not create — lives in
`boxbutler/sources/library_folder.py` and is covered by
`tests/test_media_is_append_only.py`.

A file `Ingestor` downloaded into the folder is already an item of this
library under its own kind (`YOUTUBE`, `RSS`, `UPLOAD`, ...). A scan skips
it rather than adding a second, `FOLDER_FILE` item for the same audio.

**Identity:** `source_key` is `fingerprint(path)` — the same
`f"{size}-{sha1(head4MiB + tail4MiB)[:16]}"` scheme Task 16's
`UploadSource` uses (imported from `boxbutler.sources.upload`, not
reimplemented). It depends only on content, not on path, so a rename or
move within the scanned tree is recognised as the same item rather than
re-added — `test_renamed_file_is_matched_by_fingerprint`. The fingerprint
never contains `/`, `.`, `[` or `]` (it's `<int>-<16 hex chars>`), so it
round-trips through `cache_name()`/`parse_cache_name()` exactly like the
upload key does — see `test_all_resolver_keys_round_trip_cache_name` in
`tests/sources/test_resolvers.py`, extended for a folder item.

A hash collision is rare but not impossible, and ripped-audiobook
chapters — this project's primary folder-library use case — are exactly
the shape (shared encoder header, similar length, trailing silence) that
makes a partial hash more likely to collide (see `fingerprint()`'s
docstring in `boxbutler.sources.upload` for how the sample was widened to
push this further out). Rather than rely on that alone, `scan_folder`
distinguishes a genuine rename from a collision by whether the *old* path
is still there: `find_by_key` returning an existing item whose
`local_path` differs from the current file is only treated as a rename
if that old path no longer exists. If it still exists as a file, two
distinct files are claiming one identity — that is a collision, not a
rename, and overwriting the existing row would silently make the first
file invisible (not unavailable, not re-added — just gone, with no
error). Instead the second file is recorded as a distinct item under a
disambiguated key (`"<key>-cN"`, still hex/digits/hyphen only, so it
round-trips through `cache_name()` the same as any other key) and a
warning is logged — never a silent drop of one file's row.

**Order:** natural sort on the path relative to `root`, so `01`, `02`,
`10` sort the way a human means — what lets a ripped audiobook work in
`serial` mode with no manual reordering.

**Titles:** embedded tags first (`probe(path).tags["title"]`), filename
stem as fallback — both sanitised through `sanitise_title`, the one
sanitiser used everywhere else. A `probe` failure (unreadable tags, or no
`probe` given at all) falls back to the filename; it never aborts the
whole scan over one bad file.

**Caching:** none — the file is already local, only a *rendition* enters
the cache (Task 14). This module never touches `boxbutler/audio/cache`.

**Disappearance:** an item whose fingerprint no longer appears under
`root` is marked `ItemState.UNAVAILABLE`, never deleted — positions behind
it must not shift, and a transient NFS unmount must not destroy a
library. `root` missing or not-a-directory is treated the same way: every
existing item in the library is marked unavailable and nothing else
happens (`test_unmounted_volume_marks_unavailable_and_never_deletes_library`).

**Recursion:** the whole tree under `root` is scanned recursively as one
library; per-subfolder libraries are deliberately not built (spec
§3.5.2 — "not now").
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_logger = logging.getLogger(__name__)

from boxbutler.audio.protocol import ProbeResult
from boxbutler.domain.cache_name import sanitise_title
from boxbutler.domain.models import ItemKind, ItemState, LibraryMode
from boxbutler.sources.upload import fingerprint

# The sink's 12 accepted formats (spec §3.4) plus their common extension
# spellings — a folder-backed `.m4b` audiobook or `.flac` album therefore
# ships with no transcode at all, at most a stream-copy trim past the cap
# (decided later, by `boxbutler.audio.protocol.choose_mode`; this module
# only decides which files on disk count as "audio" to scan).
AUDIO_EXTS: frozenset[str] = frozenset({
    ".aac", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".m4b",
    ".wav", ".oga", ".ogg", ".opus", ".wma",
})

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple:
    """Sort key so "10 - x" sorts after "2 - x", not before it — a plain
    string sort would put "10" before "2" character-by-character.

    A real media directory mixes digit-leading names with letter-leading
    ones (`"1 Bedtime"` next to `"Nature Sounds"`, or `"1.mp3"` next to
    `"Track.mp3"`), and `sorted()`/`list.sort()` compares corresponding
    *elements* of these tuples pairwise — so an `int` from one name's key
    can be compared directly against a `str` from another's, which Python
    3 refuses (`TypeError: '<' not supported between instances of 'str'
    and 'int'`), aborting the whole sync (Task 38 review, Critical-2).
    Each element is therefore wrapped as `(0, int)` for a digit run or
    `(1, str)` for a text run: same-group elements compare on their
    second item as before (numeric runs numerically, text runs
    case-insensitively), and a digit run always sorts before a text run
    at the same position — an arbitrary but deterministic and documented
    choice, not a claim that digits are "smaller". `"track2"` still sorts
    before `"track10"` (numeric comparison within the digit group), and
    `"01"` and `"1"` produce the *same* key -- `int()` consumes the
    leading zero -- so between just those two names the tie falls back
    to Python's stable sort, preserving whatever order they arrived in.
    """
    parts = _DIGITS.split(name)
    return tuple(
        (0, int(p)) if p.isdigit() else (1, p.lower())
        for p in parts
        if p != ""
    )


def _probe_once(path: Path, probe: "Callable[[Path], ProbeResult] | None") -> "ProbeResult | None":
    """Probe `path` at most once, `None` on no-probe-given or any probe
    failure (corrupt/unreadable header) -- never lets a probe exception
    abort the scan. Callers derive *both* title and duration from this one
    result rather than each calling `probe()` separately, so a new file
    costs one ffprobe invocation during a scan, not two."""
    if probe is None:
        return None
    try:
        return probe(path)
    except Exception:
        return None


def _title_and_seconds(path: Path, probe_result: "ProbeResult | None") -> tuple[str, float | None]:
    """Title (embedded tag first, filename stem fallback — mirrors
    `title_for`'s own rule) and duration from one already-taken probe
    result. `seconds` is `None`, never `0.0`, when there is no usable
    result: a folder item shipping with a false "0 seconds" would read as
    "this item is 0 minutes long" instead of "we don't know" -- the same
    "unknown recorded as a definite value" bug shape this codebase has
    shipped a dozen times before (see `ffmpeg.py::probe`'s identical
    guard)."""
    tag_title = probe_result.tags.get("title") if probe_result is not None else None
    title = sanitise_title(tag_title) if tag_title else sanitise_title(path.stem)
    seconds = probe_result.seconds if probe_result is not None else None
    return title, seconds


def title_for(path: Path, probe: "Callable[[Path], ProbeResult] | None") -> str:
    """Embedded tag title first, filename stem as fallback — both
    sanitised. Any probe failure (unreadable/absent tags, no probe given)
    falls back to the filename rather than raising; a single bad file
    must never abort a scan."""
    if probe is not None:
        try:
            result = probe(path)
            tag_title = result.tags.get("title")
        except Exception:
            tag_title = None
        if tag_title:
            return sanitise_title(tag_title)
    return sanitise_title(path.stem)


@dataclass(frozen=True)
class ScanResult:
    added: int
    unchanged: int
    renamed: int
    unavailable: int
    restored: int
    collisions: int = 0


@dataclass(frozen=True)
class MediaRootSyncResult:
    """Result of one `sync_media_root` pass. `libraries_created` counts
    only brand-new library rows; per-library scan detail is in `scans`
    (one `ScanResult` per library touched this pass, in the same order
    the libraries were processed)."""
    libraries_created: int
    scans: list[ScanResult]


def scan_folder(store, library, root: Path, probe: "Callable[[Path], ProbeResult] | None" = None) -> ScanResult:
    """Scan `root` recursively into `library`. Never opens anything under
    `root` for writing — fingerprinting and probing are both read-only.

    A missing/non-directory `root` (unmounted NFS share, typo'd path) is
    not an error: every existing item in the library is marked
    unavailable and nothing is deleted or reordered. Neither is a `root`
    that exists but can't be entered -- `is_dir()` itself raises
    `PermissionError` when any parent directory in the path lacks search
    (execute) permission, not just when `root` lacks it directly (Task
    38 review, Major-1: the same real NFS-permission shape that
    `_immediate_subfolders` guards against also reaches here, once
    `sync_media_root` rescans a library whose folder sits under a
    since-locked-down `media_root`). Treated identically to "missing".
    """
    root = Path(root)
    try:
        usable = root.is_dir()
    except PermissionError:
        usable = False
    if not usable:
        existing = store.items.list(library.id)
        marked = 0
        for item in existing:
            if item.state != ItemState.UNAVAILABLE:
                store.items.set_state(item.id, ItemState.UNAVAILABLE)
            marked += 1
        return ScanResult(added=0, unchanged=0, renamed=0, unavailable=marked, restored=0)

    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    files.sort(key=lambda p: natural_key(str(p.relative_to(root))))

    # Indexed by local_path as well as by source_key: a file previously
    # recorded under a disambiguated collision key (`"<key>-cN"`) must be
    # recognised as the *same* item on a later scan by its path, not
    # re-diagnosed as a fresh collision against `key` every time (its raw
    # fingerprint never changes, but its item's key is the disambiguated
    # one, which `find_by_key(key)` would never find).
    known_items = store.items.list(library.id)
    by_path = {
        item.local_path: item
        for item in known_items
        if item.kind == ItemKind.FOLDER_FILE and item.local_path
    }
    # Files this library already owns under a *different* kind: a video,
    # feed episode or upload that `Ingestor` downloaded into this folder
    # (see `boxbutler/sources/ingest.py`). They are ordinary files now,
    # so a scan walks straight over them -- but they are not new, and
    # adding them again as `FOLDER_FILE` would give one piece of audio two
    # items, two positions and two chances to be picked. Skipped by path,
    # not by name, because a re-download under a collision-avoiding name
    # is still recorded at the path the item points at.
    ingested_paths = {
        item.local_path
        for item in known_items
        if item.kind != ItemKind.FOLDER_FILE and item.local_path
    }

    seen_keys: set[str] = set()
    added = unchanged = renamed = restored = collisions = 0

    for path in files:
        if str(path) in ingested_paths:
            continue
        try:
            key = fingerprint(path)
        except OSError:
            # Unreadable file (permissions, dangling symlink, vanished
            # mid-scan): skip it, never abort the whole scan.
            continue

        new_local = str(path)
        existing = by_path.get(new_local) or store.items.find_by_key(library.id, key)

        if existing is not None and existing.local_path != new_local:
            old_path = Path(existing.local_path) if existing.local_path else None
            if old_path is not None and old_path != path and old_path.is_file():
                # Collision, not a rename: the row `find_by_key` returned
                # still points at a file that is genuinely still there.
                # Overwriting it would make that file silently invisible
                # (see module docstring). Record this file separately
                # instead, under a disambiguated key.
                n = 1
                disambiguated = f"{key}-c{n}"
                while (
                    disambiguated in seen_keys
                    or store.items.find_by_key(library.id, disambiguated) is not None
                ):
                    n += 1
                    disambiguated = f"{key}-c{n}"
                _logger.warning(
                    "fingerprint collision in %s: %r and %r share key %s; "
                    "keeping both as distinct items (%s)",
                    root, existing.local_path, new_local, key, disambiguated,
                )
                relpath = path.relative_to(root)
                probe_result = _probe_once(path, probe)
                title, seconds = _title_and_seconds(path, probe_result)
                store.items.add(
                    library.id,
                    ItemKind.FOLDER_FILE,
                    str(relpath),
                    disambiguated,
                    title,
                    seconds=seconds,
                    local_path=new_local,
                )
                seen_keys.add(disambiguated)
                collisions += 1
                continue

        # Track the item's *actual stored* key, not necessarily the raw
        # fingerprint just computed — a file matched by path may be
        # recorded under a disambiguated collision key from an earlier
        # scan, and that is the key that must survive into the
        # unavailable-detection pass below.
        seen_keys.add(existing.source_key if existing is not None else key)

        if existing is None:
            relpath = path.relative_to(root)
            probe_result = _probe_once(path, probe)
            title, seconds = _title_and_seconds(path, probe_result)
            store.items.add(
                library.id,
                ItemKind.FOLDER_FILE,
                str(relpath),
                key,
                title,
                seconds=seconds,
                local_path=new_local,
            )
            added += 1
            continue

        changed = False
        if existing.local_path != new_local:
            store.items.set_local_path(existing.id, new_local, source_ref=str(path.relative_to(root)))
            renamed += 1
            changed = True
        if existing.state == ItemState.UNAVAILABLE:
            store.items.set_state(existing.id, ItemState.OK)
            restored += 1
            changed = True
        if not changed:
            unchanged += 1

    unavailable = 0
    for item in store.items.list(library.id):
        if item.kind != ItemKind.FOLDER_FILE:
            continue
        if item.source_key not in seen_keys and item.state != ItemState.UNAVAILABLE:
            store.items.set_state(item.id, ItemState.UNAVAILABLE)
            unavailable += 1

    return ScanResult(
        added=added,
        unchanged=unchanged,
        renamed=renamed,
        unavailable=unavailable,
        restored=restored,
        collisions=collisions,
    )


def _immediate_subfolders(media_root: Path) -> list[Path]:
    """Immediate child directories of `media_root`, natural-sorted by
    name. A missing/non-directory `media_root` yields the empty list —
    the same "not an error" treatment `scan_folder` gives a missing
    `root`, not a special case here. A `media_root` that exists but is
    unreadable (an NFS mount with a permission problem is exactly as
    "unknown" as one that failed to mount at all — Task 38 review,
    Major-1) yields the empty list too, rather than letting
    `PermissionError` escape `iterdir()`: `sync_media_root` treats an
    empty listing as "nothing present right now" and marks every known
    library's items unavailable without deleting or reordering anything
    — the governing principle applies here just as much as to a missing
    root. Do not rely on the caller swallowing this; `sync_media_root`
    must behave correctly in isolation."""
    if not media_root.is_dir():
        return []
    try:
        subs = [p for p in media_root.iterdir() if p.is_dir()]
    except PermissionError:
        return []
    subs.sort(key=lambda p: natural_key(p.name))
    return subs


def sync_media_root(
    store,
    media_root: Path,
    probe: "Callable[[Path], ProbeResult] | None" = None,
) -> MediaRootSyncResult:
    """Reflect `media_root`'s immediate subfolders as folder-backed
    libraries (spec §3.5.2/§8; Task 38) — the read-only `/media:ro` mount
    made usable end to end: each subfolder present under `media_root`
    right now gets (or already has) exactly one `Library` row with a
    matching `folder_path`, scanned via `scan_folder`.

    **Never deletes a library row, ever.** A `media_root` that is
    missing entirely or present-but-empty (a failed NFS automount looks
    exactly like this) is indistinguishable from "the operator deleted
    everything" and must never be read as that — see the module
    docstring's "ninth instance of this bug family" note and the task
    brief's governing principle: when in doubt, the tonie keeps last
    night's story. Concretely: this function only ever *adds* a library
    row for a subfolder that is actually present on disk; a
    previously-synced library whose subfolder has vanished (whether
    because only that one folder disappeared, the whole mount emptied,
    or `media_root` itself is gone) is rescanned against its own
    now-missing folder path, which `scan_folder` already reports as
    every item going `UNAVAILABLE` without deleting or reordering
    anything. The library row itself is left exactly as it was.

    Matching an existing library to a subfolder is by `folder_path`
    string equality (`Library.folder_path == str(subfolder)`), and a
    library only counts as "belonging to this sync" if its
    `folder_path`'s parent is `media_root` itself — a library an
    operator created by hand pointing somewhere else entirely (spec
    still allows manual folder libraries outside `/media`, per
    `boxbutler/web/routes/library.py`) is never touched by this
    function.
    """
    media_root = Path(media_root)
    current = _immediate_subfolders(media_root)
    current_paths = {str(p) for p in current}

    known = {
        lib.folder_path: lib
        for lib in store.libraries.list()
        if lib.folder_path and Path(lib.folder_path).parent == media_root
    }

    created = 0
    scans: list[ScanResult] = []

    for sub in current:
        key = str(sub)
        lib = known.get(key)
        if lib is None:
            lib = store.libraries.create(sub.name, LibraryMode.SERIAL, folder_path=key)
            known[key] = lib
            created += 1
        scans.append(scan_folder(store, lib, sub, probe=probe))

    for key, lib in known.items():
        if key in current_paths:
            continue
        # This subfolder is not present right now (root missing, root
        # emptied, or just this one folder removed) — mark its items
        # unavailable via the same read-only path `scan_folder` already
        # uses for a missing `root`. The library row is untouched.
        scans.append(scan_folder(store, lib, Path(key), probe=probe))

    return MediaRootSyncResult(libraries_created=created, scans=scans)
