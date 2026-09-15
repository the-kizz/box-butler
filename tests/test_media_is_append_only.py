"""The guarantee that replaced the read-only `/media` mount.

`/media` used to be mounted `:ro`, and "nothing in the image may ever write
to `/media`" was asserted in code, in tests, and by the container itself
(`touch` returned "Read-only file system"). That property was correct while
the app only read. It is not weakened now that downloads land in library
folders — it is replaced by a narrower and more useful one:

- the app **only ever creates new files** in a library folder;
- it **never modifies, renames, moves, truncates or deletes a file it did
  not create**;
- a name collision with an existing file is resolved by choosing a
  different name, **never** by overwriting;
- deleting an item removes the database row — deleting the *file* is an
  explicit, confirmed choice, never implicit and never the default.

This module is the safety net for exactly that. It fills a library folder
with files the app did not create and then runs **every** operation that
touches a library — scan, add each source kind, delete an item, rotate,
retention/eviction — asserting after each one that every pre-existing file
is byte-identical, still there, and unmodified.

`_Untouched` snapshots content, size and mtime, so a rewrite that happened
to produce identical bytes would still be caught, and a file that
disappeared is a failure rather than an absence nobody noticed.

Nothing here touches the network, the real cloud, ffmpeg or a real tonie.
"""
from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from boxbutler.domain.cache_name import cache_name
from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS
from boxbutler.domain.models import ItemKind, LibraryMode
from boxbutler.orchestrator.retention import evict
from boxbutler.orchestrator.run import Deps, Orchestrator, RotationSettings
from boxbutler.sinks.fake import FakeSink
from boxbutler.sinks.protocol import LiveChapter
from boxbutler.sources.folder import scan_folder
from boxbutler.sources.ingest import Ingestor
from boxbutler.sources.protocol import ResolvedItem
from boxbutler.sources.upload import UploadSource
from tests.audio.fakes import FakeRenderer

# Invented names throughout — never a real tonie's.
TONIE_ID = "fake-amber-07"
TONIE_NAME = "Amber Tonie"


# --------------------------------------------------------------- the snapshot


