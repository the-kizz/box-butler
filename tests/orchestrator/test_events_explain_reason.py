"""`explain_reason` (final coherence review, Major-4): additive
operator-facing guidance for a machine reason code, never a substitute
for it. See `boxbutler/orchestrator/events.py`.
"""
from boxbutler.orchestrator import events as E


def test_extraction_broken_has_actionable_guidance():
    guidance = E.explain_reason(E.EXTRACTION_BROKEN)
    assert guidance is not None
    assert "yt-dlp" in guidance
    assert guidance.isascii()  # may end up in an ntfy body


def test_unknown_reason_has_no_guidance():
    assert E.explain_reason("some_other_reason") is None


def test_none_reason_has_no_guidance():
    assert E.explain_reason(None) is None
