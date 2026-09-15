import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxbutler.config import load_settings
from boxbutler.domain.models import RunTrigger
from boxbutler.main import AppDeps, OrchestratorRunner, build, create_web_app
from boxbutler.notify.ntfy import NtfyNotifier
from boxbutler.runlock import RunLock
from boxbutler.sources.ingest import Ingestor
from boxbutler.web.fake_data import FakeRunner

ENV = {"BOXBUTLER_SINK_USER": "u", "BOXBUTLER_SINK_PASSWORD": "p", "BOXBUTLER_ADMIN_USER": "admin", "BOXBUTLER_ADMIN_PASSWORD": "correct horse", "BOXBUTLER_SECRET_KEY": "k", "BOXBUTLER_SINK_KIND": "fake"}


def _build(tmp_path, **extra_env):
    # `/media` is required now (a library *is* a folder), so a built
    # process needs a real one -- exactly as a real install does.
    media_root = tmp_path / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    s = load_settings(None, {
        **ENV, **extra_env,
        "BOXBUTLER_DATA_DIR": str(tmp_path / "data"),
        "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
        "BOXBUTLER_MEDIA_ROOT": str(media_root),
    })
    return build(s)


def test_real_store_real_orchestrator_behind_the_ui(tmp_path):
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    app = create_web_app(deps)
    # Task 29 review, MAJOR-5: the original three assertions below pass
    # unchanged with a `FakeRunner` substituted for `deps.runner` — none
    # of them touch the runner at all. Pin the runner itself: identity
    # and type, so reverting to `FakeRunner` (deferred wiring 3) fails
    # this test directly rather than needing a hand-written probe to
    # notice.
    assert app.state.runner is deps.runner
    assert isinstance(app.state.runner, OrchestratorRunner)
    assert not isinstance(app.state.runner, FakeRunner)
    assert app.state.sink is deps.sink

    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "admin", "password": "correct horse"})
    assert "Green Tonie" in c.get("/").text and "Unmanaged" in c.get("/").text          # real store, real orchestrator listing real (fake-sink) targets
    assert (tmp_path / "data" / "boxbutler.sqlite").exists()
    assert c.get("/healthz").json()["status"] == "ok"


def test_ui_run_now_apply_goes_through_the_orchestrator(tmp_path):
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    c = TestClient(create_web_app(deps), follow_redirects=False)
    c.post("/login", data={"username": "admin", "password": "correct horse"})
    c.get("/")                                                     # dashboard upserts the sink's targets
    aid = deps.store.assignments.list()[0].id

    r = c.post(f"/assignments/{aid}/run", data={"mode": "dry_run"})
    assert r.status_code in (200, 303)
    run = deps.store.runs.list(1)[0]
    assert run.dry_run and run.trigger == "manual"
    assert deps.sink.calls_named("upload") == []          # dry run writes nothing

    # Task 29 review, B3: the brief's own sketch of this test is named
    # "apply" but only ever posted mode=dry_run and never checked the
    # response status — it could not distinguish a real apply from a dry
    # run. Exercise apply=True for real: the assignment has no library
    # (fake sink target with nothing assigned), so no upload happens
    # either way, but `run.dry_run` flipping to `False` in the *real*
    # store is only possible if `apply=True` genuinely reached the real
    # orchestrator's `run()` -- a substituted runner/store could not
    # produce this without also being wired identically.
    r = c.post(f"/assignments/{aid}/run", data={"mode": "apply"})
    assert r.status_code in (200, 303)
    run = deps.store.runs.list(1)[0]
    assert not run.dry_run and run.trigger == "manual"


