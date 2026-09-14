"""File-upload ingest (spec §3.5).

Not a `SourceProtocol.resolve()` in the usual sense — there is no `ref` to
match against, the operator hands over bytes directly — so this exposes
`save()` instead, called by `Ingestor.add_upload`.

`source_key` = `fingerprint(path)`, shared with Task 17's folder scanner
(same function, same key space): `f"{size}-{sha1(sampled bytes)[:16]}"`.
It depends on content, not the path, so renaming or moving a file within
the library folder (Task 17) does not change its identity — verified by
`test_fingerprint_stable_across_rename`.

Task 17 review (Important): a head+tail-only hash collides for files that
share an encoder header, similar length, and trailing silence — exactly
the shape of adjacent ripped-audiobook chapters, the project's primary
folder-library use case. Losing chapter 7 to a silent key collision is
the class of failure this project exists to eliminate, so the sample was
widened rather than left as-is:

- Files at or under `_FULL_HASH_THRESHOLD` (12 MiB — three chunks' worth)
  are hashed in full. Below that size, sampling saves little I/O anyway,
  so there is no reason not to be exact.
- Larger files are hashed over three 4 MiB windows — head, middle
  (centred on the file's midpoint), and tail — rather than two. This
  keeps fingerprinting a large audiobook cheap (a bounded ~12 MiB read
  regardless of total size) while making the two-chapters-with-identical-
  head-and-tail scenario `test_fingerprint_distinguishes_same_head_tail_different_middle`
  guards against require the middle to *also* collide, which is a much
  smaller coincidence than head+tail alone.

This still cannot make collisions impossible, only rarer — see
`boxbutler/sources/folder.py`'s collision handling (module docstring,
"Identity") for what happens on the residual chance of one anyway: a
collision is detected and made visible, never silently resolved by
overwriting a still-present file's row.

Title is taken from the filename (sanitised the same way as everywhere
else, via `sanitise_title`) rather than parsed ID3/Vorbis tags: reading
embedded tags reliably needs a tagging library (e.g. mutagen) that isn't a
dependency of this project yet, and the brief's own test only exercises
the filename path. If tag-based titles are wanted later, that's an
addition to this one function, not a second title scheme.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from boxbutler.domain.cache_name import cache_name, sanitise_title
from boxbutler.domain.models import ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError

_CHUNK = 4 * 1024 * 1024  # 4 MiB, matches the shared fingerprint scheme
_FULL_HASH_THRESHOLD = 3 * _CHUNK  # 12 MiB: below this, just hash everything


def fingerprint(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if size <= _FULL_HASH_THRESHOLD:
            # Small enough that sampling wouldn't save much I/O anyway —
            # hash it all rather than leave any blind spot.
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        else:
            h.update(f.read(_CHUNK))
            mid_start = max(_CHUNK, min(size - 2 * _CHUNK, size // 2 - _CHUNK // 2))
            f.seek(mid_start)
            h.update(f.read(_CHUNK))
            f.seek(size - _CHUNK)
            h.update(f.read(_CHUNK))
    digest = h.hexdigest()[:16]
    return f"{size}-{digest}"


class UploadSource:
    """Not registered in `Ingestor.add_ref`'s source list (it has nothing
    to `matches()` against) — `Ingestor.add_upload` calls `save()` on it
    directly. `matches`/`resolve` are still implemented, refusing, so this
    satisfies `SourceProtocol` and can sit in the same list harmlessly."""

    def __init__(self, upload_dir: Path):
        self._upload_dir = Path(upload_dir)

    def matches(self, ref: str) -> bool:
        return False

    def resolve(self, ref: str) -> list[ResolvedItem]:
        raise SourceError("UploadSource has no ref to resolve; use save()")

    def save(self, filename: str, stream: BinaryIO) -> ResolvedItem:
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix.lstrip(".") or "bin"
        title = sanitise_title(Path(filename).stem)

        # Write under a temp name first (we don't know the fingerprint —
        # part of the final name — until the bytes are on disk), then move
        # into place under the real cache-name shape via `cache_name()`,
        # never a hand-rolled f-string (spec: one sanitiser/namer, no
        # second implementation).
        tmp_path = self._upload_dir / f".upload-{id(stream):x}.tmp"
        with open(tmp_path, "wb") as f:
            while chunk := stream.read(1024 * 1024):
                f.write(chunk)

        key = fingerprint(tmp_path)
        final_path = self._upload_dir / cache_name(title, key, ext)
        tmp_path.replace(final_path)
        return ResolvedItem(ItemKind.UPLOAD, str(final_path), key, title, local_path=str(final_path))
