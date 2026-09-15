"""Deleting an item is not deleting a file.

Since a library is a folder, an item's audio is a real file the operator
can see — and some of those files they put there themselves. The standing
guarantee is that the app never deletes a file it did not create; the one
exception is an explicit, confirmed choice in the UI, which is off by
default and has to be ticked on purpose.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxbutler.domain.models import ItemKind, LibraryMode
from boxbutler.sinks.fake import FakeSink
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner
from boxbutler.web.settings import WebSettings


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def app(store, tmp_path, media_root):
    settings = WebSettings(
        secret_key="test-secret",
        admin_user="admin",
        admin_password="correct horse",
        data_dir=tmp_path,
    )
    sink = FakeSink()
    return create_app(store, sink, settings, FakeRunner(store, sink), media_root=media_root)


@pytest.fixture
def auth(app):
    client = TestClient(app, follow_redirects=False)
    assert client.post(
        "/login", data={"username": "admin", "password": "correct horse"}
    ).status_code == 303
    return client


@pytest.fixture
def library_and_item(store, media_root):
    folder = media_root / "Bedtime"
    folder.mkdir()
    audio = folder / "Wombat Lullaby.mp3"
    audio.write_bytes(b"the operator's own file")
    lib = store.libraries.create("Bedtime", LibraryMode.SERIAL, folder_path=str(folder))
    item = store.items.add(
        lib.id, ItemKind.FOLDER_FILE, "Wombat Lullaby.mp3", "size-abc", "Wombat Lullaby",
        local_path=str(audio),
    )
    return lib, item, audio


def test_removing_an_item_leaves_the_file_alone_by_default(auth, store, library_and_item):
    lib, item, audio = library_and_item

    r = auth.post(f"/libraries/{lib.id}/items/{item.id}/delete", data={})

    assert r.status_code == 303
    assert store.items.get(item.id) is None
    assert audio.exists() and audio.read_bytes() == b"the operator's own file"


def test_the_file_goes_only_when_it_is_explicitly_asked_for(auth, store, library_and_item):
    lib, item, audio = library_and_item

    r = auth.post(f"/libraries/{lib.id}/items/{item.id}/delete", data={"delete_file": "1"})

    assert r.status_code == 303
    assert store.items.get(item.id) is None
    assert not audio.exists()


def test_the_row_offers_the_choice_and_never_preticks_it(auth, store, library_and_item):
    lib, _item, _audio = library_and_item
    html = auth.get(f"/libraries/{lib.id}").text
    assert 'name="delete_file"' in html
    # Never pre-ticked: deleting a file is never the default.
    assert 'name="delete_file" value="1" checked' not in html
    assert 'checked' not in html.split('name="delete_file"')[1].split(">")[0]


def test_a_file_outside_the_library_folder_is_never_deleted(auth, store, media_root, tmp_path):
    """An item's `local_path` can point somewhere else entirely — a cache
    copy left over from before the migration. That is retention's
    business, not this route's."""
    folder = media_root / "Bedtime"
    folder.mkdir()
    elsewhere = tmp_path / "cache" / "Story [vid1].m4a"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"a cache copy")

    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE, folder_path=str(folder))
    item = store.items.add(
        lib.id, ItemKind.YOUTUBE, "u/v1", "vid1", "Story", local_path=str(elsewhere)
    )

    r = auth.post(f"/libraries/{lib.id}/items/{item.id}/delete", data={"delete_file": "1"})

    assert r.status_code == 303
    assert store.items.get(item.id) is None
    assert elsewhere.exists(), "a path outside the library folder must never be unlinked here"
    assert Path(elsewhere).read_bytes() == b"a cache copy"
