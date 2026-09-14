"""Notifier wiring into the run loop (Task 25; spec §9.1, §2).

Uses `world` from `tests/orchestrator/conftest.py` and `NullNotifier` so
no test here makes a network call.
"""
from boxbutler.fetch.protocol import ExtractionBroken
from boxbutler.notify.none import NullNotifier


def test_degraded_alerts_immediately(world):
    n = NullNotifier()
    world["deps"].notifier = n
    world["sink"].fail_upload_times = 99
    world["orch"].run(apply=True)
    assert any(c[0] == "failure" and "DEGRADED" in c[2] for c in n.calls)


def test_success_class_used_on_swap(world):
    n = NullNotifier()
    world["deps"].notifier = n
    world["orch"].run(apply=True)
    assert any(c[0] == "success" and "Green Tonie" in c[1] + c[2] for c in n.calls)


def test_relaxations_are_debug_class(world, store):
    n = NullNotifier()
    world["deps"].notifier = n
    store.items.set_enabled(world["items"][1].id, False)
    store.items.set_enabled(world["items"][2].id, False)
    world["sink"].add_target("T2", "Blue", [])
    b = store.assignments.upsert_target("fake", "T2", "Blue")
    store.assignments.assign_library(b.id, world["lib"].id)
    world["orch"].run(apply=True)
    assert any(c[0] == "debug" and "RELAXED_UNIQUENESS" in c[2] for c in n.calls)


def test_no_notifier_configured_is_a_true_noop(world):
    """`deps.notifier is None` (the default) means no notifications at all
    — not an error, not a crash. A run must still complete normally."""
    assert world["deps"].notifier is None
    world["sink"].fail_upload_times = 99
    rep = world["orch"].run(apply=True)
    assert rep.reports  # the run still completed and reported an outcome


def test_extraction_broken_notification_is_actionable_not_a_bare_code(world):
    """Final coherence review, Major-4: `"extraction_broken"` reaching an
    operator as a bare reason code, with no hint that it usually means
    yt-dlp needs updating, was the difference between a fixable evening
    and a mystery. The raw code must still be present (metrics/tests key
    off it) but the notification body must also carry guidance.
    """
    n = NullNotifier()
    world["deps"].notifier = n
    world["fetcher"].fail[world["items"][0].id] = ExtractionBroken("n challenge")
    world["orch"].run(apply=True)

    failures = [c for c in n.calls if c[0] == "failure"]
    assert failures, "no failure notification was sent"
    body = " ".join(c[2] for c in failures)
    assert "extraction_broken" in body           # raw code kept, for grepping/metrics
    assert "yt-dlp" in body                      # the actionable guidance
    assert body.isascii()                        # ntfy title constraint's spirit — the whole message stays plain ASCII
