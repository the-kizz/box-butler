"""Shared pytest fixtures for the store layer.

Perf note (test-suite-speed task): `Store.open()` -> `db.connect()` issues
`PRAGMA journal_mode = WAL` on every open. On a brand-new SQLite file this
costs ~170-180ms (measured identical across synchronous=FULL/NORMAL/OFF, so
it's WAL/-shm file creation itself, not fsync-on-write) — and with ~200
tests each getting a fresh `tmp_path` database via this fixture, that alone
accounted for most of the 4-6 minute suite.

Fix: build one already-migrated, already-WAL-mode database file once per
test session (`_migrated_template_db`), then give each test its own copy of
that file (a plain file copy, ~3-5ms) instead of building a fresh database
from scratch. Copying a file that is already in WAL mode means the copy's
header already says WAL, so `connect()`'s `PRAGMA journal_mode = WAL` on it
is just confirming the existing mode rather than paying the one-time
conversion cost.

This changes nothing about *what* gets exercised per test: every test still
calls the real `Store.open()` -> `connect()` + `migrate()` on its own
private file, with the same pragmas (`journal_mode=WAL`, `foreign_keys=ON`,
`busy_timeout=15000`) as production. `migrate()` runs every time too; against
the template's copy it's just a no-op version check (`schema_version`
already populated), so it's not a genuine from-empty migration run — that
case is still covered directly by `tests/store/test_migrations.py`, which
calls `connect()`/`migrate()` on a brand-new `tmp_path` file itself, not
through this fixture (including the atomicity test, which needs a truly
empty database to prove a failed migration leaves no partial schema/version
row behind).

Isolation: each test still gets a fully independent database *file* (a
fresh copy every time) — nothing is shared between tests except the
read-only template they're all copied from, and the template itself is
never reopened for writing after the session fixture builds it. See
`tests/store/test_fixture_isolation.py` for a test that proves this (one
test writes a row, a later test asserts a fresh `store` doesn't see it).

`tests/store/test_concurrency.py` deliberately bypasses this fixture and
calls `Store.open()` directly on its own `tmp_path`, since it's testing
real per-thread connection behaviour under concurrent load — that stays on
the slow (but realistic) path.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from boxbutler.audio.protocol import RenderSpec
from boxbutler.domain.cache_name import cache_name
from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS
from boxbutler.domain.models import Item, ItemKind, LibraryMode, RenditionMode
from boxbutler.orchestrator.run import Deps, Orchestrator, RotationSettings
from boxbutler.sinks.fake import FakeSink
from boxbutler.sinks.protocol import LiveChapter
from boxbutler.store.db import Store
from tests.audio.fakes import FakeRenderer
from tests.fetch.fakes import FakeFetcher


@pytest.fixture(scope="session")
def _migrated_template_db(tmp_path_factory) -> Path:
    """Build one migrated, WAL-mode database file, shared read-only across
    the whole test session as a copy source (see module docstring)."""
    template_path = tmp_path_factory.mktemp("store-template") / "template.sqlite"
    template_store = Store.open(template_path)
    # Checkpoint any WAL contents back into the main file and truncate the
    # WAL, so a plain copy of the main file (plus whatever sidecar files
    # remain) is a complete, consistent snapshot.
    template_store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    template_store.conn.close()
    return template_path


@pytest.fixture
def store(tmp_path, _migrated_template_db):
    dest = tmp_path / "bb.sqlite"
    shutil.copyfile(_migrated_template_db, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(_migrated_template_db) + suffix)
        if side.exists():
            shutil.copyfile(side, str(dest) + suffix)
    return Store.open(dest)


# --- Promoted from tests/orchestrator/conftest.py (Task 27) ---------------
#
# `world` originally lived only under tests/orchestrator/, where pytest
# shares fixtures down that conftest's own directory tree. tests/cli/ needs
# the same fixture (see tests/cli/test_cli.py), and pytest does not share
# fixtures sideways between sibling suites — only a conftest.py that is an
# ancestor of both is visible to both. Promoted here verbatim: no behaviour
# change (see tests/orchestrator/conftest.py, which now just re-exports
# these names for the handful of orchestrator tests that import
# `source_cache_name` / `rendition_cache_name` / `events` / `mutating_calls`
# directly as functions rather than as fixtures).

CAP = DEFAULT_CAP_SECONDS       # RotationSettings.cap_seconds default (5395), under the sink's 5400
SOURCE_SECONDS = 6000.0         # FakeRenderer's default probe: longer than the cap, so TRIM


def source_cache_name(item: Item) -> str:
    """The name `FakeFetcher` gives the item's cached source file."""
    return cache_name(item.title, item.source_key, "m4a")


def rendition_cache_name(item: Item, cap: int = CAP, mode: RenditionMode = RenditionMode.TRIM) -> str:
    """The name the orchestrator gives the item's rendition."""
    from boxbutler.audio.protocol import rendition_name

    return rendition_name(item.title, item.source_key, RenderSpec(cap_seconds=cap), mode)


@pytest.fixture
def world(store, tmp_path):
    """One managed tonie 'Green Tonie' holding 'Old Story', a Bedtime library of 3
    items, fake fetch/render, dry-run-capable orchestrator."""
    cache = tmp_path / "cache"
    cache.mkdir()
    snaps = tmp_path / "snapshots"

    sink = FakeSink()
    target = sink.add_target("T1", "Green Tonie", [LiveChapter("old1", "Old Story", 5340.0, False)])

    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE)
    items = [
        store.items.add(lib.id, ItemKind.YOUTUBE, f"u/v{i}", f"vid{i}", f"Story {i} \N{SPAGHETTI}")
        for i in range(3)
    ]
    a = store.assignments.upsert_target("fake", "T1", "Green Tonie")
    store.assignments.assign_library(a.id, lib.id)

    srcs: dict[str, Path] = {}
    for it in items:
        p = tmp_path / f"src-{it.source_key}.m4a"
        p.write_bytes(b"aac" * 100)
        srcs[it.id] = p
    fetcher = FakeFetcher(files=srcs)
    # Sources probe at 6000 s (FakeRenderer's default) so every item needs a
    # TRIM; the renditions probe at the cap, which is what the trim produces
    # and what verify must accept. See the module docstring, note 1.
    renderer = FakeRenderer(durations={rendition_cache_name(it): float(CAP) for it in items})

    deps = Deps(
        store=store,
        sink=sink,
        fetcher=fetcher,
        renderer=renderer,
        cache_dir=cache,
        snapshot_dir=snaps,
        # depth 0 keeps the swap tests' call lists exact; Task 24 sets 3.
        settings=RotationSettings(upload_backoff_s=(0, 0, 0), prefetch_depth=0),
        clock=lambda: datetime.now(UTC),
        sleep=lambda s: None,
    )
    return dict(
        store=store,
        sink=sink,
        target=target,
        lib=lib,
        items=items,
        a=store.assignments.get(a.id),
        fetcher=fetcher,
        renderer=renderer,
        deps=deps,
        orch=Orchestrator(deps),
        snaps=snaps,
        cache=cache,
    )


def events(store, run_id, assignment_id=None):
    return [
        e.event
        for e in store.runs.events(run_id)
        if assignment_id is None or e.assignment_id == assignment_id
    ]


def mutating_calls(sink):
    return [c[0] for c in sink.calls if c[0] in ("clear", "upload", "settle")]
