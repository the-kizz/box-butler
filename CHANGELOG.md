# Changelog

All notable changes to Box Butler are documented in this file. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.4] — 2026-09-15

### Fixed

- **YouTube extraction did not work at all in the 0.1.3 image.** It shipped Debian bookworm's
  `nodejs` package, which is Node 20; yt-dlp's `NodeJsRuntime.MIN_SUPPORTED_VERSION` is `(22, 0, 0)`,
  so it detected the runtime, marked it `(unsupported)` and declined to use it. Every URL then failed
  with `This video is not available` — a message that reads like a dead link rather than a broken
  image. The image now takes Node 22 from the official image (pinned, rather than adding a
  third-party apt repo: the same reasoning as installing `yt-dlp-ejs` from PyPI instead of fetching a
  solver from GitHub at runtime).
- **The test that should have caught it asserted the wrong thing.** It checked that the string
  `nodejs` appeared in the runtime stage, which a too-old Node satisfies perfectly. It now reads the
  required floor out of the *installed* yt-dlp, so a future yt-dlp raising its minimum goes red on
  the version bump rather than in someone's container. The image build additionally asks the
  installed yt-dlp, in the finished image, whether it will actually accept the bundled runtime — the
  only check that can see what the image really ended up with — and **fails the build** if not.
- `_dockerfile_stages`, which every "is X in the runtime stage?" test depends on, treated any line
  starting with `from` as a stage boundary, including one inside a multi-line `RUN`. It now honours
  line continuations.

## [0.1.3] — 2026-09-15

### Changed

- **A library is exactly one folder.** The split between "folder-backed" and "link-backed"
  libraries is gone. Every library is one directory under the media root, and a source — a video, a
  playlist, a podcast feed, an upload — is simply *how media arrives in it*: adding one downloads
  the audio into that folder, after which it is an ordinary file, indistinguishable from one you
  copied in by hand. This is the model Plex and the \*arr stack use.
- **`/media` is now read-write, and required.** The old "nothing in the image may ever write to
  `/media`" property was correct while the app only read. It is replaced, not weakened, by a
  narrower one: the app only ever *creates* files in a library folder, never modifies, renames,
  moves, truncates or deletes one it did not create, resolves a name collision by choosing a
  different name rather than overwriting, and deletes a file only on an explicit, confirmed choice
  in the UI. `tests/test_media_is_append_only.py` fills a library folder with files the app did not
  create, runs every operation that touches a library, and asserts each one is byte-identical
  afterwards.
- **Startup fails if `/media` is missing or unwritable**, naming the mount. There is deliberately no
  fallback into `/data` — that is the volume you back up, and libraries hold hours of audio. Plex,
  Sonarr, Immich and Paperless all refuse the same way.
- **`/media` is still never chowned.** `/data` and `/cache` are the app's own volumes; `/media` is
  your media library, likely shared with other apps and plausibly terabytes. Set `PUID`/`PGID` to a
  uid that can already write to it.
- **The cache holds renditions only** — the trimmed, verified audio uploaded to a tonie. Retention
  and eviction can never reach into a library folder, whatever the cache budget.
- **Setup wizard step 2 is no longer mandatory**: watch a folder / paste a link / skip, with skip
  the default when a library already exists. It previously demanded both a library name and a link
  while its own help text promised a folder option "later".

### Added

- **A folder picker** confined to the media root — it lists subfolders and offers to create one
  named after the library. It cannot navigate above the root: no `..`, no absolute path, no symlink
  escape.
- **Opening a library page scans its folder first**, so a file copied in by hand is simply listed.
  Deliberately *not* a filesystem watcher: inotify does not fire on NFS or SMB, so a watcher would
  be the feature most likely to look like it works while silently doing nothing.

### Fixed

- **The setup wizard's folder picker never appeared.** Two independent causes, either one
  sufficient: its `hx-get` pointed at a route the setup gate redirects and `require_login` refuses,
  and `setup.html` is standalone so the page had no htmx on it at all. Both silent — an htmx
  element that never loads looks exactly like one that was never there. The wizard now loads htmx
  and serves the picker from `/setup/folders`, which stops answering the moment an admin account
  exists.

### Migration

- A library with no folder is given one derived from its name under the media root, and the folder
  is created, at startup. **No audio is moved**: anything already fetched into `/cache` stays there
  and ages out by ordinary eviction. A migration that relocates audio can lose it.

## [0.1.2] — 2026-09-15

### Changed

- **A session key is generated on first start** and stored in the data volume
  (`/data/secret_key`, mode 0600) instead of being a required variable. Requiring one invited a weak
  one; a generated `token_urlsafe(48)` is stronger than anything typed by hand, and persisting it
  means restarts and upgrades no longer log everyone out. Set `BOXBUTLER_SECRET_KEY` yourself and it
  still wins. If the key cannot be persisted the app **refuses to start** rather than run with an
  ephemeral one that would silently invalidate every session on the next restart.
- The quick start is down to **two variables** — just the tonies account.
- Admin credentials deliberately unchanged: the first-run wizard still creates the account, and no
  default or generated password is printed anywhere. OWASP ASVS is explicit that there should be no
  default passwords, generated or static.

### Added

- The README now carries the compose file inline, a `docker run` one-liner, and a table of the
  environment variables — previously you had to fetch the compose file to read it.
- Documented that the Toniebox plays from its own storage, so a change in the cloud is not heard
  until the box next checks; lifting the tonie off and replacing it makes it fetch.

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
