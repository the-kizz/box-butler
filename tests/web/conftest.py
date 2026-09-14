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
def app(store, sink, settings):
    return create_app(store, sink, settings, FakeRunner(store, sink))


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def auth(client):
    r = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert r.status_code == 303
    return client


@pytest.fixture
def seeded(store, sink, auth):
    seed_fake(store, sink)
    return auth
