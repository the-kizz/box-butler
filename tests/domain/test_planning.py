from boxbutler.domain.models import LibraryMode
from boxbutler.domain.rotation import PlanInput, choose_next
from tests.domain.test_rotation import mk_item, mk_asg, ITEMS


def plan(**kw):
    defaults = dict(assignment=mk_asg(), library_mode=LibraryMode.SINGLE, items=ITEMS)
    defaults.update(kw)
    return choose_next(PlanInput(**defaults))


def test_single_picks_one_at_cursor():
    p = plan(assignment=mk_asg(cursor=1))
    assert p.item_ids == ["i1"] and p.rotates and p.relaxations == []


def test_pin_freezes():
    p = plan(assignment=mk_asg(cursor=3, pinned="i0"))
    assert p.item_ids == ["i0"] and p.reason == "PINNED" and p.rotates is False


def test_pin_ignores_duplicate_avoidance():
    p = plan(assignment=mk_asg(pinned="i0"), loaded_elsewhere=frozenset({"i0"}))
    assert p.item_ids == ["i0"]


def test_album_returns_everything_and_does_not_rotate():
    p = plan(library_mode=LibraryMode.ALBUM, assignment=mk_asg(cursor=2))
    assert p.item_ids == ["i0", "i1", "i2", "i3", "i4"] and p.rotates is False


def test_serial_returns_from_cursor_onward_wrapping():
    p = plan(library_mode=LibraryMode.SERIAL, assignment=mk_asg(cursor=3))
    assert p.item_ids == ["i3", "i4", "i0", "i1", "i2"] and p.rotates


def test_assignment_override_beats_library_mode():
    from dataclasses import replace
    p = plan(library_mode=LibraryMode.SINGLE, assignment=replace(mk_asg(), mode_override=LibraryMode.ALBUM))
    assert len(p.item_ids) == 5


def test_duplicate_on_other_tonie_is_skipped():
    p = plan(loaded_elsewhere=frozenset({"i0"}))
    assert p.item_ids == ["i1"] and p.relaxations == []


def test_ladder_step2_relaxes_cooldown_first():
    # cooldown blocks i1..i4, uniqueness blocks i0 -> drop cooldown -> i1 is available
    p = plan(loaded_elsewhere=frozenset({"i0"}), recently_held=frozenset({"i1", "i2", "i3", "i4"}), cooldown_active=True)
    assert p.item_ids == ["i1"] and p.relaxations == ["RELAXED_COOLDOWN"]


def test_ladder_step3_relaxes_uniqueness_second():
    items = [mk_item(0), mk_item(1)]
    p = plan(items=items, loaded_elsewhere=frozenset({"i0", "i1"}), recently_held=frozenset({"i0", "i1"}), cooldown_active=True)
    assert p.item_ids == ["i0"] and p.relaxations == ["RELAXED_COOLDOWN", "RELAXED_UNIQUENESS"]


def test_two_items_three_tonies_yields_repeat_never_empty():
    items = [mk_item(0), mk_item(1)]
    p = plan(items=items, loaded_elsewhere=frozenset({"i0", "i1"}))
    assert p.item_ids and p.reason is None and "RELAXED_UNIQUENESS" in p.relaxations


def test_no_candidate_when_library_empty_or_all_disabled():
    p = plan(items=[mk_item(0, enabled=False)])
    assert p.item_ids == [] and p.reason == "EMPTY_LIBRARY"


def test_avoid_duplicates_off_never_relaxes():
    p = plan(loaded_elsewhere=frozenset({"i0"}), avoid_duplicates=False)
    assert p.item_ids == ["i0"] and p.relaxations == []


# --- Pin/mode interaction: a pin freezes the cursor, it does not shrink the load (spec §4) ---

def test_pin_on_album_loads_whole_library():
    p = plan(library_mode=LibraryMode.ALBUM, assignment=mk_asg(cursor=2, pinned="i2"))
    assert p.item_ids == ["i0", "i1", "i2", "i3", "i4"]
    assert p.rotates is False and p.reason == "PINNED"


def test_pin_on_serial_fills_from_pinned_item():
    p = plan(library_mode=LibraryMode.SERIAL, assignment=mk_asg(cursor=0, pinned="i3"))
    assert p.item_ids == ["i3", "i4", "i0", "i1", "i2"]
    assert len(p.item_ids) > 1
    assert p.rotates is False and p.reason == "PINNED"


def test_pin_on_single_still_yields_pinned_item():
    p = plan(assignment=mk_asg(cursor=0, pinned="i2"))
    assert p.item_ids == ["i2"]
    assert p.rotates is False and p.reason == "PINNED"


def test_pin_on_ineligible_item_falls_back_to_normal_ordering():
    items = [mk_item(0, enabled=False), mk_item(1), mk_item(2)]
    p = plan(items=items, assignment=mk_asg(cursor=0, pinned="i0"))
    assert p.item_ids == ["i1"]
    assert p.reason is None and p.rotates is True
