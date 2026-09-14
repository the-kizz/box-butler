-- Box Butler migration 0003: the write-ahead swap marker (final safety
-- review, C3; spec §2 "the window between 6 and 8").
--
-- `assignment.state = DEGRADED` + `assignment.staged_json` are the record
-- of "this tonie was cleared and not successfully refilled" — but until
-- this table they were written only from the orchestrator's `except`
-- handler, i.e. they survived an *exception* and not a *process kill*. A
-- SIGKILL / OOM / host reboot in the window between CLEAR and the first
-- successful UPLOAD therefore left the tonie empty with the committed
-- database still reading `state=OK, staged_json=NULL, records=0`: nothing
-- degraded, no alert, no notification, no catch-up fire, and the tonie
-- silently empty until the next scheduled run ~24h later. That is the one
-- crash window in the whole swap that costs a child tonight's story.
--
-- So the intent is written here, and committed, *before* `sink.clear()` is
-- called: one row naming the assignment about to be cleared, the run doing
-- it, and the verified staged files that were about to go on. A successful
-- COMMIT deletes the row; `_degrade` deletes it too, having written the
-- same truth into `assignment` where it belongs. A row that is still here
-- at startup therefore means exactly one thing — the process died
-- mid-swap — and `boxbutler.orchestrator.reconcile` turns it into the
-- DEGRADED state the design already handles well: sticky, carrying the
-- verified staged bytes, repaired before any rotation, and visibly
-- alarming on the dashboard, in `/metrics` and to Prometheus.
--
-- One row per assignment (PRIMARY KEY on assignment_id): an assignment is
-- only ever mid-swap once at a time, and the run lock enforces that across
-- processes. `staged_json` is NOT NULL — a marker with no staged files
-- would be a marker that cannot be repaired from, and writing one would be
-- recording an unknown as a definite.
--
-- Convention (see 0001_initial.sql): the body is wrapped in a literal BEGIN
-- with no trailing COMMIT — migrate() appends the schema_version INSERT and
-- the COMMIT so the version row lands in the same transaction as the DDL.

BEGIN;

CREATE TABLE swap_marker (
  assignment_id TEXT PRIMARY KEY REFERENCES assignment(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  staged_json TEXT NOT NULL,
  written_at TEXT NOT NULL);
