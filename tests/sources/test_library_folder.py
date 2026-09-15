"""`boxbutler.sources.library_folder` — a library is exactly one folder.

Three properties are load-bearing here and each is tested on its own, not
as a side effect of some bigger flow:

1. **The media root is required.** There is no fallback to a directory
   inside `/data`; a missing mount is named, not papered over.
2. **The picker cannot leave the media root.** `..`, an absolute path and
   a symlink pointing outside are each refused separately — a check that
   only rejects the literal string `".."` would pass a test that only
   tries `".."`.
3. **Nothing already on disk is ever overwritten.** `adopt_into` picks a
   different name rather than clobbering, whatever name it was asked for.

No test here touches the network, the real cloud or ffmpeg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boxbutler.domain.cache_name import parse_cache_name
from boxbutler.domain.models import LibraryMode
from boxbutler.sources.library_folder import (
    LibraryFolderError,
    adopt_into,
    ensure_library_folder,
    ensure_library_folders,
    find_by_source_key,
    folder_name_for,
    list_subfolders,
    require_media_root,
    resolve_within,
)


@pytest.fixture
def media_root(tmp_path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


# ------------------------------------------------- the media root is required


def test_require_media_root_returns_the_directory_when_it_is_there(media_root):
    assert require_media_root(media_root) == media_root.resolve()


def test_require_media_root_names_the_missing_mount(tmp_path):
    missing = tmp_path / "not-mounted"
    with pytest.raises(LibraryFolderError) as exc:
        require_media_root(missing)
    # The operator has to be able to see *which* mount to fix.
    assert str(missing) in str(exc.value)
    # And must never be told a fallback happened.
    assert not missing.exists()


def test_require_media_root_refuses_a_file_and_refuses_none(tmp_path):
    a_file = tmp_path / "media"
    a_file.write_text("not a directory")
    with pytest.raises(LibraryFolderError):
        require_media_root(a_file)
    with pytest.raises(LibraryFolderError):
        require_media_root(None)


def test_require_media_root_refuses_an_unwritable_mount(media_root):
    """`/media` is read-write now; a still-read-only mount has to be named
    at startup, not discovered as a permission error on the first
    download."""
    media_root.chmod(0o555)
    try:
        with pytest.raises(LibraryFolderError) as exc:
            require_media_root(media_root)
        assert "writable" in str(exc.value).lower()
    finally:
        media_root.chmod(0o755)


# -------------------------------------------------------- deriving the folder


def test_folder_name_is_sanitised_and_never_escapes_a_single_segment():
    assert folder_name_for("Bedtime Stories") == "Bedtime Stories"
    # A name with a separator in it must not become two path segments.
    assert "/" not in folder_name_for("Wombat/Stories")
    assert folder_name_for("..") not in ("..", ".")
    assert folder_name_for("") != ""


def test_ensure_library_folder_creates_it_under_the_media_root(media_root):
    folder = ensure_library_folder(media_root, "Bedtime")
    assert folder == media_root / "Bedtime"
    assert folder.is_dir()


def test_ensure_library_folder_reuses_an_existing_folder(media_root):
    (media_root / "Bedtime").mkdir()
    marker = media_root / "Bedtime" / "already here.mp3"
    marker.write_bytes(b"operator audio")
    folder = ensure_library_folder(media_root, "Bedtime")
    assert folder == media_root / "Bedtime"
    assert marker.read_bytes() == b"operator audio"


# ------------------------------------------- the picker cannot leave /media


def test_resolve_within_accepts_the_root_itself_and_a_subfolder(media_root):
    (media_root / "Bedtime").mkdir()
    assert resolve_within(media_root, "") == media_root.resolve()
    assert resolve_within(media_root, ".") == media_root.resolve()
    assert resolve_within(media_root, "Bedtime") == (media_root / "Bedtime").resolve()


def test_resolve_within_refuses_dot_dot_traversal(media_root):
    for attempt in ("..", "../..", "Bedtime/../..", "a/../../b"):
        with pytest.raises(LibraryFolderError):
            resolve_within(media_root, attempt)


def test_resolve_within_refuses_an_absolute_path(media_root):
    # pathlib's `/` operator *discards* the left operand for an absolute
    # right operand, so `media_root / "/etc"` is `/etc` — the exact trap
    # this check exists for.
    for attempt in ("/etc", "/etc/passwd", str(media_root.parent)):
        with pytest.raises(LibraryFolderError):
            resolve_within(media_root, attempt)


def test_resolve_within_refuses_a_symlink_pointing_outside(media_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (media_root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LibraryFolderError):
        resolve_within(media_root, "escape")
    with pytest.raises(LibraryFolderError):
        resolve_within(media_root, "escape/deeper")


def test_list_subfolders_lists_only_directories_inside_the_root(media_root):
    (media_root / "Bedtime").mkdir()
    (media_root / "Car Trips").mkdir()
    (media_root / "loose.mp3").write_bytes(b"x")
    names = [p.name for p in list_subfolders(media_root, "")]
    assert names == ["Bedtime", "Car Trips"]


def test_list_subfolders_refuses_to_browse_above_the_root(media_root):
    with pytest.raises(LibraryFolderError):
        list_subfolders(media_root, "..")


# ------------------------------------------ nothing on disk is overwritten


def test_adopt_into_places_the_file_under_the_wanted_name(media_root, tmp_path):
    staged = tmp_path / "staged.m4a"
    staged.write_bytes(b"downloaded")
    dest = adopt_into(media_root, staged, "Story [vid1].m4a")
    assert dest == media_root / "Story [vid1].m4a"
    assert dest.read_bytes() == b"downloaded"
    assert not staged.exists()


def test_adopt_into_never_overwrites_an_existing_file(media_root, tmp_path):
    existing = media_root / "Story [vid1].m4a"
    existing.write_bytes(b"the operator's own copy")
    staged = tmp_path / "staged.m4a"
    staged.write_bytes(b"downloaded")

    dest = adopt_into(media_root, staged, "Story [vid1].m4a")

    assert dest != existing
    assert existing.read_bytes() == b"the operator's own copy"
    assert dest.read_bytes() == b"downloaded"
    # The chosen name still parses as a cache name, so a later scan still
    # recognises the source key rather than treating it as a stray file.
    _title, key, ext = parse_cache_name(dest.name)
    assert (key, ext) == ("vid1", "m4a")


def test_adopt_into_keeps_choosing_new_names_under_repeated_collision(media_root, tmp_path):
    written = []
    for i in range(3):
        staged = tmp_path / f"staged{i}.m4a"
        staged.write_bytes(f"take {i}".encode())
        written.append(adopt_into(media_root, staged, "Story [vid1].m4a"))

    assert len({p.name for p in written}) == 3
    for i, p in enumerate(written):
        assert p.read_bytes() == f"take {i}".encode()


def test_adopt_into_leaves_no_temporary_files_behind(media_root, tmp_path):
    staged = tmp_path / "staged.m4a"
    staged.write_bytes(b"downloaded")
    adopt_into(media_root, staged, "Story [vid1].m4a")
    assert [p.name for p in media_root.iterdir()] == ["Story [vid1].m4a"]


def test_find_by_source_key_recognises_a_file_the_operator_renamed(media_root):
    # Identity is the bracketed source key, never the title — renaming the
    # human-readable half must not make Box Butler download it again.
    (media_root / "A Better Title [vid1].m4a").write_bytes(b"x")
    found = find_by_source_key(media_root, "vid1")
    assert found is not None and found.name == "A Better Title [vid1].m4a"
    assert find_by_source_key(media_root, "vid2") is None


def test_find_by_source_key_ignores_a_zero_byte_file(media_root):
    (media_root / "Half Downloaded [vid1].m4a").write_bytes(b"")
    assert find_by_source_key(media_root, "vid1") is None


# ---------------------------------------------------------------- migration


def test_migration_gives_a_folderless_library_a_folder_and_creates_it(store, media_root):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE)
    assert not lib.folder_path

    changed = ensure_library_folders(store, media_root)

    assert [c.name for c in changed] == ["Bedtime"]
    migrated = store.libraries.get(lib.id)
    assert migrated.folder_path == str(media_root / "Bedtime")
    assert (media_root / "Bedtime").is_dir()


def test_migration_moves_and_deletes_nothing(store, media_root, tmp_path):
    """A migration that relocates audio can lose it. Cached copies age out
    by ordinary eviction instead."""
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / "Story [vid1].m4a"
    cached.write_bytes(b"already fetched into the cache")
    before = cached.read_bytes()

    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE)
    store.items.add(lib.id, "youtube", "u/v1", "vid1", "Story", local_path=str(cached))

    ensure_library_folders(store, media_root)

    assert cached.exists() and cached.read_bytes() == before
    assert list((media_root / "Bedtime").iterdir()) == []
    # The item row is untouched too — it still points at the cache copy.
    item = store.items.list(lib.id)[0]
    assert item.local_path == str(cached)


def test_migration_leaves_an_existing_folder_backed_library_alone(store, media_root):
    (media_root / "Book").mkdir()
    lib = store.libraries.create("Book", LibraryMode.SERIAL, folder_path=str(media_root / "Book"))
    assert ensure_library_folders(store, media_root) == []
    assert store.libraries.get(lib.id).folder_path == str(media_root / "Book")


def test_migration_does_not_collide_two_libraries_onto_one_folder(store, media_root):
    # Library *names* are unique, but two different names can sanitise to
    # the same folder segment — and two libraries sharing one folder would
    # make each one's scan add the other's files.
    a = store.libraries.create("Bedtime?", LibraryMode.SINGLE)
    b = store.libraries.create("Bedtime|", LibraryMode.SINGLE)
    ensure_library_folders(store, media_root)
    folders = {store.libraries.get(a.id).folder_path, store.libraries.get(b.id).folder_path}
    assert len(folders) == 2
    for f in folders:
        assert Path(f).is_dir()
