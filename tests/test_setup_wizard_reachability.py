"""The setup wizard must be genuinely reachable on a fresh install (final
coherence review, Critical-1).

Before this fix, `boxbutler.config.load_settings` required
`BOXBUTLER_ADMIN_USER`/`BOXBUTLER_ADMIN_PASSWORD` at startup — so
`ensure_admin` always had both by the time the process could serve a
request, `store.settings.get("admin_user")` was never `None`, and
`boxbutler.web.app`'s setup-gate middleware could never redirect to
`/setup`. Task 36's three-step wizard (`boxbutler/web/routes/setup.py`)
existed, was fully tested against a synthetic app state, and was
completely unreachable from the real composition root — the exact
"config file to write" experience spec §8.1 and the README both promise
the wizard replaces.

These tests run the real `boxbutler.main.build()`/`create_web_app()`
against a `FakeSink` (no network, no cloud, no ffmpeg) and pin both
halves: no admin configured -> the wizard is genuinely reachable and
completing it produces a working login; admin configured -> the wizard
stays closed and `POST /setup` still refuses (the account-takeover gate
this fix must not weaken).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from boxbutler.config import load_settings
from boxbutler.main import build, create_web_app

BASE_ENV = {
    "BOXBUTLER_SINK_USER": "u",
    "BOXBUTLER_SINK_PASSWORD": "p",
    "BOXBUTLER_SECRET_KEY": "k",
    "BOXBUTLER_SINK_KIND": "fake",
}


def _build(tmp_path, **extra_env):
    settings = load_settings(
        None,
        {
            **BASE_ENV,
            **extra_env,
            "BOXBUTLER_DATA_DIR": str(tmp_path / "data"),
            "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
        },
    )
    return build(settings)


def test_no_admin_env_vars_does_not_crash_startup(tmp_path):
    """`load_settings`/`build()` must succeed with neither admin var set --
    this is the "empty /data, no env vars" fresh-install case the wizard
    exists for. Before the fix, `load_settings` itself raised `ConfigError`
    here, so the process never even reached `build()`.
    """
    deps = _build(tmp_path)
    assert deps.store.settings.get("admin_user") is None


def test_fresh_install_serves_the_wizard_not_a_redirect_to_login(tmp_path):
    deps = _build(tmp_path)
    app = create_web_app(deps)
    c = TestClient(app, follow_redirects=False)

    r = c.get("/setup")
    assert r.status_code == 200
    assert "step" in r.text.lower() or "tonie" in r.text.lower()

    # Every other route bounces to /setup rather than /login -- there is
    # no admin account yet to log in as.
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_completing_the_wizard_on_a_fresh_install_creates_a_working_login(tmp_path):
    """The end-to-end promise: no env vars, no config file -- sign in
    through three POSTs and come out with a real admin account.
    """
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    app = create_web_app(deps)
    app.state.ingest = lambda library_id, ref: []  # keep the network out
    c = TestClient(app, follow_redirects=False)

    assert c.get("/setup").status_code == 200

    c.post("/setup", data={
        "step": "1", "tonie_username": "parent@example.invalid", "tonie_password": "pw",
    })
    c.post("/setup", data={
        "step": "2", "tonie_username": "parent@example.invalid",
        "library_name": "Bedtime", "source_ref": "https://example.invalid/feed.xml",
    })
    r = c.post("/setup", data={
        "step": "3", "tonie_username": "parent@example.invalid",
        "library_name": "Bedtime", "source_ref": "https://example.invalid/feed.xml",
        "target_ids": ["T1"],
        "admin_username": "newadmin", "admin_password": "correct horse battery",
        "admin_password_confirm": "correct horse battery",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    # The wizard's own account, not a pre-seeded one, now works.
    assert deps.store.settings.get("admin_user") == "newadmin"
    r = c.post("/login", data={"username": "newadmin", "password": "correct horse battery"})
    assert r.status_code in (200, 303)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_admin_configured_at_startup_skips_the_wizard_exactly_as_before(tmp_path):
    """An existing deployment (both env vars set) must not suddenly be
    asked to run the wizard -- `ensure_admin` still bootstraps from the
    env vars, and `/setup` still redirects away immediately.
    """
    deps = _build(
        tmp_path, BOXBUTLER_ADMIN_USER="admin", BOXBUTLER_ADMIN_PASSWORD="correct horse",
    )
    app = create_web_app(deps)  # ensure_admin() runs here, bootstrapping from the env vars
    assert deps.store.settings.get("admin_user") == "admin"
    c = TestClient(app, follow_redirects=False)

    r = c.get("/setup")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_post_setup_still_refuses_once_an_admin_exists_account_takeover_gate(tmp_path):
    """The security property that must not be weakened (final coherence
    review): once an admin exists, `POST /setup` returns 303 before
    parsing the form at all -- it cannot be replayed to create a second
    admin account or otherwise be used for takeover.
    """
    deps = _build(
        tmp_path, BOXBUTLER_ADMIN_USER="admin", BOXBUTLER_ADMIN_PASSWORD="correct horse",
    )
    app = create_web_app(deps)
    c = TestClient(app, follow_redirects=False)

    r = c.post("/setup", data={
        "step": "3", "tonie_username": "attacker@example.invalid",
        "library_name": "x", "source_ref": "",
        "admin_username": "attacker", "admin_password": "takeover1234",
        "admin_password_confirm": "takeover1234",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # The original admin is untouched and no attacker account was created.
    assert deps.store.settings.get("admin_user") == "admin"
    c.post("/login", data={"username": "attacker", "password": "takeover1234"})
    r2 = c.get("/", follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"].startswith("/login")  # never actually logged in
