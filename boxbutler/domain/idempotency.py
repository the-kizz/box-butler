"""Idempotency comparison: is this tonie already what we intended it to be? (spec §3.3)

No I/O here: pure comparison over already-fetched live chapters and our own
upload records. `source_key` / `sink_chapter_id` are the only identity we
trust — titles get edited upstream and two stories can share one, so they
are never consulted.
"""
from .fitting import DURATION_TOLERANCE_S
from .models import ChapterRecord
from ..sinks.protocol import LiveChapter


def already_current(
    live: list[LiveChapter],
    records: list[ChapterRecord],
    intended_item_ids: list[str],
    tolerance_s: float = DURATION_TOLERANCE_S,
) -> bool:
    """True iff the records for this assignment cover exactly intended_item_ids,
    in order, each record's sink_chapter_id is present live at the recorded
    duration (within tolerance), nothing live is mid-transcode, and the live
    set is exactly the recorded set (no extra, no missing chapters).
    """
    if not records or not intended_item_ids:
        return False

    recs = sorted(records, key=lambda r: r.position)
    if [r.item_id for r in recs] != list(intended_item_ids):
        return False
    if len(live) != len(recs):
        return False

    by_id = {c.id: c for c in live}
    for r in recs:
        c = by_id.get(r.sink_chapter_id)
        if c is None or c.transcoding or abs(c.seconds - r.seconds) > tolerance_s:
            return False
    return True
