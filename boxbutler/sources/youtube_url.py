"""Single-video YouTube resolver (spec §3.5).

`source_key` = the YouTube video id (`info["id"]`). It is YouTube's own
stable identifier for the video — unaffected by title edits, thumbnail
changes, or the URL's query-string shape (`watch?v=`, `youtu.be/`,
`shorts/` all name the same id) — so it is a safe idempotency key.

Does not download; only runs yt-dlp's metadata-only JSON dump. Task 13's
`YtDlpFetcher` does the actual fetch, keyed on the same id.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from boxbutler.domain.models import ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError
from boxbutler.sources.youtube_hosts import is_youtube_video_url


class YouTubeUrlSource:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._runner = runner

    def matches(self, ref: str) -> bool:
        # Anchored: `ref` must actually parse as an http(s) URL whose host
        # is a real YouTube host, not merely contain a YouTube-looking
        # substring somewhere inside it. An unanchored `re.search` let a
        # value like `--exec=... youtu.be/xxxxxxxxxxx` (a yt-dlp *option*,
        # not a URL) pass validation and later land in argv where yt-dlp
        # read it as `--exec=...` — arbitrary command execution (final
        # review, Major 1). `urlparse` also means a value beginning with
        # `-` can never match here: it has no scheme, so it's rejected
        # before the host is even looked at.
        return is_youtube_video_url(ref)

    def resolve(self, ref: str) -> list[ResolvedItem]:
        args = [
            "yt-dlp", "--dump-single-json", "--no-playlist",
            "--js-runtimes", "node",
            # `--` terminates option parsing so nothing after it can ever
            # be read as an option, even if `matches()`'s validation is
            # later loosened or `resolve()` is called directly with an
            # unvalidated ref (final review, Major 1).
            "--", ref,
        ]
        result = self._runner(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise SourceError((result.stderr or "yt-dlp failed")[-500:])
        try:
            info = json.loads(result.stdout)
            video_id = info["id"]
            title = info["title"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise SourceError(f"yt-dlp returned unusable metadata: {exc}") from exc
        return [ResolvedItem(ItemKind.YOUTUBE, ref, video_id, title, info.get("duration"))]
