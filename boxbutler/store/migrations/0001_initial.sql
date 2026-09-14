-- Box Butler initial schema (Task 6; spec §3.1, §3.2).
--
-- R2 (controller ruling): assignment carries a single staged_json TEXT column
-- (a JSON list of {"item_id","path","title","seconds"}), not the brief's
-- original staged_path + staged_titles_json pair — a singular path cannot
-- describe repairing a multi-track album or serial load.
--
-- R5 (controller ruling): chapter_record.sink_chapter_id and
-- item.playlist_source_id are created here, in the initial migration, not
-- added later. sink_chapter_id makes idempotency possible without matching
-- on title; playlist_source_id records which playlist/feed produced an item
-- so a re-resolve marks only that playlist's vanished entries unavailable.
--
-- Convention for every migration file (review fix round 1): the body is
-- wrapped in a literal BEGIN below. There is no trailing COMMIT in this
-- file — migrate() appends the schema_version INSERT and the COMMIT itself,
-- so recording the applied version is part of the same transaction as the
-- DDL. A script that fails partway (e.g. a duplicate CREATE TABLE) never
-- reaches COMMIT; migrate() then issues an explicit ROLLBACK, leaving zero
-- tables and no schema_version row — verified in
-- tests/store/test_migrations.py::test_failed_migration_leaves_no_partial_schema.
-- (executescript() would otherwise implicitly COMMIT any *pending*
-- transaction before running, which is why a BEGIN issued via a separate
-- conn.execute() call before executescript() doesn't work — but a BEGIN
-- that is itself literal text *inside* the script runs as an ordinary
-- statement and is unaffected.)

BEGIN;

CREATE TABLE library (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL DEFAULT 'single' CHECK (mode IN ('single','album','serial')),
  folder_path TEXT, created_at TEXT NOT NULL);

CREATE TABLE item (
  id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES library(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('youtube','playlist_entry','rss','upload','folder_file','url')),
  source_ref TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT NOT NULL,
  loudnorm INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok','unavailable')),
  enabled INTEGER NOT NULL DEFAULT 1, seconds REAL, local_path TEXT, added_at TEXT NOT NULL,
  playlist_source_id TEXT,
  UNIQUE (library_id, source_key));
CREATE INDEX item_lib_pos ON item(library_id, position);

CREATE TABLE assignment (
  id TEXT PRIMARY KEY, sink TEXT NOT NULL, target_id TEXT NOT NULL, target_name TEXT NOT NULL,
  library_id TEXT REFERENCES library(id) ON DELETE SET NULL,
  mode TEXT NOT NULL DEFAULT 'ordered' CHECK (mode IN ('ordered','shuffle')), shuffle_seed INTEGER NOT NULL DEFAULT 0,
  pinned_item_id TEXT REFERENCES item(id) ON DELETE SET NULL, cursor_position INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'OK' CHECK (state IN ('OK','DEGRADED')),
  staged_json TEXT, mode_override TEXT, allow_partial_tail INTEGER NOT NULL DEFAULT 1,
  last_success_at TEXT, UNIQUE (sink, target_id));

CREATE TABLE rendition (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  cache_path TEXT NOT NULL UNIQUE, seconds REAL NOT NULL, bytes INTEGER NOT NULL, cap_seconds INTEGER NOT NULL,
  mode TEXT NOT NULL, verified_at TEXT, last_used_at TEXT, UNIQUE (item_id, cap_seconds, mode));

CREATE TABLE run (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  trigger TEXT NOT NULL CHECK (trigger IN ('schedule','manual','cli')), dry_run INTEGER NOT NULL, outcome TEXT);

CREATE TABLE run_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, assignment_id TEXT, event TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX run_event_run ON run_event(run_id, id);

CREATE TABLE chapter_record (
  id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE, sink_chapter_id TEXT,
  title TEXT NOT NULL, seconds REAL NOT NULL, uploaded_at TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);

CREATE TABLE chapter_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL, item_id TEXT NOT NULL,
  title TEXT NOT NULL, seconds REAL NOT NULL, since TEXT NOT NULL, until TEXT);
CREATE INDEX chapter_history_asg ON chapter_history(assignment_id, since);

CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
