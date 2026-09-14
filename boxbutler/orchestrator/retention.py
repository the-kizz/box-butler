"""Cache retention: explicit LRU eviction with protections (Task 24;
spec §3.6).

The cache lives at `/cache` (a separate bind mount from `/data`, see
`compose/box-butler.yml`), deliberately outside the operator's app-data
backup scope so multi-hour audio stays out of it. Growth is unbounded by
design — every fetched source and
every rendered rendition lives there until this module says otherwise —
so eviction has to be explicit, and it has to be careful: this is the
first module in the project whose whole job is to delete things that are
still, in general, useful.

## What must never be evicted

- **A source or rendition inside the prefetch window**, for *any*
  assignment — evicting one would defeat the entire buffer Task 24's
  prefetch exists to build (`prefetch.py`'s module docstring: two YouTube
  extraction breaks on the prototype's first night are the whole reason
  this project keeps three verified items ahead of bedtime). Protecting
  only the assignment being evicted *for* would still let one tonie's
  buffer get sacrificed to make room for another's, so every assignment's
  window is protected, unconditionally.
- **The staged file(s) of a DEGRADED assignment** (`staged_json`) — the
  one thing a repair can restore from without re-fetching from a
  third party that may be broken. Deleting it turns a recoverable
  degraded tonie into an unrecoverable one.
- **Whatever an assignment's current `chapter_record` points at** — the
  story a tonie is holding *right now*, whether or not it also happens to
  fall inside the prefetch window (it usually does, but must not depend on
  it: a `repeat_cooldown`/relaxation-ladder pick or a serial cursor that
  has moved on can leave the live chapter outside the simple "next N").

## What is evicted, and in what order

The database is authoritative about what is referenced: a file in the
cache directory with no `source_file` row and no `rendition` row pointing
at it is an **orphan** — nothing recomputes it, nothing is poorer for its
absence — and orphans go first. Then **renditions**: derived, and per
spec §3.6 a stream-copy trim rebuilds in about 17 seconds. Then
**sources**: must be re-downloaded from a third party that may, per the
whole premise of this task, be broken. Within a kind, eviction is strict
LRU by `last_used_at`.

A row whose `last_used_at` cannot be read (never touched, or genuinely
NULL) is **not treated as cold** — that is the exact "unknown state read
as a definite one" failure this project keeps refusing elsewhere (a
`NaN` duration read as "fits"; a missing mount read as "deleted"). It
sorts as though it were just used, so it is evicted last within its kind,
never first, on the strength of a value nobody actually measured.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from boxbutler.domain.models import AssignmentState
from boxbutler.store.db import Store

from .prefetch import upcoming_item_ids

# Sentinel for "last-used time unknown" — sorts as newest, never as
# coldest. See the module docstring.
_UNKNOWN_IS_NOT_COLD = datetime.max


@dataclass(frozen=True)
class Evictable:
    path: Path
    bytes: int
    kind: str          # "orphan" | "rendition" | "source"
    last_used: datetime | None


def _protect_item(
    item_id: str,
    protected: set[Path],
    sources_by_item: dict[str, object],
    renditions_by_item: dict[str, list],
) -> None:
    sf = sources_by_item.get(item_id)
    if sf is not None:
        protected.add(Path(sf.cache_path))
    for r in renditions_by_item.get(item_id, []):
        protected.add(Path(r.cache_path))


def protected_paths(
    store: Store,
    depth: int,
    *,
    avoid_duplicates: bool = True,
    repeat_cooldown_days: int = 0,
    now: datetime | None = None,
) -> set[Path]:
    """Every path that must survive an eviction pass right now — see the
    module docstring for the three rules this implements.

    `avoid_duplicates` / `repeat_cooldown_days` / `now` are forwarded
    verbatim to `upcoming_item_ids` for every assignment, so the window
    protected here is the same window `prefetch_assignment` actually
    fetches (Important 2 of the Task 24 review) — protecting a
    *different*, cooldown-unaware window would leave the item genuinely
    being prefetched unprotected.
    """
    protected: set[Path] = set()
    sources_by_item: dict[str, object] = {sf.item_id: sf for sf in store.sources.list()}
    renditions_by_item: dict[str, list] = {}
    for r in store.renditions.list():
        renditions_by_item.setdefault(r.item_id, []).append(r)

    for a in store.assignments.list():
        if a.state == AssignmentState.DEGRADED and a.staged_json:
            try:
                chapters_meta = json.loads(a.staged_json)
            except (json.JSONDecodeError, TypeError):
                chapters_meta = []
            for c in chapters_meta:
                path = c.get("path") if isinstance(c, dict) else None
                if path:
                    protected.add(Path(path))

        for c in store.chapters.for_assignment(a.id):
            _protect_item(c.item_id, protected, sources_by_item, renditions_by_item)

        if a.library_id is not None:
            for item_id in upcoming_item_ids(
                store, a, depth,
                avoid_duplicates=avoid_duplicates,
                repeat_cooldown_days=repeat_cooldown_days,
                now=now,
            ):
                _protect_item(item_id, protected, sources_by_item, renditions_by_item)

    return protected


def _cache_files(cache_dir: Path) -> list[Path]:
    if not cache_dir.exists():
        return []
    return [p for p in cache_dir.iterdir() if p.is_file()]


def cache_usage_bytes(cache_dir: Path) -> int:
    """Total bytes currently in the cache directory. Used to detect a
    budget that eviction could not reach because protected content alone
    exceeds it — see `evict`'s "budget exceeded by protected content"
    event and Important 3 of the Task 24 review."""
    return sum(p.stat().st_size for p in _cache_files(cache_dir))


def _lru_key(e: Evictable):
    return e.last_used or _UNKNOWN_IS_NOT_COLD


def plan_eviction(
    store: Store,
    cache_dir: Path,
    budget_bytes: int,
    depth: int,
    *,
    avoid_duplicates: bool = True,
    repeat_cooldown_days: int = 0,
    now: datetime | None = None,
) -> list[Evictable]:
    """What eviction would do, without touching disk or the database.

    Orphans first (any deterministic order — by name), then renditions
    LRU, then sources LRU, skipping anything `protected_paths` covers,
    stopping the instant projected usage would be at or under budget.
    """
    files = _cache_files(cache_dir)
    sizes = {p: p.stat().st_size for p in files}
    total = sum(sizes.values())
    if total <= budget_bytes:
        return []

    protected = protected_paths(
        store, depth,
        avoid_duplicates=avoid_duplicates, repeat_cooldown_days=repeat_cooldown_days, now=now,
    )
    sources_by_path = {Path(sf.cache_path): sf for sf in store.sources.list()}
    renditions_by_path = {Path(r.cache_path): r for r in store.renditions.list()}

    orphans: list[Evictable] = []
    renditions: list[Evictable] = []
    sources: list[Evictable] = []

    for p in files:
        if p in protected:
            continue
        size = sizes[p]
        if p in renditions_by_path:
            r = renditions_by_path[p]
            renditions.append(Evictable(p, size, "rendition", r.last_used_at))
        elif p in sources_by_path:
            sf = sources_by_path[p]
            sources.append(Evictable(p, size, "source", sf.last_used_at))
        else:
            orphans.append(Evictable(p, size, "orphan", None))

    orphans.sort(key=lambda e: e.path.name)
    renditions.sort(key=_lru_key)
    sources.sort(key=_lru_key)

    plan: list[Evictable] = []
    evicted_bytes = 0
    for e in (*orphans, *renditions, *sources):
        if total - evicted_bytes <= budget_bytes:
            break
        plan.append(e)
        evicted_bytes += e.bytes
    return plan


def evict(
    store: Store,
    cache_dir: Path,
    budget_bytes: int,
    depth: int,
    *,
    apply: bool,
    avoid_duplicates: bool = True,
    repeat_cooldown_days: int = 0,
    now: datetime | None = None,
) -> list[Evictable]:
    """`plan_eviction`, and — only when `apply` — actually unlink the files
    and delete their database rows. `apply=False` (every dry run, per R21:
    a dry run persists nothing and deletes nothing) returns the same plan
    without touching disk or the store."""
    plan = plan_eviction(
        store, cache_dir, budget_bytes, depth,
        avoid_duplicates=avoid_duplicates, repeat_cooldown_days=repeat_cooldown_days, now=now,
    )
    if not apply:
        return plan

    renditions_by_path = {Path(r.cache_path): r for r in store.renditions.list()}
    sources_by_path = {Path(sf.cache_path): sf for sf in store.sources.list()}
    for e in plan:
        r = renditions_by_path.get(e.path)
        if r is not None:
            store.renditions.delete(r.id)
        sf = sources_by_path.get(e.path)
        if sf is not None:
            store.sources.delete(sf.item_id)
        e.path.unlink(missing_ok=True)
    return plan


__all__ = ["Evictable", "cache_usage_bytes", "evict", "plan_eviction", "protected_paths"]
