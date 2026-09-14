"""Repo-wide leak guard (final review, Major 4; spec §8/§12).

Before this test existed, the "no estate data in a public repo" rule was
only enforced against three files: `tests/test_readme.py` checked
README.md, and `tests/test_release_workflow.py` checked the Dockerfile,
entrypoint and release workflow. That is why 550+ tests could pass while
other *tracked* files shipped `/etc/estate/secrets.env`, an internal
hostname, a LAN IP and real tonie names — nothing was looking at them.

This test walks every file `git ls-files` reports (the actual publish
surface — untracked scratch files don't matter) and scans each one for a
fixed denylist of estate-identifying strings. It replaces neither of the
two existing checks (they assert additional things, like "the README's
disclosure appears in the first 2500 characters") — it exists alongside
them as a blanket backstop.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Files that legitimately contain these strings, because they *are* the
# denylist (as literals, for their own narrower checks) — not because
# estate data leaked into them. Excluded by exact path, not by skipping
# all of `tests/`: every other tracked file, test suite included, is
# still scanned.
_DENYLIST_OWNERS = {
    "tests/test_readme.py",
    "tests/test_release_workflow.py",
    "tests/test_dockerfile.py",
    "tests/test_no_leaked_estate_data.py",
    # Asserts config.example.yml carries none of these, so it necessarily
    # names them.
    "tests/test_config.py",
}

# Plain substrings, checked case-sensitively except where noted.
_FORBIDDEN_SUBSTRINGS = [
    "/etc/estate",
    "/srv/",
    "lan.kizz.space",
    "kizserv",
    "kizz.space",
    "192.168.",
    "TONIES_CLOUD_",
]

# Checked case-insensitively (repo prose, code, and comments could spell
# it either way).
_FORBIDDEN_SUBSTRINGS_CI = [
    "Infisical",
    # The operator's own Creative-Tonie and playlist names. These were
    # scattered through the fixtures and one production docstring because
    # the prototype was written against the real account -- the same leak
    # that once put a real tonie name into a screenshot destined for a
    # public README. Fixtures use invented names ("Green Tonie", "The
    # Wobbling Moon"); if one of these reappears, a real name has come
    # back with it.
    "Koala",
    "Bedtime Blue",
    "Bedtime Story Red",
]

# A bare 16-hex-character run is exactly the shape of a real Creative-
# Tonie id (see boxbutler/domain — cache keys elsewhere in this repo are
# also 16 hex chars, but those are derived from *content* hashes of
# fixture/test data, never a real tonie's id; a literal one here would
# mean an operator's tonie name/id had leaked). `\b` on both sides so it
# doesn't fire on a 16-char slice of a longer hex digest.
_HEX16_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{16}(?![0-9a-fA-F])")

# Extensions where a 16-hex match is expected/likely to be a content hash,
# lockfile digest, or similar false positive rather than a leaked tonie id.
_HEX16_EXEMPT_SUFFIXES = {".lock", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}

_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
    ".m4a", ".mp3", ".opus", ".webm", ".pdf",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [f for f in out.stdout.split("\n") if f]


def test_no_estate_data_in_any_tracked_file():
    failures = []
    for rel in _tracked_files():
        if rel in _DENYLIST_OWNERS:
            continue
        path = Path(rel)
        if not path.is_file():
            continue  # e.g. a submodule gitlink or a path git ls-files reports but the tree lacks
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not a text file we can meaningfully scan

        for bad in _FORBIDDEN_SUBSTRINGS:
            if bad in text:
                failures.append(f"{rel}: contains forbidden string {bad!r}")
        for bad in _FORBIDDEN_SUBSTRINGS_CI:
            if bad.lower() in text.lower():
                failures.append(f"{rel}: contains forbidden string {bad!r} (case-insensitive)")
        if path.suffix.lower() not in _HEX16_EXEMPT_SUFFIXES:
            m = _HEX16_RE.search(text)
            if m:
                failures.append(
                    f"{rel}: contains a 16-hex-character id-shaped string {m.group(0)!r} "
                    "(looks like a real Creative-Tonie id)"
                )

    assert not failures, "estate data leaked into tracked files:\n" + "\n".join(sorted(failures))


def test_deleted_prototype_and_planning_artefacts_stay_gone():
    """These were the prototype and planning artefacts, not part of the
    product — `scripts/phase0-load-tonie.py` additionally demonstrated the
    wipe-before-upload ordering this entire product exists to prevent.
    Deleted as part of this fix; this guards against them silently
    reappearing."""
    tracked = set(_tracked_files())
    for gone in (
        "HANDOFF-BRIEF.md",
        "scripts/phase0-load-tonie.py",
    ):
        assert gone not in tracked, f"{gone} must stay deleted"
    assert not any(f.startswith("docs/superpowers/") for f in tracked), (
        "docs/superpowers/ must stay deleted"
    )
