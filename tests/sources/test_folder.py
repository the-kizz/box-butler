"""Folder-backed library scanner (spec §3.5.2, Task 17).

Fixtures generated at test time (no audio committed): a valid audio
container is never required here since `scan_folder`/`title_for` only
need bytes with the right extension and, where tags matter, a fake
`probe` callable — the real ffprobe path is `@pytest.mark.ffmpeg`-only
elsewhere and not exercised in this file.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from boxbutler.audio.protocol import ProbeResult
from boxbutler.domain.models import ItemState, LibraryMode
from boxbutler.sources.folder import natural_key, scan_folder, title_for


def mk(root: Path, name: str, payload: bytes) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


def test_natural_sort_puts_2_before_10():
    assert sorted(["10 - c.mp3", "2 - b.mp3", "1 - a.mp3"], key=natural_key) == [
        "1 - a.mp3",
        "2 - b.mp3",
        "10 - c.mp3",
    ]


def test_natural_sort_survives_mixed_digit_and_letter_leading_names():
    """Task 38 review, Critical-2: an ordinary media folder mixes
    digit-leading names with letter-leading ones (`"1 Bedtime"` next to
    `"Nature Sounds"`), and the old key produced a plain `int` for one
    name's first element and a plain `str` for the other's -- comparing
    those directly raises `TypeError` and aborts the whole sync. The
    fixed key groups digit runs and text runs so same-position elements
    never cross types; per `natural_key`'s docstring, a digit run always
    sorts before a text run at the same position (arbitrary but
    documented and deterministic), and `"01"`/`"1"` collapse to the same
    numeric key and keep their original relative order. A key that is a
    prefix of another (both start `(0, 1)`, but one has nothing after it)
    sorts first, same as Python's own tuple/string comparison -- which is
    why the bare "01"/"1" land ahead of "1 Bedtime" below, not after it.
    """
    names = [
        "Nature Sounds",       # letter-leading
        "1 Bedtime",           # digit-leading
        "track10.mp3",         # digits mid/end, must sort after track2
        "track2.mp3",
        "Ünïcode Lullabies",   # unicode, letter-leading
        "01",                  # leading zero
        "1",                   # same numeric value as "01"
        "42",                  # digits only
        "a1b2",                # digits in the middle of letters
        "a1b10",
    ]
    assert sorted(names, key=natural_key) == [
        "01",
        "1",
        "1 Bedtime",
        "42",
        "a1b2",
        "a1b10",
        "Nature Sounds",
        "track2.mp3",
        "track10.mp3",
        "Ünïcode Lullabies",
    ]


def test_scan_adds_in_natural_order_recursively(store, tmp_path):
    lib = store.libraries.create("Book", LibraryMode.SERIAL, folder_path=str(tmp_path))
    mk(tmp_path, "disc2/10 - ten.mp3", b"j" * 100)
    mk(tmp_path, "disc1/2 - two.mp3", b"b" * 100)
    mk(tmp_path, "disc1/1 - one.mp3", b"a" * 100)
    mk(tmp_path, "cover.jpg", b"img")
    res = scan_folder(store, lib, tmp_path)
    assert res.added == 3
    assert [i.title for i in store.items.list(lib.id)] == ["1 - one", "2 - two", "10 - ten"]


def test_renamed_file_is_matched_by_fingerprint(store, tmp_path):
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    p = mk(tmp_path, "old.mp3", b"z" * 500)
    scan_folder(store, lib, tmp_path)
    before = store.items.list(lib.id)[0]
    p.rename(tmp_path / "new.mp3")
    res = scan_folder(store, lib, tmp_path)
    after = store.items.list(lib.id)
    assert len(after) == 1
    assert after[0].id == before.id
    assert after[0].local_path.endswith("new.mp3")
    assert res.renamed == 1


def test_deleted_file_becomes_unavailable_without_position_shift(store, tmp_path):
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    for n in ["1.mp3", "2.mp3", "3.mp3"]:
        mk(tmp_path, n, n.encode() * 50)
    scan_folder(store, lib, tmp_path)
    (tmp_path / "2.mp3").unlink()
    scan_folder(store, lib, tmp_path)
    items = store.items.list(lib.id)
    assert [i.position for i in items] == [0, 1, 2]
    assert items[1].state == ItemState.UNAVAILABLE


def test_unmounted_volume_marks_unavailable_and_never_deletes_library(store, tmp_path):
    lib = store.libraries.create("Book", folder_path=str(tmp_path / "gone"))
    mk(tmp_path / "gone", "1.mp3", b"a" * 50)
    scan_folder(store, lib, tmp_path / "gone")
    shutil.rmtree(tmp_path / "gone")
    res = scan_folder(store, lib, tmp_path / "gone")
    assert store.libraries.get(lib.id) is not None
    assert res.unavailable == 1
    assert all(i.state == ItemState.UNAVAILABLE for i in store.items.list(lib.id))


def test_empty_volume_same_behaviour(store, tmp_path):
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    mk(tmp_path, "1.mp3", b"a" * 50)
    scan_folder(store, lib, tmp_path)
    (tmp_path / "1.mp3").unlink()
    scan_folder(store, lib, tmp_path)
    assert store.libraries.get(lib.id)
    assert store.items.list(lib.id)[0].state == ItemState.UNAVAILABLE


def test_embedded_tag_title_wins_over_filename(tmp_path):
    p = mk(tmp_path, "01 - track.mp3", b"x")
    probe = lambda path: ProbeResult(10, "mp3", 44100, 2, 1, "mp3", {"title": "Chapter One \U0001F3A7"})
    assert title_for(p, probe) == "Chapter One"
    assert title_for(p, None) == "01 - track"


def test_probe_failure_falls_back_to_filename_without_aborting_scan(store, tmp_path):
    mk(tmp_path, "a.mp3", b"a" * 50)
    lib = store.libraries.create("Book", folder_path=str(tmp_path))

    def blows_up(_path):
        raise ValueError("unreadable tags")

    res = scan_folder(store, lib, tmp_path, probe=blows_up)
    assert res.added == 1
    assert store.items.list(lib.id)[0].title == "a"


def test_nothing_is_written_to_the_media_root(store, tmp_path):
    # NOTE: the brief's version of this test passes `tmp_path` itself as
    # both the `store` fixture's db location *and* the scanned root. The
    # `store` fixture (tests/conftest.py) also depends on `tmp_path`, and
    # pytest resolves both to the *same* directory within one test, so the
    # sqlite/WAL files created by every `store.items.add()` call during the
    # scan sit inside the "media root" being checked — a scan that (quite
    # correctly) writes items to the database then fails this test as a
    # false positive, for a reason that has nothing to do with the media
    # files themselves. Using a dedicated subdirectory for the media root
    # keeps the two concerns apart, which is what "nothing is written to
    # the media root" is actually supposed to prove.
    media_root = tmp_path / "media"
    lib = store.libraries.create("Book", folder_path=str(media_root))
    mk(media_root, "1.mp3", b"a" * 50)
    snapshot = {p: p.stat().st_mtime_ns for p in media_root.rglob("*")}
    scan_folder(store, lib, media_root)
    assert {p: p.stat().st_mtime_ns for p in media_root.rglob("*")} == snapshot


def test_scan_against_read_only_media_root_succeeds(store, tmp_path):
    # Never write to the media root, even under permission enforcement:
    # chmod the fixture directory read-only and prove a scan still works.
    # Kept separate from the store's own tmp_path (see the note above) so
    # chmod-ing the media root doesn't also lock the sqlite db's directory.
    media_root = tmp_path / "media"
    lib = store.libraries.create("Book", folder_path=str(media_root))
    mk(media_root, "1.mp3", b"a" * 50)
    media_root.chmod(0o555)
    try:
        res = scan_folder(store, lib, media_root)
        assert res.added == 1
    finally:
        media_root.chmod(0o755)


def test_collision_keeps_both_files_when_old_path_still_exists(store, tmp_path):
    # Task 17 review (Important): a same-key match whose *old* path is
    # still present on disk is a collision, not a rename — two distinct
    # files claiming one identity. The old file must never be silently
    # overwritten/dropped from the library; both must remain as items.
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    mk(tmp_path, "a.mp3", b"same content" * 20)
    mk(tmp_path, "b.mp3", b"same content" * 20)  # identical bytes -> identical fingerprint
    res = scan_folder(store, lib, tmp_path)
    items = store.items.list(lib.id)
    assert len(items) == 2
    assert res.collisions == 1
    assert res.added == 1  # only the first file is a "plain add"
    names = {Path(i.local_path).name for i in items}
    assert names == {"a.mp3", "b.mp3"}
    assert all(i.state == ItemState.OK for i in items)

    # A second scan is stable: still two items, no further collisions
    # counted as new, nothing marked unavailable.
    res2 = scan_folder(store, lib, tmp_path)
    assert len(store.items.list(lib.id)) == 2
    assert res2.unavailable == 0


def test_restored_after_reappearing(store, tmp_path):
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    p = mk(tmp_path, "1.mp3", b"a" * 50)
    scan_folder(store, lib, tmp_path)
    p.unlink()
    scan_folder(store, lib, tmp_path)
    mk(tmp_path, "1.mp3", b"a" * 50)
    res = scan_folder(store, lib, tmp_path)
    assert res.restored == 1
    assert store.items.list(lib.id)[0].state == ItemState.OK


# --- Duration probing (fix: folder items shipped with seconds=None, so
# the library page read every one of them as "unknown duration" and never
# warned that a long item would be silently trimmed to the cap) --------


def _fake_probe(durations: dict[str, float]):
    """A `Callable[[Path], ProbeResult]` stand-in — never shells out to
    real ffprobe (that's the separately-marked `@pytest.mark.ffmpeg`
    suite's job). Keyed by filename so a test can give different files
    different durations."""
    def probe(path: Path) -> ProbeResult:
        return ProbeResult(
            seconds=durations[path.name], codec="aac", sample_rate=44100,
            channels=2, bytes=0, container="mp3", tags={},
        )
    return probe


def test_scan_probes_duration_for_new_files(store, tmp_path):
    mk(tmp_path, "long.mp3", b"a" * 50)
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    res = scan_folder(store, lib, tmp_path, probe=_fake_probe({"long.mp3": 9060.0}))
    assert res.added == 1
    assert store.items.list(lib.id)[0].seconds == 9060.0


def test_unprobeable_file_stores_none_not_zero(store, tmp_path):
    """This codebase has shipped the "unknown recorded as a definite
    value" bug shape a dozen times, and `probe()` returning 0.0 for an
    unknown duration was literally one of them. A file that can't be
    probed (corrupt header, no probe given at all) must leave `seconds`
    as `None` — never a fake 0.0 that would read as "this item is 0
    minutes long" instead of "we don't know"."""
    mk(tmp_path, "bad.mp3", b"a" * 50)
    lib = store.libraries.create("Book", folder_path=str(tmp_path))

    def blows_up(_path):
        raise ValueError("corrupt header")

    res = scan_folder(store, lib, tmp_path, probe=blows_up)
    assert res.added == 1
    item = store.items.list(lib.id)[0]
    assert item.seconds is None


def test_rescan_with_no_filesystem_change_probes_nothing_and_writes_nothing(store, tmp_path):
    """Existing tested property (`test_nothing_is_written_to_the_media_root`
    and friends): a rescan of an unchanged folder must still write nothing.
    Adding duration probing must not break that -- an unchanged file's
    duration is already known (or already recorded as unknown) and its
    audio content, per the content-based fingerprint, has not changed
    either, so there is nothing to re-probe. Proven here by counting
    `probe` calls, not just by checking the DB write count: a probe call
    that happened to be idempotent would pass a write-count-only check
    while still doing the exact wasted, unnecessary I/O this property is
    supposed to rule out."""
    mk(tmp_path, "1.mp3", b"a" * 50)
    lib = store.libraries.create("Book", folder_path=str(tmp_path))
    calls: list[str] = []

    def counting_probe(path: Path) -> ProbeResult:
        calls.append(path.name)
        return ProbeResult(seconds=120.0, codec="aac", sample_rate=44100, channels=2, bytes=0, container="mp3", tags={})

    res1 = scan_folder(store, lib, tmp_path, probe=counting_probe)
    assert res1.added == 1
    assert calls == ["1.mp3"]

    calls.clear()
    res2 = scan_folder(store, lib, tmp_path, probe=counting_probe)
    assert res2.unchanged == 1
    assert res2.added == 0
    assert calls == []
