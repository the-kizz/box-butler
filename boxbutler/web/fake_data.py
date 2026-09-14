"""Seeded fake dashboard data and `FakeRunner` (Task 9; spec §5, §11 P2).

Phase 2 runs entirely on fake data so nothing on a screen can touch a real
tonie: `seed_fake` populates a `Store` and a `boxbutler.sinks.fake.FakeSink`
with four tonies exercising the three states a screen must design for
explicitly (OK, DEGRADED, Unmanaged) plus libraries in all three content
modes. Every name, URL and path in this module is invented. Real tonie
names, real podcast titles and real tonie IDs must never appear in this
file — it is seeded into dashboard screenshots and shipped in a public
repository. Every target and item title uses an obviously fake name
(colour labels, mythical creatures, etc.) that shares no shape with any
household's actual tonie or podcast collection.

`FakeRunner` gives the dashboard something to POST /run and /repair
against without a real orchestrator (Task 13+ builds that against a real
sink): dry runs record a plan and nothing else; applies upload a fake
rendition to `FakeSink`, settle it, and update the store exactly like a
real run would, using the same `boxbutler.domain.rotation.choose_next`
machinery a real orchestrator will use.

`fake_ingest`, `fake_ingest_upload` and `default_scan_folder` (Task 10)
are the Phase 2 defaults for `app.state.ingest` / `ingest_upload` /
`scan_folder`, wired in `boxbutler.web.app.create_app`. None of them ever
touch the network, `tonie_api`, ffmpeg or the real filesystem: adding a
source on the library screen in Phase 2 just records one fake `Item`
keyed by a hash of the ref (or upload content), and scanning a folder
library is a no-op that finds zero new items — Task 29 replaces these
with `boxbutler.sources.ingest.Ingestor.add_ref` / `add_upload` and a real
`scan_folder`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ..domain.cache_name import cache_name
from ..domain.models import (
    AssignmentState,
    Item,
    ItemKind,
    ItemState,
    Library,
    LibraryMode,
    RunOutcome,
    RunTrigger,
)
from ..domain.rotation import PlanInput, choose_next
from ..sinks.fake import FakeSink
from ..sinks.protocol import Target
from ..store.db import Store

# The sink identifier this fake data uses for `assignments.upsert_target` /
# `get_by_target`. Read off `FakeSink` rather than repeated as a literal:
# `SinkProtocol.name` is the single source of truth for sink identity, and
# the routes no longer import this constant at all (they ask the live sink),
# so nothing here can disagree with what a real deployment keys its rows
# under (final safety review, C1).
SINK_ID = FakeSink.name

FAKE_CACHE_DIR = "/cache/fake"


def _add_item(store: Store, library_id: str, n: int, title: str, seconds: float, **kw):
    return store.items.add(
        library_id=library_id,
        kind=kw.pop("kind", ItemKind.YOUTUBE),
        source_ref=kw.pop("source_ref", f"https://video.example.invalid/watch?v=fake{n:04d}"),
        source_key=f"fake{n:04d}",
        title=title,
        seconds=seconds,
        **kw,
    )


def seed_fake(store: Store, sink: FakeSink) -> None:
    """Populate `store` and `sink` with obviously-fake dashboard data.

    Idempotent-ish for test isolation purposes only insofar as it is meant
    to run once against a fresh store/sink per test (the `seeded` fixture
    creates both from scratch).
    """
    # --- Libraries -----------------------------------------------------
    bedtime = store.libraries.create("Bedtime", mode=LibraryMode.SINGLE)
    car_trips = store.libraries.create("Car Trips", mode=LibraryMode.ALBUM)
    audiobook = store.libraries.create(
        "Audiobook", mode=LibraryMode.SERIAL, folder_path="/media/audiobook"
    )

    bedtime_items = [
        _add_item(store, bedtime.id, 1, "The Wobbling Moon", 5340.0),
        _add_item(store, bedtime.id, 2, "Captain Custard's Nap", 480.0),
        _add_item(store, bedtime.id, 3, "Pillowfort Chronicles", 360.0),
        _add_item(store, bedtime.id, 4, "The Drowsy Dragon", 420.0),
        _add_item(store, bedtime.id, 5, "Grandpa Snail's Bedtime", 540.0),
        _add_item(store, bedtime.id, 6, "The Yawning Owl", 300.0),
    ]

    car_trips_items = [
        _add_item(store, car_trips.id, 11, "Highway Singalong Vol 1", 200.0),
        _add_item(store, car_trips.id, 12, "Highway Singalong Vol 2", 180.0),
        _add_item(store, car_trips.id, 13, "Roadtrip Rhymes", 220.0),
        _add_item(store, car_trips.id, 14, "Backseat Ballads", 150.0),
    ]

    audiobook_items = []
    for i in range(1, 13):
        it = store.items.add(
            library_id=audiobook.id,
            kind=ItemKind.FOLDER_FILE,
            source_ref=f"/media/audiobook/chapter-{i:02d}.mp3",
            source_key=f"chapter-{i:02d}",
            title=f"The Invented Chronicle, ch. {i}",
            seconds=600.0,
            local_path=f"/media/audiobook/chapter-{i:02d}.mp3",
        )
        audiobook_items.append(it)
    store.items.set_state(audiobook_items[-1].id, ItemState.UNAVAILABLE)

    # --- Targets on the fake cloud sink ---------------------------------
    # Fix round 1, important 2: these four target names must never look
    # like a real household's tonie names, so they're deliberately generic
    # colour labels rather than anything descriptive/personal.
    green_item = bedtime_items[0]
    sink.add_target("fake-green-01", "Green Tonie")
    sink.upload(Target(id="fake-green-01", name="Green Tonie"), Path(f"{FAKE_CACHE_DIR}/{cache_name(green_item.title, green_item.source_key)}"), green_item.title)
    sink.settle(Target(id="fake-green-01", name="Green Tonie"), green_item.seconds, timeout_s=1)

    blue_target = Target(id="fake-blue-02", name="Blue Tonie")
    sink.add_target("fake-blue-02", "Blue Tonie")
    for it in car_trips_items:
        sink.upload(blue_target, Path(f"{FAKE_CACHE_DIR}/{cache_name(it.title, it.source_key)}"), it.title)
    sink.settle(blue_target, sum(i.seconds for i in car_trips_items), timeout_s=1)

    sink.add_target("fake-red-03", "Red Tonie")  # cleared, empty — DEGRADED

    spare_target = Target(id="fake-spare-04", name="Spare Tonie")
    sink.add_target("fake-spare-04", "Spare Tonie")
    for i in range(1, 10):
        sink.upload(spare_target, Path(f"{FAKE_CACHE_DIR}/spare-track-{i:02d}.m4a"), f"Spare Track {i}")
        sink.settle(spare_target, 180.0 + i, timeout_s=1)

    # --- Assignments (dashboard's GET / also upserts these on load; doing
    # it here first is what lets us configure library/state/pin below) ---
    green_a = store.assignments.upsert_target(SINK_ID, "fake-green-01", "Green Tonie")
    store.assignments.assign_library(green_a.id, bedtime.id)
    now = datetime.now(UTC)
    store.chapters.replace_for_assignment(
        green_a.id, [(green_item.id, "ch0", green_item.title, green_item.seconds)], now
    )
    store.assignments.set_last_success(green_a.id, now)

    blue_a = store.assignments.upsert_target(SINK_ID, "fake-blue-02", "Blue Tonie")
    store.assignments.assign_library(blue_a.id, car_trips.id)
    store.chapters.replace_for_assignment(
        blue_a.id,
        [(it.id, f"ch{i}", it.title, it.seconds) for i, it in enumerate(car_trips_items)],
        now,
    )
    store.assignments.set_last_success(blue_a.id, now)

    red_a = store.assignments.upsert_target(SINK_ID, "fake-red-03", "Red Tonie")
    store.assignments.assign_library(red_a.id, audiobook.id)
    staged = [
        {
            "item_id": audiobook_items[0].id,
            "path": f"{FAKE_CACHE_DIR}/{cache_name(audiobook_items[0].title, audiobook_items[0].source_key)}",
            "title": audiobook_items[0].title,
            "seconds": audiobook_items[0].seconds,
        },
        {
            "item_id": audiobook_items[1].id,
            "path": f"{FAKE_CACHE_DIR}/{cache_name(audiobook_items[1].title, audiobook_items[1].source_key)}",
            "title": audiobook_items[1].title,
            "seconds": audiobook_items[1].seconds,
        },
    ]
    store.assignments.set_state(red_a.id, AssignmentState.DEGRADED, json.dumps(staged))

    # Spare Tonie stays unmanaged: upsert only, never assign a library
    # ("New tonies need no configuration" — spec §5 — this is what a brand
    # new tonie looks like before anyone has touched it).
    store.assignments.upsert_target(SINK_ID, "fake-spare-04", "Spare Tonie")

    # --- Runs with events (spec §10.17 / history screen fixture data) ---
    run1 = store.runs.start(str(RunTrigger.MANUAL), dry_run=True)
    store.runs.event(run1.id, "plan", assignment_id=green_a.id, item_ids=[green_item.id])
    store.runs.finish(run1.id, str(RunOutcome.DRY_RUN))

    run2 = store.runs.start(str(RunTrigger.SCHEDULE), dry_run=False)
    store.runs.event(run2.id, "plan", assignment_id=blue_a.id, item_ids=[i.id for i in car_trips_items])
    store.runs.event(run2.id, "swap", assignment_id=blue_a.id)
    store.runs.finish(run2.id, str(RunOutcome.SWAPPED))

    run3 = store.runs.start(str(RunTrigger.SCHEDULE), dry_run=False)
    store.runs.event(run3.id, "plan", assignment_id=red_a.id, item_ids=[s["item_id"] for s in staged])
    store.runs.event(run3.id, "degraded", assignment_id=red_a.id, reason="staging failed")
    store.runs.finish(run3.id, str(RunOutcome.DEGRADED))

    store.settings.set("last_library_id", bedtime.id)


class FakeRunner:
    """Drives `FakeSink` the way a real orchestrator would, for Phase 2's
    fake-only screens. Never imports `tonie_api`; never touches ffmpeg or
    the network — everything staged/uploaded is a fake in-memory path.

    **This class must never be handed a real sink.** It holds the only
    `clear()` call in the repo outside `boxbutler/orchestrator/run.py`, and it
    has none of §2's protections: no fetch, no render, no verify, no
    write-once snapshot before the clear, and no `DEGRADED` path if an upload
    fails afterwards. `Orchestrator` (Task 21) is the only thing that may
    drive a sink that can reach a real tonie; Phase 4 replaces this driver
    rather than pointing it at `ToniesCloudSink`.

    `_apply` resolves its staged entries *before* clearing regardless, so
    nothing in this repository shows the prototype's `wipe -> upload` order as
    if it were acceptable — the ordering here is not load-bearing, but a
    pattern someone might copy is.
    """

    def __init__(self, store: Store, sink: FakeSink):
        self.store = store
        self.sink = sink

    def run(self, *, assignment_ids: list[str] | None, apply: bool, trigger: RunTrigger) -> str:
        run = self.store.runs.start(str(trigger), dry_run=not apply)
        ids = assignment_ids if assignment_ids is not None else [a.id for a in self.store.assignments.list()]
        any_applied = False
        for aid in ids:
            a = self.store.assignments.get(aid)
            if a is None or a.library_id is None:
                continue
            library = self.store.libraries.get(a.library_id)
            items = self.store.items.list(a.library_id)
            plan = choose_next(
                PlanInput(
                    assignment=a,
                    library_mode=library.mode,
                    items=items,
                    loaded_elsewhere=frozenset(self.store.chapters.loaded_item_ids_except(aid)),
                )
            )
            self.store.runs.event(run.id, "plan", assignment_id=aid, item_ids=plan.item_ids)
            if apply and plan.item_ids:
                self._apply(aid, a, plan.item_ids)
                any_applied = True
        outcome = RunOutcome.SWAPPED if any_applied else RunOutcome.DRY_RUN
        self.store.runs.finish(run.id, str(outcome))
        return run.id

    def repair(self, assignment_id: str, *, apply: bool) -> str:
        run = self.store.runs.start(str(RunTrigger.MANUAL), dry_run=not apply)
        a = self.store.assignments.get(assignment_id)
        self.store.runs.event(run.id, "repair-plan", assignment_id=assignment_id)
        outcome = RunOutcome.DRY_RUN
        if apply and a is not None and a.staged_json:
            staged = json.loads(a.staged_json)
            item_ids = [s["item_id"] for s in staged]
            self._apply(assignment_id, a, item_ids, staged_override=staged)
            outcome = RunOutcome.REPAIRED
        self.store.runs.finish(run.id, str(outcome))
        return run.id

    def prefetch(self, assignment_id: str) -> None:
        # No-op seam in Phase 2 — Task 19+ wires real pre-download/caching
        # against a live sink; nothing to prefetch against a fake one.
        return None

    def _apply(self, assignment_id: str, a, item_ids: list[str], staged_override: list[dict] | None = None):
        target = Target(id=a.target_id, name=a.target_name)
        # Stage, then swap (spec §2), even here. There is nothing to fetch,
        # render or verify against a FakeSink — `entries` is the whole of this
        # driver's "staging" — but it is resolved in full BEFORE `clear()`, so
        # no code in this repository demonstrates the prototype's
        # `wipe -> upload` order for someone to copy. See the class docstring.
        if staged_override is not None:
            entries = staged_override
        else:
            by_id = {i.id: i for i in self.store.items.list(a.library_id)}
            entries = [
                {
                    "item_id": iid,
                    "path": f"{FAKE_CACHE_DIR}/{cache_name(by_id[iid].title, by_id[iid].source_key)}",
                    "title": by_id[iid].title,
                    "seconds": by_id[iid].seconds or 300.0,
                }
                for iid in item_ids
                if iid in by_id
            ]
        self.sink.clear(target)
        for e in entries:
            self.sink.upload(target, Path(e["path"]), e["title"])
        total_seconds = sum(e["seconds"] for e in entries)
        settle = self.sink.settle(target, total_seconds, timeout_s=5)
        now = datetime.now(UTC)
        records = [
            (e["item_id"], chapter.id, e["title"], chapter.seconds)
            for e, chapter in zip(entries, settle.chapters[-len(entries):])
        ]
        self.store.chapters.replace_for_assignment(assignment_id, records, now)
        self.store.assignments.set_state(assignment_id, AssignmentState.OK, None)
        self.store.assignments.set_last_success(assignment_id, now)


def fake_ingest(store: Store, library_id: str, ref: str) -> list[Item]:
    """Phase 2 default for `app.state.ingest` (Task 10; library screen
    "add a source" field). Records exactly one fake item — no network
    call, no yt-dlp, no real resolution of any kind — keyed by a short
    hash of the ref, per this task's brief. `store.items.add` is
    idempotent on `(library_id, source_key)`, so adding the same ref
    twice to the same library is a no-op the second time, same as a real
    resolver would be for a duplicate.
    """
    source_key = f"fake-{hashlib.sha1(ref.encode('utf-8')).hexdigest()[:8]}"
    item = store.items.add(
        library_id=library_id,
        kind=ItemKind.URL,
        source_ref=ref,
        source_key=source_key,
        title=ref,
    )
    return [item] if item is not None else []


def fake_ingest_upload(store: Store, library_id: str, filename: str, stream: BinaryIO) -> Item | None:
    """Phase 2 default for `app.state.ingest_upload`. Reads the upload
    into memory to derive a stable fake key — never writes it to disk —
    since Phase 2 must not perform anything resembling a real upload.
    """
    data = stream.read()
    source_key = f"fake-{hashlib.sha1(data).hexdigest()[:8]}"
    return store.items.add(
        library_id=library_id,
        kind=ItemKind.UPLOAD,
        source_ref=filename,
        source_key=source_key,
        title=filename,
    )


def default_scan_folder(library: Library) -> int:
    """Phase 2 default for `app.state.scan_folder`: a folder library's
    "Scan folder" button always exists in the UI, but until Task 17 wires
    a real filesystem scan, pressing it must never touch a real
    filesystem — it always finds zero new items.
    """
    return 0
