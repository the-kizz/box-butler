"""`sync_media_root` — Task 38: the read-only `/media` mount becomes usable
end to end by mapping each of its immediate subfolders to one folder-backed
library, scanned via `scan_folder` (Task 17).

Governing principle (task brief): "when in doubt, the tonie keeps last
night's story." A `media_root` that is missing or (more insidiously) present
but empty is indistinguishable from an unmounted NFS share, and must never
be read as "the operator deleted everything" — no library row is ever
deleted by this module, under any circumstance.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from boxbutler.audio.protocol import ProbeResult, RenderSpec, choose_mode
from boxbutler.domain.models import ItemState, RenditionMode
from boxbutler.sources.folder import AUDIO_EXTS, sync_media_root


def mk(root: Path, name: str, payload: bytes = b"x" * 100) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


def test_subfolder_becomes_library(store, tmp_path):
    media_root = tmp_path / "media"
    mk(media_root, "Bedtime Stories/track2.mp3")
    mk(media_root, "Bedtime Stories/track10.mp3")
    mk(media_root, "Bedtime Stories/track1.mp3")
    mk(media_root, "Nature Sounds/rain.flac")

    result = sync_media_root(store, media_root)

    assert result.libraries_created == 2
    libs = {lib.name: lib for lib in store.libraries.list()}
    assert set(libs) == {"Bedtime Stories", "Nature Sounds"}
    assert libs["Bedtime Stories"].folder_path == str(media_root / "Bedtime Stories")

    titles = [i.title for i in store.items.list(libs["Bedtime Stories"].id)]
    assert titles == ["track1", "track2", "track10"]  # natural sort, not lexical


def test_media_root_is_never_written(store, tmp_path):
    # Separate from the store's own tmp_path directory (per Task 17's own
    # note in test_folder.py) so chmod-ing the media root doesn't also
    # lock the sqlite db's directory.
    media_root = tmp_path / "media"
    mk(media_root, "Book/1.mp3")
    mk(media_root, "Book/2.mp3")

    snapshot = {p: p.stat().st_mtime_ns for p in media_root.rglob("*")}
    media_root.chmod(0o555)
    for sub in media_root.iterdir():
        sub.chmod(0o555)
    try:
        result = sync_media_root(store, media_root)
        assert result.libraries_created == 1
    finally:
        media_root.chmod(0o755)
        for sub in media_root.iterdir():
            sub.chmod(0o755)

    assert {p: p.stat().st_mtime_ns for p in media_root.rglob("*")} == snapshot


def test_missing_media_root_does_not_destroy_libraries(store, tmp_path):
    media_root = tmp_path / "media"
    mk(media_root, "Book/1.mp3")
    mk(media_root, "Book/2.mp3")

    sync_media_root(store, media_root)
    lib = store.libraries.list()[0]
    assert len(store.items.list(lib.id)) == 2

    # Shape 1: the mount point is missing entirely (directory gone).
    shutil.rmtree(media_root)
    result = sync_media_root(store, media_root)
    assert result.libraries_created == 0
    assert store.libraries.get(lib.id) is not None
    assert all(i.state == ItemState.UNAVAILABLE for i in store.items.list(lib.id))

    # Shape 2: the mount point exists but is empty (failed automount that
    # still leaves an empty directory behind) — same guarantee.
    media_root.mkdir()
    result2 = sync_media_root(store, media_root)
    assert result2.libraries_created == 0
    assert store.libraries.get(lib.id) is not None
    assert all(i.state == ItemState.UNAVAILABLE for i in store.items.list(lib.id))
    # No stray library was invented for the empty root itself.
    assert len(store.libraries.list()) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bit; chmod 000 has no effect")
def test_unreadable_media_root_marks_unavailable_and_never_deletes_library(store, tmp_path):
    """Task 38 review, Major-1: `media_root` existing but unreadable
    (a real NFS-permission shape, not a fabricated edge case) used to
    raise `PermissionError` straight out of `sync_media_root`, before
    any library got the "mark unavailable" treatment the missing/empty
    shapes already receive -- it was harmless only by accident, because
    the sole caller happened to swallow the exception. An unreadable
    mount is exactly as unknown as a missing one, so it must be treated
    identically: every library row survives, nothing is deleted or
    reordered.
    """
    media_root = tmp_path / "media"
    mk(media_root, "Book/1.mp3")
    mk(media_root, "Book/2.mp3")

    sync_media_root(store, media_root)
    lib = store.libraries.list()[0]
    assert len(store.items.list(lib.id)) == 2

    media_root.chmod(0o000)
    try:
        result = sync_media_root(store, media_root)
    finally:
        media_root.chmod(0o755)

    assert result.libraries_created == 0
    assert store.libraries.get(lib.id) is not None
    assert all(i.state == ItemState.UNAVAILABLE for i in store.items.list(lib.id))
    assert len(store.libraries.list()) == 1  # no stray library invented


def test_rescan_is_idempotent(store, tmp_path):
    media_root = tmp_path / "media"
    mk(media_root, "Book/1.mp3")
    mk(media_root, "Book/2.mp3")

    sync_media_root(store, media_root)
    before_changes = store.conn.total_changes
    before_libs = store.libraries.list()
    before_items = {lib.id: store.items.list(lib.id) for lib in before_libs}

    result = sync_media_root(store, media_root)

    assert result.libraries_created == 0
    assert store.conn.total_changes == before_changes  # nothing written
    after_libs = store.libraries.list()
    assert after_libs == before_libs
    for lib in after_libs:
        assert store.items.list(lib.id) == before_items[lib.id]


# One representative real-world (container, codec) pair per accepted
# extension (spec §3.4's 12 formats) -- enough for `choose_mode` to see
# an accepted container *and* an accepted codec, which is what actually
# routes it away from `RenditionMode.TRANSCODE`. Real ffprobe output for
# `.m4a`/`.m4b` reports a comma-joined container list rather than the
# bare extension; that shape is exercised separately in
# `tests/audio/test_protocol.py` (`_container_accepted`) -- this table
# only needs *an* accepted token per extension, so the extension itself
# is used everywhere it's simpler and no less correct.
_ACCEPTED_FORMAT_CODECS: dict[str, str] = {
    "aac": "aac",
    "aif": "pcm_s16be",
    "aiff": "pcm_s16be",
    "flac": "flac",
    "mp3": "mp3",
    "m4a": "aac",
    "m4b": "aac",
    "wav": "pcm_s16le",
    "oga": "vorbis",
    "ogg": "vorbis",
    "opus": "opus",
    "wma": "wmav2",
}


def test_accepted_format_is_not_reencoded(store, tmp_path, monkeypatch):
    """Task 38 review, Major-2: the previous version of this test built
    a `fake_encode`/`encoded` interceptor and a `monkeypatch` fixture
    that were never wired to anything -- `assert not encoded` could
    never fail no matter what the code did. This version actually
    exercises the production decision (`choose_mode`) and the
    production renderer (`FfmpegRenderer.render`) for every one of the
    12 accepted extensions, with a `runner` that raises if ffmpeg/
    ffprobe is ever actually invoked -- which only happens for a mode
    other than `COPY`. No real ffmpeg binary is needed: `probe()` is
    monkeypatched to return a synthetic-but-realistic `ProbeResult`
    instead of shelling out, and `render()`'s own COPY-mode branch is a
    plain `shutil.copyfile`.

    Deliberately verified this discriminates (see report): temporarily
    changed `choose_mode`'s branch order so an accepted format still
    hit `TRANSCODE`, re-ran, watched this test fail with the "must never
    invoke ffmpeg" AssertionError, then reverted.
    """
    from boxbutler.audio.ffmpeg import FfmpegRenderer

    media_root = tmp_path / "media"
    for ext in _ACCEPTED_FORMAT_CODECS:
        mk(media_root, f"Lib_{ext}/track1.{ext}")
        assert f".{ext}" in AUDIO_EXTS

    result = sync_media_root(store, media_root)
    assert result.libraries_created == len(_ACCEPTED_FORMAT_CODECS)

    def refuse_to_shell_out(*args, **kwargs):
        raise AssertionError("an accepted format must never invoke ffmpeg/ffprobe to encode it")

    renderer = FfmpegRenderer(runner=refuse_to_shell_out)
    spec = RenderSpec(cap_seconds=5395)  # well above every fixture's fake duration

    libs = store.libraries.list()
    assert len(libs) == len(_ACCEPTED_FORMAT_CODECS)
    for lib in libs:
        items = store.items.list(lib.id)
        assert len(items) == 1
        item = items[0]
        assert item.state == ItemState.OK
        ext = Path(item.local_path).suffix.lstrip(".")
        probe = ProbeResult(
            seconds=100.0,
            codec=_ACCEPTED_FORMAT_CODECS[ext],
            sample_rate=44100,
            channels=2,
            bytes=Path(item.local_path).stat().st_size,
            container=ext,
            tags={},
        )
        monkeypatch.setattr(renderer, "probe", lambda p, probe=probe: probe)

        # Sanity check on the pure decision itself, independent of the
        # renderer -- if this ever returns TRANSCODE for an accepted
        # format, `render()` below would try to shell out and fail loudly.
        assert choose_mode(probe, spec) == RenditionMode.COPY

        dst = tmp_path / f"out-{ext}.{ext}"
        info = renderer.render(Path(item.local_path), dst, spec)
        assert info.mode == RenditionMode.COPY
        assert dst.read_bytes() == Path(item.local_path).read_bytes()
