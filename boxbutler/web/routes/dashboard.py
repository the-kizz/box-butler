"""Dashboard screen — the first screen anyone sees (Task 9; spec §5 screen 1).

Every card comes from `store.assignments.list()`, joined with
`sink.list_targets()` via `assignments.upsert_target` on every GET / — that
upsert is what makes "a new tonie appears with no configuration" true
(spec §5): the first time a tonie shows up on the sink, this route creates
its assignment row (library_id=None, i.e. Unmanaged) without anyone having
to do anything.

DEGRADED is the most prominent thing on this screen when present (a tonie
in that state may be empty at bedtime); Unmanaged, Paused and OK are the
other explicit states. PAUSED means `assignment.enabled` is False — the
orchestrator skips it on every run (run.py:500) — and is distinct from
both OK ("being kept fresh") and DEGRADED ("may be empty right now"): a
paused tonie isn't broken, it's just not being rotated (final coherence
review, Critical-2). `Dry run` is always the primary button, `Apply`
always confirms — that ordering is a safety property (spec §10.17), not
a style choice, so don't reorder the buttons in the template without
re-reading design-system/box-butler/pages/dashboard.md.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ...domain.models import AssignmentMode, AssignmentState, RunTrigger
from ...domain.rotation import PlanInput, choose_next, pin_active
from ...store.db import Store
from ..auth import require_login
from .library import PAGE_SIZE as PIN_SEARCH_THRESHOLD

router = APIRouter()

STALE_AFTER_HOURS = 24


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _sync_targets(store: Store, sink) -> None:
    """Upsert an assignment row for every target the sink currently
    reports. A target with no matching assignment yet is a brand-new tonie
    — it appears Unmanaged (library_id=None) until someone assigns it a
    library; nothing here ever touches it beyond recording that it exists.

    The row is keyed under `sink.name` — the live sink's own identity, the
    single source of truth (`SinkProtocol.name`). It used to be a constant
    imported from `web/fake_data.py` (`"fake"`), which on a real install
    did not match the name the orchestrator looks assignments up under, so
    the row this route created was never the row a run found (final safety
    review, C1).
    """
    for target in sink.list_targets():
        store.assignments.upsert_target(sink.name, target.id, target.name)


def _minutes(seconds: float) -> int:
    return int(round(seconds / 60))


def _card(store: Store, sink, a) -> dict:
    library = store.libraries.get(a.library_id) if a.library_id else None
    items = store.items.list(a.library_id) if library else []

    if a.state == AssignmentState.DEGRADED:
        # DEGRADED outranks everything else (design-system/box-butler/
        # MASTER.md: "DEGRADED must be the most visually prominent thing
        # on the dashboard when present"): a tonie that may be empty at
        # bedtime needs repair regardless of whether it is also paused or
        # unmanaged, so the operator isn't misled into thinking pausing
        # (or unassigning) it fixed anything.
        status = "DEGRADED"
    elif not a.enabled:
        # Final coherence review, Critical-2: the orchestrator silently
        # skips a disabled assignment forever (run.py:500), so reporting
        # "OK" here was false in the way that matters -- it told the
        # operator bedtime was covered when nothing would ever run again
        # until someone re-enabled it. PAUSED is a distinct third state
        # (not OK, not DEGRADED): nothing is wrong with the tonie itself,
        # but it is deliberately not being kept fresh.
        status = "PAUSED"
    elif library is None:
        # An assignment row with no library (e.g. one `library import`
        # created via Task 33's mode-reconciliation fix) is exactly the
        # unmanaged case the orchestrator itself reports as `unmanaged`
        # for the same assignment -- showing "OK" here would be worse
        # than showing nothing at all, since it claims a tonie is fine
        # when nothing will ever run against it.
        status = "UNMANAGED"
    elif pin_active(a, items):
        # Verified in a browser: pin an item, click Set pin, and the card
        # kept showing "Rotating (checked)" as the only signal -- pinning
        # freezes the cursor (`rotation.choose_next` sets `rotates=False`)
        # but never touches `assignment.enabled`, so nothing on the card
        # changed. A pin is a deliberate operator choice, not a health
        # problem -- it does not outrank DEGRADED/PAUSED/UNMANAGED, all of
        # which mean "this needs attention" in a way a pin doesn't -- but
        # it is also not nothing, so it must not collapse into the same
        # "OK" as an unpinned, freely-rotating tonie. `pin_active` is the
        # exact same test `choose_next` itself uses to decide
        # reason="PINNED"/rotates=False (`domain/rotation.py`), imported
        # rather than re-derived here so this can never drift out of
        # agreement with what the orchestrator would actually do -- and
        # the CLI's `status` command (`cli/main.py::_cmd_status`) uses the
        # same function at the same point in the same precedence chain, so
        # the two channels can't disagree about the same tonie (final
        # coherence review: they already have, twice).
        status = "PINNED"
    else:
        status = "OK"

    chapters = store.chapters.for_assignment(a.id)
    total_seconds = sum(c.seconds for c in chapters)

    up_next = None
    plan_reason = None
    if library is not None:
        plan = choose_next(
            PlanInput(
                assignment=a,
                library_mode=library.mode,
                items=items,
                loaded_elsewhere=frozenset(store.chapters.loaded_item_ids_except(a.id)),
            )
        )
        plan_reason = plan.reason
        if plan.item_ids:
            up_next = store.items.get(plan.item_ids[0])

    stale = False
    if a.last_success_at is not None:
        stale = (datetime.now(UTC) - a.last_success_at) > timedelta(hours=STALE_AFTER_HOURS)
    elif status != "UNMANAGED":
        stale = True

    return {
        "assignment": a,
        "library": library,
        "items": items,
        "status": status,
        "chapters": chapters,
        "chapter_titles": [c.title for c in chapters],
        "total_minutes": _minutes(total_seconds) if chapters else None,
        "up_next": up_next,
        "plan_reason": plan_reason,
        "stale": stale,
        # Below PIN_SEARCH_THRESHOLD (== library.py's PAGE_SIZE, imported
        # rather than duplicated), today's plain <select> still gets every
        # item, exactly as before -- small libraries are well served by it.
        # At/above the threshold this is `None`, and tonie_card.html
        # renders the htmx search widget instead: measured on the real
        # bug, a 250-item library produced 251 <option> tags and 35 KB of
        # dashboard HTML, 838 items 839 options and 108 KB, *per managed
        # tonie* -- so at threshold the page must stop enumerating every
        # item here at all, not just stop rendering them.
        "pin_options": items if len(items) < PIN_SEARCH_THRESHOLD else None,
        "pin_search": len(items) >= PIN_SEARCH_THRESHOLD,
        "libraries": store.libraries.list(),
    }


@router.get("/")
def dashboard(request: Request, announce: str | None = None, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    sink = request.app.state.sink
    templates = _templates(request)

    _sync_targets(store, sink)
    assignments = store.assignments.list()
    cards = [_card(store, sink, a) for a in assignments]
    degraded_count = sum(1 for c in cards if c["status"] == "DEGRADED")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "cards": cards,
            "degraded_count": degraded_count,
            "announce": announce,
        },
    )


def _redirect_to_dashboard(announce: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?announce={quote(announce)}", status_code=303)


@router.post("/assignments/{assignment_id}/run")
def run_assignment(
    request: Request,
    assignment_id: str,
    mode: str = Form(...),
    user: str = Depends(require_login),
):
    runner = request.app.state.runner
    run_id = runner.run(assignment_ids=[assignment_id], apply=(mode == "apply"), trigger=RunTrigger.MANUAL)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.post("/assignments/{assignment_id}/repair")
def repair_assignment(
    request: Request,
    assignment_id: str,
    mode: str = Form(...),
    user: str = Depends(require_login),
):
    runner = request.app.state.runner
    run_id = runner.repair(assignment_id, apply=(mode == "apply"))
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.post("/assignments/{assignment_id}/pin")
def pin_assignment(
    request: Request,
    assignment_id: str,
    item_id: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    store.assignments.set_pin(assignment_id, item_id or None)
    a = store.assignments.get(assignment_id)
    phrase = f"{a.target_name}: pin cleared" if not item_id else f"{a.target_name}: pinned"
    return _redirect_to_dashboard(phrase)


@router.get("/assignments/{assignment_id}/pin-options")
def pin_options(
    request: Request,
    assignment_id: str,
    q: str = "",
    user: str = Depends(require_login),
):
    """The pin picker's search endpoint (approved design: htmx active
    search, not a `<datalist>` -- selecting a datalist option refires the
    triggering input event and double-submits -- and not a hand-rolled
    ARIA combobox, too much bespoke JS for one control in a project that
    otherwise ships almost none).

    Always returns at most `PIN_SEARCH_THRESHOLD` matches, the same cap
    the library page itself paginates at, regardless of how many items
    actually match -- an 838-episode feed searched for a common word must
    still return a small page, not another 839-option dump by another
    name. Two response shapes from one route: an htmx request (the
    `HX-Request` header htmx sets on every request it issues) gets just
    the results fragment to swap in place; anything else -- the search
    `<form>`'s own `method="get" action=...`, i.e. a plain submit with
    JavaScript disabled -- gets a full, working page built from the same
    fragment, so the feature keeps working with no JS at all.
    """
    store: Store = request.app.state.store
    templates = _templates(request)
    a = store.assignments.get(assignment_id)
    if a is None or a.library_id is None:
        return Response(status_code=404)
    library = store.libraries.get(a.library_id)
    items = store.items.list(a.library_id)

    needle = q.strip().lower()
    matches = [it for it in items if needle in it.title.lower()] if needle else items
    total_matches = len(matches)
    results = matches[:PIN_SEARCH_THRESHOLD]
    truncated = total_matches > len(results)

    ctx = {
        "assignment": a,
        "library": library,
        "q": q,
        "results": results,
        "total_matches": total_matches,
        "truncated": truncated,
        "cap": PIN_SEARCH_THRESHOLD,
    }

    is_htmx = request.headers.get("hx-request", "").lower() == "true"
    template_name = "partials/pin_options_results.html" if is_htmx else "pin_options.html"
    return templates.TemplateResponse(request, template_name, ctx)


@router.post("/assignments/{assignment_id}/library")
def assign_library(
    request: Request,
    assignment_id: str,
    library_id: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    store.assignments.assign_library(assignment_id, library_id or None)
    a = store.assignments.get(assignment_id)
    library = store.libraries.get(library_id) if library_id else None
    phrase = f"{a.target_name}: assigned to {library.name}" if library else f"{a.target_name}: unassigned"
    return _redirect_to_dashboard(phrase)


@router.post("/assignments/{assignment_id}/mode")
def set_mode(
    request: Request,
    assignment_id: str,
    mode: str = Form(...),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    store.assignments.set_mode(assignment_id, AssignmentMode(mode))
    a = store.assignments.get(assignment_id)
    return _redirect_to_dashboard(f"{a.target_name}: mode set to {mode}")


@router.post("/assignments/{assignment_id}/enabled")
def set_enabled(
    request: Request,
    assignment_id: str,
    enabled: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    is_enabled = enabled in ("1", "true", "on")
    store.assignments.set_enabled(assignment_id, is_enabled)
    a = store.assignments.get(assignment_id)
    phrase = f"{a.target_name}: enabled" if is_enabled else f"{a.target_name}: disabled"
    return _redirect_to_dashboard(phrase)
