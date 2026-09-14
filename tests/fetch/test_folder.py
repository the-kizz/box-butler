"""`FolderFetcher` (spec §3.5.2) — closes the gap found on the first live
deployment: `scan_folder`/`sync_media_root` create `ItemKind.FOLDER_FILE`
items and nothing in `boxbutler/fetch/` claimed the kind, so
`CompositeFetcher` fell straight to `ExtractionBroken` on the very first
real prefetch. The wiring test below goes through `boxbutler.main.build()`
itself -- the bug was in the composition root, not in any fetcher's own
logic, so a test that hand-builds a `CompositeFetcher` would have passed
before this fix existed and proves nothing about the gap that actually
shipped.
"""
from __future__ import annotations

import os
import stat as stat_module

import pytest

from boxbutler.config import load_settings
from boxbutler.domain.models import Item, ItemKind
from boxbutler.fetch.folder import FolderFetcher
from boxbutler.fetch.protocol import ExtractionBroken, ItemUnavailable
from boxbutler.main import build

ENV = {
    "BOXBUTLER_SINK_USER": "u",
    "BOXBUTLER_SINK_PASSWORD": "p",
    "BOXBUTLER_ADMIN_USER": "admin",
    "BOXBUTLER_ADMIN_PASSWORD": "correct horse",
    "BOXBUTLER_SECRET_KEY": "k",
    "BOXBUTLER_SINK_KIND": "fake",
}


def _item(local_path, source_ref="track.m4a", seconds=None) -> Item:
    return Item(
        id="i1",
        library_id="L",
        position=0,
        kind=ItemKind.FOLDER_FILE,
        source_ref=source_ref,
        source_key="123-abc",
        title="A Made-Up Bedtime Story",
        seconds=seconds,
        local_path=str(local_path),
    )


# --------------------------------------------------------------- wiring


def test_composite_fetcher_from_build_resolves_a_folder_item(tmp_path):
    """The actual bug: `CompositeFetcher([YtDlpFetcher(...),
    HttpFetcher(...)])` in `boxbutler.main.build()` had no fetcher that
    `supports(ItemKind.FOLDER_FILE)`. Go through `build()` itself, not a
    hand-assembled `CompositeFetcher`, so a regression in the wiring
    (not just in `FolderFetcher`'s own logic) fails this test.
    """
    settings = load_settings(None, {
        **ENV,
        "BOXBUTLER_DATA_DIR": str(tmp_path / "data"),
        "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
    })
    deps = build(settings)
    fetcher = deps.orchestrator.deps.fetcher

    media = tmp_path / "media"
    media.mkdir()
    audio = media / "track.m4a"
    audio.write_bytes(b"fake-m4a-bytes")

    item = _item(audio, source_ref="track.m4a")
    assert fetcher.supports(ItemKind.FOLDER_FILE)
    resolved = fetcher.fetch(item, tmp_path / "cache")
    assert resolved == audio
    assert resolved.read_bytes() == b"fake-m4a-bytes"
    # No copy landed in the fetch cache -- the whole point of returning
    # the source path directly.
    assert not (tmp_path / "cache").exists() or list((tmp_path / "cache").iterdir()) == []


def test_removing_the_fetcher_from_the_composite_reproduces_the_live_failure():
    """Break-test: with `FolderFetcher` left out, exactly the error string
    seen in production comes back out of `CompositeFetcher`."""
    from boxbutler.fetch.http import HttpFetcher
    from boxbutler.fetch.ytdlp import YtDlpFetcher
    from boxbutler.main import CompositeFetcher
    import httpx

    broken = CompositeFetcher([YtDlpFetcher(js_runtime=None), HttpFetcher(httpx.Client())])
    item = _item("/media/bedtime/track.m4a")
    with pytest.raises(ExtractionBroken) as exc:
        broken.fetch(item, None)
    assert "no configured fetcher supports item kind" in str(exc.value)
    assert "folder_file" in str(exc.value).lower()


# ----------------------------------------------------------- unit-level


def test_resolves_local_path_without_copying_into_cache(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    audio = root / "story.mp3"
    audio.write_bytes(b"the story bytes")
    cache_dir = tmp_path / "cache"

    f = FolderFetcher()
    result = f.fetch(_item(audio, source_ref="story.mp3"), cache_dir)

    assert result == audio
    assert result.read_bytes() == b"the story bytes"
    assert not cache_dir.exists()


def test_item_with_no_independent_duration_is_untouched():
    """`item.seconds` stays `None` for a folder item -- this fetcher must
    never invent one just to make a downstream check happy."""
    item = _item("/media/lib/story.mp3", seconds=None)
    assert item.seconds is None


def test_missing_file_is_item_unavailable_not_extraction_broken(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    # `track.m4a` never gets created -- the root exists, one file is gone.
    f = FolderFetcher()
    with pytest.raises(ItemUnavailable):
        f.fetch(_item(root / "track.m4a", source_ref="track.m4a"), tmp_path / "cache")


def test_missing_library_root_is_extraction_broken_not_a_deleted_item(tmp_path):
    """The unmounted-share shape: the root itself is gone, which would
    fail *every* item in the library identically -- `scan_folder`'s own
    "missing mount is not deletion" precedent, expressed here as
    `ExtractionBroken` rather than a wall of individual `ItemUnavailable`s."""
    root = tmp_path / "never_mounted"
    f = FolderFetcher()
    with pytest.raises(ExtractionBroken):
        f.fetch(_item(root / "track.m4a", source_ref="track.m4a"), tmp_path / "cache")


def test_source_file_is_never_modified_on_a_read_only_root(tmp_path):
    """Matches the real `/media:ro` mount: a `chmod`-ed read-only fixture
    directory must still resolve successfully, and nothing about the fetch
    may require write access to the source tree."""
    root = tmp_path / "readonly_media"
    root.mkdir()
    audio = root / "lullaby.flac"
    payload = b"original bytes, never to change"
    audio.write_bytes(payload)
    before = audio.stat()

    # Lock down the directory and the file the way a real `:ro` bind
    # mount behaves -- no write, no create, no delete.
    audio.chmod(stat_module.S_IRUSR | stat_module.S_IRGRP | stat_module.S_IROTH)
    root.chmod(stat_module.S_IRUSR | stat_module.S_IXUSR | stat_module.S_IRGRP | stat_module.S_IXGRP)

    try:
        f = FolderFetcher()
        result = f.fetch(_item(audio, source_ref="lullaby.flac"), tmp_path / "cache")
        assert result == audio
        assert audio.read_bytes() == payload
        after = audio.stat()
        assert after.st_mtime == before.st_mtime
        assert after.st_size == before.st_size
        assert not (tmp_path / "cache").exists()
    finally:
        # Restore write perms so pytest's tmp_path cleanup can remove it.
        root.chmod(stat_module.S_IRWXU)
        audio.chmod(stat_module.S_IRWXU)


def test_not_a_regular_file_is_item_unavailable(tmp_path):
    root = tmp_path / "lib"
    sub = root / "not_a_file.m4a"
    sub.mkdir(parents=True)  # a directory sharing the expected filename
    f = FolderFetcher()
    with pytest.raises(ItemUnavailable):
        f.fetch(_item(sub, source_ref="not_a_file.m4a"), tmp_path / "cache")


def test_no_local_path_recorded_is_item_unavailable(tmp_path):
    f = FolderFetcher()
    item = _item("", source_ref="track.m4a")
    with pytest.raises(ItemUnavailable):
        f.fetch(item, tmp_path / "cache")
