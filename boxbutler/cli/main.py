"""The `boxbutler` command-line interface (Task 27; spec §6, §11 P5).

The first thing a human uses to drive Box Butler. Two properties every
subcommand here is built around:

- **Dry-run is the default everywhere; writing requires `--apply`.**
  `run`, `cache evict`, and `library import` all plan and print what they
  would do, then exit 0, unless `--apply` is given. `library import`'s
  brief (`task-27-brief.md:20`) omits `[--apply]` from its interface
  line, but that line contradicts the brief's own prose and this
  project's global constraints, both of which say every writing command
  needs it — `import_json` unconditionally creates libraries/items and
  rewrites assignment state, so it is unambiguously a writing command.
  The constraints win; the brief's interface line is defective and is
  deliberately not followed here (Task 27 review, Critical 1).
  `snapshot` writes files but never touches a tonie, so it needs no
  `--apply` at all (see its own docstring). `prefetch` only ever
  fetches/renders/verifies into the cache — it never calls the sink's
  mutating methods either — so it too needs no `--apply`.
- **Exit codes are a contract with whatever calls this**: a scheduler
  (Task 28) and an operator's shell both read them. `0` means "ran fine,
  including a dry run that found nothing to do, was told not to write,
  or merely noticed a pre-existing problem while planning"; `1` means a
  real (`--apply`) run genuinely failed or left a tonie DEGRADED; `2`
  means the CLI itself was used wrong (bad flags, an unknown
  `--assignment` name) before anything was attempted. A dry run never
  returns 1: it performs no writes, so it cannot have failed one (Task
  27 review, Critical 2).

`AppDeps` is Task 29's composition root's job (`boxbutler/main.py`); this
module only ever receives one through `deps_factory`, and never
constructs a real one itself — see the module-level note in the Task 27
brief. Every test here builds its `deps_factory` from the `world`
fixture (`tests/conftest.py`), the same fixture the orchestrator suite
already exercises the sink/store fakes against.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from boxbutler.domain.models import AssignmentState, RunTrigger
from boxbutler.domain.rotation import pin_active
from boxbutler.logging import make_logger
from boxbutler.orchestrator.prefetch import prefetch_assignment, ready_depth, upcoming_item_ids
from boxbutler.orchestrator.retention import cache_usage_bytes, evict, plan_eviction
from boxbutler.runlock import RunInProgress
from boxbutler.sinks.protocol import TargetSnapshot
from boxbutler.store.export_import import export_json, import_json


class AppDeps(Protocol):
    """What every subcommand needs (Task 29 builds the real one).

    `store` / `sink` / `settings` / `snapshot_dir` / `cache_dir` are
    exposed directly for the read-only/administrative commands
    (`status`, `snapshot`, `cache`, `library`) that have no reason to go
    through the orchestrator. `orchestrator` carries the *same* sink,
    store and settings (via its own `Deps`) plus the `sink_name`
    discriminator and the `log` hook this module wires up in `main()`.

    `runner` is the `OrchestratorRunner` — the single object that holds both
    the in-process and the cross-process run lock. `run` goes through it
    rather than calling `orchestrator.run` directly (final safety review,
    M1): this command is a *separate process* from the one serving the UI
    and the scheduler, it is the command the runbook and
    `monitoring/alerts.yml` tell an operator to run "if bedtime is near",
    and without the shared lock it could CLEAR a tonie the scheduled run was
    already mid-swap on. Going through the same runner as every other caller
    also means a future fourth entry point inherits the locking by calling
    it, instead of needing someone to remember.
    """

    store: Any
    sink: Any
    orchestrator: Any
    runner: Any
    settings: Any
    snapshot_dir: Path
    cache_dir: Path


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boxbutler")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Rotate every managed tonie that is due.")
    run_p.add_argument(
        "--assignment", action="append", default=None, metavar="NAME",
        help="Limit to the tonie(s) with this target name (repeatable, case-insensitive).",
    )
    run_p.add_argument("--apply", action="store_true", help="Actually write. Default is a dry run.")

    sub.add_parser("status", help="Show every managed tonie's current state.")

    prefetch_p = sub.add_parser("prefetch", help="Warm the cache for upcoming items. Never touches a tonie.")
    prefetch_p.add_argument(
        "--depth", type=int, default=None,
        help="Override the configured prefetch depth for this run only.",
    )

    cache_p = sub.add_parser("cache", help="Inspect or reclaim cache disk space.")
    cache_sub = cache_p.add_subparsers(dest="cache_command", required=True)
    cache_sub.add_parser("report", help="Show cache usage and what eviction would do.")
    evict_p = cache_sub.add_parser("evict", help="Reclaim cache space. Default is a dry run.")
    evict_p.add_argument("--apply", action="store_true", help="Actually delete. Default is a dry run.")

    library_p = sub.add_parser("library", help="Manage libraries.")
    library_sub = library_p.add_subparsers(dest="library_command", required=True)
    library_sub.add_parser("list", help="List every library by name.")
    show_p = library_sub.add_parser("show", help="List a library's items.")
    show_p.add_argument("name")
    export_p = library_sub.add_parser("export", help="Export every library/assignment as JSON.")
    export_p.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout.")
    import_p = library_sub.add_parser("import", help="Import libraries/assignments from a JSON file.")
    import_p.add_argument("file")
    import_p.add_argument("--replace", action="store_true", help="Replace existing data instead of merging.")
    import_p.add_argument("--apply", action="store_true", help="Actually write. Default is a dry run.")

    sub.add_parser(
        "snapshot",
        help="Write a point-in-time snapshot of every target's live chapters. Writes files; touches no tonie.",
    )

    return parser


# ----------------------------------------------------------------- utilities


def _print_json_error(message: str) -> None:
    print(f"boxbutler: error: {message}", file=sys.stderr)


def _resolve_assignment_ids(store, names: Sequence[str] | None) -> tuple[list[str] | None, str | None]:
    """`None, None` means "no filter". `None, <error>` means an unknown
    name was given. Matching is case-insensitive against `target_name`
    (never title — the brief's binding constraint on idempotency doesn't
    apply to CLI filtering, but matching on the human-facing name rather
    than a story title is still the only sane behaviour here)."""
    if not names:
        return None, None
    assignments = store.assignments.list()
    ids: list[str] = []
    for name in names:
        matches = [a for a in assignments if a.target_name.lower() == name.lower()]
        if not matches:
            return None, f"no such assignment/target: {name!r}"
        ids.extend(a.id for a in matches)
    return ids, None


def _sink_name(deps: AppDeps) -> str:
    return deps.orchestrator.deps.sink_name


# --------------------------------------------------------------- subcommands


def _cmd_run(deps: AppDeps, args: argparse.Namespace) -> int:
    ids, error = _resolve_assignment_ids(deps.store, args.assignment)
    if error is not None:
        _print_json_error(error)
        return 2
    try:
        # Through the runner, so this process takes the same locks as the UI
        # and the scheduler (M1). Fail fast rather than wait: an operator
        # wants to be told another run is already going, not watch a hung
        # terminal -- and the in-flight run is doing the same work anyway.
        run_id = deps.runner.run(
            assignment_ids=ids, apply=args.apply, trigger=RunTrigger.CLI
        )
    except RunInProgress as e:
        _print_json_error(
            f"{e}; not starting a second one. Watch the run in progress in the UI, "
            "or re-run this once it has finished."
        )
        return 1
    run = deps.store.runs.get(run_id)
    # A dry run performs no writes, so a pre-existing problem it merely
    # *notices* while planning (e.g. PLAN_FAILED/EMPTY_LIBRARY) is not "a
    # write failed" — the brief's exit-code contract groups dry-run under
    # 0 unconditionally ("0 OK or dry-run, 1 a run failed or a tonie is
    # DEGRADED"). Only under --apply does a FAILED/DEGRADED outcome mean a
    # real run attempt went wrong. Without this, Task 28's scheduler would
    # see a plain nightly dry run against any already-misconfigured
    # assignment exit 1 every night, indistinguishable from a real failure
    # (Task 27 review, Critical 2 — confirmed empirically).
    if not args.apply:
        return 0
    if run is None:
        # Unreachable: `runner.run` started and finished this row itself. If
        # it ever happens, "I could not read the outcome" is not "it worked"
        # -- this project never reports an unknown as a success.
        _print_json_error(f"run {run_id} is missing from the store; cannot report its outcome")
        return 1
    return 1 if run.outcome in ("FAILED", "DEGRADED", "CRASHED") else 0


# Fixed sane width for the NEXT column. The terminal width is not knowable
# from here, so this is a static budget rather than an attempt to fit
# whatever terminal happens to be open. Reproduced live: with three
# upcoming items' full titles joined by ", ", one row ran past 400
# characters and had to be read with `cut -c1-120` to be usable at all.
_NEXT_COL_WIDTH = 60


def _truncate(text: str, width: int) -> str:
    """Ellipsise `text` to at most `width` characters. Truncating (rather
    than wrapping or dropping columns) keeps every row's later columns
    (LAST SUCCESS, PREFETCH READY) aligned regardless of how long any
    single title is."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _cmd_status(deps: AppDeps, args: argparse.Namespace) -> int:
    store = deps.store
    sink_name = _sink_name(deps)
    depth = deps.settings.prefetch_depth

    header = ("TARGET", "STATE", "HOLDS", "NEXT", "LAST SUCCESS", "PREFETCH READY")
    rows: list[tuple[str, ...]] = []

    for target in deps.sink.list_targets():
        a = store.assignments.get_by_target(sink_name, target.id)
        if a is None:
            rows.append((target.name, "UNMANAGED", "-", "-", "-", "-"))
            continue

        holds = ", ".join(c.title for c in store.chapters.for_assignment(a.id)) or "-"

        if a.library_id is not None:
            items = store.items.list(a.library_id)
            next_titles = []
            for item_id in upcoming_item_ids(store, a, depth):
                item = store.items.get(item_id)
                if item is not None:
                    next_titles.append(item.title)
            # Truncate the joined string, not each title individually: the
            # first upcoming title is what an operator needs to see, and
            # joining-then-truncating naturally keeps as much of it as fits
            # before anything later in the string is cut.
            # Truncate the joined string, not each title individually: the
            # first upcoming title is what an operator needs to see, and
            # joining-then-truncating naturally keeps as much of it as fits
            # before anything later in the string is cut.
            nxt = _truncate(", ".join(next_titles), _NEXT_COL_WIDTH) if next_titles else "-"
            ready = f"{ready_depth(store, a, depth)}/{depth}"
        else:
            nxt = "-"
            ready = "-"

        last_success = a.last_success_at.isoformat() if a.last_success_at else "-"
        # Same precedence as the dashboard card (web/routes/dashboard.py):
        # DEGRADED outranks everything (a tonie that may be empty at bedtime
        # needs repair regardless of anything else), then PAUSED, then
        # UNMANAGED, then PINNED, then OK. Reporting the raw state here
        # showed a paused assignment as "OK" -- the orchestrator skips it
        # forever, so that told the operator bedtime was covered when
        # nothing would ever run again. The dashboard was fixed for this;
        # `status` is the other channel an operator checks, and it said the
        # same wrong thing.
        #
        # Reproduced live a second time: an assignment row with
        # library_id=None (created by `library import`, Task 33) printed
        # "OK -   -   -   -" here, while the orchestrator emitted its own
        # `unmanaged` event for the exact same assignment in the same run,
        # and before the row existed at all `status` correctly said
        # UNMANAGED. The row's mere existence made the display *worse* --
        # it now claimed a tonie was fine when nothing will ever manage it.
        #
        # FIXED (verified in a browser, dashboard fix): pinning an item
        # freezes the cursor (rotation.choose_next sets rotates=False) but
        # is a deliberate operator choice, not a health problem -- it does
        # not outrank DEGRADED/PAUSED/UNMANAGED (none of which a pin
        # implies anything about), but it must not collapse into the same
        # "OK" as a freely-rotating tonie either. `pin_active` is the exact
        # test `choose_next` itself uses, imported rather than re-derived
        # so this command can never drift out of agreement with the
        # dashboard card about the same assignment (final coherence
        # review: they already have, twice).
        #
        # Wording round: the dashboard now spells this out as a sentence
        # ("Always playing: <title>") since "Pin"/"PINNED" reads as a
        # passcode in a children's product. STATE here is a fixed-width
        # column, not a sentence, so it needs a short token instead --
        # "FIXED" (as in "fixed on one item"), not "PINNED". Internal
        # identifiers (`pinned_item_id`, `pin_active`, the `/pin` route)
        # are unaffected -- this is display text only, same as the
        # dashboard's own status label vs. its internal `status` field.
        if a.state == AssignmentState.DEGRADED:
            state = str(a.state)
        elif not a.enabled:
            state = "PAUSED"
        elif a.library_id is None:
            state = "UNMANAGED"
        elif pin_active(a, items):
            state = "FIXED"
        else:
            state = str(a.state)
        rows.append((target.name, state, holds, nxt, last_success, ready))

    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]

    def _fmt(row: tuple[str, ...]) -> str:
        return "  ".join(str(v).ljust(w) for v, w in zip(row, widths))

    print(_fmt(header))
    for row in rows:
        print(_fmt(row))
    return 0


