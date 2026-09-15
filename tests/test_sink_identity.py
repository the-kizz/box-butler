"""One sink identity, end to end (final safety review, C1).

The defect these tests exist to make impossible: the web layer keyed every
assignment row under the constant `"fake"` (imported from
`boxbutler/web/fake_data.py`) while `boxbutler/main.py` built `"tonies_cloud"`
and handed it to the orchestrator. Two values that had to agree, with nothing
keeping them in agreement — the same shape as the two diverging
`RotationSettings` copies fixed earlier in this build.

With the real sink configured, the consequences were total and silent: the
operator finished the setup wizard, the assignment was written under
`sink='fake'`, every run upserted a *second* row under `sink='tonies_cloud'`
with no library, reported it `UNMANAGED` — which is not a failure, so no
notification fired — and stopped. `BoxButlerRunStale` is gated on a
`last_success_timestamp > 0` that could never become true. Box Butler did
nothing at all, forever, out of the box.

Every web test missed it because they all build a `FakeSink`, where `"fake"`
happens to be the right answer. So these tests run the **real composition
root** with `sink_kind="tonies_cloud"` against a sink that is a `FakeSink`
wearing the real sink's `name` — no network, no cloud, no tonie — and assert
the tonie the UI configured is genuinely processed: cleared, uploaded,
settled, committed. Re-hardcode either constant and these go red, because the
row the UI writes stops being the row `run()` finds.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxbutler.config import load_settings
from boxbutler.domain.models import ItemKind, LibraryMode, RunTrigger
from boxbutler.sinks.fake import FakeSink
from boxbutler.sinks.protocol import LiveChapter
from tests.audio.fakes import FakeRenderer
from tests.conftest import CAP, rendition_cache_name
from tests.fetch.fakes import FakeFetcher

ENV = {
    "BOXBUTLER_SINK_USER": "u",
    "BOXBUTLER_SINK_PASSWORD": "p",
    "BOXBUTLER_SECRET_KEY": "k",
    "BOXBUTLER_SINK_KIND": "tonies_cloud",
}

TARGET_ID = "ct-real-1"


class CloudNamedFakeSink(FakeSink):
    """A `FakeSink` that calls itself what the real sink calls itself.

    The point of the whole test: nothing here touches the network or
    `tonie_api`, but the sink's `name` is `"tonies_cloud"`, so every layer
    that has its own idea of the sink's identity is caught disagreeing.
    """

    name = "tonies_cloud"


def _build(tmp_path, monkeypatch, **extra_env):
    """The real `boxbutler.main.build()`, with `ToniesCloudSink.from_env`
    replaced so no credential, socket or cloud account is involved."""
    import boxbutler.main as main_mod

    media_root = tmp_path / "media"
    media_root.mkdir(exist_ok=True)
    sink = CloudNamedFakeSink()
    sink.add_target(TARGET_ID, "Green Tonie", [LiveChapter("old1", "Last Night", 5340.0, False)])
    monkeypatch.setattr(
        main_mod.ToniesCloudSink, "from_env", classmethod(lambda cls, u, p: sink)
    )
    settings = load_settings(
        None,
        {
            **ENV,
            **extra_env,
            "BOXBUTLER_DATA_DIR": str(tmp_path / "data"),
            "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
            "BOXBUTLER_MEDIA_ROOT": str(media_root),
        },
    )
    deps = main_mod.build(settings)
    assert deps.sink is sink
    return deps


def _stageable_library(deps, tmp_path, title="Bedtime Story"):
    """One library with one item the fake fetcher/renderer can stage, so an
    `--apply` run really does reach CLEAR/UPLOAD/SETTLE/COMMIT."""
    store = deps.store
    lib = store.libraries.create("Bedtime", LibraryMode.SINGLE)
    item = store.items.add(lib.id, ItemKind.URL, "https://example.invalid/a.m4a", "src-a", title)
    src = tmp_path / "src-a.m4a"
    src.write_bytes(b"aac" * 100)
    orch_deps = deps.orchestrator.deps
    orch_deps.fetcher = FakeFetcher(files={item.id: src})
    orch_deps.renderer = FakeRenderer(durations={rendition_cache_name(item): float(CAP)})
    orch_deps.sleep = lambda s: None
    return lib, item


def _login(app) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "admin", "password": "correct horse"})
    return c


def test_assignment_created_by_the_dashboard_is_the_one_a_real_run_processes(
    tmp_path, monkeypatch
):
    """The dashboard's own row, its own Apply button, and a real swap.

    Before the fix this exact sequence made **zero** sink calls, left the
    tonie holding last night's chapter, created a second orphan assignment
    row, and recorded the run `OK` with no events at all.
    """
    deps = _build(
        tmp_path, monkeypatch,
        BOXBUTLER_ADMIN_USER="admin", BOXBUTLER_ADMIN_PASSWORD="correct horse",
    )
    import boxbutler.main as main_mod

    lib, item = _stageable_library(deps, tmp_path)
    c = _login(main_mod.create_web_app(deps))

    c.get("/")                                   # the UI creates the assignment row
    rows = deps.store.assignments.list()
    assert len(rows) == 1, "the dashboard must not create a row a run cannot find"
    assert rows[0].sink == deps.sink.name == "tonies_cloud"

    c.post(f"/assignments/{rows[0].id}/library", data={"library_id": lib.id})
    r = c.post(f"/assignments/{rows[0].id}/run", data={"mode": "apply"})
    assert r.status_code in (200, 303)

    # Actually processed: the tonie was cleared and reloaded, not reported
    # UNMANAGED and skipped.
    sink = deps.sink
    assert [call[0] for call in sink.calls if call[0] in ("clear", "upload", "settle")] == [
        "clear", "upload", "settle",
    ]
    assert [ch.title for ch in sink.chapters[TARGET_ID]] == ["Bedtime Story"]

    run = deps.store.runs.list(1)[0]
    assert run.outcome == "OK"
    events = [e.event for e in deps.store.runs.events(run.id)]
    assert "unmanaged" not in events
    assert {"clear", "upload", "settle", "commit"} <= set(events)

    # And no second, library-less row was invented under another sink name.
    assert len(deps.store.assignments.list()) == 1
    assert deps.store.chapters.for_assignment(rows[0].id), "COMMIT recorded nothing"


def test_assignment_created_by_the_setup_wizard_is_the_one_a_real_run_processes(
    tmp_path, monkeypatch
):
    """The wizard's row too — the only row a fresh install has.

    The wizard is where a real operator's first (and often only) assignment
    comes from, so it is the path on which C1 meant "Box Butler silently does
    nothing out of the box".
    """
    deps = _build(
        tmp_path, monkeypatch,
        BOXBUTLER_ADMIN_USER="admin", BOXBUTLER_ADMIN_PASSWORD="correct horse",
    )
    import boxbutler.main as main_mod

    app = main_mod.create_web_app(deps)
    # `load_settings` requires the admin env vars, and `ensure_admin` writes
    # the account from them — which closes the setup gate. Reopen it, so this
    # test walks the real wizard on the real composition root: the state a
    # container started against an empty /data is actually in.
    deps.store.settings.set("admin_user", None)
    lib_holder: dict[str, object] = {}

    def fake_ingest(library_id, ref):
        # Stand in for the real `Ingestor.add_ref` only to keep the network
        # out of the test: one item in the library the wizard just created.
        item = deps.store.items.add(
            library_id, ItemKind.URL, "https://example.invalid/a.m4a", "src-a", "Bedtime Story"
        )
        src = tmp_path / "src-a.m4a"
        src.write_bytes(b"aac" * 100)
        orch_deps = deps.orchestrator.deps
        orch_deps.fetcher = FakeFetcher(files={item.id: src})
        orch_deps.renderer = FakeRenderer(durations={rendition_cache_name(item): float(CAP)})
        orch_deps.sleep = lambda s: None
        lib_holder["item"] = item
        return [item]

    app.state.ingest = fake_ingest
    c = TestClient(app, follow_redirects=False)

    assert c.get("/setup").status_code == 200
    c.post("/setup", data={"step": "1", "tonie_username": "parent@example.invalid",
                           "tonie_password": "pw"})
    c.post("/setup", data={"step": "2", "tonie_username": "parent@example.invalid",
                           "library_name": "Bedtime",
                           "source_ref": "https://example.invalid/feed.xml"})
    r = c.post("/setup", data={
        "step": "3", "tonie_username": "parent@example.invalid",
        "library_name": "Bedtime", "source_ref": "https://example.invalid/feed.xml",
        "target_ids": [TARGET_ID],
        "admin_username": "admin", "admin_password": "correct horse",
        "admin_password_confirm": "correct horse",
    })
    assert r.status_code == 303

    # The row the wizard wrote is the row the orchestrator looks up.
    a = deps.store.assignments.get_by_target(deps.sink.name, TARGET_ID)
    assert a is not None and a.library_id is not None
    assert lib_holder["item"] is not None

    run_id = deps.runner.run(assignment_ids=None, apply=True, trigger=RunTrigger.MANUAL)
    assert deps.store.runs.get(run_id).outcome == "OK"
    events = [e.event for e in deps.store.runs.events(run_id)]
    assert "unmanaged" not in events, "the wizard's own tonie reported UNMANAGED"
    assert {"clear", "upload", "settle", "commit"} <= set(events)
    assert [ch.title for ch in deps.sink.chapters[TARGET_ID]] == ["Bedtime Story"]


def test_one_sink_name_no_second_copy_anywhere(tmp_path, monkeypatch):
    """Identity is derived, not repeated: there is nothing left to diverge.

    A grep-style guard alongside the behavioural tests above, because the
    behaviour was correct for a `FakeSink` either way — what was wrong was
    that the value existed twice.
    """
    deps = _build(
        tmp_path, monkeypatch,
        BOXBUTLER_ADMIN_USER="admin", BOXBUTLER_ADMIN_PASSWORD="correct horse",
    )
    assert deps.orchestrator.deps.sink_name == deps.sink.name
    # Not a settable field any more: an assignment like
    # `Deps(sink_name="fake")` used to be how the two copies diverged.
    with pytest.raises(AttributeError):
        deps.orchestrator.deps.sink_name = "fake"

    # And no caller passes a literal sink name: every `upsert_target` /
    # `get_by_target` in the application reads it from the sink (directly, or
    # via `Deps.sink_name`). A literal here is precisely how C1 happened.
    literal = re.compile(r"(?:upsert_target|get_by_target)\(\s*[\"\']")
    offenders = [
        str(path)
        for path in Path("boxbutler").rglob("*.py")
        if path.name != "fake_data.py"          # the seeded screen-review data
        and literal.search(path.read_text())
    ]
    assert offenders == [], f"a sink name is hardcoded at a call site: {offenders}"
