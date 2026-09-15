"""FastAPI app factory (Task 8; spec §5).

`create_app` wires the store, sink, settings and runner into `app.state`,
mounts static assets and templates, bootstraps the admin account, and
registers every route behind `require_login` except `/login` and
`/healthz`. Tasks 9-12 replace the placeholder screen routes registered
here with the real dashboard/library/history/runs/settings routers.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from boxbutler.domain.models import RunTrigger
from boxbutler.runlock import RunInProgress
from boxbutler.sinks.protocol import SinkProtocol
from boxbutler.store.db import Store
from boxbutler.web import APP_NAME
from boxbutler.web.auth import LoginRateLimiter, ensure_admin, require_login
from boxbutler.web.fake_data import default_scan_folder, fake_ingest, fake_ingest_upload
from boxbutler.web.routes import auth as auth_routes
from boxbutler.web.routes import dashboard as dashboard_routes
from boxbutler.web.routes import history as history_routes
from boxbutler.web.routes import library as library_routes
from boxbutler.web.routes import runs as runs_routes
from boxbutler.web.routes import settings as settings_routes
from boxbutler.web.routes import setup as setup_routes
from boxbutler.web.settings import WebSettings

# Task 36: paths reachable with no admin account yet — the setup wizard
# itself and anything static/operational. Everything else 302s to
# /setup until `store.settings.get("admin_user")` is set, either by
# `ensure_admin` (env vars) or by finishing the wizard.
_SETUP_EXEMPT_PREFIXES = ("/setup", "/static/")
_SETUP_EXEMPT_PATHS = ("/healthz",)

_WEB_DIR = Path(__file__).parent
_STATIC_DIR = _WEB_DIR / "static"
_TEMPLATES_DIR = _WEB_DIR / "templates"

# Task 9 replaced "/" with the real dashboard router; Task 10 replaced
# "/libraries" with the real library router; Task 11 replaced "/history"
# and "/runs" with the real history and runs routers; Task 12 replaces
# "/settings" and "/api/export" (plus adds "/api/import") with the real
# settings router (below) — no placeholder paths remain.
PLACEHOLDER_PROTECTED_PATHS: list[str] = []


def _format_hm(seconds: float) -> str:
    """`9060.0` -> `"2h 31m"`; anything under an hour -> `"31m"` with no
    leading `0h`. Shared by the library page's item duration and its
    trim-cap warning (item_row.html) so both read in the same units."""
    total_min = int(seconds // 60)
    hours, minutes = divmod(total_min, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


class RunnerProtocol(Protocol):
    def run(self, *, assignment_ids: list[str] | None, apply: bool, trigger: RunTrigger) -> str: ...

    def repair(self, assignment_id: str, *, apply: bool) -> str: ...

    def prefetch(self, assignment_id: str) -> None: ...


def create_app(
    store: Store,
    sink: SinkProtocol,
    settings: WebSettings,
    runner: RunnerProtocol,
    *,
    ingest: Callable[..., list] | None = None,
    ingest_upload: Callable[..., object] | None = None,
    scan_folder: Callable[..., int] | None = None,
    media_root: "Path | None" = None,
) -> FastAPI:
    app = FastAPI(title=APP_NAME)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["APP_NAME"] = APP_NAME
    # "2h 31m" formatting for a duration in seconds — used by the library
    # page's over-cap trim warning (item_row.html), which needs the same
    # h/m rendering for both an item's own duration and the effective cap
    # it's being compared against.
    templates.env.filters["hm"] = _format_hm

    app.state.store = store
    app.state.sink = sink
    app.state.settings = settings
    app.state.runner = runner
    app.state.templates = templates
    app.state.login_rate_limiter = LoginRateLimiter(settings.login_rate_limit, settings.login_rate_window_s)

    # Task 10's library screen "add a source" hook. Phase 2's defaults —
    # never touch the network, tonie_api, ffmpeg or a real filesystem —
    # stay the default here so every caller that predates Task 29 (this
    # module's own test fixtures, `boxbutler/web/dev.py`) is unaffected.
    # Task 29's composition root (`boxbutler/main.py::create_web_app`)
    # passes the real `Ingestor.add_ref`/`add_upload` and a real
    # `scan_folder` instead (see boxbutler/web/routes/library.py for why
    # the hook shape here differs from this task's brief).
    app.state.ingest = ingest if ingest is not None else partial(fake_ingest, store)
    app.state.ingest_upload = (
        ingest_upload if ingest_upload is not None else partial(fake_ingest_upload, store)
    )
    app.state.scan_folder = scan_folder if scan_folder is not None else default_scan_folder
    # The one directory library folders may live in. Every path the
    # folder picker resolves is confined to it
    # (`boxbutler/sources/library_folder.py::resolve_within`), and a new
    # library's folder is created under it. `None` only for the Phase 2
    # fake/dev wiring, which has no media mount and therefore no picker.
    app.state.media_root = Path(media_root) if media_root is not None else None

    # "A run is already in progress" is the runner's answer, not the web
    # layer's, and it is now raised as a plain `RunInProgress` so that
    # nothing below this layer has to import FastAPI to say it (the CLI
    # reports the same exception as a message and a non-zero exit). The HTTP
    # contract is unchanged: 409, exactly as before.
    @app.exception_handler(RunInProgress)
    async def _run_in_progress(request: Request, exc: RunInProgress):
        return JSONResponse(status_code=409, content={"detail": str(exc) or "a run is in progress"})

    ensure_admin(store, settings)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _require_setup(request: Request, call_next):
        path = request.url.path
        if path.startswith(_SETUP_EXEMPT_PREFIXES) or path in _SETUP_EXEMPT_PATHS:
            return await call_next(request)
        if request.app.state.store.settings.get("admin_user") is None:
            return RedirectResponse(url="/setup", status_code=302)
        return await call_next(request)

    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    # Every dashboard route already depends on require_login individually
    # (boxbutler.web.auth.require_login), so no router-level dependency here.
    app.include_router(dashboard_routes.router)
    app.include_router(library_routes.router)
    app.include_router(history_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(settings_routes.router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "app": APP_NAME}

    def _placeholder(request: Request):
        return templates.TemplateResponse(request, "base.html", {})

    for path in PLACEHOLDER_PROTECTED_PATHS:
        app.add_api_route(path, _placeholder, methods=["GET"], dependencies=[Depends(require_login)])

    return app
