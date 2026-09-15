"""Opening a library page scans that library's folder first.

The gap this closes is the one the operator hit: copy a file into the
folder, open the library page, and it is not listed until you press Scan.
`sync_media_root` already runs inside the run lock, so the library is
correct whenever it is *used*; this makes it correct whenever it is
*looked at*.

Explicitly not a filesystem watcher — inotify does not fire on NFS or SMB,
so a watcher would be the feature most likely to look like it works while
doing nothing. There is no watcher to test, and there should not be one.

These tests wire the *real* `scan_folder` (the Phase 2 default is a no-op),
because a scan-on-open that called a no-op would pass a weaker test
forever.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boxbutler.domain.models import LibraryMode
from boxbutler.sinks.fake import FakeSink
from boxbutler.sources.folder import scan_folder as scan_folder_dir
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner
from boxbutler.web.settings import WebSettings


@pytest.fixture
def app(store, tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    settings = WebSettings(
        secret_key="test-secret",
        admin_user="admin",
        admin_password="correct horse",
        data_dir=tmp_path,
    )
    sink = FakeSink()

    def scan(library) -> int:
        from pathlib import Path

        if not library.folder_path:
            return 0
        return scan_folder_dir(store, library, Path(library.folder_path)).added

    return create_app(
        store, sink, settings, FakeRunner(store, sink), scan_folder=scan, media_root=media_root
    )


@pytest.fixture
def auth(app):
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert r.status_code == 303
    return client


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "media" / "Bedtime"
    f.mkdir(parents=True)
    return f


def _library(store, folder):
    return store.libraries.create("Bedtime", LibraryMode.SERIAL, folder_path=str(folder))


def test_opening_the_page_lists_a_file_copied_in_by_hand(auth, store, folder):
    lib = _library(store, folder)
    (folder / "Wombat Lullaby.mp3").write_bytes(b"a" * 64)

    html = auth.get(f"/libraries/{lib.id}").text

    assert "Wombat Lullaby" in html
    assert len(store.items.list(lib.id)) == 1


def test_opening_the_page_writes_nothing_to_the_folder(auth, store, folder):
    lib = _library(store, folder)
    (folder / "Wombat Lullaby.mp3").write_bytes(b"a" * 64)
    (folder / "notes.txt").write_bytes(b"the operator's own notes")
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()
    }

    auth.get(f"/libraries/{lib.id}")
    auth.get(f"/libraries/{lib.id}")

    after = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()
    }
    assert after == before


def test_an_unreadable_folder_still_renders_the_page_and_keeps_the_rows(auth, store, folder):
    lib = _library(store, folder)
    (folder / "Wombat Lullaby.mp3").write_bytes(b"a" * 64)
    auth.get(f"/libraries/{lib.id}")
    assert len(store.items.list(lib.id)) == 1

    folder.chmod(0o000)
    try:
        r = auth.get(f"/libraries/{lib.id}")
    finally:
        folder.chmod(0o755)

    assert r.status_code == 200
    # Unavailable, never deleted — the same rule a missing mount already
    # gets everywhere else in this codebase.
    assert len(store.items.list(lib.id)) == 1


def test_a_scan_failure_never_stops_the_page_rendering(auth, store, folder, app):
    lib = _library(store, folder)

    def explode(library):
        raise RuntimeError("scan blew up")

    app.state.scan_folder = explode
    r = auth.get(f"/libraries/{lib.id}")
    assert r.status_code == 200
    assert "Bedtime" in r.text


def test_a_library_with_no_folder_is_not_scanned(auth, store, app):
    lib = store.libraries.create("Folderless", LibraryMode.SINGLE)
    calls = []
    app.state.scan_folder = lambda library: calls.append(library) or 0

    assert auth.get(f"/libraries/{lib.id}").status_code == 200
    assert calls == []
