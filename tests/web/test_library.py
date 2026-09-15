"""Library screen tests (Task 10; spec §5 screen 2, §10.17).

Exercises the seeded fake data end-to-end through FastAPI's TestClient —
never the network, never a real tonie. See the `seeded` fixture in
conftest.py (boxbutler.web.fake_data.seed_fake against the real FakeSink).

Per-mechanism testing (per the Task 9 lesson): a string match on an
attribute proves nothing about behaviour. Where the underlying mechanism
matters (move buttons, drag threshold, the delete confirm), these tests
either exercise the route directly or assert the real JS guard
(`onclick="return confirm(...)"`) rather than an inert attribute like
`hx-confirm`. Real-browser verification (Playwright) is done separately
and recorded in the task report.

Fix round 1 (Task 10 review) added the tests below the brief's/original
set: Critical 1 (dense positions after delete), Critical 2 (page-local
reorder must not corrupt other pages' positions), and Important 3 (a move
redirect must target the moved item, not the top of the page).
"""
import re

from boxbutler.domain.models import ItemKind


def _lib(store, name="Bedtime"):
    return next(l for l in store.libraries.list() if l.name == name)


def _add_n_items(store, library_id, n, start=0):
    items = []
    for i in range(start, start + n):
        items.append(
            store.items.add(
                library_id=library_id,
                kind=ItemKind.URL,
                source_ref=f"https://example.invalid/item{i}",
                source_key=f"bulk-item-{i}",
                title=f"Bulk Item {i}",
            )
        )
    return items


def test_library_page_lists_items_in_order_with_mode(seeded, store):
    lib = _lib(store)
    html = seeded.get(f"/libraries/{lib.id}").text
    items = store.items.list(lib.id)
    assert html.index(items[0].title) < html.index(items[-1].title)
    assert 'name="mode"' in html and 'value="single" selected' in html


def test_move_buttons_exist_beside_every_drag_handle(seeded, store):
    lib = _lib(store)
    html = seeded.get(f"/libraries/{lib.id}").text
    n = len(store.items.list(lib.id))
    assert html.count('aria-label="Move up"') == n and html.count('aria-label="Move down"') == n
    assert html.count('class="drag-handle"') == n


def test_move_down_via_button_reorders(seeded, store):
    lib = _lib(store)
    ids = [i.id for i in store.items.list(lib.id)]
    r = seeded.post(f"/libraries/{lib.id}/items/{ids[0]}/move", data={"delta": "1"})
    assert r.status_code in (200, 303)
    assert [i.id for i in store.items.list(lib.id)][:2] == [ids[1], ids[0]]


def test_drag_reorder_endpoint(seeded, store):
    lib = _lib(store)
    ids = [i.id for i in store.items.list(lib.id)]
    seeded.post(f"/libraries/{lib.id}/reorder", data={"order": ",".join(reversed(ids))})
    assert [i.id for i in store.items.list(lib.id)] == list(reversed(ids))


def test_add_is_on_the_library_page(seeded, store):
    lib = _lib(store)
    html = seeded.get(f"/libraries/{lib.id}").text
    assert 'name="ref"' in html and 'enctype="multipart/form-data"' in html
    n = len(store.items.list(lib.id))
    seeded.post(f"/libraries/{lib.id}/items", data={"ref": "https://example.invalid/watch?v=fake9999"})
    assert len(store.items.list(lib.id)) == n + 1


def test_mode_change(seeded, store):
    lib = _lib(store)
    seeded.post(f"/libraries/{lib.id}/mode", data={"mode": "serial"})
    assert store.libraries.get(lib.id).mode == "serial"


def test_loudnorm_toggle_per_item(seeded, store):
    lib = _lib(store)
    it = store.items.list(lib.id)[0]
    seeded.post(f"/libraries/{lib.id}/items/{it.id}/loudnorm", data={"on": "1"})
    assert store.items.get(it.id).loudnorm is True


def test_delete_library_requires_confirm_token(seeded, store):
    lib = store.libraries.create("Doomed")
    r = seeded.post(f"/libraries/{lib.id}/delete", data={})
    assert r.status_code == 400 and store.libraries.get(lib.id) is not None
    r = seeded.post(f"/libraries/{lib.id}/delete", data={"confirm": lib.name})
    assert r.status_code == 303 and store.libraries.get(lib.id) is None


