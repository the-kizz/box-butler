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

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from ...domain.models import LibraryMode
from ...sources.library_folder import (
    LibraryFolderError,
    ensure_library_folder,
    folder_name_for,
    resolve_within,
)
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
    store: Store = request.app.state.store
    existing = store.libraries.list()
    context = {
        "step": step,
        "error": None,
        "tonie_username": "",
        "library_name": "Bedtime",
        "folder_path": "",
        "source_ref": "",
        "skip": False,
        # "No step may be mandatory when the thing it creates is already
        # present": with a library already there, skipping is not merely
        # allowed, it is the sensible default — so the template
        # pre-selects it and says why.
        "existing_libraries": existing,
        "skip_default": bool(existing),
        "media_root": str(getattr(request.app.state, "media_root", "") or ""),
        "targets": [],
        "ticked": set(),
        "admin_username": "",
    }
    context.update(ctx)
    return templates.TemplateResponse(request, "setup.html", context)


@router.get("/setup/folders")
def setup_folders(request: Request, path: str = "", name: str = ""):
    """The folder picker, for the wizard only.

    Step 2 loads the picker over htmx, and during setup there is no admin
    account and no session — so `/libraries/folders` is doubly out of
    reach: the setup gate in `web/app.py` redirects every non-`/setup`
    path, and `require_login` would refuse it anyway. The picker silently
    never appeared, which is exactly the step an operator got stuck on.

    This is the same partial from the same builder (no second
    implementation, so confinement to the media root cannot drift between
    them). It is open without a login for precisely as long as the wizard
    itself is: once an admin account exists it stops answering, so a
    configured install never exposes an unauthenticated route that lists
    directory names.
    """
    store: Store = request.app.state.store
    if _configured(store):
        return RedirectResponse(url="/", status_code=303)
    # Imported here rather than at module scope: routes/library.py imports
    # nothing from this module, and keeping it that way means the wizard
    # depends on the library screens and never the reverse.
    from .library import render_folder_picker

    return render_folder_picker(request, path, name, browse_url="/setup/folders")


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
            folder_path=form.get("folder_path", ""),
            source_ref=form.get("source_ref", ""),
            skip=form.get("skip") in ("1", "true", "on"),
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
    """Watch a folder / paste a link / skip.

    This step used to demand *both* a library name and a link while its
    own help text promised a folder option "later" — an operator with a
    library that already existed could not get past it. Nothing here is
    mandatory now except a name for a library actually being created, and
    skipping is always available.
    """
    tonie_username = form.get("tonie_username", "")
    library_name = (form.get("library_name") or "").strip()
    folder_path = (form.get("folder_path") or "").strip()
    source_ref = (form.get("source_ref") or "").strip()
    skip = form.get("action") == "skip" or form.get("skip") in ("1", "true", "on")
    # Round-trip carry (Important 2, review round 1): step 2's own form
    # carries these two along as hidden fields (setup.html) whenever step
    # 3 has already been filled in and the visitor came Back to fix
    # something here — Continue must return to step 3 with the ticks and
    # admin username still in place, not reset to empty. Never the admin
    # password: that never enters step 2's markup in the first place.
    ticked = set(form.getlist("target_ids"))
    admin_username = form.get("admin_username", "")

    error = None
    if not skip:
        if not library_name:
            error = "Name the library, or choose Skip — you can add one later."
        else:
            try:
                _folder_for(request, library_name, folder_path, create=False)
            except LibraryFolderError as exc:
                error = f"Couldn't use that folder: {exc}"

    if error is not None:
        return _render(
            request,
            step=2,
            tonie_username=tonie_username,
            library_name=library_name or "Bedtime",
            folder_path=folder_path,
            source_ref=source_ref,
            ticked=ticked,
            admin_username=admin_username,
            error=error,
        )

    # discovers-without-writing: list_targets() only, never clear/upload.
    targets = sink.list_targets()
    return _render(
        request,
        step=3,
        tonie_username=tonie_username,
        library_name=library_name,
        folder_path=folder_path,
        source_ref=source_ref,
        skip=skip,
        targets=targets,
        ticked=ticked,
        admin_username=admin_username,
    )


def _folder_for(request: Request, library_name: str, folder_path: str, *, create: bool):
    """Where this library's folder is, validating before step 3 rather
    than discovering a bad path at the very end of the wizard.

    A picked `folder_path` is relative to the media root and goes through
    `resolve_within`, the one place a caller-supplied path becomes a real
    one. An empty value means a new folder named after the library — the
    picker browses *and* creates, so a new operator never has to ssh in
    and `mkdir`.
    """
    media_root = getattr(request.app.state, "media_root", None)
    if media_root is None:
        raise LibraryFolderError(
            "no media root is mounted, so there is nowhere for a library to live"
        )
    if folder_path:
        folder = resolve_within(media_root, folder_path)
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder
    if create:
        return ensure_library_folder(media_root, library_name)
    return Path(media_root) / folder_name_for(library_name)


def _handle_step3(request: Request, form, sink, store: Store):
    tonie_username = form.get("tonie_username", "")
    library_name = (form.get("library_name") or "Bedtime").strip()
    folder_path = (form.get("folder_path") or "").strip()
    source_ref = (form.get("source_ref") or "").strip()
    skip = form.get("skip") in ("1", "true", "on")
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
            folder_path=folder_path,
            source_ref=source_ref,
            skip=skip,
            targets=targets,
            ticked=ticked,
            admin_username=admin_username,
            error=" ".join(errors),
        )

    _finish(
        store, request, sink, library_name, folder_path, source_ref, skip,
        targets, ticked, admin_username, admin_password,
    )
    return RedirectResponse(url="/", status_code=303)


def _finish(
    store: Store,
    request: Request,
    sink,
    library_name: str,
    folder_path: str,
    source_ref: str,
    skip: bool,
    targets: list,
    ticked: set[str],
    admin_username: str,
    admin_password: str,
) -> None:
    """Write the library, the item, the assignments and the admin account
    — the only place in the wizard that touches the store.

    `skip` means the operator chose not to create a library here, and that
    is honoured literally: no library is invented for them, and a ticked
    tonie gets its assignment row without one. Guessing that they "must
    have meant" some existing library would be inventing an intent, and a
    tonie that rotates something nobody chose is precisely the failure
    this wizard was rewritten to avoid.
    """
    library = None
    if not skip:
        folder = _folder_for(request, library_name, folder_path, create=True)
        library = store.libraries.create(
            name=library_name, mode=LibraryMode.SINGLE, folder_path=str(folder)
        )
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
            if library is not None:
                store.assignments.assign_library(assignment.id, library.id)
        # else: never a rotating tonie by default — no assignment row is
        # created here at all for a target nobody explicitly ticked.

    store.settings.set("admin_user", admin_username)
    store.settings.set("admin_hash", hash_password(admin_password))
