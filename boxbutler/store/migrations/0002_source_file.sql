-- Box Butler migration 0002: the source-file cache index (Task 21; spec §2 step 2, §3.5).
--
-- `rendition` already records the *output* of the render step. This records
-- the *input*: the downloaded source file a fetcher produced for an item,
-- keyed by item so a second run can tell "this item's source is already on
-- disk" (a cache hit, spec §2 step 2) from "we have never fetched it", and
-- so Task 24's cache eviction has something to walk that is not a glob over
-- the cache directory.
--
-- One row per item (PRIMARY KEY on item_id): an item has exactly one source
-- file at a time. `cache_path` is UNIQUE as well — two items sharing one
-- cached file would make eviction of either delete the other's bytes.
-- `last_used_at` is NULL until the file is used in a successful run.
--
-- Convention (see 0001_initial.sql): the body is wrapped in a literal BEGIN
-- with no trailing COMMIT — migrate() appends the schema_version INSERT and
-- the COMMIT so the version row lands in the same transaction as the DDL.

BEGIN;

CREATE TABLE source_file (
  item_id TEXT PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
  cache_path TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  last_used_at TEXT);
