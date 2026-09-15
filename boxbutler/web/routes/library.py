"""Library screen — ordered items, reorder by buttons and drag, add
sources, mode, loudnorm, delete (Task 10; spec §5 screen 2, §4.1, §10.17).

**Order is meaning on this screen.** `serial` mode plays a library's items
in this order across nights, so reordering is a content decision, not a
cosmetic one (design-system/box-butler/pages/library.md). Every reorder
mechanism the page offers — the Move up / Move down buttons beside every
drag handle, and SortableJS drag for a mouse — ends up at the same route,
`ItemRepo.reorder`/`ItemRepo.move`, so there is exactly one source of
truth for "what order is this library in".

**dragging-alternative (WCAG 2.2 AA, High)** is the single most important
rule on this screen: dragging must never be the *only* way to reorder.
`item_row.html` always renders Move up / Move down buttons regardless of
input device; the drag handle is decorative markup that SortableJS wires
up client-side only when `matchMedia("(pointer:fine)")` matches (see the
inline script in `library.html`), so a touch/phone visitor gets buttons
only — the mechanism proven to actually work there — with no broken drag
layer competing for the same taps.

**Ingest hook shape — a deliberate deviation from this task's own brief,
corrected once against the plan's actual wiring (Task 10 review, Important
4).** The brief describes `app.state.ingest` as a single
`Callable[[library_id, kind, ref | UploadFile], list[Item]]`. This task's
own plan (Task 29, "Phase 3 — ingest, audio, verify") instead sketches the
real consumer as `Ingestor.add_ref(library_id, ref) -> list[Item]` and a
separate `Ingestor.add_upload(library_id, filename, stream) -> Item` — no
`kind` parameter, because *which* source resolves a ref (and thus its
kind) is decided internally by whichever `SourceProtocol` matches it. So
this file uses two hooks, not one, matching that real shape.

Naming: `app.state.ingest` here is the ref path, named to match the
plan's own composition-root line for Task 29 verbatim
(`app.state.ingest = Ingestor(...).add_ref`, plan ~line 3489) — so that
one assignment needs no renaming. `app.state.ingest_upload` is an
**addition** beyond what that line shows; the plan's one-line sketch of
`create_web_app` doesn't mention wiring uploads at all, so Task 29 will
still need to add `app.state.ingest_upload = ingestor.add_upload` itself
— there is real, if small, friction here, not zero, and this comment
exists so that isn't discovered by surprise. The Phase 2 defaults
(`fake_ingest` / `fake_ingest_upload` in `boxbutler.web.fake_data`) never
touch the network or the filesystem in any real sense — a ref just
becomes one fake item keyed by `sha1(ref)[:8]`, exactly as this task's
brief specifies.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ...domain.fitting import DEFAULT_CAP_SECONDS, clamp_cap
from ...domain.models import Library, LibraryMode
from ...sources.library_folder import (
    LibraryFolderError,
    ensure_library_folder,
    folder_name_for,
    list_subfolders,
    resolve_within,
)
from ...store.db import Store
from ..auth import require_login

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_SIZE = 50


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _redirect(
    library_id: str,
    announce: str | None = None,
    focus_item_id: str | None = None,
    focus_dir: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    url = f"/libraries/{library_id}"
    if announce:
        url += f"?announce={quote(announce)}"
    if focus_item_id:
        # Important 3 (Task 10 review): a plain 303 -> GET redirect resets
        # focus to the top of the document, so moving an item three times
        # means re-tabbing past the nav, mode form, add form and every
        # preceding row each time — "conformant yet unusable" keyboard
        # operation. The fragment names the moved item; library.html's
        # inline script focuses that row's own move button on load so a
        # keyboard user can immediately press the same key again.
        #
        # Review round 2: focusing merely "a" move button in the row isn't
        # enough — item_row.html renders Move up before Move down in DOM
        # order, so a naive "first enabled .move-btn" selector always
        # landed on Move up, costing an extra Tab on every Move-down
        # sequence (the exact case this was meant to fix). `focus_dir`
        # carries which button was actually pressed so the script can
        # target that same one.
        frag = f"item-{focus_item_id}"
        if focus_dir:
            frag += f":{focus_dir}"
        url += f"#{frag}"
    return RedirectResponse(url=url, status_code=status_code)


@router.get("/libraries")
def libraries_index(request: Request, announce: str | None = None, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    templates = _templates(request)
    libraries = store.libraries.list()
    counts = {lib.id: len(store.items.list(lib.id)) for lib in libraries}
    return templates.TemplateResponse(
        request,
        "libraries.html",
        {"libraries": libraries, "counts": counts, "announce": announce},
    )


@router.post("/libraries")
def create_library(
    request: Request,
    name: str = Form(...),
    mode: str = Form(str(LibraryMode.SINGLE)),
    folder_path: str = Form(""),
    user: str = Depends(require_login),
):
    """A library is exactly one folder, so this route always produces one.

    `folder_path` is a path *relative to the media root*, picked with the
    folder picker below — never an absolute path, and never joined by
    hand here: `resolve_within` is the single place a caller-supplied
    path becomes a real one, and it refuses anything that would land
    outside the root. An empty value means "a new folder named after the
    library", which is what a first-time operator gets without having to
    ssh in and `mkdir` (the picker browses *and* creates, Sonarr-style).
    """
    store: Store = request.app.state.store
    media_root = _media_root(request)
    if media_root is None:
        return Response(status_code=503)
    try:
        if folder_path.strip():
            folder = resolve_within(media_root, folder_path)
            folder.mkdir(parents=True, exist_ok=True)
        else:
            folder = ensure_library_folder(media_root, name)
    except (LibraryFolderError, OSError) as exc:
        logger.warning("refused library folder %r: %s", folder_path, exc)
        return _redirect_to_index(f"Couldn't use that folder: {exc}")
    lib = store.libraries.create(name=name, mode=LibraryMode(mode), folder_path=str(folder))
    return RedirectResponse(url=f"/libraries/{lib.id}", status_code=303)


def _media_root(request: Request) -> Path | None:
    return getattr(request.app.state, "media_root", None)


def _redirect_to_index(announce: str) -> RedirectResponse:
    return RedirectResponse(url=f"/libraries?announce={quote(announce)}", status_code=303)


def render_folder_picker(
    request: Request, path: str, name: str, *, browse_url: str = "/libraries/folders"
) -> Response:
    """The folder picker partial for `path`, or the refusal that replaces
    it.

    Shared with the setup wizard, which needs the identical picker before
    an admin account exists and therefore cannot reach `/libraries/*` at
    all — the setup gate redirects it and `require_login` would refuse it
    (`boxbutler/web/routes/setup.py::setup_folders`). Both callers get
    their confinement from `resolve_within` in here, so there is no second
    implementation to keep in step.

    Confined to the media root by `resolve_within`, which is what stops
    `..`, an absolute path and a symlink pointing elsewhere alike (see
    `tests/web/test_folder_picker.py`). A refusal is a 400 naming nothing
    about the host beyond the root itself — there is no version of this
    route that browses above it.
    """
    templates = _templates(request)
    media_root = _media_root(request)
    if media_root is None:
        return Response(status_code=503)
    try:
        current = resolve_within(media_root, path)
        folders = list_subfolders(media_root, path)
    except LibraryFolderError as exc:
        return Response(status_code=400, content=str(exc), media_type="text/plain")

    root = Path(media_root).resolve()
    rel = "" if current == root else str(current.relative_to(root))
    parent = "" if rel == "" else str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
    suggested = ""
    if name.strip():
        suggested = folder_name_for(name)
    return templates.TemplateResponse(
        request,
        "partials/folder_picker.html",
        {
            "media_root": str(root),
            "current": rel,
            "at_root": rel == "",
            "parent": parent,
            "folders": [(p.name, str(p.relative_to(root))) for p in folders],
            "suggested": suggested,
            "suggested_exists": bool(suggested) and (root / (f"{rel}/{suggested}" if rel else suggested)).is_dir(),
            # The wizard's picker has to post back to /setup, not to the
            # library routes it cannot reach yet.
            "browse_url": browse_url,
        },
    )


@router.get("/libraries/folders")
def browse_folders(
    request: Request,
    path: str = "",
    name: str = "",
    user: str = Depends(require_login),
):
    """The folder picker for a logged-in operator on the library screens."""
    return render_folder_picker(request, path, name)


@router.get("/libraries/{library_id}")
def library_detail(
    request: Request,
    library_id: str,
    page: int = 1,
    announce: str | None = None,
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    templates = _templates(request)
    library = store.libraries.get(library_id)
    if library is None:
        return Response(status_code=404)

    _scan_on_open(request, library)

    all_items = store.items.list(library_id)
    total = len(all_items)
    # virtualize-lists (design-system/box-butler/pages/library.md): a
    # library over ~50 items is paginated server-side rather than dumped
    # into the DOM (and, for drag, into SortableJS) all at once.
    page = max(1, page)
    start = (page - 1) * PAGE_SIZE
    items = all_items[start : start + PAGE_SIZE]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # `-t cap -c copy` (audio/ffmpeg.py) keeps only an item's first
    # `cap_seconds` on the tonie and silently discards the rest -- a
    # folder-backed library's items now carry a real probed `seconds`
    # (boxbutler/sources/folder.py), so an item past the cap can and
    # should say so on its row (item_row.html) rather than reading as a
    # plain "N min" that implies the whole thing plays. `cap_seconds` is
    # read the same way settings.py's own render does -- the configured
    # value clamped to the sink's real `maxSeconds`, never hard-coded --
    # so the warning always matches what a run would actually do.
    sink = request.app.state.sink
    cap_seconds = clamp_cap(store.settings.get("cap_seconds", DEFAULT_CAP_SECONDS), sink.limits.max_seconds)

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "library": library,
            "items": items,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "modes": list(LibraryMode),
            "announce": announce,
            "cap_seconds": cap_seconds,
        },
    )


def _scan_on_open(request: Request, library: Library) -> None:
    """Scan this library's folder before rendering it.

    The gap this closes is the one the operator hit: copy a file into the
    folder, open the library page, and it isn't listed until you press
    Scan. `sync_media_root` already runs inside the run lock, so the
    library is correct whenever it is actually *used* — this makes it
    correct whenever it is *looked at* too.

    Deliberately **not** a filesystem watcher. inotify does not work over
    NFS or SMB (Plex documents this; its own automatic updates don't fire
    on network shares, which is why third-party watchdogs exist for it),
    and this operator's media may well be on an NFS mount. A watcher is
    therefore the feature most likely to look like it works while
    silently doing nothing — the exact failure shape this codebase spends
    its life removing. Scanning on open works on every filesystem and
    cannot fail quietly.

    A scan is a read: nothing is written to the folder (see
    `boxbutler/sources/folder.py`, and
    `test_opening_a_library_page_writes_nothing_to_the_folder`). And it
    must never stop the page rendering — an unreadable or unmounted
    folder is already handled downstream as "mark the items unavailable,
    keep every row", and anything else that goes wrong here is logged and
    swallowed, exactly as `OrchestratorRunner._sync_media_root` does
    before a run.
    """
    if not library.folder_path:
        return
    try:
        request.app.state.scan_folder(library)
    except Exception:
        logger.exception(
            "scan of %s on opening the library page failed; showing the "
            "library as last recorded", library.folder_path,
        )


@router.post("/libraries/{library_id}/mode")
def set_library_mode(
    request: Request,
    library_id: str,
    mode: str = Form(...),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    library = store.libraries.get(library_id)
    if library is None:
        return Response(status_code=404)
    store.libraries.update(replace(library, mode=LibraryMode(mode)))
    return _redirect(library_id, f"{library.name}: mode set to {mode}")


@router.post("/libraries/{library_id}/reorder")
def reorder_items(
    request: Request,
    library_id: str,
    order: str = Form(""),
    page: int = Form(1),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    ordered_ids = [i for i in order.split(",") if i]
    if ordered_ids:
        # Critical 2 (Task 10 review): SortableJS is wired to the current
        # page's <ol> only, so `order` names just that page's ids.
        # Writing them at position 0 would collide with an earlier page's
        # real positions — library.html sends `page` alongside `order` so
        # this page's ids are written back into their own contiguous
        # position range, not the library's.
        #
        # Review round 2, Critical 2 reopened: `page` is client-supplied
        # (a stale form resubmitted after the library shrank below 50
        # items, or a crafted request) and was previously clamped only
        # from below. Confirmed: an unbounded `page=99` on a 6-item
        # library pushed positions to 4900+, breaking the dense-position
        # invariant. Two layers now: clamp `page` here to the library's
        # actual last valid page (friendlier than a 400 for the common
        # stale-form case — the request still does something sensible,
        # it reorders the last real page instead of erroring); and
        # `ItemRepo.reorder` itself now self-heals positions back to
        # dense `0..n-1` regardless, so even a wrong clamp here couldn't
        # corrupt the library — belt and braces, not either/or.
        total = len(store.items.list(library_id))
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), total_pages)
        start_position = (page - 1) * PAGE_SIZE
        store.items.reorder(library_id, ordered_ids, start_position=start_position)
    return _redirect(library_id)


@router.post("/libraries/{library_id}/items")
async def add_item(
    request: Request,
    library_id: str,
    ref: str = Form(""),
    file: UploadFile | None = File(None),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    library = store.libraries.get(library_id)
    if library is None:
        return Response(status_code=404)

    added = []
    if file is not None and file.filename:
        ingest_upload = request.app.state.ingest_upload
        item = ingest_upload(library_id, file.filename, file.file)
        added = [item] if item is not None else []
    elif ref.strip():
        ingest = request.app.state.ingest
        added = ingest(library_id, ref.strip()) or []

    if added:
        store.settings.set("last_library_id", library_id)
        # inline-validation (design-system/box-butler/pages/library.md):
        # the add form's feedback says what it accepted, not just "done".
        titles = ", ".join(i.title for i in added)
        noun = "entry" if len(added) == 1 else "entries"
        phrase = f"Added {len(added)} {noun}: {titles}"
    else:
        phrase = "Nothing added — check the link or file and try again."
    return _redirect(library_id, phrase)


@router.post("/libraries/{library_id}/items/{item_id}/move")
def move_item(
    request: Request,
    library_id: str,
    item_id: str,
    delta: int = Form(...),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    store.items.move(item_id, delta)
    # Review round 2: which direction was actually pressed, so the focus
    # script can target that same button rather than always landing on
    # the first move button in DOM order (Move up).
    direction = "down" if delta > 0 else "up"
    return _redirect(library_id, focus_item_id=item_id, focus_dir=direction)


@router.post("/libraries/{library_id}/items/{item_id}/loudnorm")
def set_item_loudnorm(
    request: Request,
    library_id: str,
    item_id: str,
    on: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    is_on = on in ("1", "true", "on")
    store.items.set_loudnorm(item_id, is_on)
    return _redirect(library_id)


@router.post("/libraries/{library_id}/items/{item_id}/enabled")
def set_item_enabled(
    request: Request,
    library_id: str,
    item_id: str,
    enabled: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    is_enabled = enabled in ("1", "true", "on")
    store.items.set_enabled(item_id, is_enabled)
    return _redirect(library_id)


@router.post("/libraries/{library_id}/items/{item_id}/delete")
def delete_item(
    request: Request,
    library_id: str,
    item_id: str,
    delete_file: str = Form(""),
    user: str = Depends(require_login),
):
    """Remove the item. The file stays unless the operator explicitly
    asked for it to go.

    Since "a library is a folder", an item's audio is a real file the
    operator can see, and some of those files they put there themselves.
    The app's standing guarantee is that it never deletes a file it did
    not create — the one exception being this explicit, confirmed choice
    (`item_row.html`'s "and delete the file" checkbox, off by default).
    So: removing an item removes the row; removing the file is a second,
    separate decision that has to be made on purpose.

    The path is checked to be inside this library's own folder before
    anything is unlinked. A library folder is the only place this app may
    delete from at all, and an item's `local_path` can point elsewhere
    entirely (a cache copy left over from before the migration) — which
    retention owns, not this route.
    """
    store: Store = request.app.state.store
    item = store.items.get(item_id)
    library = store.libraries.get(library_id)
    removed_file = False
    if item is not None and delete_file in ("1", "true", "on") and item.local_path and library:
        removed_file = _delete_library_file(library, Path(item.local_path))
    store.items.delete(item_id)
    if item is None:
        phrase = "Removed"
    elif removed_file:
        phrase = f"Removed: {item.title} (and deleted its file)"
    else:
        phrase = f"Removed: {item.title}"
    return _redirect(library_id, phrase)


def _delete_library_file(library: Library, path: Path) -> bool:
    """Unlink `path`, but only if it really is inside `library`'s own
    folder. Returns whether it went."""
    if not library.folder_path:
        return False
    try:
        folder = Path(library.folder_path).resolve()
        target = path.resolve()
    except OSError:
        return False
    if folder not in target.parents:
        logger.warning("refusing to delete %s: outside library folder %s", target, folder)
        return False
    try:
        target.unlink()
    except OSError:
        logger.warning("could not delete %s", target, exc_info=True)
        return False
    return True


@router.post("/libraries/{library_id}/scan")
def scan_folder(
    request: Request,
    library_id: str,
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    library = store.libraries.get(library_id)
    if library is None:
        return Response(status_code=404)
    n = request.app.state.scan_folder(library)
    noun = "item" if n == 1 else "items"
    return _redirect(library_id, f"Scan complete: {n} new {noun}")


@router.post("/libraries/{library_id}/delete")
def delete_library(
    request: Request,
    library_id: str,
    confirm: str = Form(""),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    library = store.libraries.get(library_id)
    if library is None:
        return Response(status_code=404)
    # confirmation-dialogs (design-system/box-butler/pages/library.md):
    # deleting a library is the most destructive action on this screen —
    # it takes every item with it — so it requires typing the library's
    # name back, not just a click-through dialog.
    if confirm != library.name:
        return Response(status_code=400)
    store.libraries.delete(library_id)
    return RedirectResponse(url=f"/libraries?announce={quote(f'{library.name}: deleted')}", status_code=303)
