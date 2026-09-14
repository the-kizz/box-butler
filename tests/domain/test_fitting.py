import pytest
from boxbutler.domain.fitting import (clamp_cap, fit_single, fit_fill, DEFAULT_CAP_SECONDS, FitPiece)

CAP = 5395

@pytest.mark.parametrize("seconds,take,truncated", [
    (1000.0, 1000.0, False),   # shorter than cap
    (5395.0, 5395.0, False),   # equal
    (7200.0, 5395.0, True),    # longer (Wobbling Badger, 2 h)
    (9010.0, 5395.0, True),    # far longer (Captain Custard's Nap)
])
def test_fit_single_table(seconds, take, truncated):
    assert fit_single("x", seconds, CAP) == FitPiece("x", take, truncated)

def test_default_cap_is_5395_seconds_not_5340():
    # Measured against the live cloud: a settled chapter of 5340.0s read back with
    # remaining=59.0s and no encoder drift at all. The cap only needs 5s of paranoia
    # under the hard 5400s sink max, not 60.
    assert DEFAULT_CAP_SECONDS == 5395
    assert 5400 - DEFAULT_CAP_SECONDS == 5          # the headroom, asserted explicitly

def test_cap_clamped_to_sink_max():
    assert clamp_cap(6000, 5400) == 5400
    assert clamp_cap(5340, 5400) == 5340
    assert clamp_cap(0, 5400) == 5400                # nonsense config falls back to sink max

def test_fill_packs_in_order_until_cap():
    plan = fit_fill([("a", 2000), ("b", 2000), ("c", 2000)], CAP, allow_partial_tail=True)
    assert [p.item_id for p in plan.pieces] == ["a", "b", "c"]
    assert plan.pieces[-1] == FitPiece("c", 1395.0, True)
    assert plan.total_seconds == CAP and plan.dropped == []

def test_fill_without_partial_tail_drops_the_tail():
    plan = fit_fill([("a", 2000), ("b", 2000), ("c", 2000)], CAP, allow_partial_tail=False)
    assert [p.item_id for p in plan.pieces] == ["a", "b"]
    assert plan.dropped == ["c"] and plan.total_seconds == 4000

def test_fill_exact_fit_is_not_truncated():
    plan = fit_fill([("a", 2697), ("b", 2698)], CAP)
    assert all(not p.truncated for p in plan.pieces) and plan.total_seconds == CAP

def test_fill_first_item_longer_than_cap_is_truncated_single():
    plan = fit_fill([("a", 9000), ("b", 10)], CAP)
    assert plan.pieces == [FitPiece("a", CAP, True)] and plan.dropped == ["b"]

def test_fill_empty():
    plan = fit_fill([], CAP)
    assert plan.pieces == [] and plan.total_seconds == 0

def test_fill_exact_exhaustion_drops_next_item_not_a_zero_length_piece():
    # "a" exactly exhausts remaining via a non-truncated full pack; "b" must be dropped,
    # not appended as a spurious zero-length "truncated" piece.
    plan = fit_fill([("a", CAP), ("b", 100)], CAP)
    assert plan.pieces == [FitPiece("a", float(CAP), False)]
    assert plan.dropped == ["b"] and plan.total_seconds == CAP

def test_fill_exhaustion_via_truncation_also_drops_next_item():
    # symmetric case: "a" exhausts remaining via a truncated partial pack; "b" must still
    # be dropped, not appended as a spurious zero-length "truncated" piece.
    plan = fit_fill([("a", 6000), ("b", 100)], CAP)
    assert plan.pieces == [FitPiece("a", CAP, True)]
    assert plan.dropped == ["b"] and plan.total_seconds == CAP

def test_fill_stops_at_chapter_bound_when_it_comes_first():
    # The sink's real limits are maxSeconds=5400, maxChapters=250: 300 tracks of 20s each
    # (6000s total) would never hit the time bound (250 * 20s = 5000s < CAP) but must stop
    # at the chapter bound. Fitting stays a seconds problem — max_chapters is a count-only
    # secondary guard, no chapter-count math leaks into the truncation logic.
    candidates = [(f"t{i}", 20.0) for i in range(300)]
    plan = fit_fill(candidates, CAP, max_chapters=250)
    assert len(plan.pieces) == 250
    assert [p.item_id for p in plan.pieces] == [f"t{i}" for i in range(250)]
    assert all(not p.truncated for p in plan.pieces)          # stopped by count, not by truncation
    assert plan.total_seconds == 250 * 20.0
    assert plan.dropped == [f"t{i}" for i in range(250, 300)]

def test_fill_time_bound_still_wins_when_chapter_bound_is_not_reached():
    # Confirms the other tests' time-bound stops are unaffected by the new default
    # max_chapters=250 — well under the chapter bound, so the seconds cap still decides.
    plan = fit_fill([("a", 2000), ("b", 2000), ("c", 2000)], CAP, allow_partial_tail=True)
    assert len(plan.pieces) == 3 < 250
    assert plan.total_seconds == CAP