def test_media_root_synced_on_both_manual_and_scheduled_runs(tmp_path):
    """Task 38 review, Critical-1: `run_scheduled()` used to skip
    `_sync_media_root()` entirely -- only `run()` called it, so the
    nightly scheduled rotation (the primary "on a schedule" trigger)
    never picked up new files under `/media`. The report that shipped
    this claimed `run_scheduled` "delegates to this same method" as
    `run()`; that was false and is exactly what this test would have
    caught. Per the review's note that `run_scheduled` was itself
    introduced by an earlier fix as a *second* entry point -- precisely
    what invalidated the original single-method assumption -- this pins
    both call paths with a spy, not one call into whichever private
    method currently happens to do the syncing.
    """
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    create_web_app(deps)
    deps.store.assignments.upsert_target("fake", "T1", "Green Tonie")
    runner = deps.runner
    assert isinstance(runner, OrchestratorRunner)

    calls = []
    runner._sync_media_root = lambda: calls.append(True)

    aid = deps.store.assignments.list()[0].id
    runner.run(assignment_ids=[aid], apply=False, trigger=RunTrigger.MANUAL)
    assert calls == [True], "manual run() must sync /media"

    runner.run_scheduled()
    assert calls == [True, True], "run_scheduled() must also sync /media"


def test_scheduled_run_waits_for_a_ui_run_instead_of_dropping_the_night(tmp_path):
    """Task 29 review, MAJOR-1: `on_fire`/`run_scheduled` must not lose
    the night's rotation to a UI run in flight -- the old `runner.run`
    path took the non-blocking lock and raised `HTTPException(409)`,
    which `Scheduler.run_forever` logs and swallows. Hold the lock (as a
    UI "Run now" would), fire the scheduled path on another thread, and
    assert it still runs once the lock frees rather than bailing
    immediately.
    """
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    create_web_app(deps)                      # upserts nothing on its own; force a target via the sink below
    deps.store.assignments.upsert_target("fake", "T1", "Green Tonie")
    runner = deps.runner
    assert isinstance(runner, OrchestratorRunner)

    runner.SCHEDULED_LOCK_TIMEOUT_S = 5.0      # keep the test fast
    released = threading.Event()
    result: dict = {}

    def hold_lock():
        runner._lock.acquire()
        released.wait(timeout=2.0)
        runner._lock.release()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        import time
        time.sleep(0.05)                       # make sure the holder actually has the lock first

        def fire():
            result["run_id"] = runner.run_scheduled()

        firer = threading.Thread(target=fire)
        firer.start()
        released.set()                          # let the holder release shortly after the scheduled run starts waiting
        firer.join(timeout=5.0)
        assert not firer.is_alive()
        assert result.get("run_id") is not None    # the scheduled run actually happened, not dropped
    finally:
        released.set()
        holder.join(timeout=2.0)


def test_scheduled_run_logs_loudly_and_never_raises_when_the_lock_never_frees(tmp_path, caplog):
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    runner = deps.runner
    runner.SCHEDULED_LOCK_TIMEOUT_S = 0.05
    runner._lock.acquire()                     # never released within the test
    try:
        with caplog.at_level("CRITICAL"):
            result = runner.run_scheduled()
        assert result is None
        assert any("scheduled run deferred" in r.message for r in caplog.records)
    finally:
        runner._lock.release()


def test_ui_run_is_409_while_another_process_holds_the_run_lock(tmp_path):
    """M1's cross-process half, from the UI's side.

    The in-process 409 already worked. This is the hole: a `docker exec …
    boxbutler run --apply` holding the lock used to be invisible to the
    runner, so a "Run now" would CLEAR the same tonie the other process was
    mid-swap on.
    """
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    c = TestClient(create_web_app(deps), follow_redirects=False)
    c.post("/login", data={"username": "admin", "password": "correct horse"})
    c.get("/")
    aid = deps.store.assignments.list()[0].id

    with RunLock(deps.run_lock.path).held(timeout=0.0):
        r = c.post(f"/assignments/{aid}/run", data={"mode": "apply"})
    assert r.status_code == 409
    assert deps.sink.calls_named("clear") == []


