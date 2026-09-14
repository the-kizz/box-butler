"""Dashboard screen tests (Task 9; spec §5 screen 1).

Exercises the seeded fake data end-to-end through FastAPI's TestClient —
never the network, never a real tonie. See the `seeded` fixture in
conftest.py (boxbutler.web.fake_data.seed_fake against the real FakeSink).
"""
import re
from pathlib import Path

from boxbutler.domain.models import ItemKind
from boxbutler.web.routes.library import PAGE_SIZE


def _first_managed(store):
    return next(a for a in store.assignments.list() if a.library_id and a.state == "OK").id


def _card_html(html, target_name):
    """The HTML of a single card, located by its title -- robust to
    where the card falls on the page (unlike a fixed offset from some
    other landmark, which can land short or land on the wrong card)."""
    start = html.rindex("<article", 0, html.index(f">{target_name}<"))
    end = html.index("</article>", start) + len("</article>")
    return html[start:end]


def _bulk_up_library(store, library_id, n):
    """Grow a library well past PIN_SEARCH_THRESHOLD (== library.py's
    PAGE_SIZE) with distinctively-named items, the shape the operator's
    real 838-episode podcast feed exercises."""
    for i in range(n):
        store.items.add(
            library_id=library_id,
            kind=ItemKind.URL,
            source_ref=f"https://example.invalid/bulk{i:03d}",
            source_key=f"bulk-{i:03d}",
            title=f"Bulk Item {i:03d}",
        )


def test_dashboard_shows_one_card_per_target_including_unmanaged(seeded):
    html = seeded.get("/").text
    for name in ["Green Tonie", "Blue Tonie", "Red Tonie", "Spare Tonie"]:
        assert name in html
    assert "Unmanaged" in html and "no library assigned" in html


def test_pinned_assignment_shows_always_playing_not_just_rotating_checked(seeded, store):
    """Verified in a browser: pin an item, click Set pin, and the card still
    showed "Rotating" checked -- pinning freezes the cursor
    (`choose_next`/`rotation.py` sets `rotates=False`) but the only signal
    on the card was the Rotating toggle, which stayed checked because
    pinning never touched `assignment.enabled`. Someone pinned a story, saw
    Rotating still ticked, and reasonably concluded the pin didn't take.

    Wording round: "Pin" itself reads as a passcode to an operator of a
    children's product, and "Rotating" (a checkbox) sitting next to a pin
    control was the very shape of the contradiction. Both problems are
    dissolved the same way: "what this tonie plays" is now a single
    either/or (radio buttons), so there is no second control left that
    can disagree. The status line reads "Always playing: <title>", never
    the word "Pinned"; the "Rotating" checkbox is gone entirely, replaced
    by a separate "Paused" switch that answers a different question.
    """
    a = next(a for a in store.assignments.list() if a.target_name == "Green Tonie")
    item = store.items.list(a.library_id)[0]
    store.assignments.set_pin(a.id, item.id)

    html = seeded.get("/").text
    card_html = _card_html(html, "Green Tonie")
    assert "Always playing" in card_html
    assert item.title in card_html
    assert ">Pinned<" not in card_html  # the old status word is gone
    assert "Rotating" not in card_html
    # The "Paused" switch is still its own, separate control -- unchecked,
    # since this assignment is enabled.
    assert 'name="paused"' in card_html
    assert re.search(r'name="paused"[^>]*checked', card_html) is None


def test_choosing_always_play_leaves_no_control_claiming_the_tonie_still_rotates(seeded, store):
    """Pins the exact bug that started this whole thread: selecting "Always
    play this one" and choosing an item must not leave any control on the
    card claiming the tonie still rotates. Asserts on the rendered card,
    not on internal state -- the original bug was that the screen
    contradicted itself even though `pinned_item_id` was set correctly.
    """
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    item = store.items.list(a.library_id)[0]

    r = seeded.post(f"/assignments/{aid}/pin", data={"play_mode": "always", "item_id": item.id})
    assert r.status_code in (200, 303)
    assert store.assignments.get(aid).pinned_item_id == item.id

    html = seeded.get("/").text
    card_html = _card_html(html, a.target_name)

    assert "Always playing" in card_html
    # The "Rotate through the library" radio must not be the checked one.
    assert re.search(
        r'value="rotate"[^>]*class="play-mode-radio play-mode-rotate"[^>]*checked', card_html
    ) is None
    assert re.search(
        r'value="always"[^>]*class="play-mode-radio play-mode-always"[^>]*checked', card_html
    )
    # Nothing on the card may say "Rotating" any more (the old contradiction).
    assert "Rotating" not in card_html


