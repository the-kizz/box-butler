# Changelog

All notable changes to Box Butler are documented in this file. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] — 2026-09-14

### Changed

- The dashboard now asks **what a tonie plays** as a plain either/or — *Rotate through the
  library* or *Always play this one* — instead of a "pin", which reads as a passcode. Pausing is
  a separate switch, so the card can no longer show "Rotating" while an item is in fact frozen.
- Libraries over 50 items get a **searchable picker** rather than a dropdown holding every item.
  An 838-item library previously rendered 839 `<option>` tags and 108 KB of HTML per tonie.
- Folder scans now **probe durations**, so library rows show a real length instead of "unknown"
  and say when an item will be trimmed to fit the tonie.
- The project describes itself as a Creative-Tonie **manager** rather than a rotator; worked
  examples moved to `docs/GUIDE.md`.

## [0.1.0] — 2026-09-14

First release. Proven end to end against real Creative-Tonies: staged, verified, snapshotted,
cleared, uploaded and settled an 89-minute story, then correctly did nothing on a second run.

### Added

- Domain logic: rotation ordering, content modes (`single`/`album`/`serial`), pin, duplicate
  avoidance across tonies, repeat cooldown, length fitting against the per-item duration cap.
- SQLite-backed store for libraries, items, assignments, chapters and run history.
- Six ingest methods: YouTube video, YouTube playlist, podcast RSS, file upload, watched folder,
  direct audio URL — all normalised to the same `Item` shape through a single `Ingestor`.
- Audio pipeline: fetch, trim/transcode to the cap, verify (duration, decodability,
  non-triviality), with stream-copy used whenever no re-encode is needed.
- The stage-then-swap orchestrator (`PLAN -> FETCH -> RENDER -> VERIFY -> SNAPSHOT -> CLEAR ->
  UPLOAD -> SETTLE -> COMMIT`), so a failure at any stage before `CLEAR` leaves a tonie untouched,
  and a crash after `CLEAR` is repairable from the stage-5 snapshot on the next run.
- Pluggable sink protocol with a tonies-cloud implementation and a fake sink for local/dev use.
- Prefetch, so upcoming items are downloaded and verified ahead of the rotation that needs them.
- Cache retention / eviction against a configurable disk budget.
- A web UI: dashboard, library management, run history, settings (light and dark).
- A CLI (`boxbutler run|status|prefetch|cache|library|snapshot`) with a dry-run-by-default,
  `--apply`-to-write contract and a documented exit-code contract for scheduler/cron integration.
- A built-in scheduler for unattended nightly rotation.
- A write-ahead swap marker, committed before the one destructive step and reconciled at
  startup, so a process killed between `CLEAR` and `COMMIT` leaves a tonie marked `DEGRADED`
  (repaired before any rotation) instead of an empty tonie the database calls `OK`.
- A cross-process run lock (`<data_dir>/run.lock`), taken by the UI, the scheduler and the CLI
  alike, so `docker exec … boxbutler run --apply` can no longer clear a tonie the scheduled run
  is already mid-swap on.
- Independent verification of a fetched file's completeness: downloaded bytes are checked
  against `Content-Length` when the server states one, and the probed duration against the
  feed's own `<itunes:duration>` when it published one. A materially short download aborts
  before any tonie is cleared.
- Prometheus metrics at `/metrics` and five alert rules in `monitoring/alerts.yml`.
- Dockerfile and a multi-arch GHCR release workflow.
- README, licences, and public-facing documentation.

### Fixed

- The sink's identity is now one value, read off the sink object itself. The web UI and the
  setup wizard used to key assignment rows under a hardcoded `"fake"` while the orchestrator
  looked them up under the real sink's name, so a default install created an assignment no run
  could find, reported it `UNMANAGED` (not a failure, so nothing notified), and rotated nothing,
  silently, forever.
- Any failure at or after `CLEAR` — not only the four named sink exceptions — now lands in
  `DEGRADED` with the verified staged files kept, and one tonie's failure no longer abandons the
  rest of the night.
- A run that processed nothing reports `NOTHING_TO_DO`, and a run whose sink listed no targets
  while managed tonies exist reports `FAILED`. `OK` now means something happened and it worked.
