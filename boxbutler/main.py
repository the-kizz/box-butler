"""The composition root (Task 29; spec §7, §3.1, §8, §11 P5).

Every layer of Box Butler up to this task exists and is tested in
isolation: domain, store, sinks, fetchers, audio pipeline, orchestrator,
notifier, web UI, CLI, scheduler. Nothing joined them — no production code
anywhere constructed `boxbutler.orchestrator.run.Deps`; it appeared only in
test fixtures. This module is the first place that changes: `build()`
constructs one real `Deps`/`Orchestrator` from a `Settings`, and
`create_web_app()`/`serve()`/`python -m boxbutler` are the first
production code paths that can reach a real tonie.

## Deferred wiring this module closes

1. **`Settings` -> `build_notifier` -> `Deps.notifier`.** `LiveNotifier`
   below re-reads `store.settings` (kind/server/topic/on_*) on every call
   and rebuilds a `Notifier` via `boxbutler.notify.none.build_notifier`,
   the same way `effective_rotation_settings` re-reads schedule/timezone —
   a Settings-screen edit to notifications takes effect on the very next
   notification, not just at the next deploy. The bearer token is the one
   exception: per `boxbutler/web/routes/settings.py`'s own contract
   ("write-only from the UI's perspective"), it is never DB-owned — it
   comes from `Settings.notify_token` (i.e. `BOXBUTLER_NOTIFY_TOKEN`),
   captured once at process start, exactly like `ToniesCloudSink.from_env`
   is the only place sink credentials are read.
2. **The rotation window.** `build()` seeds the configured `schedule`/
   `timezone` into the store on first start (`seed_db_settings`) and
   `OrchestratorRunner` re-derives a fresh `RotationSettings` via
   `effective_rotation_settings` immediately before every real run it
   drives (UI "Run now"/"Repair", and the scheduler's fired job, which
   goes through the same runner) — so an operator's Settings-screen edit
   to schedule, timezone, cap, cache budget, prefetch depth, duplicate
   avoidance or cooldown reaches the orchestrator on the very next run,
   with no restart, the same guarantee `Scheduler` already gives its own
   *timing*.
3. **The fake-data screen driver never meets a real sink.** `create_web_app`
   always constructs `OrchestratorRunner` — never
   `boxbutler.web.fake_data.FakeRunner` — regardless of `settings.sink_kind`.
   `boxbutler/web/dev.py` is untouched and still wires `FakeRunner` itself.
4. **The three ingest placeholders.** `create_web_app` passes
   `Ingestor(...).add_ref` / `.add_upload` and a real folder-scan closure
   as `boxbutler.web.app.create_app`'s new `ingest`/`ingest_upload`/
   `scan_folder` keyword arguments.

## What `build()` deliberately does NOT do

`ToniesCloudSink.limits` performs a real network call (`GET /config`) on
first access and is memoized for the process lifetime — by design, so
that constructing a sink is free. `build()` never reads `.limits`: doing
so unconditionally would mean even `boxbutler status`/`library list`/a
unit test wiring a real sink touches the network before anything asked it
to. `AppDeps.settings`/the orchestrator's own `Deps.settings` are built
from the store's raw (unclamped) values at construction time; the
orchestrator itself already clamps `cap_seconds` against the sink's real
`maxSeconds` at the point it actually renders something
(`run.py:856`, inside `stage()`, which only ever runs under `apply=True`)
— so no unclamped value here can produce an over-cap upload. Clamping
again in `OrchestratorRunner`'s pre-run refresh (via
`effective_rotation_settings`) is therefore an early, informational
clamp for cache/status arithmetic, not the safety-critical one.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import uvicorn
from fastapi import FastAPI

from boxbutler.audio.ffmpeg import FfmpegRenderer
from boxbutler.audio.protocol import RendererProtocol
from boxbutler.config import ConfigError, Settings, effective_rotation_settings, load_settings, seed_db_settings
from boxbutler.domain.models import RunTrigger
from boxbutler.fetch.folder import FolderFetcher
from boxbutler.fetch.http import HttpFetcher
from boxbutler.fetch.protocol import ExtractionBroken, FetcherProtocol
from boxbutler.fetch.ytdlp import YtDlpFetcher
from boxbutler.logging import make_logger
from boxbutler import metrics as bb_metrics
from boxbutler.notify.none import NullNotifier, build_notifier
from boxbutler.notify.protocol import Notifier, NotifyPolicy
from boxbutler.orchestrator.reconcile import reconcile_interrupted_swaps
from boxbutler.orchestrator.retention import evict
from boxbutler.orchestrator.run import Deps, Orchestrator, RotationSettings, default_timezone_name
from boxbutler.runlock import RunInProgress, RunLock
from boxbutler.scheduler.scheduler import Scheduler, parse_hhmm
from boxbutler.sinks.fake import FakeSink
from boxbutler.sinks.protocol import SinkProtocol
from boxbutler.sinks.tonies_cloud import ToniesCloudSink
from boxbutler.sources.direct_url import DirectUrlSource
from boxbutler.sources.folder import scan_folder as _scan_folder_dir
from boxbutler.sources.folder import sync_media_root as _sync_media_root_dir
from boxbutler.sources.library_folder import (
    LibraryFolderError,
    ensure_library_folders,
    require_media_root,
)
from boxbutler.sources.ingest import Ingestor
from boxbutler.sources.rss import RssSource
from boxbutler.sources.upload import UploadSource
from boxbutler.sources.youtube_playlist import YouTubePlaylistSource
from boxbutler.sources.youtube_url import YouTubeUrlSource
from boxbutler.store.db import Store
from boxbutler.web.app import create_app
from boxbutler.web.settings import WebSettings

logger = logging.getLogger(__name__)

# No sink-name constant lives here any more. The `assignment.sink`
# discriminator is `sink.name` (`SinkProtocol.name`), read off whichever
# sink `_build_sink` returned, and `Deps.sink_name` derives from the same
# object — so the web layer, the setup wizard, the CLI, the orchestrator
# and this module cannot disagree about which rows belong to this
# deployment. They did disagree before (final safety review, C1): this
# module built "tonies_cloud" while the routes imported "fake", and a real
# install silently rotated nothing, forever.


# ------------------------------------------------------------- fetchers


class CompositeFetcher:
    """`FetcherProtocol` over an ordered list of fetchers — the first one
    that `supports()` an item's kind fetches it. Mirrors
    `boxbutler.sources.ingest.Ingestor`'s own "first match wins" shape for
    resolvers, just one layer down (bytes, not identity).
    """

    def __init__(self, fetchers: list[FetcherProtocol]) -> None:
        self._fetchers = fetchers

    def supports(self, kind) -> bool:
        return any(f.supports(kind) for f in self._fetchers)

    def fetch(self, item, cache_dir: Path) -> Path:
        for f in self._fetchers:
            if f.supports(item.kind):
                return f.fetch(item, cache_dir)
        raise ExtractionBroken(f"no configured fetcher supports item kind {item.kind!r}")


# ------------------------------------------------------------- notifier


class LiveNotifier:
    """`Notifier` that re-reads `store.settings` on every call (deferred
    wiring item 1 above). Never caches a built notifier across calls —
    notifications are rare (at most a handful per run) so rebuilding is
    cheap, and caching would reintroduce exactly the "ticked a box, wired
    to nothing" defect this module exists to close, if the box were
    ticked after the cached notifier was built.
    """

    def __init__(self, store: Store, token: str | None) -> None:
        self._store = store
        self._token = token
        # One `NullNotifier` reused for the process lifetime (Task 29
        # review, Minor 4): `notify/none.py`'s own docstring says `.calls`
        # is the record by which a future history/health surface tells
        # "quiet because healthy" from "quiet because unconfigured".
        # Building a fresh `NullNotifier` per call (as `build_notifier`
        # does whenever notifications resolve to a no-op) would drop that
        # record every time, so it could never accumulate.
        self._null = NullNotifier()

    def _current(self) -> Notifier:
        get = self._store.settings.get
        policy = NotifyPolicy(
            on_failure=get("notify_on_failure", True),
            on_success=get("notify_on_success", False),
            on_debug=get("notify_on_debug", False),
        )
        notifier = build_notifier(
            get("notify_kind", "none"), get("notify_server"), get("notify_topic"), self._token, policy
        )
        return self._null if isinstance(notifier, NullNotifier) else notifier

    def failure(self, title: str, body: str) -> None:
        self._current().failure(title, body)

    def success(self, title: str, body: str) -> None:
        self._current().success(title, body)

    def debug(self, title: str, body: str) -> None:
        self._current().debug(title, body)


# --------------------------------------------------------------- AppDeps


@dataclass
class AppDeps:
    """Everything a running Box Butler process needs. Structurally
    satisfies `boxbutler.cli.main.AppDeps` (`store`/`sink`/`orchestrator`/
    `settings`/`snapshot_dir`/`cache_dir`, where `settings` is the
    `RotationSettings` the CLI reads `.prefetch_depth`/`.cache_budget_bytes`
    from — not `config.Settings`, which is kept separately as `config`).

    `runner`, `run_lock` and `config` are additions beyond the brief's
    one-line field list: `runner` is the single `OrchestratorRunner` — and
    therefore the single `threading.Lock` — shared by every real-run entry
    point (UI "Run now"/"Repair", the scheduler's fired job, and now the CLI
    as well), which is what makes the "a run already in progress" 409 mean
    anything; `run_lock` is the `flock` under `<data_dir>/run.lock` that
    makes it mean something *across* processes too, because `docker exec …
    boxbutler run --apply` is a second process and is the documented
    remediation for a degraded tonie (final safety review, M1); `config` is
    the raw
    env+YAML `Settings` (secrets, listen port, web cookie policy) that
    `create_web_app` needs and `RotationSettings` doesn't carry.

    `settings` is a *property*, not a field (Task 29 review, MAJOR-4): it
    used to be a second `RotationSettings` built once in `build()` and
    never refreshed, which silently diverged from
    `orchestrator.deps.settings` — the one `OrchestratorRunner._refresh_settings`
    actually updates — after the first real run. That made `AppDeps.settings`
    (precisely the field `cli.main.AppDeps`'s Protocol reads for
    `status`/`cache` arithmetic) a stale, unclamped read the moment more
    than one run had happened. Delegating here means there is exactly one
    live `RotationSettings` per process, by construction, and no second
    copy that can ever disagree with it.
    """

    store: Store
    sink: SinkProtocol
    orchestrator: Orchestrator
    snapshot_dir: Path
    cache_dir: Path
    notifier: Notifier
    scheduler: Scheduler | None = None
    runner: "OrchestratorRunner | None" = None
    run_lock: RunLock | None = None
    config: Settings | None = None

    @property
    def settings(self) -> RotationSettings:
        return self.orchestrator.deps.settings


# ---------------------------------------------------------- OrchestratorRunner


class OrchestratorRunner:
    """Implements `boxbutler.web.app.RunnerProtocol` (and the shape
    `boxbutler.cli.main` expects of an orchestrator-driving object) by
    calling straight through to `deps.orchestrator`. `apply` passes
    through unchanged in every direction — this class adds exactly two
    things the orchestrator itself doesn't do: refusing to run two applies
    at once (a `threading.Lock`, spec's "a run already in progress" ->
    HTTP 409), and refreshing `deps.orchestrator.deps.settings` from the
    store immediately before delegating, so schedule/timezone/cap/etc.
    Settings-screen edits reach the very next run (deferred wiring item 2).
    """

    #: How long a scheduled fire will wait for a UI-held lock before
    #: giving up on tonight (Task 29 review, MAJOR-1). Generous relative
    #: to how long a "Run now" dry run actually takes (seconds to low
    #: minutes over a handful of assignments), but bounded so a
    #: genuinely stuck run can't wedge the scheduler thread forever.
    SCHEDULED_LOCK_TIMEOUT_S = 300.0

    def __init__(self, deps: AppDeps) -> None:
        self.deps = deps
        self._lock = threading.Lock()

    def _refresh_settings(self) -> None:
        self.deps.orchestrator.deps.settings = effective_rotation_settings(
            self.deps.store, self.deps.sink.limits
        )

    def _sync_media_root(self) -> None:
        """Task 38: reflect `/media`'s immediate subfolders as
        folder-backed libraries before *every* run (manual or
        scheduled — both call this through `_run_locked`, the single
        place they share, so one hook covers both halves of "scanned on
        a schedule and before each run"). A scan failure must never block
        the run itself -- last night's plan/staged tonie is still valid
        even if tonight's filesystem read glitched -- so this logs and
        swallows rather than propagating.
        """
        media_root = self.deps.config.media_root if self.deps.config else None
        if media_root is None:
            # Only reachable from a hand-built `AppDeps` with no `config`
            # (tests). `build()` has already refused to start without a
            # usable media root on every real path.
            return
        try:
            _sync_media_root_dir(
                self.deps.store, media_root, probe=self.deps.orchestrator.deps.renderer.probe
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "media_root sync failed for %s; continuing with existing library state", media_root
            )

    def _run_locked(self, *, assignment_ids: list[str] | None, apply: bool, trigger: RunTrigger) -> str:
        """Body of an orchestrator run once the run lock is already held.
        `run()` and `run_scheduled()` acquire the lock differently (fail
        fast vs. wait, per their own docstrings) but both must do exactly
        the same thing with it -- refresh settings, sync `/media`, then
        delegate to the orchestrator. Previously each entry point had its
        own copy of that sequence, and the scheduled one silently drifted
        (Task 38 review, Critical-1: it never called `_sync_media_root`
        at all). Routing both through this single method means a future
        third entry point inherits the sync just by calling it, instead
        of needing someone to remember to paste the call in again.

        Also reconciles any interrupted swap here (final fix review,
        masking-defect 2), not just once at `build()` startup. `run()` and
        `run_scheduled()` both call this only after they already hold the
        run lock (in-process `threading.Lock` and, on every production
        path, the cross-process `RunLock`) -- the same lock `build()`'s own
        startup reconciliation takes before calling
        `reconcile_interrupted_swaps`. Because every caller serializes on
        that lock, this can never run concurrently with the startup pass
        or with another `_run_locked` -- there is no window for two
        reconciliations to race. Nor can it "double-reconcile": the
        function itself is idempotent (it clears each marker the moment it
        reconciles it -- see its own docstring), so calling it again here
        after a clean startup, when there is nothing to reconcile, is one
        cheap `SELECT` against an almost-certainly-empty table -- the
        common case pays a query, not a write. What this buys: a
        `BaseException` in the run thread after `clear()` (the case
        `build()`-only reconciliation could never reach for a live
        server's whole remaining lifetime), or a lock conflict at startup
        that skipped reconciliation entirely, is now caught at the very
        next run this process drives, not only at the next process
        restart.
        """
        reconciled = reconcile_interrupted_swaps(
            self.deps.store, log=self.deps.orchestrator.deps.log
        )
        if reconciled:
            logging.getLogger(__name__).critical(
                "%d assignment(s) were mid-swap when a previous run died and "
                "have been marked DEGRADED; their tonies may be empty until "
                "repaired: %s",
                len(reconciled), ", ".join(reconciled),
            )
        self._refresh_settings()
        self._sync_media_root()
        report = self.deps.orchestrator.run(
            assignment_ids=assignment_ids, apply=apply, trigger=trigger
        )
        self._refresh_metrics()
        return report.run_id

    def _refresh_metrics(self) -> None:
        """Update the exported gauges right after a run, so the History
        screen and `/metrics` agree without waiting for the next scrape.
        `/metrics` itself refreshes again on every scrape (see
        `boxbutler.main._mount_metrics`) -- this call is a freshness
        nicety, not the only place staleness is prevented, so a failure
        here must never take down a real run.
        """
        try:
            bb_metrics.refresh(
                self.deps.store, self.deps.cache_dir, self.deps.settings.prefetch_depth
            )
        except Exception:
            logging.getLogger(__name__).exception("metrics refresh after run failed")

    def _locked(self, *, timeout: float, what: str):
        """The cross-process half of "two runs never overlap" (final safety
        review, M1).

        Returns a context manager either way so the call sites read the
        same: with no `RunLock` configured (a fixture-built runner in a
        single-process test) it is a no-op and the `threading.Lock` above is
        the only exclusion; every production path gets a real lock from
        `build()`, under `<data_dir>/run.lock`.
        """
        lock = self.deps.run_lock
        if lock is None:
            return nullcontext()
        return lock.held(timeout=timeout, what=what)

    def run(self, *, assignment_ids: list[str] | None, apply: bool, trigger: RunTrigger) -> str:
        """Fail fast on contention -- in this process, and now in any other.

        The CLI's `run` comes through here too (M1). It used to call
        `orchestrator.run` directly, taking neither lock, so the very command
        the runbook and `monitoring/alerts.yml` tell an operator to run "if
        bedtime is near" could CLEAR a tonie the scheduler was already
        mid-swap on: two CLEARs, two UPLOADs, and twice the cap on one tonie.
        """
        if not self._lock.acquire(blocking=False):
            raise RunInProgress("a run is in progress")
        try:
            with self._locked(timeout=0.0, what="a run"):
                return self._run_locked(
                    assignment_ids=assignment_ids, apply=apply, trigger=trigger
                )
        finally:
            self._lock.release()

    def run_scheduled(self) -> str | None:
        """The scheduler's fired job (`build()`'s `on_fire`) -- the one
        entry point that must never be dropped by lock contention (Task
        29 review, MAJOR-1). `run`/`repair`/`prefetch` fail fast with a
        409 on contention, which is correct for an HTTP request (two
        concurrent UI actions must not both try to CLEAR the same tonie)
        but was wrong here: `run_forever` catches *any* exception from
        `on_fire`, logs one line among many, and moves on to tomorrow --
        so a harmless dry-run "Run now" click in flight at the scheduled
        instant silently cancelled the whole night's rotation, for a
        dry run that wrote nothing.

        This method instead *waits* for the lock, because the scheduled
        run is the one that matters most and losing a night to a
        concurrent dry run is far worse than starting a few minutes
        late. The lock still guarantees only one apply runs at a time --
        this changes who loses a race, not whether one can happen. If
        the lock is still held after `SCHEDULED_LOCK_TIMEOUT_S` (some
        run is genuinely stuck), the night is skipped, but loudly: a
        `logger.critical` line distinguishable from routine `on_fire`
        logging, plus a best-effort push notification, so an operator
        watching either channel sees that a whole night was missed
        rather than one INFO line among many.

        Both locks are waited on, on the same terms and with the same
        loudness (M1): an operator's `docker exec ... boxbutler run --apply`
        holds the *cross-process* one, and losing the night to that silently
        would be the same defect MAJOR-1 fixed for the in-process case.
        """
        if not self._lock.acquire(timeout=self.SCHEDULED_LOCK_TIMEOUT_S):
            self._report_deferred_night()
            return None
        try:
            with self._locked(
                timeout=self.SCHEDULED_LOCK_TIMEOUT_S, what="a run in another process"
            ):
                return self._run_locked(
                    assignment_ids=None, apply=True, trigger=RunTrigger.SCHEDULE
                )
        except RunInProgress:
            self._report_deferred_night()
            return None
        finally:
            self._lock.release()

    def _report_deferred_night(self) -> None:
        """Say "tonight did not happen" once, loudly -- shared by the
        in-process and the cross-process lock timeout so neither can be the
        quiet one."""
        logger.critical(
            "scheduled run deferred: could not acquire the run lock "
            "within %.0fs (another run is still in progress) -- "
            "tonight's rotation did NOT happen",
            self.SCHEDULED_LOCK_TIMEOUT_S,
        )
        try:
            self.deps.notifier.failure(
                "Box Butler: scheduled run skipped",
                "The nightly rotation could not start because another run "
                f"held the lock for over {self.SCHEDULED_LOCK_TIMEOUT_S:.0f}s.",
            )
        except Exception:
            # A notification failure must never compound a missed run
            # -- the `logger.critical` above already recorded it.
            logger.exception("scheduled run deferred: notifier.failure() itself raised")

    def repair(self, assignment_id: str, *, apply: bool) -> str:
        if not self._lock.acquire(blocking=False):
            raise RunInProgress("a run is in progress")
        try:
            with self._locked(timeout=0.0, what="a repair"):
                return self._repair_locked(assignment_id, apply=apply)
        finally:
            self._lock.release()

    def _repair_locked(self, assignment_id: str, *, apply: bool) -> str:
        self._refresh_settings()
        store = self.deps.store
        run = store.runs.start(str(RunTrigger.MANUAL), dry_run=not apply)
        try:
            a = store.assignments.get(assignment_id)
            if a is None:
                store.runs.finish(run.id, "CRASHED")
                raise ValueError(f"no such assignment: {assignment_id!r}")
            report = self.deps.orchestrator.repair(run.id, a, apply=apply)
            store.runs.finish(run.id, str(report.outcome))
        except Exception:
            store.runs.finish(run.id, "CRASHED")
            raise
        self._refresh_metrics()
        return run.id

    def prefetch(self, assignment_id: str) -> None:
        # No cross-process lock: prefetch only ever fetches/renders/verifies
        # into the cache and never calls a mutating sink method, so two of
        # them cannot damage a tonie. The in-process lock stays, because it
        # is also what keeps a prefetch from competing with a real run for
        # the same cache files within this process.
        if not self._lock.acquire(blocking=False):
            raise RunInProgress("a run is in progress")
        try:
            self._refresh_settings()
            store = self.deps.store
            a = store.assignments.get(assignment_id)
            if a is None:
                # Task 29 review, Minor 11: a silent `return` here for an
                # unknown id was the sentinel pattern this project bans
                # everywhere else -- `repair` (just above) already raises
                # `ValueError` for the identical input. Match it, so a
                # caller can't mistake "nothing to do" for "no such
                # assignment".
                raise ValueError(f"no such assignment: {assignment_id!r}")
            run = store.runs.start("cli", dry_run=False)
            try:
                self.deps.orchestrator.prefetch(run.id, a)
                store.runs.finish(run.id, "OK")
            except Exception:
                store.runs.finish(run.id, "CRASHED")
                raise
            self._refresh_metrics()
        finally:
            self._lock.release()


# --------------------------------------------------------------- build()


def _initial_rotation_settings(store: Store) -> RotationSettings:
    """Same fields `effective_rotation_settings` reads, minus the clamp
    against `sink.limits` (see module docstring — `build()` must never
    touch `.limits`). Used only for the value `build()` seeds into
    `Deps`/`AppDeps` at construction; every real run refreshes this
    properly (clamped) via `OrchestratorRunner._refresh_settings`.
    """
    get = store.settings.get
    from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS

    return RotationSettings(
        cap_seconds=get("cap_seconds", DEFAULT_CAP_SECONDS),
        avoid_duplicates=get("avoid_duplicates_across_tonies", True),
        repeat_cooldown_days=get("repeat_cooldown_days", 0),
        prefetch_depth=get("prefetch_depth", 3),
        cache_budget_bytes=get("cache_budget_gb", 40) * 1024**3,
        loudnorm_default=get("loudnorm_default", False),
        schedule=get("schedule", "15:00"),
        timezone=get("timezone", default_timezone_name()),
    )


def _build_sink(settings: Settings) -> SinkProtocol:
    if settings.sink_kind == "fake":
        return FakeSink()
    # `ToniesCloudSink.from_env` is the ONLY place credentials are read
    # (its own docstring's contract) — `settings.sink_user`/`sink_password`
    # arrived here from `BOXBUTLER_SINK_USER`/`BOXBUTLER_SINK_PASSWORD`
    # only, never YAML (`boxbutler.config.load_settings` already enforced
    # that). Construction itself performs no network call.
    return ToniesCloudSink.from_env(settings.sink_user, settings.sink_password)


def build(settings: Settings) -> AppDeps:
    """The composition root (spec §11 P5). Opens the real store, builds
    the real (or fake, for `BOXBUTLER_SINK_KIND=fake` smoke tests) sink,
    wires every fetcher/renderer/notifier, and returns one `AppDeps` that
    `create_web_app`/`boxbutler.cli.main` both drive.
    """
    # `/media` is required, not optional, and checked before anything
    # else is opened (spec: a library is a folder). There is deliberately
    # no fallback into `data_dir`: that is the volume the operator backs
    # up, and library folders hold hours of audio. Plex, Sonarr, Immich
    # and Paperless all refuse to start without their media location for
    # the same reason.
    try:
        media_root = require_media_root(settings.media_root)
    except LibraryFolderError as exc:
        raise ConfigError(str(exc)) from exc

    store = Store.open(Path(settings.data_dir) / "boxbutler.sqlite")
    # Migration (see `ensure_library_folders`): a library written before
    # "a library is a folder" has no folder_path. Give it one derived
    # from its name and create it. Moves no audio — anything already
    # fetched into `/cache` stays there and ages out by ordinary
    # eviction, because a migration that relocates audio can lose it.
    for lib in ensure_library_folders(store, media_root):
        logger.info("migrated library %r to a folder under %s", lib.name, media_root)
    # First start only (spec §1.1b): after this, the database owns these
    # keys and an operator's Settings-screen edit is never overwritten by
    # a redeploy with the same env/YAML.
    seed_db_settings(store, settings)

    sink = _build_sink(settings)

    cache_dir = Path(settings.cache_dir)
    snapshot_dir = Path(settings.data_dir) / "snapshots"
    # One lock file per data directory, shared by every process that can
    # reach a mutating sink call — this one's UI and scheduler, and any
    # `docker exec … boxbutler …` against the same volume (M1).
    run_lock = RunLock(Path(settings.data_dir) / "run.lock")
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    http_client = httpx.Client()
    # Order doesn't matter for correctness -- each fetcher claims a
    # disjoint set of `ItemKind`s (`supports()` is not a fallback
    # chain across kinds) -- but `FolderFetcher` first costs nothing and
    # is the cheapest possible check (a `Path.is_dir()`/`stat()`, no
    # process spawn, no network), so a folder-backed library never waits
    # behind a YtDlp/Http `supports()` check that was always going to say
    # no.
    fetcher = CompositeFetcher([
        FolderFetcher(),
        YtDlpFetcher(js_runtime=settings.js_runtime),
        HttpFetcher(http_client),
    ])
    renderer: RendererProtocol = FfmpegRenderer()
    notifier: Notifier = LiveNotifier(store, settings.notify_token)

    rotation = _initial_rotation_settings(store)

    orch_deps = Deps(
        store=store,
        sink=sink,
        fetcher=fetcher,
        renderer=renderer,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
        settings=rotation,
        notifier=notifier,
        log=make_logger(),
    )
    orchestrator = Orchestrator(orch_deps)

    deps = AppDeps(
        store=store,
        sink=sink,
        orchestrator=orchestrator,
        snapshot_dir=snapshot_dir,
        cache_dir=cache_dir,
        notifier=notifier,
        run_lock=run_lock,
        config=settings,
    )
    runner = OrchestratorRunner(deps)
    deps.runner = runner

    # Startup reconciliation (final safety review, C3). A `swap_marker` row
    # that outlived the process that wrote it means that process was killed
    # between CLEAR and COMMIT: the tonie is empty or half-filled *right
    # now*, while the assignment row still says OK. Turning it into DEGRADED
    # here is what makes that visible (dashboard, `/metrics`,
    # `BoxButlerTonieDegraded`) and repairable before any rotation, instead
    # of leaving it to be discovered by a child at bedtime.
    #
    # Skipped while another process holds the run lock: that process is
    # legitimately mid-swap and owns its own marker, and degrading an
    # in-flight assignment from underneath it would be this fix inventing
    # the very failure it exists to report. Deferring costs nothing — the
    # marker is still there at the next start if that run really did die.
    # Held, not merely checked: asking "is anyone running?" and then acting
    # leaves a window for a run to start in between.
    try:
        with run_lock.held(timeout=0.0, what="a run"):
            reconciled = reconcile_interrupted_swaps(store, log=orch_deps.log)
        if reconciled:
            logger.critical(
                "%d assignment(s) were mid-swap when a previous process died and "
                "have been marked DEGRADED; their tonies may be empty until repaired: %s",
                len(reconciled), ", ".join(reconciled),
            )
    except RunInProgress:
        logger.warning(
            "skipping swap-marker reconciliation: another process holds %s "
            "(a run is in progress); it will be reconciled at the next start",
            run_lock.path,
        )

    def get_schedule() -> tuple[str, str]:
        # Task 29 review, CRITICAL-2: `load_settings`/`seed_db_settings`
        # now refuse an unresolvable schedule or timezone before either
        # ever reaches the store, so the store's value should always be
        # good. But it can still hold a bad value written before this
        # fix, or by direct sqlite surgery, or by a future bug -- and
        # `Scheduler.run_forever`'s own contract is that its *first* read
        # (this one, via `serve()`'s synchronous `scheduler.next()`) is
        # allowed to raise and crash the process, because there is no
        # last-known-good value yet. Crashing here is exactly the
        # unrecoverable wedge CRITICAL-2 was about: the process never
        # binds, so the Settings screen that could fix the bad value is
        # never reachable. Validate here and fall back to the
        # already-validated env/YAML `settings` (never to a hard-coded
        # UTC/15:00 -- see `default_timezone_name`'s own warning about
        # guessing) so `serve()` can still start and the operator can
        # reach Settings to correct the stored value. This is a
        # deliberate, loud exception to "never silently fall back": the
        # alternative is a process that can only be repaired with sqlite
        # surgery.
        schedule = store.settings.get("schedule", settings.schedule)
        tz_name = store.settings.get("timezone", settings.timezone)
        try:
            parse_hhmm(schedule)
            ZoneInfo(tz_name)
        except (ValueError, ZoneInfoNotFoundError, OSError):
            logger.critical(
                "stored schedule/timezone (%r, %r) in store.settings is "
                "invalid; falling back to the configured default (%r, %r) "
                "so boxbutler can start -- fix this on the Settings screen, "
                "or reset it directly: sqlite3 %s \"DELETE FROM settings "
                "WHERE key IN ('schedule','timezone')\"",
                schedule, tz_name, settings.schedule, settings.timezone,
                Path(settings.data_dir) / "boxbutler.sqlite",
            )
            return (settings.schedule, settings.timezone)
        return (schedule, tz_name)

    def on_fire() -> None:
        # The one legitimate unattended `apply=True` (spec's binding
        # constraint on dry-run-by-default). Shares `runner`'s lock with
        # every UI-triggered run (via `run_scheduled`, which -- unlike
        # `run` -- waits for that lock rather than failing fast; see
        # OrchestratorRunner.run_scheduled, Task 29 review MAJOR-1) so a
        # scheduled fire never silently loses to a manual "Run
        # now"/"Repair" against the same tonies.
        runner.run_scheduled()
        # `runner.run_scheduled` already refreshed `orch_deps.settings` from
        # the store immediately above (when it actually ran), and the
        # orchestrator's own `run()`
        # already evicted under that budget when `apply=True` — this
        # second, explicit evict (spec's own wording for the fired job)
        # is a harmless no-op the moment the budget is already met.
        live = orch_deps.settings
        evict(
            store, cache_dir, live.cache_budget_bytes, live.prefetch_depth,
            apply=True, avoid_duplicates=live.avoid_duplicates,
            repeat_cooldown_days=live.repeat_cooldown_days,
        )

    deps.scheduler = Scheduler(get_schedule, on_fire)
    return deps


# ---------------------------------------------------------- create_web_app


def create_web_app(deps: AppDeps) -> FastAPI:
    """Deferred wiring items 3 and 4: the web app is switched from
    `FakeSink`/`FakeRunner`/the Phase 2 ingest placeholders to the real
    store, the real sink and `OrchestratorRunner`. `boxbutler/web/dev.py`
    is untouched — it still builds its own `FakeSink`/`FakeRunner` for
    local screen review and never calls this function.
    """
    config = deps.config
    assert config is not None, "AppDeps.config must be set (see build())"

    web_settings = WebSettings(
        secret_key=config.secret_key or "",
        admin_user=config.admin_user,
        admin_password=config.admin_password,
        secure_cookies=config.secure_cookies,
        data_dir=Path(config.data_dir),
    )

    upload_dir = Path(deps.cache_dir) / "uploads"
    http_client = httpx.Client()
    sources = [
        YouTubeUrlSource(),
        YouTubePlaylistSource(),
        RssSource(http_client),
        DirectUrlSource(http_client),
        UploadSource(upload_dir),
    ]
    # A source is how media arrives in the library's folder, so the
    # ingestor needs a fetcher: adding a link downloads the audio into
    # that folder there and then (`boxbutler/sources/ingest.py`). It
    # fetches into a private staging directory under `/cache` first and
    # only then adopts the finished file into the library folder, so a
    # partial download is never visible in a folder the operator is
    # looking at. Same `CompositeFetcher` the orchestrator uses — one
    # download path, not a second implementation.
    ingestor = Ingestor(
        deps.store,
        sources,
        fetcher=deps.orchestrator.deps.fetcher,
        staging_dir=Path(deps.cache_dir) / "incoming",
    )

    def scan_folder(library) -> int:
        # Real folder scan (deferred wiring item 4's third placeholder).
        # Called by the Scan button *and* on every library page view (see
        # `web/routes/library.py::_scan_on_open`), so that a file copied
        # in by hand is simply listed rather than needing a click. A
        # library with no `folder_path` (a row that predates the startup
        # migration) has nothing to scan yet — matches
        # `default_scan_folder`'s "always zero" contract for that case
        # rather than raising. `probe=renderer.probe` so titles come from
        # embedded tags first (folder.py's own preference), filename stem
        # only as its fallback.
        if not library.folder_path:
            return 0
        result = _scan_folder_dir(
            deps.store, library, Path(library.folder_path), probe=deps.orchestrator.deps.renderer.probe
        )
        return result.added

    runner = deps.runner if deps.runner is not None else OrchestratorRunner(deps)

    app = create_app(
        deps.store,
        deps.sink,
        web_settings,
        runner,
        ingest=ingestor.add_ref,
        ingest_upload=ingestor.add_upload,
        scan_folder=scan_folder,
        media_root=Path(config.media_root),
    )
    _mount_metrics(app, deps)
    return app


def _mount_metrics(app: FastAPI, deps: AppDeps) -> None:
    """Mount `/metrics` (spec §9): scraped container-to-container on the
    docker network, not through the reverse proxy, so it is deliberately
    unauthenticated -- it carries no `Depends(require_login)` and is
    never routed through `boxbutler.web.app`'s login machinery at all
    (it is mounted directly on the outer app, exactly like `/healthz`).

    Refreshed on every scrape, not only after a run: a gauge that only
    updates post-run reports stale numbers forever until the first run
    happens, and a stale reading presented as current is this project's
    own recurring bug shape (spec §13.3). `OrchestratorRunner` also
    refreshes after every run/repair/prefetch so the numbers are current
    between scrapes too, but the scrape-time refresh here is what makes
    `/metrics` correct even if no run has happened yet.
    """
    inner = bb_metrics.metrics_app()

    async def _scrape(scope, receive, send):
        if scope["type"] == "http":
            bb_metrics.refresh(deps.store, deps.cache_dir, deps.settings.prefetch_depth)
        await inner(scope, receive, send)

    app.mount("/metrics", _scrape)


# ------------------------------------------------------------------ serve


def serve(settings: Settings) -> None:
    """Start the scheduler in a daemon thread and serve the UI on
    `0.0.0.0:{settings.listen_port}` (spec §7, §8, §11 P5). The fired job
    is `orchestrator.run(apply=True, trigger=SCHEDULE)` followed by
    `evict(..., apply=True)` (see `build()`'s `on_fire`).

    A bad configured timezone must be loud and legible, not a thread that
    dies quietly (task brief). `Scheduler.run_forever`'s own contract is
    that its *first* schedule/zone read is allowed to raise — there is no
    last-known-good value to fall back on yet — and every read after that
    logs and keeps the last-known-good schedule instead of dying. This
    function performs that first read itself, synchronously, on the main
    thread, *before* spawning the scheduler thread: an unresolvable
    `TZ`/Settings-screen zone therefore raises straight out of `serve()`
    and crashes the process at startup with a normal Python traceback in
    the container's own stdout/exit code — not a background thread that
    silently stops firing forever. Every read after that point runs on
    the daemon thread per `Scheduler`'s own last-known-good behaviour.
    """
    deps = build(settings)
    app = create_web_app(deps)

    assert deps.scheduler is not None
    deps.scheduler.next()  # raises here, on the main thread, if unresolvable

    thread = threading.Thread(
        target=deps.scheduler.run_forever, name="boxbutler-scheduler", daemon=True
    )
    thread.start()

    uvicorn.run(app, host="0.0.0.0", port=settings.listen_port)


def main() -> None:
    config_path = os.environ.get("BOXBUTLER_CONFIG")
    try:
        settings = load_settings(Path(config_path) if config_path else None, os.environ)
    except ConfigError as exc:
        raise SystemExit(f"boxbutler: configuration error: {exc}") from exc
    serve(settings)


# ------------------------------------------------------------------ CLI


def _cli_deps_factory() -> AppDeps:
    """`boxbutler.cli.main.main`'s `deps_factory` (Task 29 review,
    CRITICAL-1). Builds the same real `Settings` -> `build()` -> `AppDeps`
    chain `serve()` does -- from `BOXBUTLER_CONFIG` and the process
    environment -- so every CLI subcommand drives the real store, the
    real (or `BOXBUTLER_SINK_KIND=fake`) sink and the real orchestrator,
    exactly as the web process does.

    This is a plain function, not a lambda in `cli_main` below, only so a
    test can substitute an equivalent one without importing `cli_main`'s
    closure.
    """
    config_path = os.environ.get("BOXBUTLER_CONFIG")
    settings = load_settings(Path(config_path) if config_path else None, os.environ)
    return build(settings)


def cli_main(argv: list[str] | None = None) -> int:
    """The `boxbutler` console-script entry point (`pyproject.toml`'s
    `[project.scripts]`). `boxbutler.cli.main.main` owns argument parsing
    and every subcommand; this function's only job is to supply the real
    `deps_factory` that module was always built to receive (its own
    docstring: "Task 29's composition root's job") but never was.

    `deps_factory` is passed as a callable, not called here -- `cli.main`
    only calls it *after* `argparse` has successfully parsed the
    arguments, so a bare `--help` or a bad flag (which exits via
    `argparse`'s own `SystemExit` before `deps_factory` is ever invoked)
    never touches the store, the sink, or the network. `_cli_deps_factory`
    -> `build()` -> `_build_sink()` is itself network-free (see the
    module docstring's "What `build()` deliberately does NOT do"), so
    even a real subcommand that fails early (e.g. a missing secret) fails
    with a `ConfigError`, converted to a clean exit 2 here, before any
    network call could happen.
    """
    from boxbutler.cli.main import main as _cli_main

    try:
        return _cli_main(argv, deps_factory=_cli_deps_factory)
    except ConfigError as exc:
        print(f"boxbutler: configuration error: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "AppDeps",
    "CompositeFetcher",
    "LiveNotifier",
    "OrchestratorRunner",
    "build",
    "cli_main",
    "create_web_app",
    "main",
    "serve",
]