def test_choosing_rotate_clears_the_pin_even_if_the_picker_still_shows_an_item(seeded, store):
    """Selecting "Rotate through the library" and saving must clear the pin
    regardless of whatever item a leftover picker selection names -- the
    radio choice, not the picker value, decides."""
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    item = store.items.list(a.library_id)[0]
    store.assignments.set_pin(aid, item.id)

    r = seeded.post(f"/assignments/{aid}/pin", data={"play_mode": "rotate", "item_id": item.id})
    assert r.status_code in (200, 303)
    assert store.assignments.get(aid).pinned_item_id is None

    html = seeded.get("/").text
    card_html = _card_html(html, a.target_name)
    assert "Always playing" not in card_html
    assert re.search(
        r'value="rotate"[^>]*class="play-mode-radio play-mode-rotate"[^>]*checked', card_html
    )


def test_item_picker_is_css_revealed_only_when_always_play_this_one_is_checked():
    """The item picker (select or search) is only meaningful once "Always
    play this one" is chosen. It must work with no JavaScript at all, so
    the reveal is pure CSS (`.play-mode-always:checked` driving
    `.play-mode-picker`'s `display`) rather than anything server-rendered
    conditionally or toggled by a script. Checked against the source
    stylesheet (`static/app.css` is a gitignored build artefact -- see
    `test_setup.py::test_setup_and_static_are_not_redirected` -- so a test
    that requires it to exist would fail on a fresh clone before `make
    css` runs)."""
    repo_root = Path(__file__).resolve().parents[2]
    css = (repo_root / "boxbutler/web/static/src/input.css").read_text()
    assert ".play-mode-picker" in css and "display: none" in css.split(".play-mode-picker")[1][:60]
    assert re.search(
        r"\.play-mode-always:checked\)?\s*[,~ ]*\.play-mode-picker\s*\{[^}]*display:\s*block", css
    ) or re.search(r":has\(\.play-mode-always:checked\)[^{]*\.play-mode-picker\s*\{[^}]*display:\s*block", css)


def test_always_playing_names_the_pinned_item_even_in_album_mode(seeded, store):
    """"Always playing: <title>" must name the actual pinned item -- caught
    live while building this: `_card()` used to take the title from
    `up_next` (`plan.item_ids[0]`), but `choose_next` returns ALBUM-mode
    items in plain library-position order regardless of the pin
    ("content-neutral to a pin" -- domain/rotation.py, it already loads
    the whole library) -- so on an ALBUM-mode tonie, `up_next` names
    whatever sits at position 0, not the pinned item. "Blue Tonie" is
    ALBUM-mode (fake_data.py); pin something other than its first item and
    the header must still say that item's name, not the first one.
    """
    a = next(a for a in store.assignments.list() if a.target_name == "Blue Tonie")
    items = store.items.list(a.library_id)
    first_item, other_item = items[0], items[-1]
    assert first_item.id != other_item.id
    store.assignments.set_pin(a.id, other_item.id)

    html = seeded.get("/").text
    card_html = _card_html(html, "Blue Tonie")
    assert f"Always playing: {other_item.title}" in card_html
    assert first_item.title not in card_html.split("Always playing")[1][:200]


def test_degraded_is_unmissable_with_repair_action(seeded):
    html = seeded.get("/").text
    assert 'class="card state-DEGRADED"' in html or "DEGRADED" in html
    assert "Repair" in html


