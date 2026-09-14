"""Settings screen — schedule/timezone, cap, duplicate avoidance, cache
budget, normalisation default, notifications, export/import (Task 12;
spec §5 screen 5, §7, §3.4, §3.6, §4.2, §9.1, §3.1).

**progressive-disclosure** (design-system/box-butler/pages/settings.md):
every one of these settings has a working default. Only schedule and
timezone sit outside the collapsed **Advanced** `<details>` block — they
are the pair the contract calls out as "the two that change behaviour
most and the pair that must not be set inconsistently", so they stay
paired at the top. Cap, cache budget, prefetch depth, loudnorm default,
duplicate avoidance + cooldown, and notifications all live inside
Advanced: nobody needs to open it to get a working install.

(One line in that same contract paragraph — "...and the per-assignment
mode override live inside a collapsed Advanced block" — describes
per-*assignment* state, which lives on the assignment/dashboard, not a
global Settings key. There is no such key in this task's brief's
settings list, so nothing for it is rendered here; see the task report.)

**Cap default is 5395 s, not the brief's sketched 5340** (spec §3.4,
`boxbutler.domain.fitting.DEFAULT_CAP_SECONDS`): 5340 was a minute of
insurance against encoder rounding drift; live measurement against the
real cloud showed the transcode preserves source duration exactly, so
the insurance was withdrawn and the default moved to 5395 (5 s under the
hard 5400 s cap). This module uses 5395 throughout.

**Validation is server-authoritative and all-or-nothing**: on any error,
nothing is written to the store (the failing test in the brief for an
invalid schedule specifically asserts prior state survives), and the
page re-renders at 400 with the submitted values preserved and an error
that states cause and fix (`error-clarity`). `inline-validation` (blur,
not per keystroke) is implemented client-side in settings.html as a
courtesy; the server re-validates regardless since nothing here may be
enforced only in JS.

**The ntfy token is write-only from the UI's perspective.** It is read
only from `BOXBUTLER_NOTIFY_TOKEN` at call time (Phase 3); this screen
never accepts, stores or renders it — only whether one is currently set,
so the operator can tell configuration is complete without the value
ever reaching the page (see NOTIFY_TOKEN_ENV_VAR below).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ...domain.fitting import DEFAULT_CAP_SECONDS, clamp_cap
from ...store.db import Store
from ...store.export_import import export_json, import_json
from ..auth import require_login

router = APIRouter()

NOTIFY_TOKEN_ENV_VAR = "BOXBUTLER_NOTIFY_TOKEN"

_SCHEDULE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULTS: dict[str, object] = {
    "schedule": "15:00",
    "cap_seconds": DEFAULT_CAP_SECONDS,
    "avoid_duplicates_across_tonies": True,
    "repeat_cooldown_days": 0,
    "cache_budget_gb": 40,
    "prefetch_depth": 3,
    "loudnorm_default": False,
    # Zero-config default: a public install has no ntfy server of its own
    # (spec §9.1 — "configurable, not hardcoded... a public user has
    # their own server or none at all"), so "none" is the honest
    # out-of-the-box state rather than a fake example.ntfy.
    "notify_kind": "none",
    "notify_server": "",
    "notify_topic": "",
    "notify_on_failure": True,
    "notify_on_success": False,
    "notify_on_debug": False,
}


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _system_timezone() -> str:
    """Best-effort system zone from /etc/localtime's zoneinfo symlink."""
    try:
        path = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        idx = path.find(marker)
        if idx != -1:
            candidate = path[idx + len(marker) :]
            if candidate in available_timezones():
                return candidate
    except OSError:
        pass
    return "UTC"


def default_timezone() -> str:
    """TZ env var, then the system zone (spec §7)."""
    tz = os.environ.get("TZ")
    if tz and tz in available_timezones():
        return tz
    return _system_timezone()


def _bool_field(value: str | None) -> bool:
    return value in ("1", "true", "on", "True")


