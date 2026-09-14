"""Prefetch: keep the next few items already fetched, rendered and
verified, so a third-party extraction break costs nothing at bedtime
(Task 24; spec §2 "Prefetch depth", §10.9).

## Why this exists

> YouTube extraction broke twice on the prototype's first night.

That is spec §2's own justification, verbatim. After a successful swap,
the next `prefetch_depth` items for that assignment are fetched, rendered
and verified in the background — not for performance, but so that an
extraction break takes days to reach a bedtime instead of minutes. Judge
every choice in this module against that property, not against "is this
fast".

## What prefetch must never do

- **Touch a tonie.** `prefetch_assignment` only ever calls
  `orch.stage_item`, which is built from the same `_fetch` /
  `_render_and_verify` primitives `stage()` uses — neither touches
  `deps.sink`.
- **Fail the run.** The swap already succeeded; a prefetch failure is
  worth recording but must never propagate out of `Orchestrator.prefetch`
  and turn a successful swap into a crashed run (see `run.py::prefetch`,
  which wraps this module's call in a catch-all for exactly that reason).
- **Hammer a broken extractor.** `ExtractionBroken` will hit every
  remaining item — it already stops `stage()`'s own fetch loop for the
  same reason (see `run.py`'s `_fetch`) — so prefetch stops immediately
  instead of working through the rest of the window one doomed fetch at a
  time. `ItemUnavailable` is scoped to one item: mark it and move on to
  the next upcoming item.

## What "upcoming" means, per mode

`choose_next` already treats single/album/serial very differently (spec
§4.1); prefetch has to decide, separately, what "the next N" means for
each, and the three answers are genuinely different — not a single
"walk the plan N times" loop. Ruling R22 is the load-bearing idea behind
all three: **prefetch warms content that will be needed but is not yet
loaded** — content already on a tonie is already protected through the
`chapter_record` path (see `retention.py`), so a mode must never report
already-loaded items as "upcoming" just because they happen to still be
correct.

- **single** — the next `depth` *positions* in rotation order, walked one
  real `choose_next` call at a time against a **simulated** cursor (never
  `assignment.cursor_position` itself — only `_commit` may advance that).
  Each step is built with the *same* cooldown/duplicate-avoidance settings
  `run.py`'s own `plan()` uses (`avoid_duplicates`, `repeat_cooldown_days`,
  evaluated against `now`) — a simulated walk that ignored them would
  predict, fetch and *protect* a different item than the one a cooldown
  would actually pick next, silently warming and protecting the wrong
  buffer. A pin collapses this to one item, repeated forever in reality,
  so the walk stops the instant `Plan.rotates` comes back `False` rather
  than reporting the same id `depth` times.
- **serial** — the next `depth` *items* from the current cursor, via a
  single real `choose_next` call (never a raw `order_for(...)` slice:
  `order_for` is deliberately pin-agnostic per its own docstring — only
  `choose_next` applies `_rotate_to(ordered, pinned)`. A pinned serial
  assignment's real next fill starts at the pinned item, and prefetch must
  warm and protect *that*, not whatever unpinned rotation order_for alone
  would give). Spec's "the next 3 items" is genuinely ambiguous for
  serial, whose real load fills multiple items to the cap in one run; this
  reads it as "the next `depth` items after the cursor", which may well be
  the tail of what the very next fill would reload — that overlap is
  fine, `stage_item`'s render cache makes a repeat cheap. It does not
  re-simulate serial's fit-to-cap packing: each upcoming item is staged
  independently, at the ordinary single-item cap. Duplicate-avoidance
  settings don't apply here — `choose_next` itself only runs that ladder
  for SINGLE (see its docstring) — so serial's call omits them.
- **album** — the items in the library **not currently loaded** by this
  assignment (`eligible(items) - {loaded chapter_record item ids}`),
  regardless of `depth` (any positive depth means "give me the delta").
  Normally empty: an album's whole library is already on the tonie once
  the assignment is `OK`, and an empty prefetch window is the correct
  answer for a library that has not changed (spec §4.1: once the tonie
  matches, every run is `SKIPPED_ALREADY_CURRENT` until the library
  itself changes). It becomes non-empty exactly when someone has grown
  the library — precisely the case the *next* run needs those new items
  fetched, rendered and verified for. Reporting the *whole* library here
  (an earlier draft of this function did) would make retention protect
  every album file forever, converting a bounded 40 GB budget into an
  unbounded one for a reason that does not apply: already-loaded album
  items are already protected via `chapter_record`, not because they are
  "upcoming".
- **`depth <= 0`** (the sentinel `Orchestrator.prefetch` uses for
  "prefetch disabled", and the value `retention.protected_paths` passes
  to mean "nothing is protected by the prefetch window") returns nothing
  for every mode alike, album included — otherwise `depth=0` could never
  mean "protect nothing".
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from boxbutler.domain.models import Assignment, ItemState, LibraryMode
from boxbutler.domain.rotation import (
    PlanInput,
    advance_cursor,
    choose_next,
    effective_mode,
    eligible,
)
from boxbutler.fetch.protocol import ItemUnavailable
from boxbutler.store.db import Store

from . import events as E


def upcoming_item_ids(
    store: Store,
    a: Assignment,
    depth: int,
    *,
    avoid_duplicates: bool = True,
    repeat_cooldown_days: int = 0,
    now: datetime | None = None,
) -> list[str]:
    """The item ids prefetch should warm next for `a` — see the module
    docstring for what "next" means in each of the three modes. Never
    mutates `a` or the store; `a.cursor_position` is only ever simulated
    here, never advanced for real.

    `avoid_duplicates` / `repeat_cooldown_days` / `now` mirror
    `RotationSettings.avoid_duplicates` / `.repeat_cooldown_days` and
    `Deps.clock()` — pass the real values (as `prefetch_assignment` and
    `retention.protected_paths` both do) so the simulated SINGLE-mode walk
    cannot diverge from what `run.py`'s own `plan()` would actually pick
    next. The defaults (no cooldown) only matter for a caller with no
    cooldown configured, where they are exactly correct.
    """
    if depth <= 0 or a.library_id is None:
        return []
    lib = store.libraries.get(a.library_id)
    if lib is None:
        return []
    items = store.items.list(a.library_id)
    mode = effective_mode(a, lib.mode)

    if mode == LibraryMode.ALBUM:
        # R22: not the whole library -- only what isn't loaded yet. See
        # the module docstring for why the whole library would be wrong.
        loaded = {c.item_id for c in store.chapters.for_assignment(a.id)}
        return [i.id for i in eligible(items) if i.id not in loaded]

    if mode == LibraryMode.SERIAL:
        # One real choose_next call (pin-aware), not a raw order_for
        # slice (pin-agnostic) -- see the module docstring.
        plan = choose_next(PlanInput(assignment=a, library_mode=lib.mode, items=items))
        return plan.item_ids[:depth]

    # SINGLE — walk choose_next against a simulated cursor, with the same
    # cooldown/duplicate-avoidance settings the real plan() would use.
    n = len(eligible(items))
    if n == 0:
        return []
    cooldown_active = repeat_cooldown_days > 0
    recently_held: frozenset[str] = frozenset()
    if cooldown_active:
        clock_now = now if now is not None else datetime.now(UTC)
        since = clock_now - timedelta(days=repeat_cooldown_days)
        recently_held = frozenset(store.chapters.held_since(a.id, since))
    loaded_elsewhere = frozenset(store.chapters.loaded_item_ids_except(a.id))
    ids: list[str] = []
    cur = a
    for _ in range(depth):
        plan = choose_next(
            PlanInput(
                assignment=cur, library_mode=lib.mode, items=items,
                loaded_elsewhere=loaded_elsewhere, recently_held=recently_held,
                avoid_duplicates=avoid_duplicates, cooldown_active=cooldown_active,
            )
        )
        if not plan.item_ids:
            break
        ids.append(plan.item_ids[0])
        if not plan.rotates:
            # A pin (or some other frozen state): the cursor will never
            # move past this item for real either, so stop rather than
            # report it `depth` times.
            break
        cur = replace(cur, cursor_position=advance_cursor(cur.cursor_position, 1, n))
    return ids


def ready_depth(store: Store, a: Assignment, depth: int) -> int:
    """How many of the upcoming items already have a verified rendition on
    disk right now. Exposed as the `boxbutler_prefetch_ready` metric
    (Task 27's job to actually emit it)."""
    ids = upcoming_item_ids(store, a, depth)
    by_item: dict[str, list] = {}
    for r in store.renditions.list():
        by_item.setdefault(r.item_id, []).append(r)

    ready = 0
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        rends = by_item.get(item_id, [])
        if any(r.verified_at is not None and Path(r.cache_path).exists() for r in rends):
            ready += 1
    return ready


def prefetch_assignment(orch, run_id: str, a: Assignment, depth: int) -> int:
    """Fetch, render and verify each upcoming item for `a`. Never touches
    `orch.deps.sink`. Returns the number of items verified this call.

    `ItemUnavailable` marks the item `UNAVAILABLE` and moves on to the next
    upcoming item — unlike `stage()`'s own loop, prefetch does not re-plan
    around it, since it already computed its whole window up front and an
    unavailable item elsewhere in that window is still worth warming.
    `ExtractionBroken` — raised inside `orch.stage_item`'s `_fetch` call,
    which has already recorded `E.EXTRACTION_BROKEN` and wrapped it as a
    `StagingFailed(reason="extraction_broken")` — stops the loop
    immediately: it will hit every remaining item, so trying them anyway
    only wastes time and hides the real fault.
    """
    # Local import: `run.py` imports this module at top level, so importing
    # it back at module scope here would be a cycle. By the time this
    # function actually runs, `run.py` is already fully loaded.
    from .run import StagingFailed

    store = orch.deps.store
    if a.library_id is None:
        return 0

    s = orch.deps.settings
    from boxbutler.domain.fitting import clamp_cap

    cap = clamp_cap(s.cap_seconds, orch.deps.sink.limits.max_seconds)

    ids = upcoming_item_ids(
        store, a, depth,
        avoid_duplicates=s.avoid_duplicates,
        repeat_cooldown_days=s.repeat_cooldown_days,
        now=orch.deps.clock(),
    )

    verified = 0
    for item_id in ids:
        item = store.items.get(item_id)
        if item is None:
            continue
        try:
            orch.stage_item(run_id, a, item, cap)
        except ItemUnavailable as e:
            eid = getattr(e, "item_id", None) or item_id
            store.items.set_state(eid, ItemState.UNAVAILABLE)
            orch._event(run_id, E.ITEM_UNAVAILABLE, a, item_id=eid, error=str(e))
            continue
        except StagingFailed as e:
            if e.reason == "extraction_broken":
                # E.EXTRACTION_BROKEN was already recorded inside _fetch.
                break
            orch._event(run_id, E.STAGING_FAILED, a, reason=e.reason, item_id=e.item_id)
            continue
        verified += 1
    return verified


__all__ = ["prefetch_assignment", "ready_depth", "upcoming_item_ids"]
