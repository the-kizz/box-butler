"""README obligations (Task 33; spec §12, §7, §8, §3.5.1).

Enforces, mechanically, the things that must never regress in a public
README: the legal/ethical disclosures appear up front (within the first
2500 characters, not buried), the real env var names are documented, the
GPL/MIT/yt-dlp-ejs licensing facts are present, the stage-then-swap
safety property is named, and nothing operator-specific or otherwise
forbidden for a public repo leaks in.
"""
from pathlib import Path


def test_readme_obligations():
    r = Path("README.md").read_text()
    head = r[:2500]  # "up front, not buried"
    assert "not associated with Boxine" in head and "unofficial" in head.lower() and "restricted" in head.lower()
    for name in [
        "BOXBUTLER_SINK_USER", "BOXBUTLER_SINK_PASSWORD", "BOXBUTLER_ADMIN_USER",
        "BOXBUTLER_ADMIN_PASSWORD", "BOXBUTLER_SECRET_KEY", "BOXBUTLER_NOTIFY_TOKEN",
    ]:
        assert name in r
    assert "GPL" in r and "ffmpeg" in r and "MIT" in r and "yt-dlp-ejs" in r
    assert "Stage, verify, then swap" in r or "stage-then-swap" in r.lower()
    # Estate identifiers, not the bare substring "kizz": the project's own
    # public GitHub owner (`the-kizz`) must be allowed to appear, or the
    # quick start cannot name the image a reader is meant to pull. What is
    # forbidden is the operator's private infrastructure.
    for forbidden in ["kizserv", "kizz.space", "infisical", "/etc/estate",
                      "youtube.com/watch", "Koala Moon", "/srv/", "192.168."]:
        assert forbidden.lower() not in r.lower(), forbidden


def test_no_third_party_urls_or_audio_in_repo():
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    assert not [f for f in tracked if f.endswith((".m4a", ".mp3", ".opus", ".webm"))]
    assert not [f for f in tracked if f.startswith("snapshots/")]
