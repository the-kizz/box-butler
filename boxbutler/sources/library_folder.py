"""A library is exactly one folder — the rules for where that folder is
and what may happen inside it.

This module replaces the old split between "folder-backed" and
"link-backed" libraries. Every library now has one directory under the
media root; sources are only *how media arrives in it*. Adding a YouTube
video, a playlist entry, a podcast episode or an upload downloads the
audio into the library's folder, the way Sonarr imports into a root
folder, after which it is an ordinary file — indistinguishable from one
the operator copied in by hand.

## `/media` is writable now, under a narrower guarantee

The property this codebase used to assert — *nothing in the image may
ever write to `/media`* — was correct while the app only read. It is
replaced, not weakened, by four rules that this module is the single
place that implements:

- The app **only ever creates new files** in a library folder.
- It **never modifies, renames, moves, truncates or deletes a file it did
  not create.** `adopt_into` is the only way a file enters a library
  folder and it is incapable of overwriting: the final name is claimed
  with `os.link` (or, on a filesystem without hard links, an
  `O_CREAT|O_EXCL` open), both of which *fail* rather than clobber.
- A name collision is resolved by **choosing a different name**, never by
  overwriting — and the different name still parses as a cache name, so
  identity survives it.
- Deleting an item removes the database row. Deleting the *file* is an
  explicit, confirmed choice in the UI (`web/routes/library.py`), never
  implicit and never the default.

`tests/test_media_is_append_only.py` is the safety net that replaces the
read-only mount: it fills a library folder with files the app did not
create, runs every operation that touches a library, and asserts each one
is byte-identical afterwards.

## `/media` is required, not optional

Every library has a folder, so there is nowhere for one to live without
the mount. `require_media_root` fails startup with a message naming the
missing mount rather than falling back to a directory inside `/data` —
that would quietly put multi-hour audio in the volume the operator backs
up. This is what Plex, Sonarr, Immich and Paperless all do; none of them
falls back either.

## The picker is confined to the media root

`resolve_within` is the one place a caller-supplied path becomes a real
one, and it refuses three separate things rather than one: a `..`
component, an absolute path (`media_root / "/etc"` is `/etc` — pathlib
discards the left operand), and a resolved path that lands outside the
root, which is what catches a symlink pointing elsewhere. Callers must
never join a path themselves.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath

from boxbutler.domain.cache_name import parse_cache_name, sanitise_title

_logger = logging.getLogger(__name__)


class LibraryFolderError(Exception):
    """A library folder could not be established, or a caller-supplied
    path was refused. The message names the path — a media path is not a
    secret, and naming it is what makes the error actionable."""


# ------------------------------------------------------------- media root


def require_media_root(media_root: Path | str | None) -> Path:
    """The media root, resolved, or `LibraryFolderError` naming what is
    wrong with it.

    Deliberately no fallback. A library with no folder is not a thing that
    can exist any more, so a missing `/media` is a configuration failure,
    not a degraded mode — and the one "helpful" fallback available (a
    directory inside `/data`) is the worst possible one, because `/data`
    is the volume an operator backs up and library folders hold hours of
    audio.
    """
    if media_root is None:
        raise LibraryFolderError(
            "no media root configured: Box Butler needs one directory to hold "
            "library folders. Mount it at /media (or set BOXBUTLER_MEDIA_ROOT)."
        )
    root = Path(media_root)
    if not root.is_dir():
        raise LibraryFolderError(
            f"media root {root} is not mounted (no such directory). Every library "
            "is a folder under it, so Box Butler cannot start without it — bind-mount "
            "your audio directory there, e.g. `- /path/to/media:/media` in compose."
        )
    if not os.access(root, os.W_OK | os.X_OK):
        raise LibraryFolderError(
            f"media root {root} is not writable by this process (uid {os.geteuid()}). "
            "Downloads land in library folders under it, so it can no longer be "
            "mounted read-only — drop the `:ro` flag, and set PUID/PGID to a "
            "uid that can write to your media directory."
        )
    return root.resolve()


# --------------------------------------------------------- naming a folder


def folder_name_for(name: str) -> str:
    """One path segment derived from a library name, safe to create under
    the media root.

    `sanitise_title` is the project's single sanitiser (it already strips
    separators and control characters and never returns an empty string),
    so this is it plus the two names a directory may not have.
    """
    segment = sanitise_title(name)
    if segment in (".", ".."):
        return "library"
    return segment


def library_folder_for(media_root: Path, name: str) -> Path:
    """Where a library called `name` would live: `<media_root>/<name>`, or
    the first free `<name> (N)` if that directory is already some other
    library's. Creates nothing."""
    root = Path(media_root)
    base = folder_name_for(name)
    candidate = root / base
    n = 1
    while candidate.exists() and not candidate.is_dir():
        n += 1
        candidate = root / f"{base} ({n})"
    return candidate


