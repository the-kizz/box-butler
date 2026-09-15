"""The run loop: stage, verify, then swap (Task 21; spec §2, §3.3, §3.5, §4.1, §10.2, §10.3, §10.11, §10.12).

This module is the one place in Box Butler that destroys something. Every
other module exists to make these ~400 lines safe.

## The property that outranks every feature

> Stage, verify, then swap. A tonie is never cleared before its replacement
> is downloaded, trimmed and verified.

The Phase 0 prototype did `wipe -> upload`. A failure in between leaves a
child with a silent tonie at 7pm; it worked twice by luck. The order here is
spec §2's, and the *shape of the code* is what enforces it rather than a
comment asking the reader to be careful:

```
1. PLAN      choose the next item                      fail -> skip this tonie, others continue
2. FETCH     download to cache (idempotent)            fail -> ABORT. tonie untouched.
3. RENDER    trim/transcode to the cap                 fail -> ABORT. tonie untouched.
4. VERIFY    duration, decodability, non-triviality    fail -> ABORT. tonie untouched.
   ----------- only past this line may anything be destroyed -----------
5. SNAPSHOT  write-once record of what is about to go  fail -> ABORT. tonie untouched.
6. CLEAR     clear every chapter
7. UPLOAD    upload + add chapter                      fail -> retry, then DEGRADED
8. SETTLE    poll until transcoding false, duration ok fail -> DEGRADED
9. COMMIT    record success, advance cursor, prefetch
```

Steps 1-4 live in `stage()`, which touches nothing but the local cache and
the store. Steps 5-9 live in `swap()`, and `swap()` is called from exactly
**one** place: the final `return` of `run_assignment`, reached only when
`stage()` returned normally. There is no other call site, no retry that
re-enters it, and no `except` that falls through into it — a `StagingFailed`
returns an `AssignmentReport` from inside the `except` block, so the swap
path is not merely skipped, it is *unreachable*.

## Every `clear()` in this file, and what guards it

This module used to claim `sink.clear()` was called from "exactly one line"
of it. That stopped being true when Task 22 added `repair()`, and a claim a
future reader trusts is worth keeping accurate even when the behaviour it
describes is correct (final safety review, Minor 1). There are **two**, both
guarded, and both preceded by a write-once snapshot whose failure *returns*
`ABORTED_STAGING` rather than warning:

1. `swap()` step 6 — reachable only from the final `return` of
   `run_assignment`, i.e. only when FETCH, RENDER and VERIFY passed for every
   chapter, only under `apply=True`, and only after `write_snapshot`
   succeeded.
2. `repair()`'s "partially filled / stale" branch — reachable only after the
   dry-run early return, after `_reload_staged` re-verified *every* staged
   file (all-or-nothing) or a fresh `plan()`+`stage()` produced verified
   bytes, after a live `read_chapters` proved the tonie is neither empty nor
   already the staged set, and after its own `write_snapshot`.

Both write the C3 write-ahead `swap_marker` immediately before clearing, so a
process killed mid-swap is reconciled to DEGRADED at the next start rather
than coming back as `OK` over an empty tonie.

Two more `clear()` calls exist elsewhere in the repo, neither of them here:
`FakeRunner._apply` in `boxbutler/web/fake_data.py` (Phase 2's screen
driver, constructed only with a `FakeSink`, and it stages before it swaps
anyway), and `scripts/phase0-load-tonie.py` — the prototype loader, which
does reach the real cloud, behind `--apply` and after a snapshot, and which
deliberately bypasses this module entirely. So the precise claim is: **inside
the application, these two are the only `clear()` calls on any path that can
reach a real sink, and neither can be reached without verified bytes in hand
and a snapshot on disk.**

**A `SnapshotError` at step 5 is an abort, not a warning.** Clearing a tonie
with no record of what it held is the precise failure write-once snapshots
exist to prevent, so step 5 is classified with the staging failures
(`ABORTED_STAGING`, reason `"snapshot"`), not with the swap failures.

## Ambiguity is never success

The recurring bug family in this project is an *unknown* state read as a
*definite* one: `NaN` passing verification, a missing mount looking like a
deletion, "could not resolve" looking like "empty", "finished but
zero-length" looking like "still working". This module's rule is that any
ambiguous result fails the run:

- `verify_rendition` must return `ok=True`; anything else aborts.
- A settle result that is not `settled` is a failure *whatever* its reason,
  and the three reasons (`timeout`, `duration_mismatch`, `settled_empty`)
  are reported distinctly rather than collapsed — they mean different
  things to whoever reads the run log. (Known gap, accepted: a sink whose
  state alternates empty -> transcoding -> empty defeats
  `poll_until_settled`'s fast-fail and lands on `timeout`. `timeout` is
  handled as a real failure, so the outcome is still safe, only less
  precise.)
- If the settled chapter list does not have one live chapter per uploaded
  chapter, the run does **not** commit — a record that mis-maps item ids to
  sink chapter ids would poison idempotency for every later run.
- `already_current() == True` is `SKIPPED_ALREADY_CURRENT`, a success. It is
  never conflated with `PLAN_FAILED`, which is a failure. Conflating them
  hides a failure as a success.
- `SourceError` ("could not resolve this playlist at all") is reported as
  its own event and reason, never as an empty library.

## Failure classes, and why `run()` re-raises some things

`StagingFailed`, `ClearFailed`, `UploadFailed`, `SettleFailed` and `SourceError` are
*expected* failures: they are caught per assignment, reported, and the loop
continues to the next tonie (spec §4: every other tonie continues).

Anything else is a bug, and a bug is loud — but loud must not mean "and every
other tonie loses its night". Catching only the five named classes meant the
sixth was fatal, and the layer underneath is live HTTP: a dropped connection
in the settle poll abandoned the whole run and left the cleared tonie
recorded as `OK` (final safety review, C2/M3). So `_run_one` also catches
bare `Exception`, and decides which side of the destruction line it is on
from the C3 write-ahead marker rather than from a guess:

- **marker present** — the tonie has been cleared: `DEGRADED` with the
  staged files, so it is repaired before any rotation and the operator is
  told it may be empty.
- **no marker** — nothing was destroyed, the tonie still holds last night's
  story: a per-assignment `CRASHED` report (which makes the run `FAILED`) and
  a `crashed` event, with every other tonie continuing.

`run()` itself still records `CRASHED` and re-raises for anything that
escapes *outside* the per-assignment loop — a failure in `list_targets`, the
store, or the report machinery is not one tonie's problem.

A crash between CLEAR and COMMIT is survivable by construction. Nothing is
written to `chapter_record` until step 9, so the next run sees no record for
this assignment, `already_current()` returns `False`, and the **same** item
is planned again (the cursor did not move either). The cost is one extra
upload; the thing that must never happen — a story silently skipped, or a
tonie left believed-correct while empty — cannot.

`ClearFailed` / `UploadFailed` / `SettleFailed` happen at or after the moment
the tonie is cleared, so the assignment is persisted as `DEGRADED` with its
staged files, which stops the next run from rotating it and gives Task 22's
repair the bytes it needs.
`mark_repaired` is deliberately only ever called after a settled, verified
upload (Task 22) — never here, and never optimistically.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boxbutler.audio.protocol import (
    RenderError,
    RendererProtocol,
    RenderSpec,
    choose_mode,
    rendition_name,
)
from boxbutler.domain.cache_name import sanitise_title
from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS, DURATION_TOLERANCE_S, clamp_cap, fit_fill
from boxbutler.domain.idempotency import already_current
from boxbutler.domain.models import (
    Assignment,
    Item,
    ItemState,
    RenditionMode,
    RunOutcome,
    RunTrigger,
)
from boxbutler.domain.rotation import (
    Plan,
    PlanInput,
    advance_cursor,
    choose_next,
    eligible,
)
from boxbutler.domain.state import can_rotate, mark_degraded, mark_repaired
from boxbutler.fetch.protocol import ExtractionBroken, FetcherProtocol, ItemUnavailable
from boxbutler import metrics
from boxbutler.notify.protocol import Notifier
from boxbutler.sinks.protocol import LiveChapter, SettleResult, SinkProtocol, Target
from boxbutler.sources.playlist_sync import (
    playlist_refs,
    preview_sync_all_playlists,
    sync_all_playlists,
)
from boxbutler.sources.protocol import SourceError, SourceProtocol
from boxbutler.store.db import Store
from boxbutler.verify.verify import verify_rendition

from . import events as E
from .prefetch import prefetch_assignment
from .retention import cache_usage_bytes, evict
from .snapshot import SnapshotError, utcnow, write_snapshot


@dataclass
class RotationSettings:
    """Run-level knobs (spec §10.2). Defaults are the shipped ones.

    `cap_seconds` is `fitting.DEFAULT_CAP_SECONDS` (5395), not the task
    brief's 5340: spec §3.4 explicitly revises 5340 up to 5395 and records
    why (the measured cloud transcode preserves source duration exactly, so
    the minute of insurance was against a risk that does not exist). Task 2
    already shipped 5395 as the domain default; a second, lower default here
    would make the cap depend on which layer you asked.
    """

    cap_seconds: int = DEFAULT_CAP_SECONDS
    avoid_duplicates: bool = True
    repeat_cooldown_days: int = 0
    prefetch_depth: int = 3
    # spec §3.6: 40 GB default budget for the cache directory, LRU by
    # `last_used_at`. See `boxbutler.orchestrator.retention`.
    cache_budget_bytes: int = 40 * 1024**3
    settle_timeout_s: int = 180
    upload_attempts: int = 3
    upload_backoff_s: tuple[float, ...] = (5, 15, 45)
    loudnorm_default: bool = False
    # The rotation window (R19). `schedule` is spec §7's daily run time and
    # `timezone` its explicit zone — read from `TZ`, then the system zone, per
    # §7 ("never stuck on the author's clock"), never hard-coded to the
    # operator's city. Task 29 passes the configured values through; these
    # defaults must already be correct, because a wrong zone here means either
    # two rotations in one local evening or none at all.
    schedule: str = "15:00"
    timezone: str = field(default_factory=lambda: default_timezone_name())


@dataclass
class Deps:
    """Everything the orchestrator talks to. No globals, no module state:
    every test drives this with fakes, and no test may touch a real tonie.
    """

    store: Store
    sink: SinkProtocol
    fetcher: FetcherProtocol
    renderer: RendererProtocol
    cache_dir: Path
    snapshot_dir: Path
    settings: RotationSettings = field(default_factory=RotationSettings)
    notifier: Notifier | None = None        # Task 25; None -> no notifications
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str, dict], None] | None = None   # Task 27 plugs the JSON logger in
    # Task 23: the resolver `_sync_sources` re-resolves playlists/feeds
    # against. `None` (the default, and every pre-Task-23 test) means no
    # re-resolution happens — a library with no playlist refs configured
    # never needs one either.
    playlist_source: SourceProtocol | None = None

    @property
    def sink_name(self) -> str:
        """The `assignment.sink` discriminator, read straight off the sink
        object (`SinkProtocol.name`).

        This used to be a settable field defaulting to `"fake"`, which the
        composition root overrode with a second constant of its own while
        the web routes imported a third from `web/fake_data.py`. Nothing
        kept the three in agreement and they did not agree: a real install
        wrote its assignments under one name and looked them up under
        another, so every run reported UNMANAGED and no tonie was ever
        loaded (final safety review, C1). Deriving it means there is only
        one value — the sink's own — and no way for a caller to set a
        different one.
        """
        return self.sink.name


@dataclass(frozen=True)
class StagedChapter:
    item_id: str
    path: Path
    title: str
    seconds: float
    # The verified `rendition` row these bytes came from, so COMMIT can mark
    # it used without re-deriving it from the path. Not part of the
    # `staged_json` Task 22 writes (that only needs the first four).
    rendition_id: str | None = None


def staged_to_json(chapters: list["StagedChapter"], *, rotates: bool = True) -> str:
    """The `assignment.staged_json` shape (R2): a JSON list of
    {"item_id","path","title","seconds"} — every staged file and its title, so
    a repair can re-upload a partially-completed album or serial load, not
    just one path.

    `rotates` (Task 22) is stashed on every entry, redundantly but harmlessly
    — the original plan's `Plan.rotates` (False for album/pinned/
    empty-library), so a later repair that reuses these exact files can
    decide whether its own commit should advance the cursor without
    re-planning. It is *not* stored as a wrapping object because every
    existing reader of `staged_json` (this repo's own tests included, see
    `tests/orchestrator/test_stage_then_swap.py`) assumes the JSON is a bare
    list of chapter dicts.
    """
    return json.dumps(
        [
            {
                "item_id": c.item_id,
                "path": str(c.path),
                "title": c.title,
                "seconds": c.seconds,
                "rotates": rotates,
            }
            for c in chapters
        ]
    )


# M2 (final safety review): how far the probed source may fall short of the
# feed's own stated duration before the two are treated as disagreeing.
# `item.seconds` comes from `<itunes:duration>` and friends, which publishers
# round and occasionally get wrong by a few seconds, so a flat couple of
# seconds (the `DURATION_TOLERANCE_S` used for renditions, where both numbers
# come from this code) would abort real, intact episodes. The allowance is
# therefore the larger of 30 s and 5% of the expectation — still nowhere near
# the failure this catches, which is a truncated download missing minutes or
# hours, not seconds.
SOURCE_SHORTFALL_FLOOR_S = 30.0
SOURCE_SHORTFALL_FRACTION = 0.05


def source_shortfall_allowance(expect_seconds: float) -> float:
    """How much shorter than `expect_seconds` a source may probe and still be
    treated as the same content (see the constants above)."""
    return max(SOURCE_SHORTFALL_FLOOR_S, expect_seconds * SOURCE_SHORTFALL_FRACTION)


def _success_detail(a: Assignment, staged: "Staged", *, repaired: bool = False) -> str:
    """Task 25's `on_success` body — spec §9.1's own example is "Green Tonie
    Stories now has The Pawfect Pet Hotel (89 min)"; this is that shape,
    built from the already-`sanitise_title`d chapter titles (`StagedChapter.
    title`, set in `_render_and_verify`) so it needs no separate ASCII pass
    of its own before `ascii_title` sees it again downstream.
    """
    names = ", ".join(c.title for c in staged.chapters) or "nothing new"
    minutes = round(staged.total_seconds / 60)
    verb = "repaired, now has" if repaired else "now has"
    return f"{a.target_name} {verb} {names} ({minutes} min)"


@dataclass(frozen=True)
class Staged:
    chapters: list[StagedChapter]
    total_seconds: float
    plan: Plan


class StagingFailed(Exception):
    """A stage step (FETCH/RENDER/VERIFY, or the SNAPSHOT that guards
    CLEAR) failed. The tonie has not been touched."""

    def __init__(self, reason: str, item_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.item_id = item_id


class ClearFailed(Exception):
    """`sink.clear()` raised. **An expected failure, not a bug.**

    A real `tonie_api` clear is a network call against a flaky cloud, and it
    can fail after partially emptying a tonie — so the tonie may already be
    damaged, which is exactly the `DEGRADED` case: keep the verified staged
    files, repair before any rotation, and let every other tonie continue.
    Treating it as a crash instead (which is what an unclassified exception
    did) left the assignment `state=OK` while possibly half-empty, and
    abandoned the rest of the run.
    """

    def __init__(self, reason: str, staged_json: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.staged_json = staged_json


class UploadFailed(Exception):
    """Every upload attempt for one chapter failed, after the tonie was
    cleared. Task 22 turns this into a repair.

    Carries `staged_json` — the verified files that were about to go on — so
    the DEGRADED record can keep them and a repair need not redo the
    download/trim work (spec §2: "persisted as DEGRADED ... and carries the
    staged file path").
    """

    def __init__(self, reason: str, item_id: str | None = None, staged_json: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.item_id = item_id
        self.staged_json = staged_json


class SettleFailed(Exception):
    """The sink never reached a verified settled state. Carries the
    distinct settle reason (`timeout` / `duration_mismatch` /
    `settled_empty` / `chapter_count_mismatch`)."""

    def __init__(self, reason: str, staged_json: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.staged_json = staged_json


@dataclass
class AssignmentReport:
    assignment_id: str
    target_name: str
    outcome: RunOutcome
    detail: str = ""


@dataclass
class RunReport:
    run_id: str
    dry_run: bool
    reports: list[AssignmentReport]
    #: True when the sink listed no targets at all while the store holds at
    #: least one managed (library-assigned) assignment — see `outcome`.
    no_targets_but_managed: bool = False

    @property
    def outcome(self) -> str:
        """The run-level outcome. **`OK` means something happened and it
        worked** (final safety review, M4).

        It did not used to: a run with zero assignment reports returned
        `"OK"`, so "nothing to do" and "the cloud listing came back empty /
        the id filter matched nothing / every row is keyed under a sink name
        this run never looks up" all reported a clean success. Both of the
        review's probes for C1 and C2 produced `OK` runs that did literally
        nothing, one of them with no events at all. That is the project's
        recurring bug family at the run level: an unknown read as a
        definite.

        So an empty run is `NOTHING_TO_DO` — legitimate on a box with no
        managed tonies, and *visibly* not a success anywhere it is
        displayed. And an empty target listing while managed assignments
        exist is `FAILED` outright: that combination is never normal, it is
        a sink/credentials/account fault, and treating it as "nothing to do"
        is how a whole night disappears quietly.
        """
        if self.no_targets_but_managed:
            return "FAILED"
        if not self.reports:
            return "NOTHING_TO_DO"
        outcomes = {r.outcome for r in self.reports}
        if RunOutcome.DEGRADED in outcomes:
            return "DEGRADED"
        if outcomes & {RunOutcome.ABORTED_STAGING, RunOutcome.PLAN_FAILED, RunOutcome.CRASHED}:
            return "FAILED"
        return "OK"


def default_timezone_name() -> str:
    """Spec §7: read `TZ`, fall back to the system zone. Never the author's
    city, and never a casual "UTC" — a UTC window boundary falls at ~10-11am
    in the operator's zone, which splits one local day into two rotation
    windows and lets an evening run destroy a story put on that morning.

    Every candidate is validated against `ZoneInfo` before being returned,
    because the obvious one-liner
    (`datetime.now().astimezone().tzinfo`) yields an *abbreviation* like
    `"AEST"` on a host with no `TZ` set — which `ZoneInfo` cannot load, so the
    window would silently stop working (failing open, so safe, but useless).
    The candidates are, in order: `TZ`, the `/etc/localtime` symlink target,
    `/etc/timezone`, then `"UTC"` as the last resort. The symlink comes before
    `/etc/timezone` because the two really do disagree in practice — measured
    on the development host, where `/etc/timezone` reads `Etc/UTC` while
    `/etc/localtime` points at `Australia/Melbourne`, which is where the
    machine actually is. Taking the stale file would have produced precisely
    the UTC-boundary defect above on a host that could have answered
    correctly. If it comes to `"UTC"` the host genuinely cannot say where it
    is, and the configured zone from Settings (Task 29) is the only real fix.
    """
    candidates = [(os.environ.get("TZ") or "").strip()]
    try:
        link = os.readlink("/etc/localtime")
        _, _, after = link.partition("zoneinfo/")
        candidates.append(after.strip())
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
        return name
    return "UTC"


def _resolve_zone(tz: str | ZoneInfo | None) -> ZoneInfo:
    """Never silently substitutes a different zone: an unresolvable one raises,
    so `_rotation_due` fails **open** (rotate) rather than comparing windows in
    a zone nobody asked for."""
    if isinstance(tz, ZoneInfo):
        return tz
    name = (tz or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unresolvable timezone {name!r}") from exc


def _schedule_token(schedule: str | dtime) -> str:
    """`"15:00"` / `time(15, 0)` -> `"15:00"`. Raises `ValueError` on junk."""
    if isinstance(schedule, dtime):
        return schedule.strftime("%H:%M")
    hh, _, mm = str(schedule).strip().partition(":")
    return dtime(int(hh), int(mm or 0)).strftime("%H:%M")


def rotation_window_key(
    now: datetime, schedule: str | dtime = "15:00", tz: str | ZoneInfo | None = None
) -> str:
    """A comparable identity for the rotation window `now` falls in (R19).

    Two timestamps with the same key mean "this tonie has already rotated in
    this window; leave a correct tonie alone". Callers compare keys for
    **inequality**, never order — see `Orchestrator._rotation_due`.

    **The window is the local calendar day**, in the configured zone, and that
    choice is the point of the function. The obvious alternative — a window
    running from one scheduled run time to the next, e.g. [15:00, 15:00) —
    splits a single local day in two: a manual `--apply` at 09:00 lands in the
    previous window, so the 15:00 scheduled run sees a *new* window, rotates
    again, and destroys the story put on that morning before anyone hears it.
    A UTC calendar day has the same defect with its boundary at ~10-11am in
    the operator's zone. A local calendar day puts both runs in one window:
    the morning run rotates, the evening run reports
    SKIPPED_ALREADY_CURRENT, and the child hears the story that is on the
    tonie.

    `schedule` is part of the key rather than part of the arithmetic, so
    changing the configured run time starts a new window — one extra rotation,
    which is safe (stage-then-swap only ever replaces a verified story with
    another verified story) where a missed one is not.

    Known edge, accepted: a schedule set within minutes of local midnight puts
    a window boundary next to the run itself, so a late rerun can land in the
    next window and rotate again. Safe in the stage-then-swap direction, and
    no configuration in the spec does it.

    Raises `ValueError` on a naive `now`, an unparseable schedule or an
    unresolvable zone — every caller treats that as "rotation is due".
    """
    if now.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed here; pass a timezone-aware datetime "
            "(e.g. datetime.now(UTC))"
        )
    zone = _resolve_zone(tz)
    token = _schedule_token(schedule)
    local_date = now.astimezone(zone).date()
    return f"{getattr(zone, 'key', zone)}|{token}|{local_date.isoformat()}"


class Orchestrator:
    def __init__(self, deps: Deps):
        self.deps = deps

    # ------------------------------------------------------------------ run

    def run(
        self,
        *,
        assignment_ids: list[str] | None = None,
        apply: bool = False,
        trigger: RunTrigger = RunTrigger.CLI,
    ) -> RunReport:
        """One pass over every managed tonie.

        `apply` defaults to False: a dry run reads and plans but fetches
        nothing and changes nothing (binding constraint). Writing requires
        an explicit `apply=True`.
        """
        store = self.deps.store
        run = store.runs.start(str(trigger), dry_run=not apply)
        report = RunReport(run_id=run.id, dry_run=not apply, reports=[])
        try:
            # 1. Reconcile the sink's targets into assignments. A tonie we
            #    have never seen arrives with no library and is reported
            #    UNMANAGED: it is never cleared, never uploaded to.
            targets = self.deps.sink.list_targets()
            if not targets and any(
                a.library_id is not None and a.enabled for a in store.assignments.list()
            ):
                # Managed tonies exist but the sink says there are none:
                # never a normal state, and reporting it as "nothing to do"
                # is how a silent night happens (M4). Recorded on the
                # report, which makes the run FAILED.
                report.no_targets_but_managed = True
                self._event(run.id, E.NO_TARGETS, None, managed=True)
                # Run-level FAILED with zero per-assignment reports (final
                # fix review, masking-defect 1): `_notify_outcome` only
                # runs inside the `for a in due` loop below, and `due` is
                # empty here by construction (the sink listed no targets at
                # all), so without this call the run silently fails with
                # nobody told -- exactly the M4 defect this outcome exists
                # to surface, just moved one layer up to notifications.
                self._notify(
                    "failure",
                    "Box Butler: run failed",
                    "Managed tonies exist but the sink listed none -- check "
                    "the sink account/credentials. No tonie was touched.",
                )
            for t in targets:
                store.assignments.upsert_target(self.deps.sink_name, t.id, t.name)

            due: list[Assignment] = []
            for t in targets:
                a = store.assignments.get_by_target(self.deps.sink_name, t.id)
                if a is None or not a.enabled:
                    continue
                if assignment_ids is not None and a.id not in assignment_ids:
                    continue
                if a.library_id is None:
                    self._event(run.id, E.UNMANAGED, a, target_id=t.id)
                    report.reports.append(
                        AssignmentReport(a.id, a.target_name, RunOutcome.UNMANAGED, "no_library")
                    )
                    continue
                due.append(a)

            # Repairs are the first loop; rotations the second (spec §2: "a
            # degraded tonie is repaired before any rotation work"). A stable
            # sort keeps each group in its original target order but puts
            # every DEGRADED assignment ahead of every rotatable one,
            # regardless of where it falls in `targets` — the property must
            # hold for the whole run, not just for one assignment against
            # itself.
            due.sort(key=lambda a: 0 if not can_rotate(a) else 1)
            for a in due:
                r = self._run_one(run.id, a, apply=apply)
                report.reports.append(r)
                self._notify_outcome(r)

            # Retention (Task 24; spec §3.6): explicit, budgeted eviction,
            # only on a real write. R21's dry-run promise ("resolves
            # read-only and persists nothing") extends here — a dry run
            # must not delete cache files either.
            if apply:
                self._evict(run.id)

            store.runs.finish(run.id, report.outcome)
            metrics.run_total.labels(outcome=report.outcome).inc()
            return report
        except Exception as e:
            # Not an expected failure: record the run as CRASHED so the
            # History screen shows the truth, then re-raise. A bug is loud.
            store.runs.finish(run.id, "CRASHED")
            metrics.run_total.labels(outcome="CRASHED").inc()
            # Run-level notification (final fix review, masking-defect 1):
            # this branch is a bug escaping the whole run loop -- above and
            # outside any per-assignment try/except -- so no
            # `AssignmentReport` and no `_notify_outcome` call ever runs
            # for it. Without this, the *loudest* possible failure (a run
            # that crashed outright) was also the *quietest* one a human
            # would hear about: History-screen only, same shape as the
            # per-assignment CRASHED gap above.
            self._notify(
                "failure",
                "Box Butler: run crashed",
                f"The run itself crashed before finishing: {type(e).__name__}: {e}",
            )
            raise

    def _run_one(self, run_id: str, a: Assignment, *, apply: bool) -> AssignmentReport:
        """One assignment, with the expected-failure classes caught here so
        every other tonie continues (spec §4)."""
        try:
            if not can_rotate(a):
                # DEGRADED is sticky, and repair comes before any rotation
                # work (spec §2). `can_rotate` is the domain's answer, not a
                # second copy of the rule spelled out here.
                return self.repair(run_id, a, apply=apply)
            return self.run_assignment(run_id, a, apply=apply)
        except StagingFailed as e:
            # Raised by stage() itself only via run_assignment's own except
            # block; this is the belt-and-braces path for a StagingFailed
            # escaping swap()'s snapshot guard.
            self._event(run_id, E.STAGING_FAILED, a, reason=e.reason, item_id=e.item_id)
            return AssignmentReport(a.id, a.target_name, RunOutcome.ABORTED_STAGING, e.reason)
        except SourceError as e:
            # "Could not resolve" is not "the playlist is empty" — it gets
            # its own event and reason, and is never silently swallowed.
            self._event(run_id, E.SOURCE_ERROR, a, error=str(e))
            return AssignmentReport(
                a.id, a.target_name, RunOutcome.PLAN_FAILED, f"source_error:{e}"
            )
        except (ClearFailed, UploadFailed, SettleFailed) as e:
            # At or past the point of no return: the tonie may be cleared (or
            # half-cleared) and is not correctly filled. Persist DEGRADED
            # (sticky, carries the staged files) so the next run repairs
            # instead of rotating, and carry on to the next tonie.
            return self._degrade(run_id, a, e)
        except Exception as e:   # noqa: BLE001 — one tonie never costs the others
            # **The unnamed class.** Catching only the classes named above
            # means the next one is fatal, and the layer underneath is live
            # HTTP:
            # `requests`/`httpx` errors, `SessionError`, a `KeyError` from an
            # unexpected response shape, a socket `TimeoutError`. The review
            # found two consequences, both bad (C2, M3):
            #
            #   * a `settle` that raised `ConnectionError` escaped `swap()`
            #     entirely, so the assignment stayed `state=OK,
            #     staged_json=NULL` although the tonie had been cleared —
            #     no DEGRADED, no repair, no notification, no alert;
            #   * and it abandoned the rest of the run, so tonies #2 and #3
            #     were never processed, against spec §4's "every other tonie
            #     continues".
            #
            # The swap marker is the evidence of which side of the
            # destruction line we are on, and it is written before `clear()`
            # precisely so this question has a factual answer rather than a
            # guess: a marker means the tonie has been touched and must land
            # in DEGRADED carrying its staged files (and the operator must
            # be told — it may be empty right now); no marker means nothing
            # was destroyed, the tonie still holds last night's story, and
            # this is a bug to report loudly for this one assignment while
            # every other tonie continues.
            marker = self.deps.store.swap_markers.get(a.id)
            if marker is not None:
                return self._degrade(run_id, a, e, staged_json=marker.staged_json)
            self._event(run_id, E.CRASHED, a, error=f"{type(e).__name__}: {e}")
            if self.deps.log is not None:
                self.deps.log(
                    "assignment_crashed",
                    {"assignment_id": a.id, "target": a.target_name, "error": repr(e)},
                )
            return AssignmentReport(
                a.id, a.target_name, RunOutcome.CRASHED, f"crashed:{type(e).__name__}: {e}"
            )

    # ----------------------------------------------------------- one tonie

    def run_assignment(self, run_id: str, a: Assignment, *, apply: bool) -> AssignmentReport:
        target = self._target(a)
        if target is None:
            self._event(run_id, E.PLAN_FAILED, a, reason="target_missing")
            return AssignmentReport(
                a.id, a.target_name, RunOutcome.PLAN_FAILED, "target_missing"
            )

        plan = self.plan(run_id, a, apply=apply)
        if not plan.item_ids:
            reason = plan.reason or E.NO_CANDIDATE
            self._event(run_id, E.PLAN_FAILED, a, reason=reason)
            return AssignmentReport(a.id, a.target_name, RunOutcome.PLAN_FAILED, reason)
        for r in plan.relaxations:
            self._event(run_id, r, a)
            self._notify(
                "debug", f"{a.target_name}: plan relaxed", f"{a.target_name} plan relaxation: {r}"
            )

        # A READ. Nothing is written yet.
        live = self.deps.sink.read_chapters(target)
        if self._already_current(a, live.chapters, plan):
            self._event(run_id, E.SKIP_CURRENT, a, items=plan.item_ids)
            return AssignmentReport(
                a.id, a.target_name, RunOutcome.SKIPPED_ALREADY_CURRENT, plan.reason or ""
            )

        if not apply:
            self._event(
                run_id, E.DRY_RUN_PLAN, a, items=plan.item_ids, would_clear=len(live.chapters)
            )
            return AssignmentReport(a.id, a.target_name, RunOutcome.DRY_RUN, plan.reason or "")

        try:
            staged = self.stage(run_id, a, plan)
        except StagingFailed as e:
            self._event(run_id, E.STAGING_FAILED, a, reason=e.reason, item_id=e.item_id)
            # The tonie is untouched: no swap call exists on this path.
            return AssignmentReport(a.id, a.target_name, RunOutcome.ABORTED_STAGING, e.reason)

        return self.swap(run_id, a, target, staged)

    # -------------------------------------------------------------- 1. PLAN

    def plan(self, run_id: str, a: Assignment, *, apply: bool = True) -> Plan:
        store = self.deps.store
        if a.library_id is None:
            return Plan([], [], "EMPTY_LIBRARY", rotates=False)

        # Task 23 re-resolves playlists here — on a dry run too (R21): a
        # preview that planned against a stale catalog could report "tonight
        # plays X" when the real run, re-resolving for itself, would play a
        # newly-appeared Y instead. `apply=False` makes this read-only (see
        # `_sync_sources`): nothing is written, but the returned item list
        # already reflects what the write would have produced, so the plan
        # below sees it either way. A SourceError propagates on both paths:
        # it means "could not resolve at all", which must never be read as
        # an empty library.
        synced_items = self._sync_sources(run_id, a, apply=apply)

        lib = store.libraries.get(a.library_id)
        if lib is None:
            return Plan([], [], "EMPTY_LIBRARY", rotates=False)
        items = synced_items if synced_items is not None else store.items.list(a.library_id)

        s = self.deps.settings
        cooldown_active = s.repeat_cooldown_days > 0
        recently_held: frozenset[str] = frozenset()
        if cooldown_active:
            since = self.deps.clock() - timedelta(days=s.repeat_cooldown_days)
            recently_held = frozenset(store.chapters.held_since(a.id, since))

        plan = choose_next(
            PlanInput(
                assignment=a,
                library_mode=lib.mode,
                items=items,
                loaded_elsewhere=frozenset(store.chapters.loaded_item_ids_except(a.id)),
                recently_held=recently_held,
                avoid_duplicates=s.avoid_duplicates,
                cooldown_active=cooldown_active,
            )
        )
        if plan.item_ids:
            self._event(run_id, E.PLAN, a, items=plan.item_ids, reason=plan.reason,
                        rotates=plan.rotates)
            self._notify(
                "debug",
                f"{a.target_name}: plan",
                f"{a.target_name} plan: items={plan.item_ids} reason={plan.reason}",
            )
        return plan

    def _already_current(self, a: Assignment, live: list[LiveChapter], plan: Plan) -> bool:
        """Is there genuinely nothing to do for this tonie right now?

        Two questions, both answered by `already_current` (which matches on
        `sink_chapter_id` and duration within tolerance, **never** on title):

        1. Does the tonie already hold exactly what this run intends? This is
           spec §3.3's check, and it is the whole story for `album` and for a
           pinned assignment — "once the tonie matches the library, every run
           is SKIPPED_ALREADY_CURRENT until the library changes" (§4.1).

        2. Does it still hold, intact, the load this assignment most recently
           committed — with that commit inside the current rotation window?

        Question 2 exists because spec §3.3 and §4.1 pull in opposite
        directions for `single`/`serial`, and both are requirements:

        - §3.3 / §10.2: "three runs in one afternoon produce one upload", the
          cursor does not move on a skip.
        - §4.1 / §10.12: `single` advances by one, `serial` advances past
          everything it loaded — so the run *after* a successful swap plans a
          *different* item, and question 1 alone can never be true for it.
          Checking only question 1 would give three uploads in one afternoon,
          three different stories, and a cursor three places on.

        The reconciliation is in §3.3's own words — "three runs in one
        *afternoon*" — and in §1.1/§7's schedule: **rotation advances on the
        schedule** (default 15:00 daily), not once per invocation. A correct
        tonie is not replaced again inside the same rotation window just
        because someone re-ran the job or clicked twice; the next window's run
        rotates, using the cursor that already moved on.

        `_rotation_due` is the window test, and it is deliberately the
        conservative half: if it cannot tell, it says a rotation IS due, which
        costs one upload of the right content and never leaves the wrong
        content in place. The window itself is a named, timezone-aware
        function — `rotation_window_key(now, schedule, tz)` — whose docstring
        explains why the window is the local calendar day and not the interval
        between scheduled runs. Task 29 wires the configured schedule and zone
        into `RotationSettings`; these defaults already behave correctly.
        """
        records = self.deps.store.chapters.for_assignment(a.id)
        if already_current(live, records, plan.item_ids):
            return True
        if not plan.rotates:
            # Question 2 exists only to give a *rotating* plan (single,
            # serial) the "three runs in one afternoon, one upload"
            # window: it re-checks against records already known good so a
            # second run in the same window doesn't replace a still-correct
            # tonie with a *different* still-correct one. A non-rotating
            # plan (album; a pin) has no "next" item to fall back to being
            # equal to — question 1 already compared it against the full,
            # current intended set, which is (per spec §4.1) the whole
            # story for album. Falling through here would compare the live
            # tonie against a *stale* recorded set instead, which is
            # exactly how a grown library silently failed to reload: intent
            # changed (a 4th item joined the album), the old 3-item record
            # still matched the live tonie, and the window fallback below
            # said "current" using the wrong question.
            return False
        if not records or self._rotation_due(a):
            return False
        return already_current(live, records, [r.item_id for r in records])

    def _rotation_due(self, a: Assignment) -> bool:
        """Whether this assignment has not yet rotated in the current window.

        **Inequality, not `<`.** An earlier version asked whether the last
        success was in an *earlier* window, which fails permanently CLOSED on a
        clock that runs backwards: a `last_success_at` 30 days in the future —
        an NTP step, a restored backup, a mis-written row — made every run
        report SKIPPED_ALREADY_CURRENT indefinitely, even with the library
        changed underneath, and nobody would notice the service had stopped. A
        *different* window is due, whichever side of now it sits on.

        Everything ambiguous is due as well: no recorded success, a naive
        timestamp (`_parse_dt` returns one for a row written without an offset,
        and `naive.astimezone()` does not raise — it silently reinterprets in
        the host zone, which is exactly the "unknown read as definite" family
        this project keeps catching), an unparseable schedule, an unresolvable
        zone. Being due costs one upload of the right content; not being due
        costs a story.
        """
        last = a.last_success_at
        if last is None or last.tzinfo is None:
            return True
        s = self.deps.settings
        try:
            return rotation_window_key(
                self.deps.clock(), s.schedule, s.timezone
            ) != rotation_window_key(last, s.schedule, s.timezone)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return True

    def _sync_sources(self, run_id: str, a: Assignment, *, apply: bool = True) -> list[Item] | None:
        """Re-resolve this assignment's playlists/feeds before planning
        (Task 23; spec §3.5.1 "re-resolve each run"). **Runs on a dry run
        too** — read-only, per R21 below — so a preview never plans against
        a catalog it knows is stale.

        A no-op (returns `None`, meaning "planning should read the catalog
        itself") unless the library has playlist refs configured
        (`settings["playlists:<library_id>"]`, see
        `boxbutler.sources.playlist_sync.playlist_refs`) *and* a
        `Deps.playlist_source` is wired up — a library with nothing to
        re-resolve, or a caller that never configured a resolver, does
        nothing here.

        `apply=True`: mutates via `sync_all_playlists` exactly as before,
        and returns `None` — `plan()` reads the now-updated catalog itself.

        `apply=False` (R21 — a dry run must not mutate the catalog, and
        must still preview accurately): calls `preview_sync_all_playlists`
        instead, which resolves for real (so the preview is honest about
        what tonight would actually hold) but performs **no**
        `items.add`/`items.set_state` write, and returns the merged item
        list in memory for `plan()` to use directly. Logs a `PLAYLIST_PREVIEW`
        event with what *would* change (added/marked-unavailable/restored
        counts) whenever any of them is nonzero, so a dry run is more
        informative than before, not less — simply gating the write on
        `apply` without this would make the preview *lie*: it would plan
        against last run's catalog and report tonight's pick as if a new
        episode hadn't just appeared.

        `SourceError` ("could not resolve at all") is deliberately **not**
        caught on either path: it propagates out of `plan()` to
        `_run_one`'s existing, already-tested route (`E.SOURCE_ERROR` ->
        `PLAN_FAILED "source_error:..."`, see
        `tests/orchestrator/test_stage_then_swap.py::
        test_source_error_is_reported_distinctly_never_as_an_empty_library`).
        Swallowing it here and "continuing with what is stored" — which is
        what an earlier draft of this task's brief called for — would mean
        a stale/broken playlist resolver silently degrades into "the
        library is whatever it happened to be last", the exact "unknown
        state read as a definite one" failure this project keeps refusing
        elsewhere; the existing test above requires the propagate-and-
        classify behaviour, so that is what ships, on both paths.
        """
        store = self.deps.store
        source = self.deps.playlist_source
        if a.library_id is None or source is None:
            return None
        refs = playlist_refs(store, a.library_id)
        if not refs:
            return None
        if apply:
            sync_all_playlists(store, a.library_id, source)
            return None

        results, items = preview_sync_all_playlists(store, a.library_id, source)
        would_add = sum(r.appended for r in results)
        would_unavailable = sum(r.unavailable for r in results)
        would_restore = sum(r.restored for r in results)
        if would_add or would_unavailable or would_restore:
            self._event(
                run_id, E.PLAYLIST_PREVIEW, a,
                would_add=would_add, would_mark_unavailable=would_unavailable,
                would_restore=would_restore,
            )
        return items

    # ------------------------------------------- 2-4. FETCH, RENDER, VERIFY

    def stage(self, run_id: str, a: Assignment, plan: Plan) -> Staged:
        """Produce verified local files for `plan`, touching no tonie.

        `ItemUnavailable` on any item marks it unavailable and *re-plans*
        (an unavailable item changes what "next" means), up to one attempt
        per item in the library. `ExtractionBroken` stops immediately: it
        will hit every item, so trying the next one only wastes time and
        hides the real fault.
        """
        store = self.deps.store
        budget = max(1, len(store.items.list(a.library_id))) if a.library_id else 1
        current = plan
        for _ in range(budget):
            try:
                return self._stage_plan(run_id, a, current)
            except ItemUnavailable as e:
                item_id = getattr(e, "item_id", None)
                if item_id:
                    store.items.set_state(item_id, ItemState.UNAVAILABLE)
                self._event(run_id, E.ITEM_UNAVAILABLE, a, item_id=item_id, error=str(e))
                fresh = store.assignments.get(a.id)
                # stage() only ever runs after run_assignment's apply gate:
                # always a real (apply=True) re-plan, never a preview.
                current = self.plan(run_id, fresh, apply=True)
                if not current.item_ids:
                    raise StagingFailed(current.reason or E.NO_CANDIDATE, None) from e
        raise StagingFailed("no_available_item", None)

    def _stage_plan(self, run_id: str, a: Assignment, plan: Plan) -> Staged:
        cap = clamp_cap(self.deps.settings.cap_seconds, self.deps.sink.limits.max_seconds)
        store = self.deps.store

        # Measure before committing to a fit: fetch + probe in plan order
        # until the cap is accounted for, then let `fit_fill` decide what
        # actually goes on (it owns truncation and tail-dropping policy).
        measured: list[tuple[str, float]] = []
        sources: dict[str, tuple[Path, float]] = {}
        accumulated = 0.0
        for item_id in plan.item_ids:
            if accumulated >= cap:
                break
            item = store.items.get(item_id)
            if item is None:
                raise StagingFailed("item_missing", item_id)
            src = self._fetch(run_id, a, item)
            seconds = self._probe_source(run_id, a, item, src)
            sources[item_id] = (src, seconds)
            measured.append((item_id, seconds))
            accumulated += seconds

        if not measured:
            raise StagingFailed(E.NO_CANDIDATE, None)

        fit = fit_fill(
            measured,
            cap,
            a.allow_partial_tail,
            max_chapters=self.deps.sink.limits.max_chapters,
        )
        if not fit.pieces:
            raise StagingFailed("nothing_fits", measured[0][0])

        chapters: list[StagedChapter] = []
        for piece in fit.pieces:
            item = store.items.get(piece.item_id)
            src, _src_seconds = sources[piece.item_id]
            chapters.append(self._render_and_verify(run_id, a, item, src, piece.take_seconds))
        total = sum(c.seconds for c in chapters)
        return Staged(chapters=chapters, total_seconds=total, plan=plan)

    def _fetch(self, run_id: str, a: Assignment, item: Item) -> Path:
        """FETCH. Idempotent by source key: an existing cached file is a hit.

        A library is a folder, and `Ingestor` puts a source's audio in it
        when the item is added — so for most items the bytes are already
        sitting in the library folder at `item.local_path` and there is
        nothing to fetch. That is a hit, not a special case: it is exactly
        what `FolderFetcher` already does for a scanned file, generalised
        to every kind now that every kind lands in the same place. If the
        file is gone (the operator deleted it, or the item predates the
        download-on-add path) this falls through to the fetcher, which
        re-downloads it into the cache as before.
        """
        store = self.deps.store
        prior = store.sources.get(item.id)
        cache_hit = prior is not None and Path(prior.cache_path).exists()
        local = Path(item.local_path) if item.local_path else None
        if local is not None and local.is_file():
            now = self.deps.clock()
            store.sources.record(item.id, local, now)
            store.sources.touch(item.id, now)
            self._event(run_id, E.FETCH, a, item_id=item.id, path=str(local), cache_hit=True)
            return local
        try:
            src = self.deps.fetcher.fetch(item, self.deps.cache_dir)
        except ExtractionBroken as e:
            # Will hit every item: stop this run's fetch attempts here.
            self._event(run_id, E.EXTRACTION_BROKEN, a, item_id=item.id, error=str(e))
            metrics.extraction_broken_total.inc()
            raise StagingFailed("extraction_broken", item.id) from e
        except ItemUnavailable as e:
            # This one item only. `stage()` marks it and re-plans.
            e.item_id = item.id  # type: ignore[attr-defined]
            raise
        if not src.exists():
            # A fetcher that returns a path to nothing is the "missing mount
            # looks like a deletion" failure family: refuse, never proceed.
            raise StagingFailed("fetch_missing_file", item.id)
        now = self.deps.clock()
        store.sources.record(item.id, src, now)
        if cache_hit:
            store.sources.touch(item.id, now)
        self._event(run_id, E.FETCH, a, item_id=item.id, path=str(src), cache_hit=cache_hit)
        return src

    def _probe_source(self, run_id: str, a: Assignment, item: Item, src: Path) -> float:
        """Measure the fetched source, and check that measurement against an
        **independent** expectation where one exists (final safety review,
        M2).

        Every duration in the staging path used to descend from this one
        probe of this one file: `fit_fill` derives `take_seconds` from it,
        `_render_and_verify` hands that same number to `verify_rendition` as
        `expect_seconds`, and `verify_rendition` compares the rendition's own
        probe against it. That chain is self-consistent by construction, so
        it can catch a *render* that lost time and can never catch a *fetch*
        that arrived short: the store said 3600 s, 900 s was on disk, and the
        run reported `SWAPPED` with "now has Episode One (15 min)" — the
        eleventh instance of this project's recurring bug family, "I don't
        know how long this should be" and "it is this long" being the same
        value.

        `item.seconds` is the independent expectation, and until this method
        nothing in `boxbutler/orchestrator`, `verify` or `audio` read it. It
        comes from the feed (`<itunes:duration>`, `sources/rss.py`) or the
        resolver, is correctly `None` when unknown, and — crucially — was
        written before this file was ever downloaded, by something other than
        the thing being checked.

        Directions are deliberately not symmetric:

        * **Materially shorter than the feed says** is the dangerous case: a
          truncated download. It raises `StagingFailed`, so the tonie keeps
          last night's story, which is the governing rule when in doubt. It
          is never a warning — proceeding would clear a tonie for content of
          unknown completeness.
        * **Materially longer** is reported (`E.SOURCE_DURATION_MISMATCH`)
          and allowed: dynamic ad insertion and re-cut episodes legitimately
          run past the published duration, nothing is missing, and `fit_fill`
          caps what is actually used anyway. Aborting on it would cost a
          child the story for a mismatch that carries no risk.
        * **No `item.seconds`** is the honest unknown: there is no
          independent expectation, so this step verifies nothing and says so
          in the event payload (`independent=False`) rather than letting the
          self-consistent chain pass for verification. The byte-count check
          in `boxbutler/fetch/http.py` is the other, lower half of the same
          answer, and is the only independent check available for an item
          whose feed published no duration.
        """
        probe = self.deps.renderer.probe(src)        # raises on unknown duration
        expect = item.seconds
        if expect is None or expect <= 0:
            self._event(
                run_id, E.SOURCE_DURATION, a, item_id=item.id, seconds=probe.seconds,
                independent=False,
            )
            return probe.seconds

        allowance = source_shortfall_allowance(expect)
        delta = probe.seconds - expect
        self._event(
            run_id, E.SOURCE_DURATION, a, item_id=item.id, seconds=probe.seconds,
            expect_seconds=expect, independent=True, allowance_s=allowance,
        )
        if delta < -allowance:
            self._event(
                run_id, E.SOURCE_DURATION_MISMATCH, a, item_id=item.id,
                seconds=probe.seconds, expect_seconds=expect, allowance_s=allowance,
                reason="short",
            )
            raise StagingFailed("source_duration_mismatch", item.id)
        if delta > allowance:
            self._event(
                run_id, E.SOURCE_DURATION_MISMATCH, a, item_id=item.id,
                seconds=probe.seconds, expect_seconds=expect, allowance_s=allowance,
                reason="long",
            )
        return probe.seconds

    def _render_and_verify(
        self, run_id: str, a: Assignment, item: Item, src: Path, take_seconds: float
    ) -> StagedChapter:
        """RENDER then VERIFY. Neither step can touch a tonie."""
        store = self.deps.store
        spec = RenderSpec(
            cap_seconds=int(take_seconds),
            loudnorm=item.loudnorm or self.deps.settings.loudnorm_default,
            accepts=self.deps.sink.limits.accepts,
        )
        probe = self.deps.renderer.probe(src)
        mode = choose_mode(probe, spec)

        if mode == RenditionMode.COPY:
            # Short enough and already an accepted format: the source file
            # *is* the rendition. No ffmpeg invocation at all (spec §3.4).
            dst = src
            self._event(run_id, E.RENDER, a, item_id=item.id, mode=str(mode), cached=True,
                        path=str(dst))
        else:
            dst = self.deps.cache_dir / rendition_name(item.title, item.source_key, spec, mode)
            existing = store.renditions.for_item(item.id, spec.cap_seconds, mode)
            reusable = existing is not None and existing.verified_at is not None and dst.exists()
            if reusable:
                # `verified_at` records that this file passed VERIFY once,
                # not that it still would: the cache is evictable, a
                # container can be recreated, and disk contents can rot or
                # be truncated underneath a stale DB row. Trusting the flag
                # alone here would be exactly this project's recurring bug
                # ("a staged file that exists but no longer verifies, read
                # as good") — so a cache hit still earns a fresh
                # `verify_rendition` before the render step is skipped. A
                # cached file that fails re-verification falls through to an
                # actual re-render from `src` (still on disk, untouched)
                # rather than shipping — or aborting on — stale bytes.
                reusable = verify_rendition(
                    dst, take_seconds, self.deps.renderer, self.deps.sink.limits
                ).ok
            if reusable:
                self._event(run_id, E.RENDER, a, item_id=item.id, mode=str(mode), cached=True,
                            path=str(dst))
            else:
                try:
                    self.deps.renderer.render(src, dst, spec)
                except RenderError as e:
                    raise StagingFailed("render", item.id) from e
                self._event(run_id, E.RENDER, a, item_id=item.id, mode=str(mode), cached=False,
                            path=str(dst))

        result = verify_rendition(dst, take_seconds, self.deps.renderer, self.deps.sink.limits)
        self._event(
            run_id, E.VERIFY, a, item_id=item.id, ok=result.ok, reason=result.reason,
            seconds=result.seconds, bytes=result.bytes,
        )
        if not result.ok:
            # Refuse unless demonstrably correct. Anything else — including
            # a reason we have no special handling for — aborts.
            raise StagingFailed(f"verify:{result.reason}", item.id)

        rend = store.renditions.upsert(
            item_id=item.id,
            cache_path=str(dst),
            seconds=result.seconds,
            bytes=result.bytes,
            cap_seconds=spec.cap_seconds,
            mode=mode,
            verified_at=self.deps.clock(),
        )
        return StagedChapter(
            item_id=item.id,
            path=dst,
            title=sanitise_title(item.title),   # the sanitised title reaches the sink (§3.5)
            seconds=result.seconds,
            rendition_id=rend.id if rend else None,
        )

    # ------------------------------------------------ 5-9. the swap, in order

    def swap(
        self, run_id: str, a: Assignment, target: Target, staged: Staged
    ) -> AssignmentReport:
        # 5. SNAPSHOT — the only record of what is about to be destroyed.
        snap = self.deps.sink.read_chapters(target)
        try:
            path = write_snapshot(self.deps.snapshot_dir, snap, clock=self.deps.clock, source="live")
        except (SnapshotError, OSError) as e:
            # An abort, not a warning: clearing without a record is exactly
            # the failure write-once snapshots exist to prevent.
            self._event(run_id, E.STAGING_FAILED, a, reason="snapshot", error=str(e))
            return AssignmentReport(a.id, a.target_name, RunOutcome.ABORTED_STAGING, "snapshot")
        self._event(run_id, E.SNAPSHOT, a, path=str(path), chapters=len(snap.chapters))

        # ------- only past this line may anything be destroyed -------

        staged_json = staged_to_json(staged.chapters, rotates=staged.plan.rotates)

        # 5b. WRITE-AHEAD the intent, and commit it, before destroying
        #     anything (final safety review, C3). `DEGRADED` + `staged_json`
        #     used to be written only from `_degrade`'s except handler, so
        #     they survived an *exception* and not a *process kill*: a
        #     SIGKILL, OOM or reboot between CLEAR and the first UPLOAD left
        #     the tonie empty with the committed database still reading
        #     `state=OK, staged_json=NULL, records=0` — nothing degraded, no
        #     alert, no notification, no catch-up fire, and the tonie
        #     silently empty for ~24 hours.
        #
        #     This is not a tonie-touching step and does not change the
        #     ordering: it is a local note, committed (the store connection
        #     is in autocommit mode) saying "a clear is about to happen, and
        #     these are the verified files that were about to go on".
        #     `_commit` deletes it on success; `_degrade` deletes it having
        #     written the same truth into `assignment`. A row that survives
        #     into the next process start is read by
        #     `boxbutler.orchestrator.reconcile` as "died mid-swap" and
        #     becomes DEGRADED, which the design already handles: repaired
        #     before any rotation, from these same bytes.
        self.deps.store.swap_markers.put(a.id, run_id, staged_json, self.deps.clock())

        # 6. CLEAR — one of the two calls to sink.clear() in this file that
        #    can reach a real sink (the other is `repair()`'s; see the module
        #    docstring for both and their guards). A failure here is an
        #    expected failure (a flaky cloud call that may have partially
        #    emptied the tonie), so it degrades with the staged files rather
        #    than crashing the whole run.
        try:
            self.deps.sink.clear(target)
        except Exception as e:   # noqa: BLE001 — any sink failure is a DEGRADED, not a bug
            raise ClearFailed(f"clear:{e}", staged_json) from e
        self._event(run_id, E.CLEAR, a, chapters_removed=len(snap.chapters))

        # 7-8. UPLOAD (retried) then SETTLE. Shared with `repair()`, which is
        # this same tail minus CLEAR — see `_upload_settle`.
        result = self._upload_settle(run_id, a, target, staged, staged_json)

        # 9. COMMIT
        new_cursor = self._commit(run_id, a, staged, result)
        self._event(
            run_id, E.COMMIT, a, items=[c.item_id for c in staged.chapters], cursor=new_cursor
        )
        self.prefetch(run_id, a)
        return AssignmentReport(a.id, a.target_name, RunOutcome.SWAPPED, _success_detail(a, staged))

    def _upload_settle(
        self, run_id: str, a: Assignment, target: Target, staged: Staged, staged_json: str
    ):
        """Steps 7-8 of the run loop: upload the already-verified bytes
        (retried), then settle. Shared verbatim by `swap()` (always after a
        CLEAR) and `repair()` (after a CLEAR only when R20 finds the tonie
        partially filled — see `repair`'s docstring; never after an empty or
        already-correct read). Raises `UploadFailed` / `SettleFailed`, both
        carrying `staged_json` so a caught failure can `_degrade()` without
        losing the verified files.
        """
        for ch in staged.chapters:
            try:
                self._upload_with_retry(run_id, a, target, ch)
            except UploadFailed as e:
                e.staged_json = staged_json     # keep the verified bytes for the repair
                raise

        # `transcoding` is the settle signal; a non-settled result is a
        # failure whatever its reason, and the reason is kept distinct.
        #
        # Wrapped exactly like `clear` and `upload` are, and for the same
        # reason (final safety review, C2): the real `settle` polls
        # `read_chapters`, which is live HTTP, and this call was the one
        # mutating-path sink call left bare. A dropped connection during the
        # poll therefore escaped `swap()` with the tonie already cleared and
        # uploaded, left the assignment `state=OK, staged_json=NULL`, and
        # abandoned every remaining tonie in the run. Any failure here is a
        # DEGRADED carrying the staged files, never a crash.
        try:
            result = self.deps.sink.settle(
                target, staged.total_seconds, self.deps.settings.settle_timeout_s
            )
        except Exception as e:   # noqa: BLE001 — any sink failure is a DEGRADED, not a bug
            raise SettleFailed(f"settle:{e}", staged_json) from e
        self._event(
            run_id, E.SETTLE, a, settled=result.settled, seconds=result.seconds,
            waited=result.waited_s, reason=result.reason,
        )
        if not result.settled:
            raise SettleFailed(result.reason or "not_settled", staged_json)
        if len(result.chapters) != len(staged.chapters):
            # Ambiguous mapping: committing would record the wrong sink
            # chapter id against an item and poison idempotency forever.
            raise SettleFailed("chapter_count_mismatch", staged_json)
        return result

    def _upload_with_retry(self, run_id, a: Assignment, target: Target, ch: StagedChapter) -> None:
        """Retries the same verified file. Never re-fetches, never re-renders —
        the bytes are already known good (spec §2)."""
        attempts = max(1, self.deps.settings.upload_attempts)
        backoff = self.deps.settings.upload_backoff_s or (0.0,)
        for attempt in range(attempts):
            try:
                self.deps.sink.upload(target, ch.path, ch.title)
            except Exception as e:   # noqa: BLE001 — any sink failure is retryable here
                self._event(
                    run_id, E.UPLOAD_RETRY, a, item_id=ch.item_id, attempt=attempt + 1,
                    attempts=attempts, error=str(e),
                )
                if attempt == attempts - 1:
                    raise UploadFailed(f"upload:{e}", ch.item_id) from e
                self.deps.sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            self._event(
                run_id, E.UPLOAD, a, item_id=ch.item_id, title=ch.title, path=str(ch.path),
                attempt=attempt + 1,
            )
            return

    def _commit(self, run_id: str, a: Assignment, staged: Staged, result) -> int:
        """Record the success. Nothing before this line has written a
        `chapter_record`, which is what makes a crash between CLEAR and here
        recoverable: the next run finds no record, re-plans the same item and
        re-stages it. One extra upload, never a skipped story.
        """
        store = self.deps.store
        now = self.deps.clock()
        records = [
            (ch.item_id, live.id, ch.title, live.seconds)
            for ch, live in zip(staged.chapters, result.chapters, strict=True)
        ]
        store.chapters.replace_for_assignment(a.id, records, now)

        new_cursor = a.cursor_position
        if staged.plan.rotates:
            # Advance by N, not by one: a serial load of 8 chapters must not
            # replay chapter 2 tomorrow night.
            n_eligible = len(eligible(store.items.list(a.library_id))) if a.library_id else 0
            new_cursor = advance_cursor(a.cursor_position, len(staged.chapters), n_eligible)
            store.assignments.set_cursor(a.id, new_cursor)
        store.assignments.set_last_success(a.id, now)
        for ch in staged.chapters:
            store.sources.touch(ch.item_id, now)
            if ch.rendition_id:
                store.renditions.touch(ch.rendition_id, now)
        # The swap is over and recorded: the write-ahead marker has done its
        # job, so drop it (C3). Deleted last, after every success write
        # above — a kill between those writes and this delete leaves a
        # marker whose assignment is already correct, and reconciliation
        # then degrades a tonie that is actually fine. That costs one
        # needless repair (which reads the live tonie first, finds it
        # already correct and uploads nothing); the opposite ordering would
        # cost a genuinely empty tonie its only record.
        store.swap_markers.clear(a.id)
        return new_cursor

    # ------------------------------------------------------- degraded/repair

    def _degrade(
        self, run_id: str, a: Assignment, exc: Exception, *, staged_json: str | None = None
    ) -> AssignmentReport:
        """Persist DEGRADED with the verified staged files, and report it.

        `staged_json` is the caller's override, used by `_run_one`'s
        unclassified-exception path to supply the swap marker's copy — an
        exception that nothing in this module raised carries no staged files
        of its own, and the marker is the only place they survive.
        """
        reason = getattr(exc, "reason", str(exc))
        staged_json = staged_json or getattr(exc, "staged_json", None) or a.staged_json or "[]"
        degraded = mark_degraded(a, staged_json)
        self.deps.store.assignments.set_state(
            a.id, degraded.state, staged_json=degraded.staged_json
        )
        # The assignment row now carries the same truth the marker held (and
        # more durably: DEGRADED is sticky and drives repair, metrics and
        # alerting), so the marker has nothing left to say. Cleared after
        # the state write, never before — see `_commit`.
        self.deps.store.swap_markers.clear(a.id)
        self._event(run_id, E.DEGRADED, a, reason=reason)
        return AssignmentReport(a.id, a.target_name, RunOutcome.DEGRADED, reason)

    def repair(self, run_id: str, a: Assignment, *, apply: bool) -> AssignmentReport:
        """Fill a DEGRADED tonie back in, from the staged file (Task 22;
        spec §2 "the window between 6 and 8"; R20).

        **R20 — repair observes before it acts.** `DEGRADED` means "cleared
        but not successfully filled", and it is tempting to read that as
        "the tonie is empty" and skip straight to uploading. That is an
        assumption, not a measurement, and it is wrong for a multi-chapter
        load where some chapters uploaded before the failure: the tonie is
        then *partially filled*, not empty, and blindly appending the full
        staged set on top duplicates every chapter that already made it —
        unboundedly, once per retried repair. So repair always reads the
        live chapters first and only then decides what to do:

        - **Empty tonie** — nothing to destroy. Upload the staged set, no
          clear, no snapshot.
        - **Already exactly the staged set** (same count, none transcoding,
          each position's title *and* duration matching within tolerance —
          see `_live_matches_staged` for why title is the right signal
          here, even though it is never trusted for item identity elsewhere
          in this project) — nothing to do. Commit the record and mark
          repaired; zero uploads.
        - **Anything else (partially filled / stale content)** — repair
          *may* clear here. What stage-then-swap actually protects is never
          "never clear during repair" as a blanket rule; it is "never
          destroy without a verified replacement already in hand" — and a
          repair holds a fully verified staged set by definition, which is
          what makes it a repair rather than a fresh run. So: write-once
          SNAPSHOT (this *is* about to destroy state, so the record is
          exactly the point — `SnapshotError` aborts, same as `swap()`),
          then CLEAR, then upload the full staged set, then settle.

        No re-download, no re-render: the staged bytes were verified before
        the tonie was cleared. But the cache is evictable and a container can
        be recreated, so every staged file is re-verified (not just checked
        for existence) before being reused — `verify_rendition` catches a
        file that still exists but no longer decodes correctly, not only one
        that vanished (`_reload_staged` reports which, distinctly). If any
        staged file fails that check, repair falls back to staging fresh
        content via the ordinary `plan()` -> `stage()` path, and says so via
        `E.REPAIR_RESTAGED` — never a silent skip, never a silent "success"
        over unverified bytes.

        `mark_repaired` is called only after the tonie is proven, by a fresh
        read or a settled upload, to hold the staged set. A dry run reports
        what repair would do (`RunOutcome.DRY_RUN`) and stops before touching
        anything, including the staged-file re-verification and the live
        read, both of which are genuinely repair work, not planning.
        """
        self._event(run_id, E.REPAIR_START, a)

        try:
            chapters_meta = json.loads(a.staged_json) if a.staged_json else []
        except (json.JSONDecodeError, TypeError):
            chapters_meta = []

        if not apply:
            detail = (
                f"would repair from {chapters_meta[0]['path']}"
                if chapters_meta
                else "would repair"
            )
            return AssignmentReport(a.id, a.target_name, RunOutcome.DRY_RUN, detail)

        target = self._target(a)
        if target is None:
            self._event(run_id, E.PLAN_FAILED, a, reason="target_missing")
            return AssignmentReport(a.id, a.target_name, RunOutcome.PLAN_FAILED, "target_missing")

        staged, reload_failure = self._reload_staged(chapters_meta)
        if staged is None:
            # The staged files are gone or no longer verify: fall back to
            # fetching/rendering/verifying fresh content (still side-effect
            # free on the tonie) rather than silently failing or silently
            # shipping something unverified.
            self._event(
                run_id, E.REPAIR_RESTAGED, a, reason=reload_failure or "no_staged_chapters"
            )
            try:
                # repair() only ever reaches here after its own dry-run
                # early-return, so this is always a real (apply=True) plan.
                fresh_plan = self.plan(run_id, a, apply=True)
                if not fresh_plan.item_ids:
                    reason = fresh_plan.reason or E.NO_CANDIDATE
                    self._event(run_id, E.PLAN_FAILED, a, reason=reason)
                    return AssignmentReport(a.id, a.target_name, RunOutcome.PLAN_FAILED, reason)
                staged = self.stage(run_id, a, fresh_plan)
            except StagingFailed as e:
                # Keeps whatever staged_json the assignment already had
                # (`_degrade` falls back to `a.staged_json` when the
                # exception carries none) — still DEGRADED, still retried
                # next run.
                self._event(run_id, E.STAGING_FAILED, a, reason=e.reason, item_id=e.item_id)
                return self._degrade(run_id, a, e)

        staged_json = staged_to_json(staged.chapters, rotates=staged.plan.rotates)

        # R20: measure the tonie's actual state, never inherit it from the
        # reason the assignment was degraded.
        live = self.deps.sink.read_chapters(target)
        try:
            if not live.chapters:
                result = self._upload_settle(run_id, a, target, staged, staged_json)
            elif self._live_matches_staged(live.chapters, staged):
                # Already correct: a prior attempt evidently finished the
                # upload even though *this* record never saw it settle.
                # Nothing to destroy and nothing to upload — just prove and
                # commit what is already there.
                result = SettleResult(
                    settled=True,
                    seconds=sum(c.seconds for c in live.chapters),
                    chapters=list(live.chapters),
                    waited_s=0.0,
                    reason=None,
                )
                self._event(
                    run_id, E.SETTLE, a, settled=True, seconds=result.seconds, waited=0.0,
                    reason=None,
                )
            else:
                # Partially filled or stale: neither empty nor correct. A
                # repair holds a fully verified replacement by definition —
                # that is what makes it a repair — so it may destroy the
                # current (already-wrong) content, but only behind the same
                # write-once record `swap()` requires.
                try:
                    path = write_snapshot(self.deps.snapshot_dir, live, clock=self.deps.clock, source="live")
                except (SnapshotError, OSError) as e:
                    self._event(run_id, E.STAGING_FAILED, a, reason="snapshot", error=str(e))
                    return AssignmentReport(
                        a.id, a.target_name, RunOutcome.ABORTED_STAGING, "snapshot"
                    )
                self._event(run_id, E.SNAPSHOT, a, path=str(path), chapters=len(live.chapters))
                # Write-ahead, same as `swap()` (C3): a repair's clear can be
                # interrupted by a process kill exactly like a rotation's,
                # and a repair that dies mid-clear must not come back as OK.
                # The assignment is already DEGRADED here, so the marker's
                # real work is keeping `staged_json` — which `mark_repaired`
                # would otherwise be free to clear — recoverable.
                self.deps.store.swap_markers.put(
                    a.id, run_id, staged_json, self.deps.clock()
                )
                try:
                    self.deps.sink.clear(target)
                except Exception as e:   # noqa: BLE001 — any sink failure is a DEGRADED, not a bug
                    raise ClearFailed(f"clear:{e}", staged_json) from e
                self._event(run_id, E.CLEAR, a, chapters_removed=len(live.chapters))
                result = self._upload_settle(run_id, a, target, staged, staged_json)
        except (ClearFailed, UploadFailed, SettleFailed) as e:
            return self._degrade(run_id, a, e)

        new_cursor = self._commit(run_id, a, staged, result)
        self._event(
            run_id, E.COMMIT, a, items=[c.item_id for c in staged.chapters], cursor=new_cursor
        )

        # Only now — after the tonie is proven (by a fresh read or a settled
        # upload) to hold the staged set — is the assignment allowed back to
        # OK. `mark_repaired` takes no evidence parameter by design (R2): it
        # is never called optimistically, only from this one place.
        repaired = mark_repaired(self.deps.store.assignments.get(a.id), self.deps.clock())
        self.deps.store.assignments.set_state(
            a.id, repaired.state, staged_json=repaired.staged_json
        )
        self._event(run_id, E.REPAIRED, a)
        self.prefetch(run_id, a)
        return AssignmentReport(
            a.id, a.target_name, RunOutcome.REPAIRED, _success_detail(a, staged, repaired=True)
        )

    def _live_matches_staged(self, live: list[LiveChapter], staged: Staged) -> bool:
        """Best-effort "does the tonie already hold exactly the staged set,
        in order" check, used only to decide whether repair has any
        destructive or uploading work left to do.

        Matches position-wise on **(title, duration-within-tolerance, not
        transcoding)**. Title is deliberately included here even though this
        project's identity discipline elsewhere (see
        `boxbutler/domain/idempotency.py`) never matches on title — that
        rule protects *item identity against upstream mutation* (a YouTube
        video or RSS entry can be re-titled by its publisher, so
        `source_key`/`sink_chapter_id` must be the real key). A live
        chapter's title is not upstream-mutable: it is a string this code
        set itself at upload time, from its own sanitised staged set, so
        comparing it here is comparing this code's own output against its
        own intent — the only signal available, since a partial upload never
        reached COMMIT and so has no `chapter_record`/`sink_chapter_id` to
        anchor against.

        Duration alone is not enough: two same-length chapters swapped would
        pass a duration-only check with an identical count and set of
        durations, silently committing an audiobook with chapters 4 and 5
        transposed. A live chapter still transcoding is never "already
        correct" either: its true duration is not known yet.
        """
        if len(live) != len(staged.chapters):
            return False
        return all(
            not c.transcoding
            and c.title == ch.title
            and abs(c.seconds - ch.seconds) <= DURATION_TOLERANCE_S
            for c, ch in zip(live, staged.chapters, strict=True)
        )

    def _reload_staged(self, chapters_meta: list[dict]) -> tuple[Staged | None, str | None]:
        """Re-verify every chapter named in a DEGRADED assignment's
        `staged_json` and rebuild a `Staged` from them, with **no** fetch or
        render call. Returns `(None, reason)` — never a partial result — if
        the list is empty or any single chapter fails: a repair reusing
        three good files and one corrupt one would ship a mis-mapped
        chapter set, which is the same "unknown read as definite" failure
        this project keeps refusing elsewhere (`verify.py`'s NaN guard,
        `settle.py`'s `settled_empty`).

        The failure `reason` distinguishes "missing" from "present but
        fails verification" — an evicted cache and actual corruption call
        for different operator responses, so the distinction is carried
        through rather than collapsed into one generic label.
        """
        if not chapters_meta:
            return None, None
        chapters: list[StagedChapter] = []
        for c in chapters_meta:
            path = Path(c["path"])
            if not path.exists():
                return None, "staged_file_missing"
            result = verify_rendition(path, c["seconds"], self.deps.renderer, self.deps.sink.limits)
            if not result.ok:
                return None, f"staged_file_failed_verify:{result.reason}"
            chapters.append(
                StagedChapter(item_id=c["item_id"], path=path, title=c["title"], seconds=c["seconds"])
            )
        rotates = bool(chapters_meta[0].get("rotates", True))
        plan = Plan(item_ids=[c.item_id for c in chapters], relaxations=[], reason=None, rotates=rotates)
        return Staged(chapters=chapters, total_seconds=sum(c.seconds for c in chapters), plan=plan), None

    def stage_item(self, run_id: str, a: Assignment, item: Item, cap: float) -> StagedChapter:
        """Fetch, render and verify one item on its own, capped at `cap`
        seconds — the single-item unit `prefetch_assignment` (Task 24)
        builds on, made of the same `_fetch` / `_render_and_verify`
        primitives `_stage_plan` uses for the item it is currently packing.
        Never touches a tonie.
        """
        src = self._fetch(run_id, a, item)
        # Same independent check as `_stage_plan` (M2): a prefetch that
        # accepted a short download would warm the cache with it, and a
        # later run's cache hit would ship it.
        seconds = self._probe_source(run_id, a, item, src)
        take_seconds = min(seconds, float(cap))
        return self._render_and_verify(run_id, a, item, src, take_seconds)

    def prefetch(self, run_id: str, a: Assignment) -> None:
        """Fetch/render/verify the next `prefetch_depth` items (Task 24;
        spec §2 "Prefetch depth"). Called from the tail of `swap()` and
        `repair()`, both of which still hold the pre-commit `a` — so this
        re-reads the assignment from the store to see the cursor `_commit`
        just advanced, not the stale one the caller has in hand.

        A prefetch failure must never fail an already-successful run: the
        tonie is correctly loaded either way, so anything unexpected here
        is recorded and swallowed, never re-raised. `prefetch_assignment`
        itself already handles the two *expected* fetch failure classes
        (`ItemUnavailable`, `ExtractionBroken`) without raising; this catch
        is the belt-and-braces backstop for a genuine bug in that path.
        """
        depth = self.deps.settings.prefetch_depth
        if depth <= 0:
            return
        fresh = self.deps.store.assignments.get(a.id)
        if fresh is None:
            return
        try:
            prefetch_assignment(self, run_id, fresh, depth)
        except Exception as e:  # noqa: BLE001 — see docstring: never fail the run
            self._event(run_id, E.STAGING_FAILED, a, reason=f"prefetch_error:{e}")

    def _evict(self, run_id: str) -> None:
        """Retention (Task 24; spec §3.6): budgeted LRU eviction. Never
        called on a dry run (see `run()`). A failure here must not fail an
        otherwise-successful run either — an over-budget cache is a
        housekeeping problem, not a reason to mark tonight's rotations as
        crashed.

        Threads the real cooldown/duplicate-avoidance settings through to
        `evict` -> `protected_paths` -> `upcoming_item_ids`, so retention
        protects exactly the window prefetch actually warms (review
        Important 2), and reports (`E.EVICT`,
        `reason="budget_exceeded_by_protected_content"`) when protected
        content alone still exceeds the budget after eviction — a budget
        that quietly stops being enforced must be visible, not silent
        (review Important 3). This is not necessarily a misconfiguration:
        several tonies legitimately holding close to the cap is real
        content nothing here may delete.
        """
        s = self.deps.settings
        kwargs = dict(
            avoid_duplicates=s.avoid_duplicates,
            repeat_cooldown_days=s.repeat_cooldown_days,
            now=self.deps.clock(),
        )
        try:
            evicted = evict(
                self.deps.store,
                self.deps.cache_dir,
                s.cache_budget_bytes,
                s.prefetch_depth,
                apply=True,
                **kwargs,
            )
        except Exception as e:  # noqa: BLE001 — retention must never fail the run
            self._event(run_id, E.EVICT, None, reason=f"evict_error:{e}")
            return
        if evicted:
            self._event(
                run_id, E.EVICT, None,
                count=len(evicted),
                bytes=sum(e.bytes for e in evicted),
                orphans=sum(1 for e in evicted if e.kind == "orphan"),
                renditions=sum(1 for e in evicted if e.kind == "rendition"),
                sources=sum(1 for e in evicted if e.kind == "source"),
            )
        try:
            remaining = cache_usage_bytes(self.deps.cache_dir)
        except OSError:
            return
        if remaining > s.cache_budget_bytes:
            self._event(
                run_id, E.EVICT, None,
                reason="budget_exceeded_by_protected_content",
                bytes=remaining, budget_bytes=s.cache_budget_bytes,
            )

    # ---------------------------------------------------------------- plumbing

    def _target(self, a: Assignment) -> Target | None:
        for t in self.deps.sink.list_targets():
            if t.id == a.target_id:
                return t
        return None

    # --------------------------------------------------------- notifications

    def _notify(self, kind: str, title: str, body: str) -> None:
        """Task 25 (spec §9.1). `deps.notifier is None` means "no
        notifications configured" and is a true no-op. Any other notifier —
        including a caller's own — must never be allowed to fail a run: a
        notification is reporting on the outcome, not part of producing it,
        so any exception it raises is logged (never re-raised) exactly like
        `NtfyNotifier` already swallows its own network errors.
        """
        notifier = self.deps.notifier
        if notifier is None:
            return
        try:
            getattr(notifier, kind)(title, body)
        except Exception as e:   # noqa: BLE001 — a notifier must never fail a run
            if self.deps.log is not None:
                self.deps.log("notify_error", {"kind": kind, "error": str(e)})

    def _notify_outcome(self, r: AssignmentReport) -> None:
        """The failure/success half of Task 25's wiring: DEGRADED,
        CRASHED and ABORTED_STAGING (which includes `extraction_broken` as
        a reason — see `_fetch`) are `on_failure`; SWAPPED and REPAIRED
        are `on_success`. Every other outcome (PLAN_FAILED, SKIPPED_
        ALREADY_CURRENT, DRY_RUN, UNMANAGED) is neither: a normal "nothing
        to do" or "will retry next run" result is not the thing spec
        §9.1 is for.

        `CRASHED` (final fix review, masking-defect 1): an unclassified
        exception in `_run_one` reaches here as `RunOutcome.CRASHED`, with
        the tonie deliberately untouched (its `assignment.state` is
        whatever it already was — see `_run_one`'s bare
        `except Exception`). Before this branch existed, a CRASHED
        assignment raised no notification at all, and the only fallback
        signal, `BoxButlerRunStale`, was a single gauge shared across every
        assignment on the box, so one tonie crashing every single night
        stayed invisible for as long as its siblings kept succeeding.
        Notified here the same way DEGRADED already is, so the operator
        hears about it on the channel they actually watch, not only on the
        History screen. Title is ASCII-only, like every other notification
        title here.
        """
        if r.outcome == RunOutcome.CRASHED:
            body = f"{r.target_name} hit an unexpected error; its tonie was not touched ({r.detail})"
            guidance = E.explain_reason(r.detail)
            if guidance:
                body += f" -- {guidance}"
            self._notify("failure", f"{r.target_name}: run crashed", body)
        elif r.outcome == RunOutcome.DEGRADED:
            body = f"{r.target_name} is DEGRADED and may be empty until repaired ({r.detail})"
            guidance = E.explain_reason(r.detail)
            if guidance:
                body += f" -- {guidance}"
            self._notify("failure", f"{r.target_name}: tonie may be empty", body)
        elif r.outcome == RunOutcome.ABORTED_STAGING:
            body = f"{r.target_name} run aborted before touching the tonie ({r.detail})"
            # Final coherence review, Major-4: the raw reason code (e.g.
            # "extraction_broken") stays in the body above -- metrics/tests
            # key off it and it must never disappear -- but a bare code
            # is not actionable at bedtime. Append operator guidance when
            # one exists for this reason, so a fixable evening isn't
            # reported as a mystery.
            guidance = E.explain_reason(r.detail)
            if guidance:
                body += f" -- {guidance}"
            self._notify("failure", f"{r.target_name}: run failed", body)
        elif r.outcome in (RunOutcome.SWAPPED, RunOutcome.REPAIRED):
            self._notify(
                "success",
                f"{r.target_name} updated",
                r.detail or f"{r.target_name} rotated",
            )

    def _event(self, run_id: str, event: str, a: Assignment | None = None, **payload) -> None:
        payload = {k: (str(v) if isinstance(v, Path) else v) for k, v in payload.items()}
        self.deps.store.runs.event(run_id, event, a.id if a else None, **payload)
        if self.deps.log is not None:
            self.deps.log(
                event,
                {
                    "run_id": run_id,
                    "assignment_id": a.id if a else None,
                    "target": a.target_name if a else None,
                    **payload,
                },
            )


__all__ = [
    "AssignmentReport",
    "ClearFailed",
    "Deps",
    "Orchestrator",
    "RotationSettings",
    "RunReport",
    "SettleFailed",
    "Staged",
    "StagedChapter",
    "StagingFailed",
    "UploadFailed",
    "default_timezone_name",
    "rotation_window_key",
    "staged_to_json",
]
