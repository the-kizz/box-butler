"""Length-fitting logic for packing item audio onto a sink target (spec §3.4).

No I/O here: pure functions over durations.
"""
from dataclasses import dataclass

DEFAULT_CAP_SECONDS = 5395             # 5s of headroom under the hard 5400 (§3.4) — measured
                                        # against the live cloud: settled chapters preserve
                                        # source duration exactly, no encoder drift observed.
DURATION_TOLERANCE_S = 2.0
DEFAULT_MAX_CHAPTERS = 250             # sink's real maxChapters; a secondary count-only guard —
                                        # fitting stays a seconds problem otherwise (§3.4)


@dataclass(frozen=True)
class FitPiece:
    item_id: str
    take_seconds: float
    truncated: bool


@dataclass(frozen=True)
class FitPlan:
    pieces: list[FitPiece]
    total_seconds: float
    dropped: list[str]


def clamp_cap(configured: int, sink_max_seconds: int) -> int:
    if configured <= 0:
        return sink_max_seconds
    return min(configured, sink_max_seconds)


def fit_single(item_id: str, seconds: float, cap: int) -> FitPiece:
    if seconds > cap:
        return FitPiece(item_id, float(cap), True)
    return FitPiece(item_id, float(seconds), False)


def fit_fill(
    candidates: list[tuple[str, float]],
    cap: int,
    allow_partial_tail: bool = True,
    *,
    max_chapters: int = DEFAULT_MAX_CHAPTERS,
) -> FitPlan:
    # Fitting is a seconds problem: max_chapters is a count-only secondary guard on top of
    # the time-based packing below, not a second fitting strategy. It stops packing once
    # the chapter count is reached even if there is still time remaining — whichever bound
    # (seconds or chapters) comes first wins.
    pieces: list[FitPiece] = []
    dropped: list[str] = []
    consumed = 0.0
    remaining = float(cap)
    stopped = False
    for item_id, seconds in candidates:
        if stopped:
            dropped.append(item_id)
            continue
        if seconds <= remaining:
            pieces.append(FitPiece(item_id, float(seconds), False))
            consumed += seconds
            remaining -= seconds
            if remaining <= 0.0:
                stopped = True   # exact exhaustion: nothing after this may be packed
        elif allow_partial_tail or not pieces:
            # a first item longer than the cap is always truncated — an empty plan is never the answer
            take = remaining
            pieces.append(FitPiece(item_id, take, True))
            consumed += take
            remaining = 0.0
            stopped = True
        else:
            # tail dropped without a partial fill: nothing after it may be packed either
            # (library order matters), but total_seconds must reflect what was actually
            # consumed, not the cap.
            dropped.append(item_id)
            stopped = True
        if not stopped and len(pieces) >= max_chapters:
            stopped = True   # chapter bound reached before the time bound
    return FitPlan(pieces, consumed, dropped)