@dataclass
class _Values:
    schedule: str
    timezone: str
    cap_seconds: int
    avoid_duplicates_across_tonies: bool
    repeat_cooldown_days: int
    cache_budget_gb: int
    prefetch_depth: int
    loudnorm_default: bool
    notify_kind: str
    notify_server: str
    notify_topic: str
    notify_on_failure: bool
    notify_on_success: bool
    notify_on_debug: bool


def _current_values(store: Store) -> _Values:
    get = store.settings.get
    return _Values(
        schedule=get("schedule", DEFAULTS["schedule"]),
        timezone=get("timezone", default_timezone()),
        cap_seconds=get("cap_seconds", DEFAULTS["cap_seconds"]),
        avoid_duplicates_across_tonies=get(
            "avoid_duplicates_across_tonies", DEFAULTS["avoid_duplicates_across_tonies"]
        ),
        repeat_cooldown_days=get("repeat_cooldown_days", DEFAULTS["repeat_cooldown_days"]),
        cache_budget_gb=get("cache_budget_gb", DEFAULTS["cache_budget_gb"]),
        prefetch_depth=get("prefetch_depth", DEFAULTS["prefetch_depth"]),
        loudnorm_default=get("loudnorm_default", DEFAULTS["loudnorm_default"]),
        notify_kind=get("notify_kind", DEFAULTS["notify_kind"]),
        notify_server=get("notify_server", DEFAULTS["notify_server"]),
        notify_topic=get("notify_topic", DEFAULTS["notify_topic"]),
        notify_on_failure=get("notify_on_failure", DEFAULTS["notify_on_failure"]),
        notify_on_success=get("notify_on_success", DEFAULTS["notify_on_success"]),
        notify_on_debug=get("notify_on_debug", DEFAULTS["notify_on_debug"]),
    )


def _timezone_choices() -> list[str]:
    return sorted(available_timezones())


