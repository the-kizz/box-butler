"""Web test fixtures (Task 8, updated Task 9).

`FakeSink` and `FakeRunner` are now the real implementations from
`boxbutler.sinks.fake` and `boxbutler.web.fake_data` (Task 9's placeholder
stand-ins have been removed). The `seeded` fixture seeds fake dashboard
data via `boxbutler.web.fake_data.seed_fake`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boxbutler.sinks.fake import FakeSink
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner, seed_fake
from boxbutler.web.settings import WebSettings


@pytest.fixture
def settings(tmp_path):
    return WebSettings(
        secret_key="test-secret",
        admin_user="admin",
        admin_password="correct horse",
        data_dir=tmp_path,
    )


@pytest.fixture
def sink():
    return FakeSink()


@pytest.fixture
def media_root(tmp_path):
    """Every library is a folder now, and every folder lives under the
    media root — so the web app always has one, exactly as a real install
    does (`/media` is required, not optional)."""
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def app(store, sink, settings, media_root):
    return create_app(store, sink, settings, FakeRunner(store, sink), media_root=media_root)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def auth(client):
    r = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert r.status_code == 303
    return client


@pytest.fixture
def seeded(store, sink, auth, media_root):
    # Seeded libraries get real folders under the media root — every
    # library is a folder now, and the library page scans its own on open.
    seed_fake(store, sink, media_root=media_root)
    return auth
