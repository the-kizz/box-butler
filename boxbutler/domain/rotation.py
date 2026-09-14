"""Rotation ordering (spec §4): which item plays next for a given assignment.

Pure and stateless: the cursor lives on the `Assignment`, never on the
`Library` or `Item` — two tonies sharing one library must not steal each
other's place. Shuffle is a seeded deterministic permutation (never
`random.choice`): every item plays once before any repeats, and re-running
planning selects the same "next" item, which idempotency depends on.

Content-mode selection (single/album/serial) and the duplicate-avoidance
ladder are Task 4's job and belong in a separate module/extension of this
one — not built here.
"""
import random
from dataclasses import dataclass

from .models import Assignment, AssignmentMode, Item, ItemState, LibraryMode


def eligible(items: list[Item]) -> list[Item]:
    """Enabled items in an OK state, sorted by their library position."""
    return sorted(
        (i for i in items if i.enabled and i.state == ItemState.OK),
        key=lambda i: i.position,
    )


def _permutation(items: list[Item], seed: int) -> list[Item]:
    """A single seeded permutation of `items`, stable across calls for a
    given seed and input — same seed, same permutation, every run."""
    rng = random.Random(seed)
    perm = list(items)
    rng.shuffle(perm)
    return perm


def order_for(assignment: Assignment, items: list[Item]) -> list[Item]:
    """The eligible items for `assignment`, in the order they should be
    consumed next, starting with whatever is "next" right now.

    - ORDERED: eligible items rotated so index 0 is the item at
      cursor_position (mod n).
    - SHUFFLE: one seeded permutation (assignment.shuffle_seed), rotated to
      cursor_position % n so repeated cursor values keep picking the same
      item (idempotency) while the cursor still walks the whole permutation
      once before wrapping.

    Pin handling (freezing an assignment on one item, and not advancing its
    cursor) is Task 4's responsibility, expressed by `plan()` returning
    `reason="PINNED", rotates=False` — this function is pin-agnostic; it
    only orders by cursor/shuffle/eligibility.
    """
    elig = eligible(items)
    n = len(elig)
    if n == 0:
        return []

    cursor = assignment.cursor_position
    if assignment.mode == AssignmentMode.SHUFFLE:
        elig = _permutation(elig, assignment.shuffle_seed)
    k = cursor % n
    return elig[k:] + elig[:k]


def advance_cursor(cursor: int, loaded_count: int, n_items: int) -> int:
    """The next cursor position after loading `loaded_count` items from a
    rotation of `n_items` eligible items. Wraps modulo n; 0 when n == 0."""
    if n_items <= 0:
        return 0
    return (cursor + loaded_count) % n_items


# --- Planning (spec §4.1 content modes, §4.2 duplicate-avoidance ladder, §4 pin) ---
#
# Task 3's `order_for` is deliberately pin-agnostic (see its docstring); pin
# freezing belongs here, in `choose_next`, because "pin" means "do not
# advance the cursor" — a concept `order_for` has no vocabulary for (R8).


@dataclass(frozen=True)
class PlanInput:
    assignment: Assignment
    library_mode: LibraryMode                # effective = assignment.mode_override or library.mode
    items: list[Item]
    loaded_elsewhere: frozenset[str] = frozenset()   # item ids currently on OTHER managed tonies (chapter_record)
    recently_held: frozenset[str] = frozenset()      # item ids this tonie held within repeat_cooldown_days
    avoid_duplicates: bool = True
    cooldown_active: bool = False           # repeat_cooldown_days > 0


@dataclass(frozen=True)
class Plan:
    item_ids: list[str]        # ordered candidates: single -> [one]; album -> all eligible; serial -> from cursor onward
    relaxations: list[str]     # subset of ["RELAXED_COOLDOWN", "RELAXED_UNIQUENESS"] in order applied
    reason: str | None         # "NO_CANDIDATE" | "PINNED" | "EMPTY_LIBRARY" | None
    rotates: bool              # False for album (and for PINNED/EMPTY_LIBRARY)


def effective_mode(assignment: Assignment, library_mode: LibraryMode) -> LibraryMode:
    """Content amount is the library's mode, overridable per assignment."""
    return assignment.mode_override or library_mode


def _first_allowed(ordered: list[Item], blocked: set[str]) -> Item | None:
    return next((i for i in ordered if i.id not in blocked), None)


def _rotate_to(ordered: list[Item], item_id: str) -> list[Item]:
    """Rotate `ordered` so the item with `item_id` is first."""
    idx = next(i for i, it in enumerate(ordered) if it.id == item_id)
    return ordered[idx:] + ordered[:idx]


def choose_next(inp: PlanInput) -> Plan:
    """Decide what an assignment should hold next.

    Duplicate avoidance (cooldown + cross-tonie uniqueness) applies to
    SINGLE mode only: ALBUM loads its whole library by definition and
    SERIAL follows its own cursor position, so neither can meaningfully
    avoid an item another tonie holds.

    A pin freezes the cursor — it does not shrink the load (spec §4: "Pin
    freezes ... it simply never advances"). So a pin sets where the mode's
    normal fill starts (the pinned item becomes the head of the ordered
    candidates) and forces `rotates=False`; it does not change ALBUM's
    "whole library" or SERIAL's "fill to cap" behaviour. Only SINGLE
    happens to collapse to exactly the pinned item, because SINGLE's normal
    load is one item anyway. A pinned item that is not itself eligible
    (disabled or unavailable) is not a valid freeze point, so the pin is
    ignored and normal ordering applies instead — a pin must never empty a
    tonie.
    """
    a = inp.assignment
    mode = effective_mode(a, inp.library_mode)
    ordered = order_for(a, inp.items)
    if not ordered:
        return Plan([], [], "EMPTY_LIBRARY", rotates=False)

    pinned = a.pinned_item_id
    pin_active = pinned is not None and any(i.id == pinned for i in ordered)
    reason = "PINNED" if pin_active else None
    if pin_active:
        ordered = _rotate_to(ordered, pinned)

    if mode == LibraryMode.ALBUM:
        # Content-neutral to a pin: already the whole library, already
        # never rotates.
        return Plan([i.id for i in eligible(inp.items)], [], reason, rotates=False)
    if mode == LibraryMode.SERIAL:
        return Plan([i.id for i in ordered], [], reason, rotates=not pin_active)

    # SINGLE
    if pin_active:
        return Plan([pinned], [], "PINNED", rotates=False)

    # walk the relaxation ladder (spec §4.2) — only reached unpinned
    relax: list[str] = []
    cooldown = set(inp.recently_held) if inp.cooldown_active else set()
    unique = set(inp.loaded_elsewhere) if inp.avoid_duplicates else set()
    pick = _first_allowed(ordered, cooldown | unique)
    if pick is None and cooldown:
        relax.append("RELAXED_COOLDOWN")
        pick = _first_allowed(ordered, unique)
    if pick is None and unique:
        relax.append("RELAXED_UNIQUENESS")
        pick = _first_allowed(ordered, set())
    if pick is None:
        # Reachable only when the orchestrator's own constraints (e.g. the
        # chosen item can't be staged) leave nothing usable, never from
        # relaxation alone: dropping both cooldown and uniqueness clears
        # every candidate ordered by cursor already contains.
        return Plan([], relax, "NO_CANDIDATE", rotates=False)
    return Plan([pick.id], relax, None, rotates=True)