def test_every_library_shows_its_folder_and_a_scan_button(seeded, store):
    """This used to be `test_scan_button_only_for_folder_libraries`, and
    it encoded the model this change removed: a library that might or
    might not have a folder. A library *is* a folder now, so the folder
    and its Scan button belong on every one of them.

    The page has already scanned the folder before rendering (see
    `routes/library.py::_scan_on_open`); the button stays for a
    deliberate re-scan.
    """
    for name in ("Audiobook", "Bedtime", "Car Trips"):
        lib = _lib(store, name)
        assert lib.folder_path, f"{name} has no folder"
        html = seeded.get(f"/libraries/{lib.id}").text
        assert "Scan folder" in html
        assert lib.folder_path in html


# --- Additional coverage beyond the brief's sketch --------------------

def test_library_requires_login(client, store):
    # No seeded fixture (unauthenticated client) — every route on this
    # screen must be behind require_login like every other screen.
    r = client.get("/libraries")
    assert r.status_code in (303, 401)


def test_remove_item_button_has_a_working_confirm_not_an_inert_attribute(seeded, store):
    """Mirrors the dashboard's Apply-button lesson (Task 9, fix round 1,
    critical 1): a plain `<form method="post">` submit is not intercepted
    by htmx, so `hx-confirm` alone does nothing. Removing an item is
    destructive (design-system/box-butler/pages/library.md
    `confirmation-dialogs`), so it must carry the same real
    `onclick="return confirm(...)"` guard already proven to work for
    Apply/Repair on the dashboard.

    Final coherence review, Major-5: like the dashboard's Apply-button
    test, this one only ever string-matched `onclick="return confirm("`
    and never checked `type="submit"` — a break-test proof found that
    switching the button to `type="button"` (inert: never submits its
    form) left this test green. Extract the actual `<button ...>` tag with
    a regex and pin `type="submit"` on it explicitly.
    """
    lib = _lib(store)
    it = store.items.list(lib.id)[0]
    html = seeded.get(f"/libraries/{lib.id}").text
    assert "hx-confirm" not in html
    remove_form = html.split(f'/items/{it.id}/delete"')[1][:600]
    buttons = re.findall(r"<button\b[^>]*>", remove_form)
    assert buttons, "no Remove button found"
    tag = buttons[0]
    assert 'onclick="return confirm(' in tag
    assert 'type="submit"' in tag, (
        "Remove button must be type=\"submit\" -- type=\"button\" never "
        "submits the form, so a working confirm() would still be inert"
    )


def test_remove_item_deletes_it(seeded, store):
    lib = _lib(store)
    it = store.items.list(lib.id)[0]
    n = len(store.items.list(lib.id))
    r = seeded.post(f"/libraries/{lib.id}/items/{it.id}/delete", data={})
    assert r.status_code in (200, 303)
    assert len(store.items.list(lib.id)) == n - 1
    assert store.items.get(it.id) is None


def test_enabled_toggle_per_item(seeded, store):
    lib = _lib(store)
    it = store.items.list(lib.id)[0]
    seeded.post(f"/libraries/{lib.id}/items/{it.id}/enabled", data={})
    assert store.items.get(it.id).enabled is False
    seeded.post(f"/libraries/{lib.id}/items/{it.id}/enabled", data={"enabled": "1"})
    assert store.items.get(it.id).enabled is True


def test_scan_folder_is_a_no_op_in_phase_2(seeded, store):
    # Phase 2 must never touch a real filesystem — the default
    # app.state.scan_folder is a no-op returning 0 new items.
    audiobook = _lib(store, "Audiobook")
    n_before = len(store.items.list(audiobook.id))
    r = seeded.post(f"/libraries/{audiobook.id}/scan", data={})
    assert r.status_code in (200, 303)
    assert len(store.items.list(audiobook.id)) == n_before


def test_libraries_index_lists_and_creates(seeded, store):
    html = seeded.get("/libraries").text
    assert "Bedtime" in html and "Car Trips" in html and "Audiobook" in html
    r = seeded.post("/libraries", data={"name": "New Playlist", "mode": "album"})
    assert r.status_code == 303
    created = next(l for l in store.libraries.list() if l.name == "New Playlist")
    assert created.mode == "album"


def test_state_pill_is_not_colour_only(seeded, store):
    """compact-label-semantics: item state pills must carry a word, not
    just a colour class, and must be static markup (not an interactive
    control) since state is derived, not user-set.
    """
    audiobook = _lib(store, "Audiobook")
    html = seeded.get(f"/libraries/{audiobook.id}").text
    assert "unavailable" in html.lower()


