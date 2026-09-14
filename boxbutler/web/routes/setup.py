"""First-run setup wizard (Task 36; spec §8.1, §5).

A container started against an empty `/data` has no admin account yet —
`boxbutler.web.app.create_app`'s setup-gate middleware sends every route
except `/setup` and static assets here until one exists. Three steps, no
config file: sign in to the tonie account (verified against the sink,
never `tonie_api`, an HTTP client or ffmpeg) -> add one source to a new
library -> tick which discovered tonies it feeds. Only on a successful
final step are the library, the item, the assignment(s) and the admin
account written — nothing before that touches the store.

**State between steps never carries a secret.** The tonie password is
checked once, in step 1, and never carried forward — Phase 2's `FakeSink`
needs it only for that one `verify_login` call. The admin password is
collected in step 3 and hashed immediately; it is never echoed back into
a `value=` or a hidden field (design-system/box-butler/MASTER.md:
"Secrets are never rendered back to the page"). Everything that *does*
cross a step boundary (tonie username for display, library name, source
ref, which target ids are ticked) is not a secret, so it travels in plain
hidden form fields rather than a cookie/session — there is nothing here
worth signing.

**Never a rotating tonie by default (the reason this task exists):** an
unticked target gets no assignment row at all from this module. The
dashboard's own `_sync_targets` will still upsert an Unmanaged
(`library_id=None`) row for it the first time anyone loads `/`, exactly
like any other new tonie noticed on the sink — but nothing here ever
calls `assign_library` for a target that wasn't explicitly ticked, so it
can never rotate.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from ...domain.models import LibraryMode
from ...store.db import Store
from ..auth import hash_password

router = APIRouter()

MIN_ADMIN_PASSWORD_LEN = 8


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _configured(store: Store) -> bool:
    return store.settings.get("admin_user") is not None


def _render(request: Request, step: int, **ctx):
    templates = _templates(request)
    context = {
        "step": step,
        "error": None,
        "tonie_username": "",
        "library_name": "Bedtime",
        "source_ref": "",
        "targets": [],
        "ticked": set(),
        "admin_username": "",
    }
    context.update(ctx)
    return templates.TemplateResponse(request, "setup.html", context)


@router.get("/setup")
def setup_start(request: Request):
    store: Store = request.app.state.store
    if _configured(store):
        return RedirectResponse(url="/", status_code=303)
    return _render(request, step=1)


@router.post("/setup")
async def setup_submit(request: Request):
    store: Store = request.app.state.store
    if _configured(store):
        # test_wizard_unreachable_once_configured / idempotency: once an
        # admin account exists this route does nothing further, ever —
        # not even re-parse the form — so a retried/duplicated final
        # POST can never create a second library or assignment.
        return RedirectResponse(url="/", status_code=303)

    sink = request.app.state.sink
    form = await request.form()
    step = int(form.get("step", "1") or "1")
    action = form.get("action", "next")

    if action == "back":
        if step <= 1:
            return _render(request, step=1)
        # Important 2 (review round 1): going back from step 3 must not
        # drop the ticks or the admin username — only step 3's own form
        # has them, so they're only ever read here when backing off of
        # step 3. The admin *password* is deliberately never read back
        # out of the form at all; it never appears in step 2's markup
        # either (see _handle_step2 / setup.html), so there is nothing to
        # lose there in the sense that matters — it was never written.
        return _render(
            request,
            step=step - 1,
            tonie_username=form.get("tonie_username", ""),
            library_name=form.get("library_name", "Bedtime"),
            source_ref=form.get("source_ref", ""),
            ticked=set(form.getlist("target_ids")) if step == 3 else set(),
            admin_username=(form.get("admin_username") or "") if step == 3 else "",
        )

    if step == 1:
        return _handle_step1(request, form, sink)
    if step == 2:
        return _handle_step2(request, form, sink)
    if step == 3:
        return _handle_step3(request, form, sink, store)
    return _render(request, step=1)


def _handle_step1(request: Request, form, sink):
    tonie_username = (form.get("tonie_username") or "").strip()
    tonie_password = form.get("tonie_password") or ""

    # accessible-authentication / login.md: vague about *which* half was
    # wrong, same as the real login screen — this isn't the operator's
    # own account, but the principle (don't help an attacker enumerate)
    # still costs nothing to keep.
    if not sink.verify_login(tonie_username, tonie_password):
        return _render(
            request,
            step=1,
            tonie_username=tonie_username,
            error="Couldn't sign in — check the tonie account email and password.",
        )
    return _render(request, step=2, tonie_username=tonie_username)


def _handle_step2(request: Request, form, sink):
    tonie_username = form.get("tonie_username", "")
    library_name = (form.get("library_name") or "").strip()
    source_ref = (form.get("source_ref") or "").strip()
    # Round-trip carry (Important 2, review round 1): step 2's own form
    # carries these two along as hidden fields (setup.html) whenever step
    # 3 has already been filled in and the visitor came Back to fix
    # something here — Continue must return to step 3 with the ticks and
    # admin username still in place, not reset to empty. Never the admin
    # password: that never enters step 2's markup in the first place.
    ticked = set(form.getlist("target_ids"))
    admin_username = form.get("admin_username", "")

    if not library_name or not source_ref:
        return _render(
            request,
            step=2,
            tonie_username=tonie_username,
            library_name=library_name or "Bedtime",
            source_ref=source_ref,
            ticked=ticked,
            admin_username=admin_username,
            error="Enter a library name and one source (a link or feed) to continue.",
        )

    # discovers-without-writing: list_targets() only, never clear/upload.
    targets = sink.list_targets()
    return _render(
        request,
        step=3,
        tonie_username=tonie_username,
        library_name=library_name,
        source_ref=source_ref,
        targets=targets,
        ticked=ticked,
        admin_username=admin_username,
    )


def _handle_step3(request: Request, form, sink, store: Store):
    tonie_username = form.get("tonie_username", "")
    library_name = (form.get("library_name") or "Bedtime").strip()
    source_ref = (form.get("source_ref") or "").strip()
    admin_username = (form.get("admin_username") or "").strip()
    admin_password = form.get("admin_password") or ""
    admin_password_confirm = form.get("admin_password_confirm") or ""
    ticked = set(form.getlist("target_ids"))

    # Re-list rather than trust anything client-supplied about *which*
    # targets exist — only their ids, ticked or not, come from the form.
    targets = sink.list_targets()

    errors = []
    if not admin_username:
        errors.append("Choose an admin username.")
    if len(admin_password) < MIN_ADMIN_PASSWORD_LEN:
        errors.append(f"Admin password must be at least {MIN_ADMIN_PASSWORD_LEN} characters.")
    elif admin_password != admin_password_confirm:
        errors.append("Admin password and confirmation must match.")

    if errors:
        return _render(
            request,
            step=3,
            tonie_username=tonie_username,
            library_name=library_name,
            source_ref=source_ref,
            targets=targets,
            ticked=ticked,
            admin_username=admin_username,
            error=" ".join(errors),
        )

    _finish(store, request, sink, library_name, source_ref, targets, ticked, admin_username, admin_password)
    return RedirectResponse(url="/", status_code=303)


def _finish(
    store: Store,
    request: Request,
    sink,
    library_name: str,
    source_ref: str,
    targets: list,
    ticked: set[str],
    admin_username: str,
    admin_password: str,
) -> None:
    library = store.libraries.create(name=library_name, mode=LibraryMode.SINGLE)
    if source_ref:
        ingest = request.app.state.ingest
        ingest(library.id, source_ref)

    # Keyed under `sink.name`, the live sink's own identity and the single
    # source of truth for it (`SinkProtocol.name`). This used to be a
    # constant imported from `web/fake_data.py` (`"fake"`): on a real
    # install the wizard therefore wrote the operator's one and only
    # assignment under a sink name no run ever looks up, so finishing the
    # wizard produced a tonie that was never loaded and never complained
    # (final safety review, C1).
    for target in targets:
        if target.id in ticked:
            assignment = store.assignments.upsert_target(sink.name, target.id, target.name)
            store.assignments.assign_library(assignment.id, library.id)
        # else: never a rotating tonie by default — no assignment row is
        # created here at all for a target nobody explicitly ticked.

    store.settings.set("admin_user", admin_username)
    store.settings.set("admin_hash", hash_password(admin_password))
