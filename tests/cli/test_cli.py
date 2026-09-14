import json

import pytest
from types import SimpleNamespace as NS

from boxbutler.cli.main import main
from boxbutler.main import OrchestratorRunner
from boxbutler.runlock import RunInProgress, RunLock
from boxbutler.sinks.protocol import LiveChapter


@pytest.fixture
def factory(world, tmp_path):
    d = world["deps"]
    deps = NS(
        store=d.store,
        sink=d.sink,
        orchestrator=world["orch"],
        runner=None,
        settings=d.settings,
        snapshot_dir=d.snapshot_dir,
        cache_dir=d.cache_dir,
        # `OrchestratorRunner` reads these two as well: `config=None` means
        # "no /media sync configured", and `run_lock` is the cross-process
        # lock the CLI now takes along with every other entry point (final
        # safety review, M1).
        config=None,
        run_lock=RunLock(tmp_path / "run.lock"),
    )
    deps.runner = OrchestratorRunner(deps)
    return lambda: deps


def test_run_is_dry_by_default(factory, world, capsys):
    assert main(["run"], deps_factory=factory) == 0
    assert [c[0] for c in world["sink"].calls if c[0] in ("clear", "upload")] == []
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert any(l["event"] == "dry_run_plan" for l in lines)


def test_run_apply_writes(factory, world):
    assert main(["run", "--apply"], deps_factory=factory) == 0
    assert len(world["sink"].calls_named("upload")) == 1


def test_run_apply_exit_1_when_degraded(factory, world):
    world["sink"].fail_upload_times = 99
    assert main(["run", "--apply"], deps_factory=factory) == 1


def test_run_filters_by_assignment_name(factory, world, store):
    world["sink"].add_target("T2", "Blue", [])
    b = store.assignments.upsert_target("fake", "T2", "Blue")
    store.assignments.assign_library(b.id, world["lib"].id)
    main(["run", "--assignment", "Blue", "--apply"], deps_factory=factory)
    assert {c[1] for c in world["sink"].calls_named("upload")} == {"T2"}


def test_status_lists_targets(factory, capsys):
    assert main(["status"], deps_factory=factory) == 0
    assert "Green Tonie" in capsys.readouterr().out


def test_status_shows_a_paused_assignment_as_paused_not_ok(factory, world, capsys):
    """The dashboard was fixed to show PAUSED; `status` is the other channel an
    operator checks, and it printed the raw state -- so a paused tonie, which
    the orchestrator skips forever, still read as "OK". Showing OK there says
    bedtime is covered when nothing will ever run again.
    """
    store = world["store"]
    a = store.assignments.list()[0]
    store.assignments.set_enabled(a.id, False)

    assert main(["status"], deps_factory=factory) == 0
    out = capsys.readouterr().out
    assert "PAUSED" in out
    # Not merely "PAUSED appears somewhere": the row for this tonie must not
    # still claim OK.
    row = next(line for line in out.splitlines() if line.startswith(a.target_name))
    assert "OK" not in row


def test_status_shows_an_assignment_with_no_library_as_unmanaged_not_ok(factory, world, capsys):
    """Reproduced live: `library import` (Task 33) created assignment rows
    with library_id=None, and `status` printed them as "OK" -- while the
    orchestrator, correctly, emitted `unmanaged` for the very same
    assignments in the same run. Before the row existed at all, `status`
    said UNMANAGED (the no-assignment path); the row's mere existence made
    the display *worse*, claiming a tonie was fine when nothing will ever
    manage it.
    """
    store = world["store"]
    a = store.assignments.list()[0]
    store.assignments.assign_library(a.id, None)

    assert main(["status"], deps_factory=factory) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith(a.target_name))
    assert "UNMANAGED" in row
    assert "OK" not in row


