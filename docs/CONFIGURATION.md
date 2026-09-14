# Configuration and CLI reference

Everything Box Butler reads, and everything you can run. The README covers only the
minimum needed to get a container up.

## Environment variables

Every credential is read exclusively from environment variables — never from YAML — so a value
placed in `config.yml` for one of these is silently ignored rather than accidentally honoured.

| Variable | Required | Purpose |
|---|---|---|
| `BOXBUTLER_SINK_USER` | yes | Tonies cloud account email |
| `BOXBUTLER_SINK_PASSWORD` | yes | Tonies cloud account password |
| `BOXBUTLER_ADMIN_USER` | no | Web UI admin username (bootstrap only — omit both to use the setup wizard instead) |
| `BOXBUTLER_ADMIN_PASSWORD` | no | Web UI admin password (bootstrap only — omit both to use the setup wizard instead) |
| `BOXBUTLER_SECRET_KEY` | no | Session-signing key. Omit it and one is generated with `secrets.token_urlsafe(48)` on first start and persisted at `<data_dir>/secret_key` (mode 0600), so it stays stable across restarts; set it yourself only if you want to manage it |
| `BOXBUTLER_NOTIFY_TOKEN` | no | Bearer token for the notification server, if it needs one |
| `BOXBUTLER_DATA_DIR` | no | SQLite store + snapshots (default `/data`) |
| `BOXBUTLER_CACHE_DIR` | no | Fetched/rendered audio cache (default `/cache`) |
| `BOXBUTLER_MEDIA_ROOT` | no | Read-only mount for folder-backed libraries (default `/media`) |
| `BOXBUTLER_CONFIG` | no | Path to a `config.yml` (default: none — env + defaults only) |
| `BOXBUTLER_SINK_KIND` | no | `tonies_cloud` (default) or `fake` (smoke-testing only) |
| `TZ` | no | Fallback timezone if `timezone` isn't set in `config.yml` |

Everything else lives in `config.yml` and is editable live from the Settings screen — a change
there takes effect on the next run, with no restart:

| Setting | Default | Notes |
|---|---|---|
| `schedule` | `15:00` | Local wall-clock time of the daily run |
| `timezone` | `TZ`, then the host's zone | Set it explicitly if the host's zone is unreliable |
| `cap_seconds` | `5395` | 5 s under the cloud's hard 5400 s limit |
| `cache_budget_gb` | `40` | Eviction target for the cache directory |
| `prefetch_depth` | `3` | How many upcoming items to fetch ahead |
| `avoid_duplicates_across_tonies` | `true` | Don't put the same item on two tonies at once |
| `repeat_cooldown_days` | `0` | Days before an item may repeat on the same tonie |
| `loudnorm_default` | `false` | See *A note on bedtime audio* below |
| `listen_port` | `8410` | |
| `secure_cookies` | `false` | Set `true` when served over HTTPS |

**Measured cloud limits.** The service clamps itself to what the cloud actually accepts, which was
measured rather than assumed: **5400 s** per chapter (hence the 5395 default), **250 chapters** per
tonie, **1 GiB** per upload, and an access token that lives **300 seconds** — short enough that a
single three-tonie run outlives it, which is why the session refreshes mid-run rather than
authenticating once at startup.

See [`config.example.yml`](../config.example.yml) for the full commented list.

## CLI reference

**Dry-run is the default everywhere; writing requires `--apply`.** This is the property that makes
the tool safe to try: `run`, `cache evict` and `library import` all plan and print what they would
do, then exit, unless you pass `--apply`.

| Command | What it does |
|---|---|
| `boxbutler run [--assignment NAME ...] [--apply]` | Rotate every managed tonie that's due (or just the named ones) |
| `boxbutler status` | Show every managed tonie's current state |
| `boxbutler prefetch [--depth N]` | Warm the cache for upcoming items; never touches a tonie |
| `boxbutler cache report` | Show cache usage and what eviction would reclaim |
| `boxbutler cache evict [--apply]` | Reclaim cache disk space |
| `boxbutler library list` / `show NAME` | List libraries, or one library's items |
| `boxbutler library export [-o FILE]` | Export every library/assignment as JSON |
| `boxbutler library import FILE [--replace] [--apply]` | Import libraries/assignments from JSON |
| `boxbutler snapshot` | Write a point-in-time snapshot of every target's live chapters (writes files, never touches a tonie — no `--apply` needed) |

**Exit codes** are a contract with whatever calls this (a scheduler, cron, your own shell script):

| Code | Meaning |
|---|---|
| `0` | Ran fine — including a dry run that found nothing to do, or one that noticed a pre-existing problem while merely planning |
| `1` | A real (`--apply`) run genuinely failed, or left a tonie `DEGRADED` |
| `2` | The CLI itself was used wrong — bad flags, an unknown `--assignment` name — before anything was attempted |

## What a run actually looks like

Every run emits one JSON line per step to stdout, so `docker logs box-butler` is the whole story.
A dry run plans and prints, then stops:

```console
$ docker exec box-butler boxbutler run
{"ts":"2026-09-12T04:00:01+00:00","event":"run_started","run_id":"0f3c","dry_run":true}
{"ts":"2026-09-12T04:00:02+00:00","event":"dry_run_plan","run_id":"0f3c","assignment_id":"a1",
 "target":"Green Tonie","items":["ep-014"],"would_clear":3}
{"ts":"2026-09-12T04:00:02+00:00","event":"run_finished","run_id":"0f3c","outcome":"DRY_RUN"}
$ echo $?
0
```

The same command with `--apply` walks the full sequence — `fetch`, `render`, `verify`, `snapshot`,
`clear`, `upload`, `settle`, `commit` — one event each, so a failure names the step it stopped at
and what the tonie was left holding.

`boxbutler status` is the at-a-glance view:

```console
$ docker exec box-butler boxbutler status
TARGET       STATE      HOLDS              NEXT           LAST SUCCESS               PREFETCH READY
Green Tonie  OK         The Wobbling Moon  Sleepy Hollow  2026-09-12T04:00:02+00:00  3/3
Red Tonie    PAUSED     Old Story          -              2026-09-10T04:00:01+00:00  0/3
Blue Tonie   UNMANAGED  -                  -              -                          -
```

`PAUSED` is its own state on purpose: a paused tonie is skipped forever by the scheduler, so
reporting it as `OK` would tell you bedtime was covered when nothing would ever run again.


## Monitoring

The service exposes Prometheus metrics at `/metrics`. Because that endpoint reports on internal
run/cache/tonie state rather than serving anything a person needs to browse to, it's meant to be
scraped **container-to-container** (Prometheus reaching the container directly on its Docker
network) rather than published through a reverse proxy.

[`monitoring/alerts.yml`](../monitoring/alerts.yml) ships six alert rules:

| Alert | Fires when |
|---|---|
| `BoxButlerScrapeDown` | `/metrics` hasn't answered for 10 minutes |
| `BoxButlerTonieDegraded` | A tonie is in the `DEGRADED` state — cleared but not yet refilled |
| `BoxButlerRunStale` | A specific assignment has had no successful run in 36 hours, once it has ever succeeded — per assignment, so one stalled tonie is not masked by its healthy siblings |
| `BoxButlerRunCrashedOrFailed` | A run crashed or failed in the last 24 hours |
| `BoxButlerExtractionBroken` | YouTube extraction failed on a run in the last 24 hours |
| `BoxButlerPrefetchLow` | The prefetch buffer has been empty for 24 hours |