def _render(request: Request, values: _Values, *, errors: list[str] | None = None,
            saved: bool = False, status_code: int = 200) -> Response:
    templates = _templates(request)
    token_set = bool(os.environ.get(NOTIFY_TOKEN_ENV_VAR))
    sink = request.app.state.sink
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "v": values,
            "timezones": _timezone_choices(),
            "sink_max_seconds": sink.limits.max_seconds,
            "token_set": token_set,
            "token_env_var": NOTIFY_TOKEN_ENV_VAR,
            "errors": errors or [],
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/settings")
def settings_page(request: Request, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    saved = request.query_params.get("saved") == "1"
    return _render(request, _current_values(store), saved=saved)


@router.post("/settings")
def save_settings(
    request: Request,
    schedule: str = Form(...),
    timezone: str = Form(...),
    cap_seconds: str = Form(str(DEFAULT_CAP_SECONDS)),
    avoid_duplicates_across_tonies: str | None = Form(None),
    repeat_cooldown_days: str = Form("0"),
    cache_budget_gb: str = Form("40"),
    prefetch_depth: str = Form("3"),
    loudnorm_default: str | None = Form(None),
    notify_kind: str = Form("none"),
    notify_server: str = Form(""),
    notify_topic: str = Form(""),
    notify_on_failure: str | None = Form(None),
    notify_on_success: str | None = Form(None),
    notify_on_debug: str | None = Form(None),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store
    sink = request.app.state.sink

    errors: list[str] = []

    schedule = schedule.strip()
    if not _SCHEDULE_RE.match(schedule):
        errors.append(
            f'"{schedule}" is not a 24-hour HH:MM time — use hours 00-23 and minutes 00-59, '
            'for example 15:00.'
        )

    timezone = timezone.strip()
    if timezone not in available_timezones():
        errors.append(f'"{timezone}" is not a recognised timezone — pick one from the list below.')

    def _parse_int(raw: str, field: str, *, minimum: int = 0) -> int | None:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            errors.append(f'{field} must be a whole number — got "{raw}".')
            return None
        if n < minimum:
            errors.append(f'{field} can\'t be negative — got {n}, minimum is {minimum}.')
            return None
        return n

    cap_raw = _parse_int(cap_seconds, "Cap (seconds)", minimum=1)
    cooldown_raw = _parse_int(repeat_cooldown_days, "Repeat cooldown (days)", minimum=0)
    budget_raw = _parse_int(cache_budget_gb, "Cache budget (GB)", minimum=1)
    prefetch_raw = _parse_int(prefetch_depth, "Prefetch depth", minimum=0)

    if notify_kind not in ("ntfy", "none"):
        errors.append(f'"{notify_kind}" is not a recognised notification kind — choose ntfy or none.')

    if errors:
        # Re-render with exactly what was submitted so the operator isn't
        # made to retype anything (submit-feedback / error-clarity).
        submitted = _Values(
            schedule=schedule,
            timezone=timezone,
            cap_seconds=cap_raw if cap_raw is not None else DEFAULTS["cap_seconds"],
            avoid_duplicates_across_tonies=_bool_field(avoid_duplicates_across_tonies),
            repeat_cooldown_days=cooldown_raw if cooldown_raw is not None else DEFAULTS["repeat_cooldown_days"],
            cache_budget_gb=budget_raw if budget_raw is not None else DEFAULTS["cache_budget_gb"],
            prefetch_depth=prefetch_raw if prefetch_raw is not None else DEFAULTS["prefetch_depth"],
            loudnorm_default=_bool_field(loudnorm_default),
            notify_kind=notify_kind,
            notify_server=notify_server,
            notify_topic=notify_topic,
            notify_on_failure=_bool_field(notify_on_failure),
            notify_on_success=_bool_field(notify_on_success),
            notify_on_debug=_bool_field(notify_on_debug),
        )
        return _render(request, submitted, errors=errors, status_code=400)

    # cap_seconds is clamped, not rejected — an over-large value is a
    # harmless "as much as the sink allows" request, not a mistake to
    # bounce back (brief step 3 / test_cap_clamped_to_sink_max).
    cap_final = clamp_cap(cap_raw, sink.limits.max_seconds)

    store.settings.set("schedule", schedule)
    store.settings.set("timezone", timezone)
    store.settings.set("cap_seconds", cap_final)
    store.settings.set("avoid_duplicates_across_tonies", _bool_field(avoid_duplicates_across_tonies))
    store.settings.set("repeat_cooldown_days", cooldown_raw)
    store.settings.set("cache_budget_gb", budget_raw)
    store.settings.set("prefetch_depth", prefetch_raw)
    store.settings.set("loudnorm_default", _bool_field(loudnorm_default))
    store.settings.set("notify_kind", notify_kind)
    store.settings.set("notify_server", notify_server.strip())
    store.settings.set("notify_topic", notify_topic.strip())
    store.settings.set("notify_on_failure", _bool_field(notify_on_failure))
    store.settings.set("notify_on_success", _bool_field(notify_on_success))
    store.settings.set("notify_on_debug", _bool_field(notify_on_debug))

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.get("/api/export")
def api_export(request: Request, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    return JSONResponse(export_json(store))


@router.post("/api/import")
async def api_import(
    request: Request,
    file: UploadFile = File(...),
    replace: str | None = Form(None),
    user: str = Depends(require_login),
):
    store: Store = request.app.state.store

    raw = await file.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Response("That file isn't valid JSON — export a fresh copy from this page and try again.",
                         status_code=400)

    do_replace = _bool_field(replace)
    try:
        result = import_json(store, data, replace=do_replace)
    except ValueError as exc:
        return Response(f"Import failed: {exc}", status_code=400)
    except NotImplementedError:
        # destructive-emphasis: "replace" clears libraries absent from the
        # document, which store.export_import.import_json doesn't yet
        # implement (Phase 2). Fail clearly rather than silently ignoring
        # the checkbox or letting a 500 leak past it.
        return Response(
            "\"Replace\" isn't available yet — it would delete libraries not in the "
            "imported file, and that isn't implemented. Uncheck Replace to merge instead.",
            status_code=400,
        )

    counts = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in result.items())
    return RedirectResponse(url=f"/settings?saved=1&imported={counts}", status_code=303)
