"""Runs screen — run history and per-run event detail (Task 11; spec §5
screen 4).

`GET /runs/{run_id}` replaces Task 9's stopgap (`dashboard.run_stub` /
`templates/run_stub.html`, added in fix round 1 important 3 so `Run now` /
`Apply` / `Repair` never redirected to a silent blank placeholder). This
route keeps that behaviour — the run id and its outcome are still the
first thing shown — and adds the real event log: `run_event` rows grouped
by assignment, rendered as `<pre class="mono">` lines a human can read
without `docker logs` (spec §5 screen 4).

**Severity is not "success vs everything else" (review round 1, Important
1).** The spec (§2) treats `ABORTED_STAGING` and `DEGRADED` as opposite
outcomes even though both are non-`SWAPPED`: staging fails *before*
`clear`, so the tonie is untouched ("fail → ABORT. tonie untouched.");
`DEGRADED` means failure happened *after* `clear` — the tonie may be
empty. MASTER.md reserves the red `DEGRADED` treatment specifically for
"may be empty at bedtime" and calls it the most visually prominent thing
in the whole app, so `PLAN_FAILED`/`ABORTED_STAGING` (also "tonie
untouched" — nothing was ever planned to run) must not share that red +
warning-icon treatment. `_SEVERITY_STYLE` below is the single place that
maps an outcome to (status class, icon) so both `runs.html` and
`run_detail.html` render the same way:

  - "failed"    — DEGRADED only: red, warning icon. The one true
                  may-be-silent-tonight state.
  - "untouched" — PLAN_FAILED, ABORTED_STAGING: amber/muted, a
                  circle-dashed (skip) icon, never the warning icon — and
                  the label itself says "tonie untouched" so the operator
                  never has to decode the enum.
  - "ok"        — SWAPPED, REPAIRED, SKIPPED_ALREADY_CURRENT: green,
                  check-circle.
  - "neutral"/"progress" — NO_CANDIDATE, DRY_RUN, UNMANAGED, or no
                  outcome yet: muted, circle-dashed — informational, not
                  an alarm.

`OUTCOME_LABELS`/`TRIGGER_LABELS` (Minor 1) render plain language per
MASTER.md's "Copy" contract ("no jargon") instead of raw enum text; the
raw enum value is still shown (a small `.mono` detail beside the label,
plus a `title=` attribute) so anyone debugging against `run_event`/`run`
rows in the DB can still find it verbatim.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.templating import Jinja2Templates

from ...orchestrator.events import explain_reason
from ...store.db import Store
from ..auth import require_login

router = APIRouter()

# "FAILED"/"CRASHED"/"NOTHING_TO_DO" are run-level outcomes (`RunReport.
# outcome`, `store.runs.finish`) rather than per-assignment ones; they are
# rendered by the same helpers, so they belong in these maps too. Before the
# final safety review a CRASHED run fell through to "neutral" — a crash shown
# in the same muted grey as "nothing to do".
FAILURE_OUTCOMES = {"DEGRADED", "FAILED", "CRASHED"}
UNTOUCHED_OUTCOMES = {"PLAN_FAILED", "ABORTED_STAGING"}
SUCCESS_OUTCOMES = {"SWAPPED", "REPAIRED", "SKIPPED_ALREADY_CURRENT", "OK"}

OUTCOME_LABELS = {
    "SWAPPED": "Story swapped",
    "SKIPPED_ALREADY_CURRENT": "Already up to date",
    "NO_CANDIDATE": "Nothing to play",
    "PLAN_FAILED": "Couldn't plan — tonie untouched",
    "ABORTED_STAGING": "Staging failed — tonie untouched",
    "DEGRADED": "Degraded — may be empty at bedtime",
    "REPAIRED": "Repaired",
    "DRY_RUN": "Dry run — nothing changed",
    "UNMANAGED": "Unmanaged",
    "CRASHED": "Crashed — tonie untouched, see the log",
    "OK": "All good",
    "FAILED": "Run failed",
    "NOTHING_TO_DO": "Nothing to do — no tonie was processed",
}

TRIGGER_LABELS = {
    "schedule": "Scheduled",
    "manual": "Run by hand",
    "cli": "Command line",
}

# (status class, icon name) per severity — the single source of truth so
# runs.html and run_detail.html can never drift apart.
_SEVERITY_STYLE = {
    "failed": ("status-DEGRADED", "warning"),
    "untouched": ("status-UNTOUCHED", "circle-dashed"),
    "ok": ("status-OK", "check-circle"),
    "neutral": ("status-UNMANAGED", "circle-dashed"),
    "progress": ("status-UNMANAGED", "circle-dashed"),
}


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _severity(outcome: str | None) -> str:
    if outcome in FAILURE_OUTCOMES:
        return "failed"
    if outcome in UNTOUCHED_OUTCOMES:
        return "untouched"
    if outcome in SUCCESS_OUTCOMES:
        return "ok"
    if outcome is None:
        return "progress"
    return "neutral"


def _outcome_display(outcome: str | None) -> dict:
    severity = _severity(outcome)
    css_class, icon_name = _SEVERITY_STYLE[severity]
    label = "In progress" if outcome is None else OUTCOME_LABELS.get(outcome, outcome)
    return {"severity": severity, "css_class": css_class, "icon": icon_name, "label": label, "raw": outcome}


def _trigger_label(trigger) -> str:
    return TRIGGER_LABELS.get(str(trigger), str(trigger))


def _run_row(store: Store, run) -> dict:
    events = store.runs.events(run.id)
    assignment_ids = {e.assignment_id for e in events if e.assignment_id}
    return {
        "run": run,
        "started_display": run.started_at.strftime("%Y-%m-%d %H:%M"),
        "assignment_count": len(assignment_ids),
        "trigger_label": _trigger_label(run.trigger),
        "outcome": _outcome_display(run.outcome),
    }


@router.get("/runs")
def runs_index(request: Request, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    templates = _templates(request)
    rows = [_run_row(store, r) for r in store.runs.list()]
    return templates.TemplateResponse(request, "runs.html", {"rows": rows})


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: str, user: str = Depends(require_login)):
    store: Store = request.app.state.store
    templates = _templates(request)
    run = store.runs.get(run_id)
    if run is None:
        return Response(status_code=404)

    events = store.runs.events(run_id)

    groups: dict[str | None, list] = {}
    order: list[str | None] = []
    for ev in events:
        if ev.assignment_id not in groups:
            groups[ev.assignment_id] = []
            order.append(ev.assignment_id)
        groups[ev.assignment_id].append(ev)

    grouped = []
    for assignment_id in order:
        assignment = store.assignments.get(assignment_id) if assignment_id else None
        label = assignment.target_name if assignment else "General"
        lines = []
        for ev in groups[assignment_id]:
            ts = ev.ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ev.ts.tzinfo else ev.ts.isoformat()
            line = f"{ts}  {ev.event}"
            payload = " ".join(f"{k}={v}" for k, v in ev.payload.items())
            if payload:
                line += f"  {payload}"
            # Final coherence review, Major-4: a bare `reason=extraction_broken`
            # in this log is a machine code, not an explanation — the raw
            # code stays on the line above (it's what a grep/support request
            # would search for) and this appends operator guidance right
            # next to it, only when one exists for the reason.
            guidance = explain_reason(ev.payload.get("reason"))
            if guidance:
                line += f"\n    -> {guidance}"
            lines.append(line)
        grouped.append({"label": label, "lines": lines})

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "grouped": grouped,
            "trigger_label": _trigger_label(run.trigger),
            "outcome": _outcome_display(run.outcome),
        },
    )