def test_item_position_numbers_are_shown(seeded, store):
    lib = _lib(store)
    html = seeded.get(f"/libraries/{lib.id}").text
    assert 'class="item-position"' in html


def test_long_item_warns_it_will_be_trimmed(seeded, store):
    """`-t cap -c copy` silently keeps only the first `cap_seconds` of a
    long item -- a 2h31m story becomes its first 89 minutes on the tonie,
    with nothing on the library page saying so. A folder-backed library's
    items are exactly the case that used to hide this worst: `seconds` is
    probed now (fix elsewhere), but a known duration past the cap must
    still say so plainly on the row, not just show a plain "N min" as if
    the whole thing would play.
    """
    lib = _lib(store)
    long_item = store.items.add(
        library_id=lib.id,
        kind=ItemKind.URL,
        source_ref="https://example.invalid/long",
        source_key="long-story",
        title="The Very Long Bedtime Saga",
        seconds=9060.0,  # 2h31m -- well past the ~89m default cap
    )
    html = seeded.get(f"/libraries/{lib.id}").text
    row = html[html.index(long_item.title):]
    row = row[: row.index("</li>")]
    assert "2h 31m" in row
    assert "trimmed to 89m" in row


def test_unknown_duration_item_still_says_unknown_not_a_guess(seeded, store):
    """A folder item whose duration couldn't be probed must keep saying
    "unknown duration" -- never silently guess, and never warn about a
    trim it has no evidence will actually happen."""
    lib = _lib(store)
    unknown_item = store.items.add(
        library_id=lib.id,
        kind=ItemKind.FOLDER_FILE,
        source_ref="unprobeable.mp3",
        source_key="unprobeable",
        title="Mystery Length Story",
        seconds=None,
    )
    html = seeded.get(f"/libraries/{lib.id}").text
    row = html[html.index(unknown_item.title):]
    row = row[: row.index("</li>")]
    assert "unknown duration" in row
    assert "trimmed to" not in row


# --- Fix round 1 (Task 10 review) --------------------------------------

def test_delete_renumbers_remaining_items_densely(seeded, store):
    """Critical 1: deleting from the middle must not leave a gap — a gap
    breaks the displayed position numbers, the Move-down disabled check,
    and move()'s neighbour lookup, all of which assume dense 0-based
    positions. Reproduced against the store directly before the fix:
    deleting position 2 of 5 left positions 0,1,3,4.
    """
    lib = _lib(store)
    items = store.items.list(lib.id)
    assert len(items) >= 5
    victim = items[2]
    store.items.delete(victim.id)

    remaining = store.items.list(lib.id)
    assert [i.position for i in remaining] == list(range(len(remaining)))


def test_move_down_on_the_new_last_item_is_a_disabled_noop(seeded, store):
    """Critical 1, continued: once positions are dense again, the true
    last item's Move-down button must render disabled (not merely absent
    a neighbour), and pressing move on it anyway must be a genuine no-op
    — the item was already last, not silently failing to find a neighbour
    across a stale gap.
    """
    lib = _lib(store)
    items = store.items.list(lib.id)
    victim = items[2]
    store.items.delete(victim.id)

    remaining = store.items.list(lib.id)
    last = remaining[-1]

    html = seeded.get(f"/libraries/{lib.id}").text
    row_start = html.index(f'data-item-id="{last.id}"')
    row_html = html[row_start : row_start + 2000]
    move_down_idx = row_html.index('aria-label="Move down"')
    # `disabled` sits after aria-label in the template's attribute order
    # (`aria-label="Move down" {% if ... %}disabled{% endif %}>`).
    assert "disabled" in row_html[move_down_idx : move_down_idx + 40]

    before_position = store.items.get(last.id).position
    r = seeded.post(f"/libraries/{lib.id}/items/{last.id}/move", data={"delta": "1"})
    assert r.status_code in (200, 303)
    assert store.items.get(last.id).position == before_position


