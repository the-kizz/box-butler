"""Adding a source downloads the audio into the library's folder.

A library is a folder; a source is only *how* media arrives in it. After
`add_ref`/`add_upload` the file is an ordinary file in the library folder —
indistinguishable from one the operator copied in by hand — and a re-scan
recognises it rather than adding it twice.

Nothing here touches the network: the fetcher is a local double that
writes bytes, exactly as the rest of the suite does.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from boxbutler.domain.cache_name import cache_name, parse_cache_name
from boxbutler.domain.models import ItemKind, LibraryMode
from boxbutler.fetch.protocol import ItemUnavailable
from boxbutler.sources.folder import scan_folder
from boxbutler.sources.ingest import Ingestor
from boxbutler.sources.protocol import ResolvedItem, SourceError
from boxbutler.sources.upload import UploadSource


class StubSource:
    """One `SourceProtocol` standing in for every link-shaped resolver —
    the ingest path is the same for all of them by design (`Ingestor`'s
    own docstring), so one is enough to exercise it."""

    def __init__(self, kind: ItemKind, prefix: str, count: int = 1):
        self.kind, self.prefix, self.count = kind, prefix, count

    def matches(self, ref: str) -> bool:
        return ref.startswith(self.prefix)

    def resolve(self, ref: str) -> list[ResolvedItem]:
        return [
            ResolvedItem(self.kind, f"{ref}#{i}", f"{self.prefix[:3]}{i}", f"Story {i}")
            for i in range(self.count)
        ]


class StubFetcher:
    """Writes deterministic bytes under the cache-name convention, the way
    a real fetcher does, into whatever directory it is handed."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.dirs: list[Path] = []

    def supports(self, kind) -> bool:
        return True

    def fetch(self, item, cache_dir: Path) -> Path:
        self.dirs.append(Path(cache_dir))
        if self.fail is not None:
            raise self.fail
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / cache_name(item.title, item.source_key, "m4a")
        target.write_bytes(b"audio for " + item.source_key.encode())
        return target


@pytest.fixture
def folder(tmp_path) -> Path:
    f = tmp_path / "media" / "Bedtime"
    f.mkdir(parents=True)
    return f


@pytest.fixture
def staging(tmp_path) -> Path:
    return tmp_path / "cache" / "incoming"


def _ingestor(store, staging, sources, fetcher=None):
    return Ingestor(store, sources, fetcher=fetcher, staging_dir=staging)


@pytest.mark.parametrize(
    "kind,prefix",
    [
        (ItemKind.YOUTUBE, "https://video.example.invalid/"),
        (ItemKind.PLAYLIST_ENTRY, "https://video.example.invalid/list/"),
        (ItemKind.RSS, "https://feed.example.invalid/"),
        (ItemKind.URL, "https://audio.example.invalid/"),
    ],
)
def test_adding_each_source_kind_puts_a_real_file_in_the_library_folder(
    store, folder, staging, kind, prefix
):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    fetcher = StubFetcher()
    ing = _ingestor(store, staging, [StubSource(kind, prefix)], fetcher)

    items = ing.add_ref(lib.id, prefix + "thing")

    assert len(items) == 1
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == 1, files
    landed = folder / files[0]
    assert landed.read_bytes().startswith(b"audio for ")
    # The item in the store points at the file in the library folder.
    stored = store.items.list(lib.id)[0]
    assert stored.local_path == str(landed)
    # And the name still carries the source key, so identity survives.
    _title, key, _ext = parse_cache_name(landed.name)
    assert key == stored.source_key


def test_a_playlist_lands_every_entry_in_the_folder(store, folder, staging):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(
        store,
        staging,
        [StubSource(ItemKind.PLAYLIST_ENTRY, "https://video.example.invalid/list/", count=3)],
        StubFetcher(),
    )

    items = ing.add_ref(lib.id, "https://video.example.invalid/list/abc")

    assert len(items) == 3
    assert len([p for p in folder.iterdir() if p.is_file()]) == 3


def test_an_upload_lands_in_the_library_folder_not_the_cache(store, folder, staging, tmp_path):
    upload_dir = tmp_path / "cache" / "uploads"
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(store, staging, [UploadSource(upload_dir)])

    item = ing.add_upload(lib.id, "Wombat Lullaby.mp3", io.BytesIO(b"uploaded bytes"))

    landed = Path(item.local_path)
    assert landed.parent == folder
    assert landed.read_bytes() == b"uploaded bytes"
    # Nothing is left behind in the upload staging directory.
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_adding_the_same_ref_twice_downloads_once_and_adds_one_item(store, folder, staging):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    fetcher = StubFetcher()
    ing = _ingestor(store, staging, [StubSource(ItemKind.YOUTUBE, "https://v/")], fetcher)

    ing.add_ref(lib.id, "https://v/x")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    ing.add_ref(lib.id, "https://v/x")

    assert len(store.items.list(lib.id)) == 1
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_an_existing_file_with_the_same_source_key_is_adopted_not_re_downloaded(
    store, folder, staging
):
    """The operator renamed the title half. Identity is the bracketed
    source key, so this is the same item — no second download, no
    duplicate."""
    existing = folder / "A Much Better Title [htt0].m4a"
    existing.write_bytes(b"already here")

    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    fetcher = StubFetcher()
    ing = _ingestor(store, staging, [StubSource(ItemKind.YOUTUBE, "https://v/")], fetcher)

    items = ing.add_ref(lib.id, "https://v/x")

    assert fetcher.dirs == [], "should not have fetched anything"
    assert items[0].local_path == str(existing)
    assert existing.read_bytes() == b"already here"
    assert [p.name for p in folder.iterdir()] == ["A Much Better Title [htt0].m4a"]


def test_a_rescan_after_adding_a_source_does_not_duplicate_the_item(store, folder, staging):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(store, staging, [StubSource(ItemKind.YOUTUBE, "https://v/")], StubFetcher())
    ing.add_ref(lib.id, "https://v/x")
    assert len(store.items.list(lib.id)) == 1

    # The downloaded file is now an ordinary file in the folder, and the
    # scanner walks the same folder. It must recognise it as already ours.
    result = scan_folder(store, store.libraries.get(lib.id), folder)

    assert result.added == 0
    assert len(store.items.list(lib.id)) == 1


def test_a_failed_download_still_records_the_item(store, folder, staging):
    """An extraction failure at add time must not swallow the item — the
    orchestrator fetches it again at run time. Losing the row would make
    the paste look like it did nothing."""
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(
        store,
        staging,
        [StubSource(ItemKind.YOUTUBE, "https://v/")],
        StubFetcher(fail=ItemUnavailable("private video")),
    )

    items = ing.add_ref(lib.id, "https://v/x")

    assert len(items) == 1
    assert len(store.items.list(lib.id)) == 1
    assert items[0].local_path is None
    assert [p for p in folder.iterdir()] == []


def test_a_download_never_leaves_anything_in_the_staging_directory(store, folder, staging):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(store, staging, [StubSource(ItemKind.YOUTUBE, "https://v/")], StubFetcher())
    ing.add_ref(lib.id, "https://v/x")
    leftovers = list(staging.rglob("*")) if staging.exists() else []
    assert [p for p in leftovers if p.is_file()] == []


def test_an_unrecognised_ref_still_raises(store, folder, staging):
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    ing = _ingestor(store, staging, [StubSource(ItemKind.YOUTUBE, "https://v/")], StubFetcher())
    with pytest.raises(SourceError):
        ing.add_ref(lib.id, "not-a-link")
