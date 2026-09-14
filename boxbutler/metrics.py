"""Prometheus metrics (Task 31; spec §9, §13.3).

## Why a private registry

`prometheus_client`'s default `REGISTRY` is process-global: anything else
importing it (another library, a stray second definition) shares the same
namespace, and defining the *same* metric name twice against it raises
`Duplicated timeseries in CollectorRegistry`. This module owns its own
`CollectorRegistry` (module-level `REGISTRY`, below) instead, created
exactly once at import time. Because Python caches modules in
`sys.modules`, importing `boxbutler.metrics` a second time under the same
name is a no-op — the module body never runs twice — so the metric objects
below are constructed exactly once per process no matter how many times
`boxbutler.main.build()`/`create_web_app()` are called (see
`tests/test_main_wiring.py`, which does this repeatedly in one process).

## Test isolation

Counters accumulate for the life of the process; a gauge keeps whatever the
last call to `refresh()` set. Both are fine in production (that's the whole
point) but make `REGISTRY.get_sample_value(...)` assertions order-dependent
in tests. `tests/test_metrics.py` does not read this module's real,
process-shared `REGISTRY` at all: it monkeypatches every module-level
metric name below (`run_total`, `assignment_state`, ...) with a fresh
object bound to a throwaway `CollectorRegistry` before each test. Because
every read in this module (`refresh()`) and in
`boxbutler.orchestrator.run` (`metrics.run_total.labels(...).inc()`,
`metrics.extraction_broken_total.inc()`) looks the name up on the module
*at call time* rather than capturing it in a default argument or a
`from ... import name` binding, monkeypatching the module attribute is
enough to redirect every reader and writer to the fresh, empty registry —
no test can see another test's counts or gauge values.
"""
from __future__ import annotations

from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Gauge, make_asgi_app

from boxbutler.domain.models import AssignmentState
from boxbutler.orchestrator.prefetch import ready_depth
from boxbutler.orchestrator.retention import cache_usage_bytes
from boxbutler.store.db import Store

#: This project's own registry -- never `prometheus_client.REGISTRY` (see
#: module docstring). Built once, at import time.
REGISTRY = CollectorRegistry()

run_total = Counter(
    "boxbutler_run_total", "Runs by outcome", ["outcome"], registry=REGISTRY
)
#: **Per assignment** (final fix review, masking-defect 1). This used to be
#: a single global gauge set to `max(a.last_success_at for a in assignments)`
#: -- on a box with two or more tonies, any one of them succeeding kept the
#: whole gauge fresh, so a tonie that failed every single night stayed
#: hidden behind its healthy siblings forever: `BoxButlerRunStale` never
#: fired and no notification exists for the failure either (see
#: `_notify_outcome`'s `CRASHED` branch). Labelling by `assignment`, the
#: same way `assignment_state`/`tonie_seconds`/`prefetch_ready` already are,
#: makes one stalled tonie visible regardless of how the others are doing.
#:
#: A Prometheus `Gauge` defaults to `0` (the Unix epoch) until the first
#: `.set()` call for a given label combination -- i.e. before an assignment
#: has ever succeeded, its series reports `0.0`, not "unknown" (`refresh()`
#: sets it explicitly to `0.0` in that case, every call, so the series
#: always exists once an assignment does -- see `refresh()` below).
#: `monitoring/alerts.yml`'s `BoxButlerRunStale` relies on that: it treats
#: `> 0` as "this assignment has ever succeeded" and gates the staleness
#: comparison on it per assignment, so a fresh install/new assignment
#: (series still at its `0` default) never reads as "36h+ since success".
#: See the comment on that rule for the full contract -- do not change this
#: gauge's default-value semantics without updating that rule too.
last_success_timestamp = Gauge(
    "boxbutler_last_success_timestamp",
    "Unix time of the last successful run, per assignment",
    ["assignment"],
    registry=REGISTRY,
)
assignment_state = Gauge(
    "boxbutler_assignment_state",
    "1 for the current state",
    ["assignment", "state"],
    registry=REGISTRY,
)
tonie_seconds = Gauge(
    "boxbutler_tonie_seconds", "Seconds believed loaded", ["assignment"], registry=REGISTRY
)
cache_bytes = Gauge("boxbutler_cache_bytes", "Cache directory size", registry=REGISTRY)
prefetch_ready = Gauge(
    "boxbutler_prefetch_ready",
    "Verified renditions ready ahead",
    ["assignment"],
    registry=REGISTRY,
)
extraction_broken_total = Counter(
    "boxbutler_extraction_broken_total",
    "Runs that hit EXTRACTION_BROKEN",
    registry=REGISTRY,
)

#: Every `state` value `boxbutler_assignment_state` can take. `refresh()`
#: sets *all* of them (1.0 for the current one, 0.0 for the rest) on every
#: assignment on every call -- never just the current one -- so a tonie
#: that goes DEGRADED -> OK doesn't leave a stale `state="DEGRADED"} == 1`
#: sample lying around from its last refresh (spec §13.3: a stale reading
#: presented as current is exactly the failure shape this build has
#: already shipped eight times).
_ASSIGNMENT_STATES = tuple(AssignmentState)


def refresh(store: Store, cache_dir: Path, depth: int) -> None:
    """Recompute every gauge from the store and the cache directory. Safe
    to call as often as needed (after a run, and again on every `/metrics`
    scrape -- see `boxbutler/main.py`): it only reads, it never mutates the
    store, the cache, or a tonie.

    Called on every scrape (not just after a run) because a gauge that is
    only refreshed post-run reports stale numbers forever until the first
    run happens -- and a stale reading read as current is this project's
    own recurring bug shape (spec §13.3).
    """
    assignments = store.assignments.list()
    for a in assignments:
        for state in _ASSIGNMENT_STATES:
            assignment_state.labels(assignment=a.target_name, state=str(state)).set(
                1.0 if a.state == state else 0.0
            )
        seconds = sum(c.seconds for c in store.chapters.for_assignment(a.id))
        tonie_seconds.labels(assignment=a.target_name).set(seconds)
        prefetch_ready.labels(assignment=a.target_name).set(ready_depth(store, a, depth))
        # Per-assignment, every call -- like every gauge above and for the
        # same reason (spec §13.3: never leave a stale reading from a
        # previous refresh looking current). `0.0` for "never succeeded"
        # is the same sentinel the old global gauge used, now scoped to one
        # assignment instead of masked by whichever assignment last
        # succeeded (final fix review, masking-defect 1).
        last_success_timestamp.labels(assignment=a.target_name).set(
            a.last_success_at.timestamp() if a.last_success_at is not None else 0.0
        )

    cache_bytes.set(cache_usage_bytes(cache_dir))


def metrics_app():
    """A fresh ASGI app over this module's own `REGISTRY`. Cheap to call
    more than once (it is not itself a registration -- constructing it
    does not define any new collector), so mounting it more than once in
    one process (or rebuilding the web app repeatedly, as
    `tests/test_main_wiring.py` does) is safe.
    """
    return make_asgi_app(registry=REGISTRY)