def test_status_next_column_truncates_long_titles_and_keeps_columns_aligned(factory, world, store, capsys):
    """Real output: NEXT joined the full titles of all three upcoming items
    and one row ran past 400 characters, unreadable without `cut -c1-120`.
    A fixed, sane column width (with ellipsis) must keep every row narrow
    and every later column starting at the same offset, while still
    showing as much of the *first* upcoming title as possible -- that one
    matters more to the operator than the third.
    """
    lib = world["lib"]
    long_title = "Story " + ("Z" * 200)
    store.items.set_title(world["items"][0].id, long_title)
    world["deps"].settings.prefetch_depth = 3

    assert main(["status"], deps_factory=factory) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    row = next(line for line in lines if line.startswith("Green Tonie"))

    assert long_title not in row, "the full 200+ char title must not appear verbatim"
    assert "Story " in row, "the first upcoming title's content must still be visible"
    assert "..." in row, "a truncated title must be marked with an ellipsis"

    # Columns must still line up: every row is the same length as the header.
    header_line = lines[0]
    assert len(row) == len(header_line)
    for line in lines[1:]:
        assert len(line) == len(header_line)


def test_prefetch_touches_no_tonie(factory, world):
    main(["prefetch", "--depth", "2"], deps_factory=factory)
    assert [c[0] for c in world["sink"].calls if c[0] in ("clear", "upload")] == []
    assert world["fetcher"].calls


def test_snapshot_writes_files_only(factory, world):
    main(["snapshot"], deps_factory=factory)
    assert len(list(world["snaps"].glob("*.json"))) == 1
    # `list_targets` (enumeration) and `read_chapters` (the live read this
    # command now takes per target) are both non-mutating reads; nothing
    # that could touch a tonie -- `clear`, `upload`, `settle` -- may appear.
    assert [c[0] for c in world["sink"].calls if c[0] not in ("list_targets", "read_chapters")] == []


def test_snapshot_records_live_chapters_not_local_record(factory, world):
    """The regression this project shipped on its first live deployment:
    an UNMANAGED target (no assignment, therefore no `chapter_record`)
    that genuinely holds chapters must not be snapshotted as empty just
    because the local store has never heard of it."""
    world["sink"].add_target(
        "T2", "Unmanaged Tonie", [LiveChapter("live1", "Held Story", 5340.0, False)]
    )
    assert main(["snapshot"], deps_factory=factory) == 0

    files = sorted(world["snaps"].glob("*.json"))
    assert len(files) == 2
    bodies = [json.loads(p.read_text()) for p in files]
    unmanaged = next(b for b in bodies if b["target"]["id"] == "T2")
    assert unmanaged["chapters"] == [
        {"id": "live1", "title": "Held Story", "seconds": 5340.0, "transcoding": False}
    ]
    assert unmanaged["source"] == "live"


def test_snapshot_fails_loudly_when_live_read_fails(factory, world, monkeypatch):
    """A live read that cannot be completed must abort the whole command
    with a failure exit code, never fall back to writing an empty (or any
    other guessed) chapter list."""

    def boom(target):
        raise RuntimeError("simulated cloud outage")

    monkeypatch.setattr(world["sink"], "read_chapters", boom)
    assert main(["snapshot"], deps_factory=factory) == 1
    assert list(world["snaps"].glob("*.json")) == []


def test_cache_evict_requires_apply(factory, world, capsys):
    main(["run", "--apply"], deps_factory=factory)
    before = sorted(p.name for p in world["deps"].cache_dir.iterdir())
    main(["cache", "evict"], deps_factory=factory)
    assert sorted(p.name for p in world["deps"].cache_dir.iterdir()) == before


def test_library_export_import_round_trip(factory, tmp_path):
    out = tmp_path / "lib.json"
    assert main(["library", "export", "-o", str(out)], deps_factory=factory) == 0
    assert main(["library", "import", str(out)], deps_factory=factory) == 0


def test_library_import_dry_run_writes_nothing(factory, world, tmp_path):
    """Task 27 review, Critical 1: `library import` had no dry-run gate at
    all — a plain invocation silently created a new library. Pin this the
    way the review says actually pins it: assert the store is unchanged
    (library/item counts), not merely that some text was printed."""
    store = world["store"]
    doc = tmp_path / "newlib.json"
    doc.write_text(json.dumps({
        "version": 1,
        "libraries": [{
            "name": "NewLib",
            "mode": "serial",
            "folder_path": None,
            "items": [],
        }],
        "assignments": [],
    }))

    libs_before = {l.name for l in store.libraries.list()}
    assert main(["library", "import", str(doc)], deps_factory=factory) == 0
    assert {l.name for l in store.libraries.list()} == libs_before
    assert "NewLib" not in libs_before


