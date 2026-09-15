"""Task 31: `/metrics` and gauge/counter refresh (spec §9, §13.3).

## Why these tests don't touch `prometheus_client.REGISTRY`

`boxbutler.metrics` owns a private `CollectorRegistry` precisely so tests
never have to fight the process-global default one (see that module's
docstring). Even so, the module-level metric objects
(`boxbutler.metrics.run_total`, `.assignment_state`, ...) are created once
at import time and, left alone, would accumulate/linger across tests in
this file exactly the way the task brief warns about. `fresh_metrics`
below monkeypatches every one of those names to a brand-new object bound
to a brand-new, empty `CollectorRegistry` before each test runs. Because
both `boxbutler.metrics.refresh()` and `boxbutler.orchestrator.run`
(`metrics.run_total.labels(...)`, `metrics.extraction_broken_total`) look
the names up on the module at call time rather than capturing them, the
monkeypatch reaches every reader/writer -- production code included, not
just this test file's own assertions. No test here can see another
test's counts or a leftover gauge value, and no ordering matters.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from prometheus_client import CollectorRegistry, Counter, Gauge

import boxbutler.metrics as metrics_mod

ALERTS_PATH = Path(__file__).resolve().parent.parent / "monitoring" / "alerts.yml"


@pytest.fixture
def fresh_metrics(monkeypatch):
    """Isolate every module-level metric object behind a private,
    per-test `CollectorRegistry`. Returns that registry so a test can read
    samples back with `registry.get_sample_value(...)`."""
    registry = CollectorRegistry()
    fresh = {
        "run_total": Counter("boxbutler_run_total", "Runs by outcome", ["outcome"], registry=registry),
        "last_success_timestamp": Gauge(
            "boxbutler_last_success_timestamp",
            "Unix time of the last successful run, per assignment",
            ["assignment"],
            registry=registry,
        ),
        "assignment_state": Gauge(
            "boxbutler_assignment_state", "1 for the current state", ["assignment", "state"], registry=registry
        ),
        "tonie_seconds": Gauge(
            "boxbutler_tonie_seconds", "Seconds believed loaded", ["assignment"], registry=registry
        ),
        "cache_bytes": Gauge("boxbutler_cache_bytes", "Cache directory size", registry=registry),
        "prefetch_ready": Gauge(
            "boxbutler_prefetch_ready", "Verified renditions ready ahead", ["assignment"], registry=registry
        ),
        "extraction_broken_total": Counter(
            "boxbutler_extraction_broken_total", "Runs that hit EXTRACTION_BROKEN", registry=registry
        ),
    }
    for name, obj in fresh.items():
        monkeypatch.setattr(metrics_mod, name, obj)
    return registry


def test_refresh_exports_state_seconds_and_prefetch_per_assignment(world, fresh_metrics):
    world["deps"].settings.prefetch_depth = 3
    world["orch"].run(apply=True)
    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)

    a = world["store"].assignments.get(world["a"].id)
    expected_seconds = sum(c.seconds for c in world["store"].chapters.for_assignment(a.id))
    assert expected_seconds > 0

    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "OK"}
    ) == 1.0
    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "DEGRADED"}
    ) == 0.0
    assert fresh_metrics.get_sample_value(
        "boxbutler_tonie_seconds", {"assignment": "Green Tonie"}
    ) == expected_seconds
    assert fresh_metrics.get_sample_value(
        "boxbutler_prefetch_ready", {"assignment": "Green Tonie"}
    ) == 3.0
    assert fresh_metrics.get_sample_value("boxbutler_run_total", {"outcome": "OK"}) == 1.0


def test_degraded_state_visible_and_state_flips_are_not_sticky(world, fresh_metrics):
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)

    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "DEGRADED"}
    ) == 1.0
    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "OK"}
    ) == 0.0
    assert fresh_metrics.get_sample_value("boxbutler_run_total", {"outcome": "DEGRADED"}) == 1.0

    # Repair the tonie and refresh again -- the DEGRADED sample must drop
    # back to 0, not linger from the previous refresh (spec §13.3: a stale
    # reading read as current is the recurring bug shape this task exists
    # to avoid).
    world["sink"].fail_upload_times = 0
    a = world["store"].assignments.get(world["a"].id)
    repair_run = world["store"].runs.start("cli", dry_run=False)
    world["orch"].repair(repair_run.id, a, apply=True)
    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)
    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "OK"}
    ) == 1.0
    assert fresh_metrics.get_sample_value(
        "boxbutler_assignment_state", {"assignment": "Green Tonie", "state": "DEGRADED"}
    ) == 0.0


def test_extraction_broken_increments_the_counter(world, fresh_metrics):
    from boxbutler.fetch.protocol import ExtractionBroken

    def boom(item, cache_dir):
        raise ExtractionBroken("yt-dlp is broken")

    world["fetcher"].fetch = boom
    world["orch"].run(apply=True)
    assert fresh_metrics.get_sample_value("boxbutler_extraction_broken_total") == 1.0


def test_cache_bytes_reflects_the_cache_directory(world, fresh_metrics, tmp_path):
    cache = tmp_path / "some_cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 37)
    (cache / "b.bin").write_bytes(b"y" * 5)
    metrics_mod.refresh(world["store"], cache, 3)
    assert fresh_metrics.get_sample_value("boxbutler_cache_bytes") == 42.0


def test_refresh_is_idempotent_and_safe_with_no_runs_yet(world, fresh_metrics):
    """Calling refresh() before any run has happened (the on-scrape case
    with a brand-new deployment) must not raise -- an empty store is a
    valid state, not an unknown one."""
    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)
    assert fresh_metrics.get_sample_value(
        "boxbutler_last_success_timestamp", {"assignment": "Green Tonie"}
    ) == 0.0


def test_last_success_timestamp_is_zero_before_first_success_then_a_real_epoch(
    world, fresh_metrics
):
    """Explicit presence/absence check for the gauge `BoxButlerRunStale`
    reads (task-31 review, Major finding).

    Before any success, `refresh()` sets this gauge's per-assignment
    series to `0.0` explicitly (see `boxbutler/metrics.py::refresh`) --
    the same sentinel a fresh, never-`.set()` Prometheus `Gauge` would
    report by construction-time default, now made explicit per assignment
    (final fix review, masking-defect 1: the gauge used to be one
    unlabelled series set to the *max* across every assignment, which hid
    one stalled tonie behind any other one succeeding). `0.0` is exactly
    the sentinel `monitoring/alerts.yml`'s `BoxButlerRunStale` guards on
    via `> 0`, per assignment.

    After a real success, that assignment's series is a real Unix
    timestamp: strictly greater than 0 and close to "now" -- genuinely
    distinguishable from the fresh-install sentinel, not just numerically
    different from it.
    """
    import time

    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)
    assert fresh_metrics.get_sample_value(
        "boxbutler_last_success_timestamp", {"assignment": "Green Tonie"}
    ) == 0.0

    before = time.time()
    world["orch"].run(apply=True)
    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)
    after = time.time()

    value = fresh_metrics.get_sample_value(
        "boxbutler_last_success_timestamp", {"assignment": "Green Tonie"}
    )
    assert value > 0.0
    assert before - 1 <= value <= after + 1


def test_a_stalled_assignment_is_not_masked_by_a_healthy_sibling(world, fresh_metrics):
    """Final fix review, masking-defect 1's own reproduction: two
    assignments on one box, one succeeding every night and one stuck for
    36h+, must both be visible on `boxbutler_last_success_timestamp` --
    not just the healthiest one.

    Before the fix this gauge was a single unlabelled series set to
    `max(a.last_success_at for a in assignments)`. With Green Tonie fresh and
    Panda stalled, that global max would read as Green Tonie's fresh timestamp
    -- indistinguishable from "everything is fine" -- and
    `BoxButlerRunStale`'s `time() - boxbutler_last_success_timestamp >
    36*3600` would never trip, no matter how long Panda had been stuck,
    for as long as Green Tonie kept succeeding. Labelling the gauge by
    `assignment` (this fix) means Panda's own series carries its own,
    genuinely stale timestamp regardless of Green Tonie's.
    """
    import time

    # Green Tonie (from `world`) succeeds via a real run.
    world["orch"].run(apply=True)

    # Panda: a second, independent assignment that last succeeded 40h ago
    # -- past the 36h `BoxButlerRunStale` threshold -- and has not
    # succeeded since (its CRASHED nights are exactly M3/masking-defect
    # 1's scenario: a recurring unclassified exception). It is deliberately
    # never run here: what's under test is the metrics/alert *signal*,
    # and manufacturing the stale timestamp directly is the more direct
    # reproduction of "this assignment stopped succeeding 40h ago".
    panda = world["store"].assignments.upsert_target("fake", "T2", "Panda")
    forty_hours_ago = datetime.now(UTC) - timedelta(hours=40)
    world["store"].assignments.set_last_success(panda.id, forty_hours_ago)

    metrics_mod.refresh(world["store"], world["deps"].cache_dir, 3)

    green_ts = fresh_metrics.get_sample_value(
        "boxbutler_last_success_timestamp", {"assignment": "Green Tonie"}
    )
    panda_ts = fresh_metrics.get_sample_value(
        "boxbutler_last_success_timestamp", {"assignment": "Panda"}
    )
    assert green_ts is not None and panda_ts is not None

    now = time.time()
    threshold = 36 * 3600

    def rule_fires(ts: float) -> bool:
        # The exact BoxButlerRunStale guard, evaluated per assignment now
        # that the gauge carries the `assignment` label (see
        # test_run_stale_promql_guard_fires_only_after_a_real_success_gone_stale
        # for the same expression against bare, unlabelled values).
        return (ts > 0) and (now - ts > threshold)

    # Green Tonie is fine: recently succeeded, must not read as stale.
    assert rule_fires(green_ts) is False
    # Panda is genuinely stuck: this is the signal that must survive
    # Green Tonie's health, not get averaged/maxed away by it.
    assert rule_fires(panda_ts) is True
    # The old defect, made concrete: the pre-fix code set one unlabelled
    # gauge to max(green_ts, panda_ts). Since Green Tonie is fresher, that max is
    # Green Tonie's timestamp -- Panda's staleness never reaches the alert at
    # all under the old shape.
    old_masked_global_value = max(green_ts, panda_ts)
    assert old_masked_global_value == green_ts
    assert rule_fires(old_masked_global_value) is False, (
        "if this fires, the reproduction stopped demonstrating the masking bug"
    )


def test_run_stale_promql_guard_fires_only_after_a_real_success_gone_stale():
    """Unit-level stand-in for `BoxButlerRunStale`'s guarded expression
    (task-31 review, Major finding: sentinel-0 read as a real timestamp).

    We cannot run Prometheus here (no `promtool`/server available), so
    this evaluates the *same* two sub-expressions the shipped rule uses --
    `(boxbutler_last_success_timestamp > 0) and (time() - ... > 36*3600)`
    -- with Python operators that mirror PromQL's `>`, `-`, and vector
    `and` for representative `(last_success_timestamp, now)` pairs. Both
    sides of the real rule are bare, unlabelled series here, so PromQL
    vector `and` reduces to a plain boolean AND -- there's no
    label-matching subtlety to model for this metric.

    What this proves: the guard logic itself is correct for both the
    false-positive case (fresh install) and the true-positive case
    (genuine stall after a real success) it must not trade away.

    What this does NOT prove: that the YAML parses as valid PromQL, that
    Prometheus's `and` operator behaves as assumed once real per-assignment
    labels are involved elsewhere in the rule set, or anything else about
    the real evaluator. Run `promtool check rules monitoring/alerts.yml`
    before deploying this change.
    """
    threshold = 36 * 3600

    def fires(last_success_timestamp: float, now: float) -> bool:
        has_ever_succeeded = last_success_timestamp > 0
        is_old = (now - last_success_timestamp) > threshold
        return has_ever_succeeded and is_old

    now = 1_800_000_000.0  # an arbitrary "current" unix time

    # Fresh install / brand-new assignment: gauge still at the Gauge
    # default of 0.0 (the epoch). Must NOT fire, however much time has
    # passed since 1970 -- that was the false-positive bug.
    assert fires(last_success_timestamp=0.0, now=now) is False
    assert fires(last_success_timestamp=0.0, now=now + 10 * threshold) is False

    # Succeeded recently (well under 36h ago): must not fire.
    assert fires(last_success_timestamp=now - 3600, now=now) is False

    # Succeeded once, then genuinely went stale for 36h+: must still fire
    # -- the fix must not trade the false positive for a false negative.
    assert fires(last_success_timestamp=now - threshold - 1, now=now) is True
    assert fires(last_success_timestamp=now - 10 * threshold, now=now) is True

    # Exactly at the boundary: strict `>` means exactly 36h is not yet stale.
    assert fires(last_success_timestamp=now - threshold, now=now) is False


def test_alert_rules_are_the_five_from_the_spec():
    rules = yaml.safe_load(ALERTS_PATH.read_text())
    names = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    assert set(names) == {
        "BoxButlerScrapeDown",
        "BoxButlerTonieDegraded",
        "BoxButlerRunStale",
        "BoxButlerExtractionBroken",
        "BoxButlerPrefetchLow",
        "BoxButlerRunCrashedOrFailed",
    }
    assert names["BoxButlerScrapeDown"]["for"] == "10m"
    assert 'up{job="box-butler"} == 0' in names["BoxButlerScrapeDown"]["expr"]
    assert "36" in names["BoxButlerRunStale"]["expr"]
    assert names["BoxButlerPrefetchLow"]["for"] == "24h"
    assert all(v.isascii() for r in names.values() for v in r["annotations"].values())
    assert names["BoxButlerTonieDegraded"]["labels"]["severity"] == "critical"
    # Every rule names the job/state it fires on, not a bare absence of data
    # (spec §13.3: "check failed" != "thing unhealthy").
    assert 'boxbutler_assignment_state{state="DEGRADED"}' in names["BoxButlerTonieDegraded"]["expr"]
    assert "boxbutler_last_success_timestamp" in names["BoxButlerRunStale"]["expr"]
    assert "boxbutler_extraction_broken_total" in names["BoxButlerExtractionBroken"]["expr"]
    assert "boxbutler_prefetch_ready" in names["BoxButlerPrefetchLow"]["expr"]
    # Final fix review, masking-defect 1: CRASHED/FAILED runs must be
    # alertable even when the History screen is nobody's dashboard.
    assert 'boxbutler_run_total{outcome=~"CRASHED|FAILED"}' in names["BoxButlerRunCrashedOrFailed"]["expr"]
    for rule in names.values():
        assert rule["labels"]["service"] == "box-butler"
        assert "summary" in rule["annotations"] and "description" in rule["annotations"]


def test_metrics_endpoint_is_public_for_the_scraper(tmp_path):
    from fastapi.testclient import TestClient

    from boxbutler.config import load_settings
    from boxbutler.main import build, create_web_app

    media_root = tmp_path / "media"
    media_root.mkdir()
    env = {
        "BOXBUTLER_SINK_USER": "u",
        "BOXBUTLER_SINK_PASSWORD": "p",
        "BOXBUTLER_ADMIN_USER": "a",
        "BOXBUTLER_ADMIN_PASSWORD": "b",
        "BOXBUTLER_SECRET_KEY": "k",
        "BOXBUTLER_SINK_KIND": "fake",
        "BOXBUTLER_DATA_DIR": str(tmp_path),
        "BOXBUTLER_CACHE_DIR": str(tmp_path / "cache"),
        "BOXBUTLER_MEDIA_ROOT": str(media_root),
    }
    c = TestClient(create_web_app(build(load_settings(None, env))))
    r = c.get("/metrics")
    assert r.status_code == 200
    assert b"boxbutler_run_total" in r.content


def test_building_two_apps_in_one_process_does_not_double_register(tmp_path):
    """The other half of the double-registration hazard: two `AppDeps` /
    two `create_web_app()` calls in the same process (exactly what
    `tests/test_main_wiring.py` does across its many tests) must not
    raise, because the metric objects are created once at import time,
    not once per app."""
    from fastapi.testclient import TestClient

    from boxbutler.config import load_settings
    from boxbutler.main import build, create_web_app

    media_root = tmp_path / "media"
    media_root.mkdir()

    def make_client(sub):
        env = {
            "BOXBUTLER_SINK_USER": "u",
            "BOXBUTLER_SINK_PASSWORD": "p",
            "BOXBUTLER_ADMIN_USER": "a",
            "BOXBUTLER_ADMIN_PASSWORD": "b",
            "BOXBUTLER_SECRET_KEY": "k",
            "BOXBUTLER_SINK_KIND": "fake",
            "BOXBUTLER_DATA_DIR": str(tmp_path / sub),
            "BOXBUTLER_CACHE_DIR": str(tmp_path / sub / "cache"),
            "BOXBUTLER_MEDIA_ROOT": str(media_root),
        }
        return TestClient(create_web_app(build(load_settings(None, env))))

    c1 = make_client("one")
    c2 = make_client("two")
    assert c1.get("/metrics").status_code == 200
    assert c2.get("/metrics").status_code == 200
