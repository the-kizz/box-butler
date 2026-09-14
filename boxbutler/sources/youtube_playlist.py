"""YouTube playlist resolver (spec §3.5).

Expands a playlist reference into one `ResolvedItem` per entry, in
upstream order — the order a rotation should present them in by default.
`source_key` = each entry's own video id (`entry["id"]`), same identity
space as `YouTubeUrlSource`: a video that is both a playlist entry and
later added by direct URL dedupes on the same key.

Uses `--flat-playlist` so this is a single cheap metadata call regardless
of playlist length — no per-video extraction here. Task 18 re-resolves a
recorded playlist reference (`Ingestor.add_ref` records it under
`playlists:<library_id>`) to pick up new/removed entries later.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from boxbutler.domain.models import ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError
from boxbutler.sources.youtube_hosts import is_youtube_playlist_url


class YouTubePlaylistSource:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._runner = runner

    def matches(self, ref: str) -> bool:
        # Anchored: same "unanchored substring" hole as YouTubeUrlSource
        # (final review, Major 1) — `"list=" in ref` matched a yt-dlp
        # option string that merely contained `list=` anywhere, e.g.
        # `--exec=... &list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Require a
        # real http(s) URL on a YouTube watch-page host with a `list`
        # query param instead.
        return is_youtube_playlist_url(ref)

    def resolve(self, ref: str) -> list[ResolvedItem]:
        # `--` terminates option parsing so nothing after it can be read
        # as an option even if `matches()` is loosened later or `resolve`
        # is called directly with an unvalidated ref (final review, Major 1).
        args = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--", ref]
        result = self._runner(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise SourceError((result.stderr or "yt-dlp failed")[-500:])
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError(f"yt-dlp returned unusable metadata: {exc}") from exc
        items = []
        for entry in info.get("entries") or []:
            try:
                entry_id = entry["id"]
                title = entry["title"]
            except KeyError as exc:
                raise SourceError(f"playlist entry missing {exc}; refusing to guess identity") from exc
            entry_url = entry.get("url") or ref
            items.append(ResolvedItem(ItemKind.PLAYLIST_ENTRY, entry_url, entry_id, title, entry.get("duration")))
        return items