def test_reorder_on_page_2_does_not_corrupt_other_pages_positions(seeded, store):
    """Critical 2: the library screen paginates at 50 items and
    SortableJS is wired to only the current page's <ol>, so a drag on
    page 2 posts only page-2 ids. Reproduced before the fix: reordering
    within page 2 of a 60-item library produced duplicate positions
    (0,0,1,1,2,...), colliding with page 1's real items. The fix passes
    `page` alongside `order` so the server writes that page's own
    contiguous position range.
    """
    lib = store.libraries.create("Sixty Items")
    _add_n_items(store, lib.id, 60)
    items = store.items.list(lib.id)
    assert [i.position for i in items] == list(range(60))

    page2_ids = [i.id for i in items[50:60]]
    reversed_page2 = list(reversed(page2_ids))

    r = seeded.post(
        f"/libraries/{lib.id}/reorder",
        data={"order": ",".join(reversed_page2), "page": "2"},
    )
    assert r.status_code in (200, 303)

    all_items = store.items.list(lib.id)
    positions = [i.position for i in all_items]
    assert positions == list(range(60)), "positions must stay unique and contiguous across pages"
    assert [i.id for i in all_items[50:60]] == reversed_page2
    # page 1 untouched
    assert [i.id for i in all_items[:50]] == [i.id for i in items[:50]]


def test_reorder_dropped_back_in_place_is_a_real_noop(seeded, store):
    """States explicitly what a drag that ends where it started does: it
    must not rewrite positions, even though writing the same values back
    would be observably identical — `ItemRepo.reorder`'s `position != ?`
    guard means no row is even touched.
    """
    lib = _lib(store)
    items = store.items.list(lib.id)
    ids = [i.id for i in items]

    r = seeded.post(f"/libraries/{lib.id}/reorder", data={"order": ",".join(ids), "page": "1"})
    assert r.status_code in (200, 303)
    assert [i.id for i in store.items.list(lib.id)] == ids
    assert [i.position for i in store.items.list(lib.id)] == [i.position for i in items]


def test_move_redirect_targets_the_moved_item_not_the_page_top(seeded, store):
    """Important 3: a plain 303 -> GET redirect resets focus to the top
    of the document, making keyboard reordering technically operable but
    not usable (re-tabbing past the nav/mode/add-item forms and every
    preceding row after every single move). The redirect must instead
    target the moved item's own row.
    """
    lib = _lib(store)
    ids = [i.id for i in store.items.list(lib.id)]
    r = seeded.post(f"/libraries/{lib.id}/items/{ids[0]}/move", data={"delta": "1"})
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"#item-{ids[0]}:down")


def test_move_redirect_identifies_the_pressed_direction(seeded, store):
    """Important 3, review round 2: landing focus on *the row* wasn't
    enough — item_row.html always renders Move up before Move down, so a
    "first enabled move button" selector kept landing on Move up even
    after a Move-down press. The redirect must name which direction was
    actually pressed so the client script can target that same button.
    """
    lib = _lib(store)
    ids = [i.id for i in store.items.list(lib.id)]

    r_down = seeded.post(f"/libraries/{lib.id}/items/{ids[0]}/move", data={"delta": "1"})
    assert r_down.headers["location"].endswith(f"#item-{ids[0]}:down")

    # ids[1] is now at position 0 after the swap above; move it back up.
    r_up = seeded.post(f"/libraries/{lib.id}/items/{ids[1]}/move", data={"delta": "-1"})
    assert r_up.headers["location"].endswith(f"#item-{ids[1]}:up")


def test_reorder_with_out_of_range_page_stays_dense(seeded, store):
    """Critical 2, reopened in review round 2: `start_position` was only
    ever bounded from below (`max(0, page - 1)`), never from above.
    Reproduced before this fix: `page=99` on a 6-item library computed
    `start_position = 4900`, leaving positions `4900..4905` — breaking
    the dense-0-based invariant `delete()` (Critical 1) established, and
    reintroducing the dead-end-button bug the same way a delete-caused
    gap did (`item.position == 0` / `== total - 1` never matching).

    Two independent layers now guard this: the route clamps `page` to
    the library's real last page, and `ItemRepo.reorder` self-heals
    positions to dense `0..n-1` regardless of what `start_position` it
    was given. This test posts the attack input directly at the route
    (not at the repo) so it also exercises the clamp.
    """
    lib = _lib(store)
    items = store.items.list(lib.id)
    n = len(items)
    assert n < 50, "fixture assumption: fits on a single page"
    ids = [i.id for i in items]
    reversed_ids = list(reversed(ids))

    r = seeded.post(
        f"/libraries/{lib.id}/reorder",
        data={"order": ",".join(reversed_ids), "page": "99"},
    )
    assert r.status_code in (200, 303)

    all_items = store.items.list(lib.id)
    assert [i.position for i in all_items] == list(range(n))
    assert [i.id for i in all_items] == reversed_ids
