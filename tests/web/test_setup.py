"""First-run setup wizard tests (Task 36; spec §8.1, §5).

Overrides `settings` / `sink` / `app` / `client` from tests/web/conftest.py:
every other web test file bootstraps an admin account at app startup (via
`BOXBUTLER_ADMIN_USER`/`PASSWORD`) and never sees the wizard. These tests
need the opposite starting point — a store with **no** admin account yet,
which is the only thing that makes `/setup` reachable at all.

No test here touches the network, `tonie_api`, or ffmpeg: sink credential
verification and target discovery both go through `FakeSink`.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from boxbutler.sinks.fake import FakeSink
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import SINK_ID, FakeRunner
from boxbutler.web.settings import WebSettings

TONIE_USER = "parent@example.invalid"
TONIE_PASS = "sw0rdfish-example"
SOURCE_REF = "https://video.example.invalid/watch?v=abc123"


@pytest.fixture
def settings(tmp_path):
    # Deliberately no admin_user/admin_password: this is the "empty /data,
    # no env vars" case the wizard exists for.
    return WebSettings(secret_key="test-secret", data_dir=tmp_path)


@pytest.fixture
def sink():
    return FakeSink(valid_username=TONIE_USER, valid_password=TONIE_PASS)


@pytest.fixture
def app(store, sink, settings):
    return create_app(store, sink, settings, FakeRunner(store, sink))


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _step1(client, username=TONIE_USER, password=TONIE_PASS):
    return client.post(
        "/setup", data={"step": "1", "tonie_username": username, "tonie_password": password}
    )


def _step2(client, tonie_username=TONIE_USER, library_name="Bedtime", source_ref=SOURCE_REF):
    return client.post(
        "/setup",
        data={
            "step": "2",
            "tonie_username": tonie_username,
            "library_name": library_name,
            "source_ref": source_ref,
        },
    )


def _step3(
    client,
    tonie_username=TONIE_USER,
    library_name="Bedtime",
    source_ref=SOURCE_REF,
    target_ids=None,
    admin_username="admin",
    admin_password="correct horse battery",
    admin_password_confirm=None,
):
    data = {
        "step": "3",
        "tonie_username": tonie_username,
        "library_name": library_name,
        "source_ref": source_ref,
        "admin_username": admin_username,
        "admin_password": admin_password,
        "admin_password_confirm": admin_password_confirm if admin_password_confirm is not None else admin_password,
    }
    if target_ids:
        data["target_ids"] = target_ids
    return client.post("/setup", data=data)


PROTECTED = ["/", "/libraries", "/history", "/runs", "/settings", "/api/export"]


@pytest.mark.parametrize("path", PROTECTED)
def test_empty_data_redirects_to_wizard(client, path):
    r = client.get(path)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_setup_and_static_are_not_redirected(client):
    """The setup gate must not intercept `/setup` itself or the stylesheet —
    a wizard redirected to itself is a loop, and one without CSS is unusable.

    `/static/app.css` is asserted *not redirected* rather than 200: it is a
    build artefact (`make css`, gitignored) that exists in the container image
    and in a working checkout but not in a fresh clone. Asserting 200 made this
    test fail on any machine that had not run the build — which is exactly how
    it failed in CI on the first release tag, while passing locally. The
    property this test is named for is about routing, so that is what it
    checks; `make css` runs in CI and in the Dockerfile to cover the rest.
    """
    assert client.get("/setup").status_code == 200

    r = client.get("/static/app.css")
    assert r.status_code != 303, "the setup gate must not redirect static assets"
    assert r.headers.get("location") != "/setup"


def test_wizard_rejects_bad_sink_credentials(client, store, sink):
    r = _step1(client, password="wrong-password")
    assert r.status_code == 200
    assert "couldn" in r.text.lower() and "sign in" in r.text.lower()
    # Still step 1 in the response, and nothing persisted.
    assert 'name="step" value="1"' in r.text
    assert store.settings.get("admin_user") is None
    assert store.libraries.list() == []
    assert store.assignments.list() == []
    assert sink.calls_named("clear") == []
    assert sink.calls_named("upload") == []


def test_wizard_discovers_targets_without_writing(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    sink.add_target("fake-blue-02", "Blue Tonie")

    r1 = _step1(client)
    assert r1.status_code == 200

    r2 = _step2(client)
    assert r2.status_code == 200
    assert "Green Tonie" in r2.text
    assert "Blue Tonie" in r2.text

    # list_targets() is fine; clear/upload must never happen from the wizard.
    assert sink.calls_named("clear") == []
    assert sink.calls_named("upload") == []
    assert store.libraries.list() == []
    assert store.assignments.list() == []


def test_wizard_completion_is_idempotent(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    sink.add_target("fake-blue-02", "Blue Tonie")

    _step1(client)
    _step2(client)

    r1 = _step3(client, target_ids=["fake-green-01"])
    assert r1.status_code == 303
    assert r1.headers["location"] == "/"
    assert len(store.libraries.list()) == 1
    assert len(store.assignments.list()) == 1
    lib = store.libraries.list()[0]
    assert len(store.items.list(lib.id)) == 1

    # Re-POSTing the final step (double-click, retried request, etc.)
    # must not create a second library or a second assignment.
    r2 = _step3(client, target_ids=["fake-green-01"])
    assert r2.status_code == 303
    assert len(store.libraries.list()) == 1
    assert len(store.assignments.list()) == 1
    assert len(store.items.list(lib.id)) == 1


def test_wizard_unreachable_once_configured(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    _step1(client)
    _step2(client)
    _step3(client, target_ids=["fake-green-01"])

    r = client.get("/setup")
    assert r.status_code in (404, 303)
    if r.status_code == 303:
        assert r.headers["location"] == "/"

    r = client.post("/setup", data={"step": "1"})
    assert r.status_code in (404, 303)


def test_no_tonie_is_enabled_without_an_explicit_tick(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    sink.add_target("fake-blue-02", "Blue Tonie")

    _step1(client)
    _step2(client)
    _step3(client, target_ids=["fake-green-01"])

    ticked = store.assignments.get_by_target(SINK_ID, "fake-green-01")
    assert ticked is not None
    assert ticked.enabled is True
    assert ticked.library_id is not None

    unticked = store.assignments.get_by_target(SINK_ID, "fake-blue-02")
    assert unticked is None or (unticked.enabled is False and unticked.library_id is None)

    # And no upload/clear ever happened against either target.
    assert sink.calls_named("clear") == []
    assert sink.calls_named("upload") == []


def test_wizard_back_then_continue_preserves_ticks_and_admin_username(client, store, sink):
    """Full round trip (review round 1, Important 2): step 3 -> Back ->
    Continue must land back on step 3 with the ticked targets and admin
    username still in place. The admin *password* must never be
    pre-filled — it's never put in markup at all, so the round trip
    requires re-entering it, which is the point.
    """
    sink.add_target("fake-green-01", "Green Tonie")
    sink.add_target("fake-blue-02", "Blue Tonie")

    _step1(client)
    _step2(client, library_name="Bedtime", source_ref=SOURCE_REF)

    # On step 3: tick both, name an admin username, then go Back.
    r_back = client.post(
        "/setup",
        data={
            "step": "3",
            "action": "back",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
            "target_ids": ["fake-green-01", "fake-blue-02"],
            "admin_username": "parentadmin",
        },
    )
    assert r_back.status_code == 200
    assert "Bedtime" in r_back.text

    # Continue forward from step 2 again — nothing new re-ticked here,
    # relying only on what step 2's own form carries forward.
    r_forward = client.post(
        "/setup",
        data={
            "step": "2",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
            "target_ids": ["fake-green-01", "fake-blue-02"],
            "admin_username": "parentadmin",
        },
    )
    assert r_forward.status_code == 200
    assert re.search(r'value="fake-green-01"[^>]*checked', r_forward.text)
    assert re.search(r'value="fake-blue-02"[^>]*checked', r_forward.text)
    assert 'value="parentadmin"' in r_forward.text
    # The admin password input has no value= at all — never pre-filled.
    assert not re.search(r'id="admin_password"[^>]*value=', r_forward.text)

    # And finishing from here actually uses the carried-forward ticks.
    r_finish = client.post(
        "/setup",
        data={
            "step": "3",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
            "target_ids": ["fake-green-01", "fake-blue-02"],
            "admin_username": "parentadmin",
            "admin_password": "correct horse battery",
            "admin_password_confirm": "correct horse battery",
        },
    )
    assert r_finish.status_code == 303
    assert store.assignments.get_by_target(SINK_ID, "fake-green-01") is not None
    assert store.assignments.get_by_target(SINK_ID, "fake-blue-02") is not None


def test_wizard_back_preserves_earlier_step_data(client, store, sink):
    sink.add_target("fake-green-01", "Green Tonie")
    _step1(client)
    _step2(client, library_name="Bedtime", source_ref=SOURCE_REF)

    # From step 3, go back to step 2 — the library name/source entered
    # there must still be shown, not blanked out.
    r = client.post(
        "/setup",
        data={
            "step": "3",
            "action": "back",
            "tonie_username": TONIE_USER,
            "library_name": "Bedtime",
            "source_ref": SOURCE_REF,
        },
    )
    assert r.status_code == 200
    assert "Bedtime" in r.text
    assert SOURCE_REF in r.text