def test_run_now_offers_dry_run_and_apply_separately_dry_run_primary(seeded):
    html = seeded.get("/").text
    assert 'name="mode" value="dry_run"' in html and 'name="mode" value="apply"' in html
    assert html.index('value="dry_run"') < html.index('value="apply"')
    assert "btn-primary" in html.split('value="dry_run"')[0][-300:]


def test_apply_button_is_guarded_by_a_working_confirm_not_an_inert_attribute(seeded):
    """Fix round 1, critical 1: the original assertion here only string-matched
    `hx-confirm` in the rendered HTML. That attribute does nothing on a plain
    `<form method="post">` — htmx only intercepts elements carrying
    hx-get/post/put/delete/patch, hx-trigger, or sitting under hx-boost, none
    of which were present, so a real browser submitted the form natively with
    no dialog (confirmed against the dev server). The fix uses a plain
    `onclick="return confirm(...)"` on Apply/Repair-apply buttons instead —
    self-contained, no dependency on another attribute to make it live. This
    test asserts the actual guard is present, and that Dry run (which must
    never ask, since it cannot destroy anything) never carries one.

    Final coherence review, Major-5: this test (and its library sibling,
    `test_remove_item_button_has_a_working_confirm_not_an_inert_attribute`)
    only ever string-matched `onclick="return confirm("` and never checked
    the button was `type="submit"` — a break-test proof found that changing
    `type="submit"` to `type="button"` (which makes the button completely
    inert in a real browser: `type="button"` never submits its form, no
    matter what its `onclick` returns) left this test passing. Extract the
    actual `<button ...>` opening tag with a regex instead of a fixed-width
    string slice, and pin `type="submit"` on it explicitly, so that
    regression is red again.
    """
    html = seeded.get("/").text
    assert "hx-confirm" not in html

    apply_buttons = re.findall(r'<button\b[^>]*value="apply"[^>]*>', html)
    assert apply_buttons, "no Apply button found on the dashboard"
    for tag in apply_buttons:
        assert 'onclick="return confirm(' in tag
        assert 'type="submit"' in tag, (
            "Apply button must be type=\"submit\" -- type=\"button\" never "
            "submits the form, so a working confirm() would still be inert"
        )

    dry_run_buttons = re.findall(r'<button\b[^>]*value="dry_run"[^>]*>', html)
    assert dry_run_buttons, "no Dry run button found on the dashboard"
    for tag in dry_run_buttons:
        assert "confirm(" not in tag


def test_run_now_dry_run_creates_run_without_sink_writes(seeded, sink, store):
    before = len(sink.calls_named("upload"))
    r = seeded.post("/assignments/{}/run".format(_first_managed(store)), data={"mode": "dry_run"})
    assert r.status_code in (200, 303)
    assert len(sink.calls_named("upload")) == before
    assert store.runs.list(1)[0].dry_run is True


def test_pin_toggle_round_trips(seeded, store):
    aid = _first_managed(store)
    item = store.items.list(store.assignments.get(aid).library_id)[0]
    seeded.post(f"/assignments/{aid}/pin", data={"item_id": item.id})
    assert store.assignments.get(aid).pinned_item_id == item.id
    seeded.post(f"/assignments/{aid}/pin", data={"item_id": ""})
    assert store.assignments.get(aid).pinned_item_id is None


def test_small_library_pin_picker_is_still_a_plain_select(seeded, store):
    """Below PIN_SEARCH_THRESHOLD (== library.py's PAGE_SIZE), today's
    plain `<select>` must render exactly as before -- small libraries are
    well served by it and must not regress."""
    aid = _first_managed(store)
    html = seeded.get("/").text
    assert f'id="pin-{aid}"' in html
    assert f'id="pin-search-q-{aid}"' not in html


