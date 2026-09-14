"""Shared, anchored YouTube URL recognition (final review, Major 1).

`YouTubeUrlSource.matches` and `YouTubePlaylistSource.matches` used to
test whether a YouTube-looking substring appeared *anywhere* inside the
input (an unanchored regex / bare `"list=" in ref`). That let a yt-dlp
*option* string such as `--exec=curl ... youtu.be/xxxxxxxxxxx` pass
validation and later be placed in argv where yt-dlp reads it as an
option, not a URL — arbitrary command execution from the "add a source"
field.

Both resolvers now go through `urlparse` here instead: a candidate must
actually parse as an `http`/`https` URL whose *host* is a real YouTube
host. A value beginning with `-` (or any other non-URL garbage) has no
scheme and is rejected before the host is even inspected.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# youtu.be is share-link only (no playlist query param support worth
# trusting); the rest are the watch-page domains.
_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_ALL_HOSTS = _VIDEO_HOSTS | _SHORT_HOSTS


def is_youtube_video_url(ref: str) -> bool:
    """True if `ref` is an http(s) URL that names a single YouTube video
    (`watch?v=...`, `shorts/...`, or a `youtu.be/...` share link)."""
    try:
        parsed = urlparse(ref)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host in _SHORT_HOSTS:
        return bool(parsed.path.strip("/"))
    if host in _VIDEO_HOSTS:
        return parsed.path.startswith("/watch") or parsed.path.startswith("/shorts/")
    return False


def is_youtube_playlist_url(ref: str) -> bool:
    """True if `ref` is an http(s) URL on a YouTube watch-page host that
    names a playlist (`?list=...`)."""
    try:
        parsed = urlparse(ref)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host not in _VIDEO_HOSTS:
        return False
    return "list" in parse_qs(parsed.query)