def test_scheduled_run_is_loud_when_another_process_never_frees_the_lock(tmp_path, caplog):
    """A missed night is a missed night whichever lock was held (M1).

    The in-process timeout already logged `critical` and pushed a
    notification; the cross-process one must do exactly the same rather than
    dropping the night with one INFO line.
    """
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    runner = deps.runner
    runner.SCHEDULED_LOCK_TIMEOUT_S = 0.05
    with RunLock(deps.run_lock.path).held(timeout=0.0):
        with caplog.at_level("CRITICAL"):
            assert runner.run_scheduled() is None
    assert any("scheduled run deferred" in r.message for r in caplog.records)
    assert deps.sink.calls_named("clear") == []


def test_startup_reconciles_a_swap_marker_left_by_a_killed_process(tmp_path):
    """C3: `build()` is where a tonie that was mid-swap when the container
    died stops being invisible.

    Two `build()`s over the same data directory stand in for a restart: the
    first writes a marker (as `swap()` does immediately before `clear()`), the
    second must find it and degrade the assignment, so repair-before-rotation
    picks it up instead of the tonie staying empty until tomorrow.
    """
    from boxbutler.domain.models import AssignmentState

    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    a = deps.store.assignments.upsert_target(deps.sink.name, "T1", "Green Tonie")
    deps.store.swap_markers.put(
        a.id, "run-killed",
        '[{"item_id": "i1", "path": "/cache/x.m4a", "title": "X", "seconds": 10.0}]',
        datetime.now(UTC),
    )
    assert deps.store.assignments.get(a.id).state == AssignmentState.OK

    restarted = _build(tmp_path)                       # same /data: the restart
    after = restarted.store.assignments.get(a.id)
    assert after.state == AssignmentState.DEGRADED
    assert "/cache/x.m4a" in after.staged_json
    assert restarted.store.swap_markers.get(a.id) is None


def test_startup_leaves_markers_alone_while_another_process_is_mid_run(tmp_path):
    """Reconciliation must never degrade an assignment out from under a live
    run in another process -- that would invent the failure it exists to
    report. Deferring costs nothing: the marker is still there next start.
    """
    from boxbutler.domain.models import AssignmentState

    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    a = deps.store.assignments.upsert_target(deps.sink.name, "T1", "Green Tonie")
    deps.store.swap_markers.put(
        a.id, "run-live",
        '[{"item_id": "i1", "path": "/cache/x.m4a", "title": "X", "seconds": 10.0}]',
        datetime.now(UTC),
    )
    with RunLock(Path(tmp_path / "data" / "run.lock")).held(timeout=0.0):
        restarted = _build(tmp_path)
    assert restarted.store.assignments.get(a.id).state == AssignmentState.OK
    assert restarted.store.swap_markers.get(a.id) is not None


def test_notifications_wiring_is_the_same_object_and_goes_live(tmp_path):
    # Deferred wiring 1: `Deps.notifier` and `orchestrator.deps.notifier`
    # must be the same object, and a live settings change must actually
    # switch which concrete `Notifier` gets used -- not merely construct
    # something.
    deps = _build(tmp_path, BOXBUTLER_NOTIFY_TOKEN="tok-abc")
    assert deps.notifier is deps.orchestrator.deps.notifier
    assert deps.notifier._current().__class__.__name__ == "NullNotifier"       # seeded notify_kind=none, no server
    deps.store.settings.set("notify_kind", "ntfy")
    deps.store.settings.set("notify_server", "https://ntfy.example.org")
    deps.store.settings.set("notify_topic", "boxbutler-alerts")
    live = deps.notifier._current()
    assert isinstance(live, NtfyNotifier)
    assert live._token == "tok-abc"