def _cmd_prefetch(deps: AppDeps, args: argparse.Namespace) -> int:
    store = deps.store
    orch = deps.orchestrator
    sink_name = _sink_name(deps)
    depth_override = args.depth

    run = store.runs.start("cli", dry_run=False)
    try:
        for target in deps.sink.list_targets():
            a = store.assignments.get_by_target(sink_name, target.id)
            if a is None or a.library_id is None:
                continue
            depth = depth_override if depth_override is not None else orch.deps.settings.prefetch_depth
            prefetch_assignment(orch, run.id, a, depth)
        store.runs.finish(run.id, "OK")
    except Exception:
        store.runs.finish(run.id, "CRASHED")
        raise
    return 0


def _cmd_cache(deps: AppDeps, args: argparse.Namespace) -> int:
    store = deps.store
    settings = deps.settings
    if args.cache_command == "report":
        usage = cache_usage_bytes(deps.cache_dir)
        plan = plan_eviction(store, deps.cache_dir, settings.cache_budget_bytes, settings.prefetch_depth)
        print(f"cache usage: {usage} bytes (budget {settings.cache_budget_bytes} bytes)")
        print(f"would evict: {len(plan)} file(s), {sum(e.bytes for e in plan)} bytes")
        return 0
    if args.cache_command == "evict":
        plan = evict(
            store, deps.cache_dir, settings.cache_budget_bytes, settings.prefetch_depth,
            apply=args.apply,
        )
        event = "cache_evict" if args.apply else "dry_run_plan"
        # Routed through the same log_event path as every other event (Task
        # 27 review, Major finding): a hand-rolled print(json.dumps(...))
        # here had no `ts`, no flush guarantee, and — the part that
        # actually matters — never passed through _sanitise, undercutting
        # the "one central path" redaction guarantee the logging module's
        # own docstring promises.
        deps.orchestrator.deps.log(event, {
            "apply": args.apply,
            "count": len(plan),
            "bytes": sum(e.bytes for e in plan),
        })
        return 0
    _print_json_error(f"unknown cache subcommand: {args.cache_command!r}")
    return 2