def test_library_import_apply_writes(factory, world, tmp_path):
    store = world["store"]
    doc = tmp_path / "newlib.json"
    doc.write_text(json.dumps({
        "version": 1,
        "libraries": [{
            "name": "NewLib",
            "mode": "serial",
            "folder_path": None,
            "items": [],
        }],
        "assignments": [],
    }))

    assert main(["library", "import", str(doc), "--apply"], deps_factory=factory) == 0
    assert any(l.name == "NewLib" for l in store.libraries.list())


def test_cache_evict_dry_run_line_goes_through_log_event(factory, world, capsys):
    """Task 27 review, Major finding: the dry-run line used to be a
    hand-rolled print(json.dumps(...)) with no `ts` and no redaction
    pass. It must now be indistinguishable in shape from any other
    log_event line."""
    main(["run", "--apply"], deps_factory=factory)
    capsys.readouterr()
    main(["cache", "evict"], deps_factory=factory)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    plan_lines = [l for l in lines if l["event"] == "dry_run_plan"]
    assert plan_lines
    assert "ts" in plan_lines[0]
    assert list(plan_lines[0])[:2] == ["ts", "event"]


def test_run_dry_run_exit_0_on_healthy_assignment(factory):
    assert main(["run"], deps_factory=factory) == 0


def test_run_dry_run_exit_0_on_misconfigured_empty_library(factory, world, store):
    """Task 27 review, Critical 2 (confirmed empirically): a plain
    `boxbutler run` (no --apply) against an assignment with an empty
    library used to exit 1, indistinguishable from a real failed --apply
    run. A dry run merely notices a pre-existing problem; it must exit 0.
    """
    # Clear the assignment's library so planning fails with EMPTY_LIBRARY.
    store.assignments.assign_library(world["a"].id, None)
    assert main(["run"], deps_factory=factory) == 0


def test_unknown_assignment_is_usage_error(factory):
    assert main(["run", "--assignment", "Nope"], deps_factory=factory) == 2


def test_cli_run_refuses_while_another_process_holds_the_run_lock(factory, world, capsys):
    """M1: the documented remediation can no longer produce a second CLEAR.

    `monitoring/alerts.yml` tells the operator to run `boxbutler run --apply`
    "now if bedtime is near" -- i.e. at the hour the scheduler fires -- and
    the CLI is a separate process, so the in-process `threading.Lock` could
    not see it. The reviewer measured two CLEARs on one tonie and 10790 s
    against a 5400 s cap. Now the CLI goes through the same
    `OrchestratorRunner`, takes the same `flock`, and says so.
    """
    deps = factory()
    with RunLock(deps.run_lock.path).held(timeout=0.0):      # stand in for the scheduled run
        assert main(["run", "--apply"], deps_factory=factory) == 1
    err = capsys.readouterr().err
    assert "already in progress in another process" in err
    assert "not starting a second one" in err
    assert [c[0] for c in world["sink"].calls if c[0] in ("clear", "upload")] == []


def test_cli_run_takes_the_lock_so_a_concurrent_one_cannot_start(factory, world):
    """The other direction: while the CLI runs, nothing else may clear."""
    deps = factory()
    sink = world["sink"]
    real_clear = sink.clear
    observed = {}

    def clear_and_probe(target):
        # Mid-swap, from "another process": the lock must already be held.
        try:
            with RunLock(deps.run_lock.path).held(timeout=0.0):
                observed["second_run_possible"] = True
        except RunInProgress:
            observed["second_run_possible"] = False
        return real_clear(target)

    sink.clear = clear_and_probe
    assert main(["run", "--apply"], deps_factory=factory) == 0
    assert observed == {"second_run_possible": False}
