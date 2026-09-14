from datetime import datetime, UTC
from boxbutler.domain.idempotency import already_current
from boxbutler.domain.models import ChapterRecord
from boxbutler.sinks.protocol import LiveChapter

NOW = datetime.now(UTC)


def rec(item, chap, secs, pos=0):
    return ChapterRecord(f"r{item}", "A", item, chap, "whatever", secs, NOW, pos)


def test_same_chapter_within_tolerance_is_current():
    live = [LiveChapter("c1", "Renamed upstream", 5340.0, False)]
    assert already_current(live, [rec("i0", "c1", 5340.5)], ["i0"]) is True


def test_title_is_never_the_key():
    live = [LiveChapter("c9", "Story 0", 5340.0, False)]        # same title, different chapter
    assert already_current(live, [rec("i0", "c1", 5340.0)], ["i0"]) is False


def test_wrong_duration_is_not_current():
    live = [LiveChapter("c1", "x", 0.0, False)]
    assert already_current(live, [rec("i0", "c1", 5340.0)], ["i0"]) is False


def test_transcoding_is_not_current():
    live = [LiveChapter("c1", "x", 5340.0, True)]
    assert already_current(live, [rec("i0", "c1", 5340.0)], ["i0"]) is False


def test_different_intended_item_is_not_current():
    live = [LiveChapter("c1", "x", 5340.0, False)]
    assert already_current(live, [rec("i0", "c1", 5340.0)], ["i1"]) is False


def test_multi_chapter_album_matches_whole_set():
    live = [LiveChapter("c1", "a", 100.0, False), LiveChapter("c2", "b", 200.0, False)]
    recs = [rec("i0", "c1", 100.0, 0), rec("i1", "c2", 200.0, 1)]
    assert already_current(live, recs, ["i0", "i1"]) is True
    assert already_current(live, recs, ["i0"]) is False           # library shrank → not current
    assert already_current(live[:1], recs, ["i0", "i1"]) is False  # tonie lost a chapter → not current


def test_no_records_is_not_current():
    assert already_current([LiveChapter("c1", "x", 5.0, False)], [], ["i0"]) is False