def ensure_library_folder(media_root: Path, name: str, *, taken: set[str] | None = None) -> Path:
    """The folder for a library called `name`, created if it isn't there.

    An existing directory is reused as-is — never emptied, never
    re-owned. `taken` lets a caller creating several folders in one pass
    (the migration below) avoid handing two libraries the same one.
    """
    root = Path(media_root)
    base = folder_name_for(name)
    taken = taken or set()
    candidate = root / base
    n = 1
    while str(candidate) in taken or (candidate.exists() and not candidate.is_dir()):
        n += 1
        candidate = root / f"{base} ({n})"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ------------------------------------------------------------- confinement


def resolve_within(media_root: Path, relative: str | None) -> Path:
    """`media_root / relative`, resolved, guaranteed to be the root itself
    or something inside it.

    Three separate refusals, because a check that only catches one of them
    reads as safe while leaving the other two open:

    1. an absolute `relative` — `Path("/media") / "/etc"` is `/etc`;
    2. any `..` component, before resolution, so the intent is refused
       rather than silently normalised;
    3. a resolved path outside the root — the only check that catches a
       symlink inside the root pointing somewhere else.
    """
    root = Path(media_root).resolve()
    rel = (relative or "").strip()
    if rel in ("", "."):
        return root

    pure = PurePosixPath(rel)
    if pure.is_absolute() or Path(rel).is_absolute():
        raise LibraryFolderError(
            f"{rel!r} is an absolute path; folders are chosen inside {root}, not anywhere on the host"
        )
    if ".." in pure.parts:
        raise LibraryFolderError(f"{rel!r} tries to leave {root}")

    candidate = (root / pure).resolve()
    if candidate != root and root not in candidate.parents:
        # Reached only via a symlink (or a race): the components looked
        # innocent and the destination is not.
        raise LibraryFolderError(f"{rel!r} resolves outside {root}")
    return candidate


def list_subfolders(media_root: Path, relative: str | None = "") -> list[Path]:
    """The immediate child directories of `relative` inside the media
    root, name-sorted. Confined by `resolve_within`, so there is no
    browsing above the root.

    A directory that is missing or unreadable yields the empty list rather
    than raising — the same "not an error, just nothing there right now"
    treatment `scan_folder` gives a missing root.
    """
    base = resolve_within(media_root, relative)
    try:
        subs = [p for p in base.iterdir() if p.is_dir()]
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    subs.sort(key=lambda p: p.name.lower())
    return subs


# ------------------------------------------------- putting a file in a folder


def _collision_names(name: str):
    """The names to try, in order, when `name` is taken. The counter goes
    into the *title* half so the result still parses as a cache name —
    identity lives in the bracketed source key, and a re-scan has to keep
    finding it."""
    yield name
    try:
        title, key, ext = parse_cache_name(name)
        template = lambda n: f"{title} ({n}) [{key}].{ext}"  # noqa: E731
    except ValueError:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        template = lambda n: f"{stem} ({n})" + (f".{ext}" if ext else "")  # noqa: E731
    n = 1
    while True:
        n += 1
        yield template(n)


