from boxbutler.domain.models import Assignment, AssignmentMode, Item, ItemKind, ItemState
from boxbutler.domain.rotation import eligible, order_for, advance_cursor


def mk_item(i, enabled=True, state=ItemState.OK):
    return Item(id=f"i{i}", library_id="L", position=i, kind=ItemKind.YOUTUBE,
                source_ref=f"https://example.invalid/{i}", source_key=f"vid{i}", title=f"Story {i}",
                enabled=enabled, state=state)


def mk_asg(cursor=0, mode=AssignmentMode.ORDERED, seed=42, pinned=None, aid="A"):
    return Assignment(id=aid, sink="fake", target_id=f"T-{aid}", target_name=aid, library_id="L",
                       mode=mode, shuffle_seed=seed, pinned_item_id=pinned, cursor_position=cursor)


ITEMS = [mk_item(i) for i in range(5)]


def test_ordered_starts_at_cursor():
    assert [i.id for i in order_for(mk_asg(cursor=2), ITEMS)] == ["i2", "i3", "i4", "i0", "i1"]


def test_cursor_is_per_assignment_not_per_library():
    a = order_for(mk_asg(cursor=0, aid="A"), ITEMS)
    b = order_for(mk_asg(cursor=3, aid="B"), ITEMS)
    assert a[0].id == "i0" and b[0].id == "i3"       # same library, independent places


def test_disabled_items_skipped_without_consuming_cursor():
    items = [mk_item(0), mk_item(1, enabled=False), mk_item(2), mk_item(3, state=ItemState.UNAVAILABLE)]
    assert [i.id for i in eligible(items)] == ["i0", "i2"]
    # cursor indexes the eligible list: cursor 1 -> i2, not the disabled i1
    assert order_for(mk_asg(cursor=1), items)[0].id == "i2"
    assert advance_cursor(1, 1, len(eligible(items))) == 0


def test_shuffle_is_full_deterministic_permutation():
    seen = [order_for(mk_asg(cursor=c, mode=AssignmentMode.SHUFFLE, seed=7), ITEMS)[0].id for c in range(5)]
    assert sorted(seen) == sorted(i.id for i in ITEMS)      # every item once before any repeat
    again = [order_for(mk_asg(cursor=c, mode=AssignmentMode.SHUFFLE, seed=7), ITEMS)[0].id for c in range(5)]
    assert seen == again                                     # re-run picks the same "next" (idempotency)


def test_shuffle_cursor_past_n_wraps_and_still_covers_all():
    cyc2 = [order_for(mk_asg(cursor=c, mode=AssignmentMode.SHUFFLE, seed=7), ITEMS)[0].id for c in range(5, 10)]
    assert sorted(cyc2) == sorted(i.id for i in ITEMS)


def test_different_seeds_differ():
    a = [order_for(mk_asg(cursor=c, mode=AssignmentMode.SHUFFLE, seed=1), ITEMS)[0].id for c in range(5)]
    b = [order_for(mk_asg(cursor=c, mode=AssignmentMode.SHUFFLE, seed=2), ITEMS)[0].id for c in range(5)]
    assert a != b


def test_advance_wraps():
    assert advance_cursor(4, 1, 5) == 0
    assert advance_cursor(3, 4, 5) == 2
    assert advance_cursor(3, 1, 0) == 0


def test_empty_library_orders_to_empty():
    assert order_for(mk_asg(), []) == []
