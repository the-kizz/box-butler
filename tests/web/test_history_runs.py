"""History and Runs screen tests (Task 11; spec §5 screens 3 and 4).

History is read from the write-once snapshots and `chapter_record`/
`chapter_history` — it is the one screen that must never be lossy, so
these tests exercise the real store data the `seeded` fixture leaves
behind (boxbutler.web.fake_data.seed_fake) rather than inventing new
fixtures that could drift from what the route actually reads.

Runs replaces Task 9's `/runs/{run_id}` stopgap (a bare "named the run and
its outcome" page) with the real run-history table and per-run event
detail. `test_run_landing_page_names_the_run_and_its_outcome_not_blank` /
`test_dry_run_landing_page_names_the_run_and_its_outcome` in
test_dashboard.py already pin the "still names the run and its outcome"
behaviour that must survive the swap; this file does not repeat those,
only adds what's new.
"""
from __future__ import annotations

import re

from boxbutler.domain.models import RunOutcome, RunTrigger


def _lib(store, name="Bedtime"):
    return next(l for l in store.libraries.list() if l.name == name)


# --- Brief's own tests (Step 1), verbatim behaviour -----------------------


def test_history_says_how_long_a_tonie_held_a_story(seeded, store):
    html = seeded.get("/history").text
    assert "held" in html and "night" in html  # "held <title> for 2 nights"


def test_history_lists_snapshot_files_when_present(seeded, settings):
    snaps = settings.data_dir / "snapshots"
    snaps.mkdir(exist_ok=True)
    (snaps / "tonie-snapshot-20260911T081435Z.json").write_text("{}")
    assert "20260911T081435Z" in seeded.get("/history").text


def test_runs_list_and_detail_render_events(seeded, store):
    run = store.runs.list(1)[0]
    html = seeded.get("/runs").text
    assert run.id[:8] in html
    detail = seeded.get(f"/runs/{run.id}").text
    for ev in store.runs.events(run.id):
        assert ev.event in detail


def test_run_detail_404_for_unknown(seeded):
    assert seeded.get("/runs/nope").status_code == 404


# --- History: empty-states -------------------------------------------------


def test_history_empty_state_when_nothing_held(auth):
    # `auth` (unlike `seeded`) has no libraries, items, assignments or
    # chapter history at all — the true "nothing loaded yet" case.
    html = auth.get("/history").text
    assert auth.get("/history").status_code == 200
    assert "empty-state" in html
    # a helpful message *and* an action, never a blank screen
    assert 'href="/"' in html


def test_history_snapshots_empty_state_when_none_present(seeded):
    html = seeded.get("/history").text
    assert "No snapshots yet" in html


# --- History: sortable-table ------------------------------------------------


def test_history_table_has_aria_sort_on_date_header(seeded):
    html = seeded.get("/history").text
    assert re.search(r'aria-sort="(ascending|descending)"', html)


def test_history_sort_direction_toggles_and_reflects_in_aria_sort(seeded):
    desc = seeded.get("/history").text
    assert 'aria-sort="descending"' in desc

    asc = seeded.get("/history?dir=asc").text
    assert 'aria-sort="ascending"' in asc


# --- History: number-tabular / truncation-strategy --------------------------


def test_history_dates_use_mono_tabular_class(seeded):
    html = seeded.get("/history").text
    assert re.search(r'<td class="mono">', html)


def test_history_title_rendered_in_full_not_truncated(seeded, store):
    lib = _lib(store)
    long_title = "The Wobbling Moon"  # seeded bedtime title, held by Green Tonie
    html = seeded.get("/history").text
    assert long_title in html
    assert "…" not in html.split(long_title)[0][-40:]  # no ellipsis just before it


# --- History: snapshot download route ---------------------------------------


def test_snapshot_download_returns_file_contents(seeded, settings):
    snaps = settings.data_dir / "snapshots"
    snaps.mkdir(exist_ok=True)
    name = "tonie-snapshot-20260911T081435Z.json"
    (snaps / name).write_text('{"ok": true}')
    r = seeded.get(f"/history/snapshots/{name}")
    assert r.status_code == 200
    assert r.text == '{"ok": true}'


def test_snapshot_download_404_for_missing_file(seeded, settings):
    (settings.data_dir / "snapshots").mkdir(exist_ok=True)
    r = seeded.get("/history/snapshots/tonie-snapshot-20260911T081435Z.json")
    assert r.status_code == 404


def test_snapshot_download_rejects_name_outside_pattern(seeded, settings):
    snaps = settings.data_dir / "snapshots"
    snaps.mkdir(exist_ok=True)
    (snaps / "not-a-snapshot.json").write_text("{}")
    r = seeded.get("/history/snapshots/not-a-snapshot.json")
    assert r.status_code == 404


def test_snapshot_download_rejects_traversal_attempt(seeded, settings):
    (settings.data_dir / "snapshots").mkdir(exist_ok=True)
    r = seeded.get("/history/snapshots/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404


# --- Runs: color-not-only ----------------------------------------------------


def test_runs_table_marks_failed_outcome_with_icon_and_word(seeded, store):
    degraded_run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.DEGRADED))
    html = seeded.get("/runs").text
    # the plain-language word is present (MASTER.md "Copy": no jargon —
    # the raw enum is not shown as visible text, only as a title= tooltip)...
    assert "may be empty at bedtime" in html
    # ...and it is not conveyed by a bare colour class alone: an <svg> icon
    # sits beside it (icon("warning") from partials/_icons.html).
    idx = html.index(degraded_run.id[:8])
    surrounding = html[idx : idx + 2000]
    assert "<svg" in surrounding


