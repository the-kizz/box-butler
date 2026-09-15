"""Step 2 of the setup wizard is never mandatory.

It used to demand *both* a library name and a link (`if not library_name
or not source_ref`) while its own help text promised a folder option
"later" — which is exactly why an operator whose library already existed
could not get past it. A step may not be mandatory when the thing it
creates is already present.

Step 2 is now: watch a folder / paste a link / skip — and skip is the
default when a library already exists.

Same starting point as `tests/web/test_setup.py`: a store with no admin
account, which is the only thing that makes `/setup` reachable. Nothing
here touches the network, `tonie_api` or ffmpeg.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boxbutler.sinks.fake import FakeSink
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner
from boxbutler.web.settings import WebSettings

TONIE_USER = "parent@example.invalid"
TONIE_PASS = "sw0rdfish-example"
SOURCE_REF = "https://video.example.invalid/watch?v=abc123"


@pytest.fixture
def settings(tmp_path):
    return WebSettings(secret_key="test-secret", data_dir=tmp_path)


@pytest.fixture
def sink():
    return FakeSink(valid_username=TONIE_USER, valid_password=TONIE_PASS)


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def app(store, sink, settings, media_root):
    return create_app(store, sink, settings, FakeRunner(store, sink), media_root=media_root)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _step1(client):
    return client.post(
        "/setup",
        data={"step": "1", "tonie_username": TONIE_USER, "tonie_password": TONIE_PASS},
    )


def test_step2_can_be_skipped_and_the_wizard_still_finishes(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    _step1(client)

    r = client.post("/setup", data={"step": "2", "action": "skip", "tonie_username": TONIE_USER})
    assert r.status_code == 200
    assert 'name="step" value="3"' in r.text

    r = client.post(
        "/setup",
        data={
            "step": "3",
            "skip": "1",
            "tonie_username": TONIE_USER,
            "target_ids": ["fake-green-01"],
            "admin_username": "admin",
            "admin_password": "correct horse battery",
            "admin_password_confirm": "correct horse battery",
        },
    )
    assert r.status_code == 303
    # Skipped means skipped: no library was invented for the operator.
    assert store.libraries.list() == []
    assert store.settings.get("admin_user") == "admin"


def test_step2_completes_with_a_folder_and_no_link(client, store, sink, media_root):
    sink.add_target("fake-green-01", "Green Tonie")
    (media_root / "Wombat Tales").mkdir()
    _step1(client)

    r = client.post(
        "/setup",
        data={
            "step": "2",
            "tonie_username": TONIE_USER,
            "library_name": "Wombat Tales",
            "folder_path": "Wombat Tales",
            "source_ref": "",
        },
    )
    assert r.status_code == 200, r.text
    assert 'name="step" value="3"' in r.text

    r = client.post(
        "/setup",
        data={
            "step": "3",
            "tonie_username": TONIE_USER,
            "library_name": "Wombat Tales",
            "folder_path": "Wombat Tales",
            "source_ref": "",
            "target_ids": ["fake-green-01"],
            "admin_username": "admin",
            "admin_password": "correct horse battery",
            "admin_password_confirm": "correct horse battery",
        },
    )
    assert r.status_code == 303
    lib = store.libraries.list()[0]
    assert lib.name == "Wombat Tales"
    assert lib.folder_path == str(media_root / "Wombat Tales")
    assert store.items.list(lib.id) == []


def test_a_library_created_by_the_wizard_always_has_a_folder(client, store, sink, media_root):
    sink.add_target("fake-green-01", "Green Tonie")
    _step1(client)
    client.post(
        "/setup",
        data={
            "step": "2",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
        },
    )
    r = client.post(
        "/setup",
        data={
            "step": "3",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
            "target_ids": ["fake-green-01"],
            "admin_username": "admin",
            "admin_password": "correct horse battery",
            "admin_password_confirm": "correct horse battery",
        },
    )
    assert r.status_code == 303
    lib = store.libraries.list()[0]
    assert lib.folder_path == str(media_root / "Bedtime")
    assert (media_root / "Bedtime").is_dir()


def test_skip_is_offered_and_explained_when_a_library_already_exists(
    client, store, sink, media_root
):
    (media_root / "Existing").mkdir()
    store.libraries.create("Existing", folder_path=str(media_root / "Existing"))

    r = _step1(client)

    assert r.status_code == 200
    assert 'value="skip"' in r.text
    assert "already" in r.text.lower()


def test_step2_still_asks_for_a_name_when_it_is_not_being_skipped(client, store, sink):
    _step1(client)
    r = client.post(
        "/setup",
        data={"step": "2", "tonie_username": TONIE_USER, "library_name": "", "source_ref": ""},
    )
    assert r.status_code == 200
    assert 'name="step" value="2"' in r.text
    assert store.libraries.list() == []