class _Untouched:
    """Content, size and mtime of every file under a folder, so "unchanged"
    means genuinely unchanged rather than "still parses the same"."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.state = self._read(folder)

    @staticmethod
    def _read(folder: Path) -> dict[str, tuple[bytes, int, int]]:
        out = {}
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(folder))] = (p.read_bytes(), st.st_size, st.st_mtime_ns)
        return out

    def assert_intact(self, what: str) -> None:
        now = self._read(self.folder)
        for name, before in self.state.items():
            assert name in now, f"{what} deleted {name!r} from the library folder"
            after = now[name]
            assert after[0] == before[0], f"{what} changed the contents of {name!r}"
            assert after[1] == before[1], f"{what} changed the size of {name!r}"
            assert after[2] == before[2], f"{what} modified {name!r} (mtime moved)"


# ------------------------------------------------------------------ fixtures

# The files the operator put there themselves: a ripped chapter, something
# that already looks exactly like one of ours (same cache-name shape, same
# key we are about to use), a file with no extension we recognise, and one
# in a subfolder.
OPERATOR_FILES = {
    "01 The Wombat Wakes Up.mp3": b"chapter one" * 40,
    "02 The Wombat Goes Out.mp3": b"chapter two" * 40,
    "Story [stub0].m4a": b"looks exactly like one of ours but is not" * 8,
    "sleeve notes.txt": b"not audio at all",
    "Extras/Bonus Track.flac": b"a bonus" * 40,
}


@pytest.fixture
def folder(tmp_path) -> Path:
    f = tmp_path / "media" / "Bedtime"
    (f / "Extras").mkdir(parents=True)
    for name, content in OPERATOR_FILES.items():
        (f / name).write_bytes(content)
    return f


@pytest.fixture
def cache(tmp_path) -> Path:
    c = tmp_path / "cache"
    c.mkdir()
    return c


@pytest.fixture
def library(store, folder):
    return store.libraries.create("Bedtime", LibraryMode.SERIAL, folder_path=str(folder))


class _StubSource:
    """Stands in for every link-shaped resolver — the ingest path into the
    library folder is identical for all of them by design."""

    def __init__(self, kind: ItemKind, prefix: str):
        self.kind, self.prefix = kind, prefix

    def matches(self, ref: str) -> bool:
        return ref.startswith(self.prefix)

    def resolve(self, ref: str) -> list[ResolvedItem]:
        key = "stub" + str(abs(hash(ref)) % 1000)
        return [ResolvedItem(self.kind, ref, key, "Story")]


class _StubFetcher:
    def supports(self, kind) -> bool:
        return True

    def fetch(self, item, cache_dir: Path) -> Path:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / cache_name(item.title, item.source_key, "m4a")
        target.write_bytes(b"freshly downloaded " + item.source_key.encode())
        return target


def _ingestor(store, cache):
    return Ingestor(
        store,
        [
            _StubSource(ItemKind.YOUTUBE, "https://video.example.invalid/"),
            _StubSource(ItemKind.PLAYLIST_ENTRY, "https://video.example.invalid/list/"),
            _StubSource(ItemKind.RSS, "https://feed.example.invalid/"),
            _StubSource(ItemKind.URL, "https://audio.example.invalid/"),
            UploadSource(cache / "uploads"),
        ],
        fetcher=_StubFetcher(),
        staging_dir=cache / "incoming",
    )


# -------------------------------------------------------------- the operations


def test_scanning_a_library_touches_nothing(store, library, folder):
    untouched = _Untouched(folder)
    scan_folder(store, library, folder)
    scan_folder(store, library, folder)  # and again, in case the second differs
    untouched.assert_intact("scan_folder")


@pytest.mark.parametrize(
    "ref",
    [
        "https://video.example.invalid/watch?v=abc",
        "https://video.example.invalid/list/abc",
        "https://feed.example.invalid/show.xml",
        "https://audio.example.invalid/episode.mp3",
    ],
)
def test_adding_each_source_kind_touches_nothing(store, library, folder, cache, ref):
    untouched = _Untouched(folder)
    _ingestor(store, cache).add_ref(library.id, ref)
    untouched.assert_intact(f"add_ref({ref})")


def test_adding_an_upload_touches_nothing(store, library, folder, cache):
    untouched = _Untouched(folder)
    _ingestor(store, cache).add_upload(
        library.id, "Wombat Lullaby.mp3", io.BytesIO(b"uploaded bytes")
    )
    untouched.assert_intact("add_upload")


def test_a_download_whose_name_collides_never_overwrites(store, library, folder, cache):
    """`Story [stub0].m4a` is already there and is *not* ours. A download
    that wants exactly that name must pick a different one."""
    before = (folder / "Story [stub0].m4a").read_bytes()
    untouched = _Untouched(folder)

    ing = _ingestor(store, cache)
    # Drive the collision directly: the same name the operator's file has.
    from boxbutler.sources.library_folder import adopt_into

    staged = cache / "staged.m4a"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"our download")
    landed = adopt_into(folder, staged, "Story [stub0].m4a")

    assert landed.name != "Story [stub0].m4a"
    assert (folder / "Story [stub0].m4a").read_bytes() == before
    untouched.assert_intact("a colliding download")
    assert ing is not None


def test_deleting_an_item_does_not_delete_its_file_by_default(store, library, folder, cache):
    scan_folder(store, library, folder)
    item = next(i for i in store.items.list(library.id) if i.title.startswith("01"))
    untouched = _Untouched(folder)

    store.items.delete(item.id)

    assert store.items.get(item.id) is None
    untouched.assert_intact("deleting an item")


def test_rotating_touches_nothing(store, library, folder, cache):
    untouched = _Untouched(folder)
    orch, sink = _orchestrator(store, library, folder, cache)

    report = orch.run(apply=True)

    assert report is not None
    assert sink.calls_named("upload"), "the run should actually have staged and uploaded something"
    untouched.assert_intact("a full rotate")


def test_retention_never_touches_a_library_folder(store, library, folder, cache):
    """The cache holds renditions only. Whatever the budget, eviction may
    never reach into a library folder — the operator's own media is not
    derived from anything and cannot be rebuilt."""
    orch, _sink = _orchestrator(store, library, folder, cache)
    orch.run(apply=True)
    untouched = _Untouched(folder)

    # A budget of zero: eviction is under maximum pressure and will take
    # everything it is allowed to take.
    plan = evict(store, cache, budget_bytes=0, depth=0, apply=True)

    untouched.assert_intact("eviction at a zero budget")
    for e in plan:
        assert folder not in Path(e.path).parents, f"eviction planned to delete {e.path}"


def test_retention_will_not_delete_a_library_file_even_if_a_row_points_at_one(
    store, library, folder, cache
):
    """The stronger form: the database *does* carry `source_file` rows
    pointing into the library folder (that is how a downloaded source is
    recorded now), so this proves the guard is the thing protecting them,
    not the accident that nothing pointed there."""
    scan_folder(store, library, folder)
    item = store.items.list(library.id)[0]
    store.sources.record(item.id, Path(item.local_path), datetime.now(UTC))
    untouched = _Untouched(folder)

    evict(store, cache, budget_bytes=0, depth=0, apply=True)

    untouched.assert_intact("eviction with a source row inside the library folder")


def test_every_operation_in_one_pass_leaves_the_folder_intact(store, library, folder, cache):
    """Belt and braces: the whole sequence, in the order a real install
    would do it, against one folder."""
    untouched = _Untouched(folder)

    scan_folder(store, library, folder)
    ing = _ingestor(store, cache)
    ing.add_ref(library.id, "https://video.example.invalid/watch?v=abc")
    ing.add_ref(library.id, "https://feed.example.invalid/show.xml")
    ing.add_upload(library.id, "Wombat Lullaby.mp3", io.BytesIO(b"uploaded bytes"))
    scan_folder(store, library, folder)

    orch, _sink = _orchestrator(store, library, folder, cache)
    orch.run(apply=True)
    evict(store, cache, budget_bytes=0, depth=0, apply=True)

    doomed = store.items.list(library.id)[0]
    store.items.delete(doomed.id)

    untouched.assert_intact("the whole sequence")
    # And the operator's own files are all still listed, none lost.
    assert set(OPERATOR_FILES) <= {
        str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
    }


# ------------------------------------------------------------- the orchestrator


def _orchestrator(store, library, folder, cache):
    """A real `Orchestrator` over fake sink/renderer/fetcher — the same
    shape `tests/conftest.py::world` builds, pointed at a folder library so
    a rotate genuinely reads from `/media`."""
    scan_folder(store, library, folder)
    sink = FakeSink()
    sink.add_target(TONIE_ID, TONIE_NAME, [LiveChapter("old1", "Last Night", 60.0, False)])
    a = store.assignments.upsert_target(sink.name, TONIE_ID, TONIE_NAME)
    store.assignments.assign_library(a.id, library.id)

    class _PassThroughFetcher:
        """A folder item's bytes are already on disk — hand back the path,
        never copy it, and never open it for writing."""

        def supports(self, kind) -> bool:
            return True

        def fetch(self, item, cache_dir: Path) -> Path:
            return Path(item.local_path)

    class _Renderer(FakeRenderer):
        """Sources probe long (so every item needs a trim); renditions
        probe at exactly the cap, which is what a trim produces and what
        `verify_rendition` has to accept. Keyed on where the file lives
        rather than on a name computed in advance, since the items here
        come from a real scan."""

        def probe(self, path: Path):
            result = super().probe(path)
            if Path(path).parent == Path(cache):
                from dataclasses import replace as _replace

                return _replace(result, seconds=float(DEFAULT_CAP_SECONDS))
            return result

    deps = Deps(
        store=store,
        sink=sink,
        fetcher=_PassThroughFetcher(),
        renderer=_Renderer(),
        cache_dir=cache,
        snapshot_dir=cache / "snapshots",
        settings=RotationSettings(
            cap_seconds=DEFAULT_CAP_SECONDS, upload_backoff_s=(0, 0, 0), prefetch_depth=0
        ),
        clock=lambda: datetime.now(UTC),
        sleep=lambda s: None,
    )
    return Orchestrator(deps), sink
