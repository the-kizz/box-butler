"""The `run_event` vocabulary (Task 21; spec §2, §10.2, §10.3).

One module, one list of strings. Every event the orchestrator persists to
`run_event` — and (Task 27) emits as a structured JSON log line — comes from
here, so the log a human reads without `docker logs` has a closed,
greppable vocabulary rather than ad-hoc literals scattered through
`run.py`.

`STAGE_ORDER` and `SWAP_ORDER` are not decoration: they *are* spec §2's
correctness property written as data. Everything in `STAGE_ORDER` is
side-effect-free on the tonie; nothing in `SWAP_ORDER` may happen until
every stage step succeeded. `tests/orchestrator/test_stage_then_swap.py`
asserts the recorded event stream equals `STAGE_ORDER + SWAP_ORDER` on the
happy path, and that no `SWAP_ORDER` event (above all `CLEAR`) ever appears
when a stage step failed.

The three upper-case names (`RELAXED_COOLDOWN`, `RELAXED_UNIQUENESS`,
`NO_CANDIDATE`) are deliberately not lower-cased: they are the exact
strings `boxbutler.domain.rotation.Plan.relaxations` / `.reason` already
carry, so the orchestrator can log a relaxation without translating it into
a second spelling that would then need keeping in sync.
"""

# --- stage: nothing here may change a tonie ---
PLAN = "plan"
PLAN_FAILED = "plan_failed"
SKIP_CURRENT = "skipped_already_current"
FETCH = "fetch"
RENDER = "render"
VERIFY = "verify"
STAGING_FAILED = "staging_failed"
ITEM_UNAVAILABLE = "item_unavailable"
EXTRACTION_BROKEN = "extraction_broken"
# The measurement inside the fetch step, and its cross-check against the one
# expectation the system did not derive from the file itself (`item.seconds`,
# from the feed) — final safety review, M2. `independent=False` in the payload
# says, explicitly, that no independent expectation existed for this item and
# that nothing beyond the byte-count check in `fetch/http.py` verified its
# completeness. Deliberately **not** members of `STAGE_ORDER` below: they are
# part of FETCH's measurement, not a sixth ordering step, and `STAGE_ORDER` is
# the spec's step list, not a log of everything emitted.
SOURCE_DURATION = "source_duration"
SOURCE_DURATION_MISMATCH = "source_duration_mismatch"

# --- swap: only reachable once a verified local file exists ---
SNAPSHOT = "snapshot"
CLEAR = "clear"
UPLOAD = "upload"
UPLOAD_RETRY = "upload_retry"
SETTLE = "settle"
COMMIT = "commit"

# --- state / housekeeping ---
DEGRADED = "degraded"
REPAIR_START = "repair_start"
REPAIR_RESTAGED = "repair_restaged"
REPAIRED = "repaired"
RELAXED_COOLDOWN = "RELAXED_COOLDOWN"
RELAXED_UNIQUENESS = "RELAXED_UNIQUENESS"
NO_CANDIDATE = "NO_CANDIDATE"
PREFETCH = "prefetch"
EVICT = "evict"
DRY_RUN_PLAN = "dry_run_plan"
UNMANAGED = "unmanaged"
# An unclassified exception for one assignment (final safety review, C2/M3):
# reported per assignment so the rest of the run continues, and loud — a bug,
# not an expected failure. Emitted only when no swap marker exists, i.e. only
# when the tonie was provably never touched; with a marker the same exception
# becomes `DEGRADED` instead.
CRASHED = "crashed"
# The sink listed no targets at all while managed assignments exist (M4):
# never normal, and the reason such a run is FAILED rather than "nothing to
# do".
NO_TARGETS = "no_targets"
SOURCE_ERROR = "source_error"
PLAYLIST_PREVIEW = "playlist_preview"

STAGE_ORDER = (PLAN, FETCH, RENDER, VERIFY)
SWAP_ORDER = (SNAPSHOT, CLEAR, UPLOAD, SETTLE, COMMIT)


# --- operator-facing guidance for reason codes -----------------------------

# Final coherence review, Major-4: `"extraction_broken"` reached an operator
# (a notification, the run detail page) as a bare machine reason code, while
# the README's own "Ingest notes" already explains in prose that this
# usually means YouTube changed something and yt-dlp needs updating. The
# difference between the two is the difference between a fixable evening and
# a mystery. `explain_reason` is additive, never a replacement: every caller
# keeps logging/reporting the raw code (metrics and tests key off the exact
# string) and appends this guidance alongside it, only where a human reads
# it. ASCII-only, since it can end up in an ntfy notification *body* (never
# the title -- see `notify.protocol.ascii_title`, run through separately).
_REASON_GUIDANCE: dict[str, str] = {
    EXTRACTION_BROKEN: (
        "This usually means YouTube changed something and yt-dlp needs "
        "updating: try `pip install -U yt-dlp yt-dlp-ejs`. Format 140 "
        "(the pinned extraction format), the yt-dlp-ejs JS-challenge "
        "solver, and the Node.js runtime are all load-bearing -- see the "
        "README's Ingest notes before changing any of them."
    ),
}


def explain_reason(reason: str | None) -> str | None:
    """Operator-facing remediation text for a machine `reason` code, or
    `None` when there is none. Callers append this to the raw reason,
    never substitute it -- the raw code stays intact for metrics/tests.
    """
    if reason is None:
        return None
    return _REASON_GUIDANCE.get(reason)