def _cmd_library(deps: AppDeps, args: argparse.Namespace) -> int:
    store = deps.store
    if args.library_command == "list":
        for lib in store.libraries.list():
            print(lib.name)
        return 0
    if args.library_command == "show":
        lib = next((l for l in store.libraries.list() if l.name.lower() == args.name.lower()), None)
        if lib is None:
            _print_json_error(f"no such library: {args.name!r}")
            return 2
        for item in store.items.list(lib.id):
            print(item.title)
        return 0
    if args.library_command == "export":
        data = export_json(store)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.library_command == "import":
        try:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _print_json_error(f"could not read {args.file!r}: {exc}")
            return 2
        try:
            counts = import_json(store, data, replace=args.replace, dry_run=not args.apply)
        except NotImplementedError as exc:
            _print_json_error(str(exc))
            return 2
        event = "library_import" if args.apply else "dry_run_plan"
        deps.orchestrator.deps.log(event, {"apply": args.apply, **counts})
        return 0
    _print_json_error(f"unknown library subcommand: {args.library_command!r}")
    return 2


def _cmd_snapshot(deps: AppDeps, args: argparse.Namespace) -> int:
    """Write-once snapshot of every target (spec §2 step 5's own writer,
    called on demand). Reads chapter state live, from `sink.read_chapters`,
    for every target — never from our own `chapter_record` (the last
    verified/committed upload for an *assignment*), because a target with
    no assignment, or one that has never swapped, has no `chapter_record`
    regardless of what it actually holds. Reading that absence back as
    `"chapters": []` used to write a snapshot claiming a tonie was empty
    when the live cloud reported real chapters on it — found on this
    project's first deployment against the live Tonies cloud, against
    four real Creative-Tonies that were UNMANAGED (no assignment) and
    each held audio the snapshot recorded as nothing. An unknown state
    ("we have no local record") is not the same fact as a known one
    ("this tonie is empty"), and this command exists precisely so the
    file on disk can be relied on after the tonie itself is gone.

    The live read costs nothing extra: `sink.list_targets()`, which this
    command already calls to enumerate every target, is the same listing
    call that populates each tonie's chapters cloud-side, so a live
    `read_chapters` per target is not an incremental round-trip this
    command wouldn't otherwise make -- it is still "writes files, touches
    no tonie": `read_chapters` is a read, never a mutation.

    If the live read fails for a target, this command raises rather than
    writing a snapshot for it -- a missing snapshot is recoverable (rerun
    once the fault clears); a snapshot confidently recording "empty" for
    a tonie we simply failed to read is not. Every snapshot this command
    writes records `"source": "live"` (see `write_snapshot`), so a future
    reader never has to guess whether the chapter list on disk reflects
    the tonie's real state at `taken_at`."""
    sink_name = _sink_name(deps)
    orch = deps.orchestrator
    clock = orch.deps.clock

    from boxbutler.orchestrator.snapshot import write_snapshot

    for target in deps.sink.list_targets():
        try:
            live = deps.sink.read_chapters(target)
        except Exception as exc:
            _print_json_error(
                f"could not read live chapter state for target {target.id!r} ({sink_name}): {exc}"
            )
            return 1
        snap = TargetSnapshot(target=target, taken_at=clock(), chapters=live.chapters)
        write_snapshot(deps.snapshot_dir, snap, clock=clock, source="live")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None, *, deps_factory: Callable[[], AppDeps] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse's own usage errors already print to stderr; normalise
        # the exit code (argparse uses 2 for usage errors, which already
        # matches this CLI's contract) rather than let a bare SystemExit
        # propagate out of main().
        return exc.code if isinstance(exc.code, int) else 2

    if deps_factory is None:
        # Task 29's composition root is the only real supplier of this;
        # nothing in this module may build one itself (see module
        # docstring). A bare `boxbutler` invocation with no factory wired
        # up yet is a configuration error, not a usage mistake by the
        # operator's flags -- still exit 2 per the CLI's own contract.
        _print_json_error("no deps_factory configured (see boxbutler/main.py)")
        return 2

    deps = deps_factory()
    # Every `run_event` the orchestrator records is also emitted as a
    # structured JSON line on stdout (dozzle-friendly) from here on.
    deps.orchestrator.deps.log = make_logger()

    if args.command == "run":
        return _cmd_run(deps, args)
    if args.command == "status":
        return _cmd_status(deps, args)
    if args.command == "prefetch":
        return _cmd_prefetch(deps, args)
    if args.command == "cache":
        return _cmd_cache(deps, args)
    if args.command == "library":
        return _cmd_library(deps, args)
    if args.command == "snapshot":
        return _cmd_snapshot(deps, args)

    _print_json_error(f"unknown command: {args.command!r}")
    return 2


__all__ = ["AppDeps", "build_parser", "main"]