def test_run_detail_marks_failed_run_with_icon_and_word(seeded, store):
    degraded_run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.DEGRADED))
    html = seeded.get(f"/runs/{degraded_run.id}").text
    assert "may be empty at bedtime" in html
    assert "status-DEGRADED" in html
    assert "<svg" in html


def test_aborted_staging_does_not_render_like_degraded(seeded, store):
    """Review round 1, Important 1: ABORTED_STAGING/PLAN_FAILED ("tonie
    untouched" — staging failed before `clear` ever ran) must not carry
    the same red/warning-icon treatment as DEGRADED ("may be empty at
    bedtime") — they are opposite outcomes in spec §2, and color-not-only
    only holds if the *icon and class*, not just the enum word, differ.
    """
    plan_failed_run = store.runs.start(str(RunTrigger.SCHEDULE), dry_run=False)
    store.runs.event(plan_failed_run.id, "plan_failed", assignment_id=None, reason="no candidate resolvable")
    store.runs.finish(plan_failed_run.id, str(RunOutcome.PLAN_FAILED))

    degraded_run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.DEGRADED))

    # warning icon's path data (partials/_icons.html) — the failure-only icon
    warning_icon_marker = "M236.8,188.09"
    # circle-dashed icon's path data — the untouched/neutral icon
    untouched_icon_marker = "M96.26,37.05"

    def _row(html: str, marker: str) -> str:
        idx = html.index(marker)
        start = html.rfind("<tr", 0, idx)
        end = html.index("</tr>", idx)
        return html[start:end]

    # /runs: both rows on one page, so compare each row's own markup —
    # not a fixed-size window, which can bleed into a neighbouring row.
    listing = seeded.get("/runs").text
    deg_chunk = _row(listing, degraded_run.id[:8])
    untouched_chunk = _row(listing, plan_failed_run.id[:8])

    assert "status-DEGRADED" in deg_chunk
    assert warning_icon_marker in deg_chunk

    assert "status-UNTOUCHED" in untouched_chunk
    assert "status-DEGRADED" not in untouched_chunk
    assert untouched_icon_marker in untouched_chunk
    assert warning_icon_marker not in untouched_chunk
    assert "tonie untouched" in untouched_chunk.lower()

    # /runs/{id}: each run's own detail page carries only its own styling.
    deg_detail = seeded.get(f"/runs/{degraded_run.id}").text
    assert "status-DEGRADED" in deg_detail
    assert warning_icon_marker in deg_detail

    untouched_detail = seeded.get(f"/runs/{plan_failed_run.id}").text
    assert "status-UNTOUCHED" in untouched_detail
    assert "status-DEGRADED" not in untouched_detail
    assert untouched_icon_marker in untouched_detail
    assert warning_icon_marker not in untouched_detail
    assert "tonie untouched" in untouched_detail.lower()


# --- Runs: event log rendering / grouping -----------------------------------


def test_run_detail_renders_events_as_pre_mono_log(seeded, store):
    run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.SWAPPED))
    html = seeded.get(f"/runs/{run.id}").text
    assert '<pre class="mono' in html


def test_run_detail_groups_events_by_assignment(seeded, store):
    run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.SWAPPED))
    events = store.runs.events(run.id)
    assignment = store.assignments.get(events[0].assignment_id)
    html = seeded.get(f"/runs/{run.id}").text
    assert assignment.target_name in html


def test_run_detail_event_line_has_key_value_payload(seeded, store):
    run = next(r for r in store.runs.list() if r.outcome == str(RunOutcome.SWAPPED))
    html = seeded.get(f"/runs/{run.id}").text
    assert "assignment_id=" not in html  # the field itself, not restated in the payload
    assert "item_ids=" in html


def test_extraction_broken_event_line_carries_actionable_guidance(seeded, store):
    """Final coherence review, Major-4: a bare `reason=extraction_broken`
    on this page told an operator nothing about how to fix it. The raw
    code must stay (it's what a grep/support request would search for),
    but the line must also carry the README's own remediation advice.
    """
    run = store.runs.start("manual", dry_run=False)
    store.runs.event(run.id, "staging_failed", None, reason="extraction_broken", item_id="x")
    store.runs.finish(run.id, str(RunOutcome.ABORTED_STAGING))

    html = seeded.get(f"/runs/{run.id}").text
    assert "reason=extraction_broken" in html
    assert "yt-dlp" in html


# --- Runs: empty state -------------------------------------------------------


def test_runs_empty_state(auth):
    html = auth.get("/runs").text
    assert auth.get("/runs").status_code == 200
    assert "empty-state" in html
    assert "No runs yet" in html


# --- Runs: list shows trigger and dry-run badge -----------------------------


def test_runs_list_shows_trigger_and_dry_run_badge(seeded, store):
    dry_run = next(r for r in store.runs.list() if r.dry_run)
    html = seeded.get("/runs").text
    idx = html.index(dry_run.id[:8])
    surrounding = html[idx : idx + 1500]
    assert str(RunTrigger.MANUAL) in surrounding or "manual" in surrounding
    assert "dry run" in surrounding.lower()