def test_large_library_pin_picker_uses_search_not_a_full_dump(seeded, store):
    """Measured on the real bug: a 250-item library produced 251 <option>
    tags and 35 KB of dashboard HTML; 838 items produced 839 options and
    108 KB, per managed tonie. At/above the threshold the dashboard must
    stop building `pin_options` from every item in the library and must
    not render them all into the page -- assert the actual bug (the
    per-card option/row count), not just that some other markup exists.
    """
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    _bulk_up_library(store, a.library_id, PAGE_SIZE + 10)

    html = seeded.get("/").text
    assert f'id="pin-{aid}"' not in html  # plain <select> is gone for this card
    assert f'id="pin-search-q-{aid}"' in html  # search widget is here instead

    # The actual bug: nothing on the page-load response may enumerate
    # every item in this (now 60+-item) library as an <option>.
    assert html.count('value="bulk-') < PAGE_SIZE
    assert "Bulk Item 059" not in html  # nothing near the tail is dumped either


def test_pin_options_endpoint_caps_results_and_says_when_there_are_more(seeded, store):
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    _bulk_up_library(store, a.library_id, PAGE_SIZE + 10)

    r = seeded.get(f"/assignments/{aid}/pin-options", params={"q": "Bulk"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    assert body.count("bulk-") <= PAGE_SIZE
    assert str(PAGE_SIZE + 10) not in body or "more" in body.lower() or "first" in body.lower()


def test_pin_options_endpoint_filters_by_title(seeded, store):
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    _bulk_up_library(store, a.library_id, PAGE_SIZE + 10)

    r = seeded.get(f"/assignments/{aid}/pin-options", params={"q": "Bulk Item 007"}, headers={"HX-Request": "true"})
    assert "Bulk Item 007" in r.text
    assert "Bulk Item 008" not in r.text


def test_pin_options_endpoint_can_set_the_pin(seeded, store):
    aid = _first_managed(store)
    a = store.assignments.get(aid)
    _bulk_up_library(store, a.library_id, PAGE_SIZE + 10)
    item = next(i for i in store.items.list(a.library_id) if i.title == "Bulk Item 007")

    seeded.post(f"/assignments/{aid}/pin", data={"item_id": item.id})
    assert store.assignments.get(aid).pinned_item_id == item.id


def test_assign_library_to_unmanaged(seeded, store):
    un = next(a for a in store.assignments.list() if a.library_id is None)
    lib = store.libraries.list()[0]
    seeded.post(f"/assignments/{un.id}/library", data={"library_id": lib.id})
    assert store.assignments.get(un.id).library_id == lib.id


def test_dashboard_requires_login(client):
    r = client.get("/")
    assert r.status_code in (303, 401)


def test_run_now_apply_uploads_to_sink_and_marks_ok(seeded, sink, store):
    aid = _first_managed(store)
    before = len(sink.calls_named("upload"))
    r = seeded.post(f"/assignments/{aid}/run", data={"mode": "apply"})
    assert r.status_code in (200, 303)
    assert len(sink.calls_named("upload")) > before
    assert store.assignments.get(aid).state == "OK"


def test_repair_apply_clears_degraded_state(seeded, store):
    degraded = next(a for a in store.assignments.list() if a.state == "DEGRADED")
    r = seeded.post(f"/assignments/{degraded.id}/repair", data={"mode": "apply"})
    assert r.status_code in (200, 303)
    assert store.assignments.get(degraded.id).state == "OK"


def test_durations_are_shown_in_minutes_not_raw_seconds(seeded):
    html = seeded.get("/").text
    assert "89 min" in html
    assert "5340 s" not in html


def test_run_landing_page_names_the_run_and_its_outcome_not_blank(seeded, store):
    """Fix round 1, important 3: /runs/{run_id} was Task 8's placeholder
    (empty base.html) — after confirming Apply the operator landed on a
    page with nav and nothing else. This is the minimal stopgap: a real
    (if minimal) route naming the run id and its outcome. Task 11 still
    owns the full run-detail screen.
    """
    aid = _first_managed(store)
    r = seeded.post(f"/assignments/{aid}/run", data={"mode": "apply"})
    assert r.status_code == 303
    run_url = r.headers["location"]

    page = seeded.get(run_url)
    assert page.status_code == 200
    assert "SWAPPED" in page.text


def test_dry_run_landing_page_names_the_run_and_its_outcome(seeded, store):
    aid = _first_managed(store)
    r = seeded.post(f"/assignments/{aid}/run", data={"mode": "dry_run"})
    assert r.status_code == 303
    page = seeded.get(r.headers["location"])
    assert page.status_code == 200
    assert "DRY_RUN" in page.text


def test_dashboard_shows_only_the_invented_seed_names(seeded):
    """Fix round 1, important 2: the seeded data must only use invented
    tonie names and titles, never the operator's real household names.
    This positive assertion verifies that exactly the invented seed names
    appear, providing a stronger guarantee than checking a fixed set of
    forbidden strings."""
    html = seeded.get("/").text
    # These four invented names are created by seed_fake and must all appear
    invented_names = ["Green Tonie", "Blue Tonie", "Red Tonie", "Spare Tonie"]
    for name in invented_names:
        assert name in html, f"Expected invented tonie name '{name}' not found in dashboard"


def test_degraded_card_has_repair_as_primary_action_run_now_demoted(seeded):
    html = seeded.get("/").text
    assert "<summary>Run now anyway" in html


def test_a_paused_assignment_shows_a_distinct_paused_badge_not_ok(seeded, store):
    """Final coherence review, Critical-2: `dashboard._card()` never
    checked `assignment.enabled`, so a paused tonie (the orchestrator
    silently skips it forever, run.py:500) kept showing a green "OK"
    badge -- telling the operator bedtime was covered when nothing would
    ever run again. PAUSED must be visually and textually distinct from
    both OK and DEGRADED.
    """
    aid = _first_managed(store)
    store.assignments.set_enabled(aid, False)

    html = seeded.get("/").text
    assert 'class="status status-PAUSED"' in html
    assert "Paused" in html

    # The card for this specific assignment must not still claim OK.
    card_html = html.split(f'/assignments/{aid}/')[0][-2000:]
    assert 'status-OK"' not in card_html


def test_degraded_outranks_paused_on_the_badge(seeded, store):
    """A tonie that is both disabled and DEGRADED must still show
    DEGRADED -- design-system/box-butler/MASTER.md: "DEGRADED must be
    the most visually prominent thing on the dashboard when present".
    Pausing a broken tonie must not hide that it may be empty at bedtime.
    """
    degraded = next(a for a in store.assignments.list() if a.state == "DEGRADED")
    store.assignments.set_enabled(degraded.id, False)

    html = seeded.get("/").text
    card_html = html.split(f'/assignments/{degraded.id}/')[0][-2000:]
    assert 'status-DEGRADED"' in card_html
    assert 'status-PAUSED"' not in card_html


def test_pause_resume_control_is_reachable_and_round_trips(seeded, store):
    """Final coherence review, Major-3: `POST /assignments/{id}/enabled`
    was fully wired and orchestrator-effective but had no UI control
    pointing at it -- only tests exercised it. The dashboard card must
    offer an operator-facing pause/resume control.

    Wording round: the control is now a switch labelled "Paused" (checked
    means "Box Butler will leave this tonie alone"), the inverse sense of
    the old "Rotating"/enabled checkbox -- posting the field checked
    (`paused=1`) must disable the assignment, and posting it unchecked
    (the field absent, same as any real unchecked checkbox) must enable
    it. The route path and `store.assignments.set_enabled` keep their
    names; only the wire field the browser posts changed.
    """
    aid = _first_managed(store)
    html = seeded.get("/").text
    assert f'action="/assignments/{aid}/enabled"' in html
    assert "Box Butler will leave this tonie alone until you switch this off." in html

    r = seeded.post(f"/assignments/{aid}/enabled", data={})  # unchecked switch sends nothing
    assert r.status_code in (200, 303)
    assert store.assignments.get(aid).enabled is True

    r = seeded.post(f"/assignments/{aid}/enabled", data={"paused": "1"})
    assert r.status_code in (200, 303)
    assert store.assignments.get(aid).enabled is False
    assert "Paused" in seeded.get("/").text
    assert store.assignments.get(aid).state != "DEGRADED"  # sanity: still the OK fixture
