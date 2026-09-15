"""The wizard's folder picker actually loads.

Step 2 pulls the picker in over htmx. Every route it could pull it from
is behind two doors that are both shut during setup: the setup gate in
`web/app.py` redirects any path outside `/setup`, and `require_login`
refuses anything without a session. When the markup pointed at
`/libraries/folders`, the fetch was redirected, the swap put nothing on
the page, and step 2 rendered with no way to choose a folder at all —
silently, because a failed htmx load looks exactly like an element that
was never there.

These tests fail against that markup and pass against `/setup/folders`.
Nothing here touches a real media directory: every media root is a
`tmp_path`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxbutler.sinks.fake import FakeSink
from boxbutler.store.db import Store
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner
from boxbutler.web.settings import WebSettings


def _app(tmp_path: Path, *, configured: bool = False):
    """An app in the state a first start is really in: a media root with
    folders in it, and (unless `configured`) no admin account, so the
    wizard is reachable."""
    media_root = tmp_path / "media"
    for name in ("Bedtime", "Car Trips"):
        (media_root / name).mkdir(parents=True, exist_ok=True)
    store = Store.open(tmp_path / "w.sqlite")
    sink = FakeSink()
    sink.add_target("fake-amber-07", "Amber Tonie", [])
    settings = WebSettings(
        secret_key="k",
        # `ensure_admin` writes the bootstrap admin from these, and the
        # setup gate then considers the install configured -- so an
        # unconfigured app must have neither half.
        admin_user="admin" if configured else "",
        admin_password="admin-password" if configured else "",
        data_dir=tmp_path,
    )
    app = create_app(store, sink, settings, FakeRunner(store, sink), media_root=media_root)
    return app, media_root


@pytest.fixture
def client(tmp_path):
    app, _root = _app(tmp_path)
    return TestClient(app, follow_redirects=False)


def test_step_two_points_its_picker_at_a_url_the_wizard_can_actually_reach(client):
    # Step 1 accepts anything against the FakeSink; step 2 is the one with
    # the picker.
    r = client.post(
        "/setup",
        data={"step": "1", "tonie_username": "you@example.com", "tonie_password": "pw"},
    )
    assert r.status_code == 200, r.status_code
    assert 'hx-get="/setup/folders' in r.text, (
        "step 2 must load its picker from a path the setup gate lets through; "
        "/libraries/folders is redirected and login-gated during setup"
    )


def test_the_wizard_loads_htmx_at_all(client):
    """The other half of the same bug, and the half that made it total:
    `setup.html` is standalone (it does not extend `base.html`, which is
    where every other page gets htmx). With no htmx on the page an
    `hx-get` is inert markup — so fixing the URL alone still rendered a
    step 2 with no picker."""
    r = client.post(
        "/setup",
        data={"step": "1", "tonie_username": "you@example.com", "tonie_password": "pw"},
    )
    assert r.status_code == 200
    assert "htmx.min.js" in r.text, "step 2's hx-get does nothing without htmx on the page"


def test_the_picker_route_answers_during_setup_without_a_login(client):
    r = client.get("/setup/folders")
    assert r.status_code == 200, r.status_code
    # The real thing, not a login page or a redirect body.
    assert 'name="folder_path"' in r.text
    assert "Bedtime" in r.text
    assert "Car Trips" in r.text


def test_the_pickers_own_links_stay_inside_the_wizard(client):
    """Descending a level must not bounce the operator out of setup: the
    partial's Open/Up links carry the same wizard URL, not the library
    one."""
    r = client.get("/setup/folders")
    assert r.status_code == 200
    assert "/libraries/folders" not in r.text
    assert r.text.count("/setup/folders") >= 2


def test_it_can_descend_and_come_back_up(client):
    r = client.get("/setup/folders", params={"path": "Bedtime"})
    assert r.status_code == 200
    assert "Up one level" in r.text


def test_it_is_still_confined_to_the_media_root(client):
    for attempt in ("..", "../..", "/etc", "Bedtime/../.."):
        r = client.get("/setup/folders", params={"path": attempt})
        assert r.status_code == 400, f"{attempt!r} should be refused, got {r.status_code}"


def test_it_stops_answering_once_an_admin_account_exists(tmp_path):
    """The reason this route may be open at all is that it closes. A
    configured install must not expose an unauthenticated route that
    lists directory names."""
    app, _root = _app(tmp_path, configured=True)
    client = TestClient(app, follow_redirects=False)
    r = client.get("/setup/folders")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_the_library_screen_picker_still_belongs_to_the_library_screen(tmp_path):
    """The shared builder must not have handed the logged-in picker the
    wizard's URL: after setup, `/setup/folders` redirects, so a library
    page whose picker pointed there would break on the first click."""
    app, _root = _app(tmp_path, configured=True)
    client = TestClient(app)
    login = client.post(
        "/login", data={"username": "admin", "password": "admin-password"}
    )
    assert login.status_code == 200, login.status_code
    r = client.get("/libraries/folders")
    assert r.status_code == 200
    assert "/setup/folders" not in r.text
    assert "/libraries/folders" in r.text


def test_the_picker_never_writes_to_the_media_root(client, tmp_path):
    """Browsing is reading. The one thing a page view of the picker may
    never do is create the folder it is offering to create."""
    media_root = tmp_path / "media"
    before = sorted(p.name for p in media_root.iterdir())
    client.get("/setup/folders", params={"name": "Something New"})
    client.get("/setup/folders", params={"path": "Bedtime", "name": "Something New"})
    after = sorted(p.name for p in media_root.iterdir())
    assert before == after
    assert not (media_root / "Something New").exists()


def test_a_missing_media_root_is_a_503_not_a_crash(tmp_path):
    """`create_app` can be handed no media root at all (the CLI and some
    tests do). The picker says so rather than raising."""
    store = Store.open(tmp_path / "w.sqlite")
    sink = FakeSink()
    app = create_app(
        store,
        sink,
        WebSettings(secret_key="k", admin_user="", admin_password="", data_dir=tmp_path),
        FakeRunner(store, sink),
    )
    r = TestClient(app, follow_redirects=False).get("/setup/folders")
    assert r.status_code == 503