def test_rotation_window_reaches_effective_rotation_settings(tmp_path):
    # Deferred wiring 2: a Settings-screen edit must reach
    # `orchestrator.deps.settings` (and, since Task 29 review MAJOR-4,
    # the *same* object `AppDeps.settings` reads) on the very next run.
    deps = _build(tmp_path)
    deps.sink.add_target("T1", "Green Tonie", [])
    deps.store.assignments.upsert_target("fake", "T1", "Green Tonie")
    before = (deps.orchestrator.deps.settings.schedule, deps.orchestrator.deps.settings.timezone)

    deps.store.settings.set("schedule", "18:00")
    deps.store.settings.set("timezone", "Europe/Berlin")
    deps.runner.run(assignment_ids=None, apply=False, trigger=RunTrigger.MANUAL)

    after = (deps.orchestrator.deps.settings.schedule, deps.orchestrator.deps.settings.timezone)
    assert after != before
    assert after == ("18:00", "Europe/Berlin")
    # MAJOR-4: AppDeps.settings must never be a second, stale copy.
    assert deps.settings is deps.orchestrator.deps.settings


def test_ingest_hooks_are_the_real_ingestor_not_the_fake(tmp_path):
    deps = _build(tmp_path)
    app = create_web_app(deps)
    assert app.state.ingest.__self__.__class__ is Ingestor
    assert app.state.ingest.__func__.__qualname__ == "Ingestor.add_ref"
    assert app.state.ingest_upload.__self__.__class__ is Ingestor
    assert app.state.ingest_upload.__func__.__qualname__ == "Ingestor.add_upload"


# ------------------------------------------------- `/media` is required


def test_startup_fails_naming_the_missing_media_mount(tmp_path):
    """A library *is* a folder, so there is nowhere for one to live
    without `/media`. Startup fails with a message naming the missing
    mount rather than falling back to a directory inside `/data` — that
    would quietly put hours of audio in the volume the operator backs up.
    Plex, Sonarr, Immich and Paperless all refuse the same way.
    """
    from boxbutler.config import ConfigError, load_settings
    from boxbutler.main import build

    missing = tmp_path / "not-mounted"
    settings = load_settings(None, {
        **ENV,
        "BOXBUTLER_DATA_DIR": str(tmp_path / "data"),
        "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
        "BOXBUTLER_MEDIA_ROOT": str(missing),
    })

    with pytest.raises(ConfigError) as exc:
        build(settings)

    assert str(missing) in str(exc.value)
    # No fallback was invented, anywhere.
    assert not missing.exists()
    assert not (tmp_path / "data" / "media").exists()


def test_startup_gives_a_folderless_library_a_folder_without_moving_anything(tmp_path):
    """Migration: a library written before "a library is a folder" gets
    one derived from its name, created under the media root. Nothing is
    moved — a migration that relocates audio can lose it, and a cached
    copy ages out by ordinary eviction instead."""
    from pathlib import Path

    from boxbutler.config import load_settings
    from boxbutler.main import build
    from boxbutler.store.db import Store

    media_root = tmp_path / "media"
    media_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # A library with no folder, and an item already fetched into the cache.
    store = Store.open(data_dir / "boxbutler.sqlite")
    lib = store.libraries.create("Wombat Tales")
    cached = cache_dir / "Story [vid1].m4a"
    cached.write_bytes(b"already fetched")
    store.items.add(lib.id, "youtube", "u/v1", "vid1", "Story", local_path=str(cached))
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    settings = load_settings(None, {
        **ENV,
        "BOXBUTLER_DATA_DIR": str(data_dir),
        "BOXBUTLER_CACHE_DIR": str(cache_dir),
        "BOXBUTLER_MEDIA_ROOT": str(media_root),
    })
    deps = build(settings)

    migrated = deps.store.libraries.get(lib.id)
    assert migrated.folder_path == str(media_root / "Wombat Tales")
    assert Path(migrated.folder_path).is_dir()
    # Nothing moved: the cache copy is exactly where it was.
    assert cached.read_bytes() == b"already fetched"
    assert list(Path(migrated.folder_path).iterdir()) == []
