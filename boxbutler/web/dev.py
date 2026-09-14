"""Local review entrypoint for the dashboard (Task 9).

`uvicorn boxbutler.web.dev:app --reload` — seeds a throwaway SQLite store
under a tempdir with `boxbutler.web.fake_data.seed_fake` against a real
`FakeSink`, then serves the app so a human can eyeball `/` at 375px width
in a browser. Never imports a real sink — this module exists so nothing on
a screen can touch a real tonie during design review.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from boxbutler.sinks.fake import FakeSink
from boxbutler.store.db import Store
from boxbutler.web.app import create_app
from boxbutler.web.fake_data import FakeRunner, seed_fake
from boxbutler.web.settings import WebSettings

_tmp_dir = Path(tempfile.mkdtemp(prefix="boxbutler-dev-"))

_store = Store.open(_tmp_dir / "dev.sqlite")
_sink = FakeSink()
seed_fake(_store, _sink)

_settings = WebSettings(
    secret_key="dev",
    admin_user="admin",
    admin_password="admin",
    data_dir=_tmp_dir,
)

app = create_app(_store, _sink, _settings, FakeRunner(_store, _sink))