def _claim(tmp: Path, target: Path) -> bool:
    """Put `tmp`'s bytes at `target` without any possibility of
    overwriting. `False` means `target` already existed and nothing was
    touched.

    `os.link` is the primary mechanism precisely because it *fails* on an
    existing destination, where `rename`/`replace` would silently clobber
    it. A filesystem with no hard links (SMB, exFAT — plausible for a
    household media share) falls back to reserving the name with
    `O_CREAT|O_EXCL`, which fails the same way.
    """
    try:
        os.link(tmp, target)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as out, open(tmp, "rb") as src:
        shutil.copyfileobj(src, out)
    return True


def adopt_into(folder: Path, staged: Path, name: str) -> Path:
    """Move `staged` into `folder` under `name`, or the first free
    variation of it. Returns the path it ended up at.

    The only way a file ever enters a library folder. It cannot overwrite:
    see `_claim`. `staged` is expected to be somewhere disposable (a
    staging directory under `/cache`); it is consumed.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    # Land the bytes in the destination folder first, under a dot-prefixed
    # name no scan will pick up, so the final claim is a same-directory
    # link rather than a cross-device copy that could half-finish under a
    # name the operator can see.
    tmp = folder / f".bb-incoming-{uuid.uuid4().hex}"
    shutil.move(str(staged), str(tmp))
    try:
        for candidate_name in _collision_names(name):
            target = folder / candidate_name
            if _claim(tmp, target):
                if target != tmp:
                    _logger.debug("adopted %s into %s", staged.name, target)
                return target
    finally:
        tmp.unlink(missing_ok=True)
    raise LibraryFolderError(f"could not find a free name for {name!r} in {folder}")


def find_by_source_key(folder: Path, source_key: str) -> Path | None:
    """An existing file in `folder` whose bracketed key is `source_key`,
    if there is one.

    This is what makes adding a source idempotent and what makes an
    operator's rename harmless: identity is the source key inside the
    brackets, never the title in front of them and never the path. A
    zero-byte file does not count — a half-written download must never be
    mistaken for a finished one (the same rule `HttpFetcher` and
    `YtDlpFetcher` already apply in the cache).
    """
    folder = Path(folder)
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    for path in entries:
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            _title, key, _ext = parse_cache_name(path.name)
        except ValueError:
            continue
        if key == source_key:
            return path
    return None


# ---------------------------------------------------------------- migration


def ensure_library_folders(store, media_root: Path) -> list:
    """Give every library that has no folder one, derived from its name
    under the media root, and create it. Returns the libraries changed.

    **Moves no audio.** A library that was link-backed has items already
    fetched into `/cache`; those are left exactly where they are and
    re-fetched into the library folder on next use, with the cache copy
    ageing out by ordinary eviction. A migration that relocates audio can
    lose it, and there is nothing here worth that risk.
    """
    from dataclasses import replace

    root = Path(media_root)
    taken = {lib.folder_path for lib in store.libraries.list() if lib.folder_path}
    changed = []
    for lib in store.libraries.list():
        if lib.folder_path:
            continue
        folder = ensure_library_folder(root, lib.name, taken=taken)
        taken.add(str(folder))
        store.libraries.update(replace(lib, folder_path=str(folder)))
        changed.append(lib)
        _logger.info(
            "library %r had no folder; gave it %s (nothing was moved or deleted)",
            lib.name, folder,
        )
    return changed


__all__ = [
    "LibraryFolderError",
    "adopt_into",
    "ensure_library_folder",
    "ensure_library_folders",
    "find_by_source_key",
    "folder_name_for",
    "library_folder_for",
    "list_subfolders",
    "require_media_root",
    "resolve_within",
]
