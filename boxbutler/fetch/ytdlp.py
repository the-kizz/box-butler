"""yt-dlp fetcher (spec §3.5.1) — the proven YouTube ingest path.

Prototype findings, not to be rediscovered (do not "fix" these):

- `yt-dlp-ejs` (PyPI) supplies the JS challenge solver, pinned and
  auditable rather than `--remote-components ejs:github` fetching and
  executing one from GitHub at runtime. It needs a JS runtime, passed as
  `--js-runtimes node`.
- `-f 140` is pinned (AAC 129k 44.1 kHz stereo). `-f bestaudio` fails under
  YouTube's SABR-only experiment: formats come back with missing URLs and
  it errors `Requested format is not available`, while `-F` lists them
  fine. 140 is measured to work; 251 (opus) is not — the pin stays (R12).
- yt-dlp skips an already-downloaded file by name, which is free cache
  idempotency built around `boxbutler/domain/cache_name.py`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from boxbutler.domain.cache_name import cache_name, parse_cache_name, sanitise_title
from boxbutler.domain.models import Item, ItemKind
from boxbutler.fetch.protocol import ExtractionBroken, FetchError, ItemUnavailable

YT_FORMAT = "140"

# Substrings in yt-dlp stderr that mean "this one item", not "the extractor
# is broken" (spec §3.5.1). Everything else defaults to ExtractionBroken —
# it is safer to over-alert on a new message than to silently skip every
# item in the run because we didn't recognise the failure.
_ITEM_UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "this video has been removed",
    "not available in your country",
)


def ytdlp_args(url: str, out_template: str, js_runtime: str = "node") -> list[str]:
    return [
        "yt-dlp",
        "-f", YT_FORMAT,
        "--js-runtimes", js_runtime,
        "--no-playlist",
        "--no-overwrites",
        "-o", out_template,
        # `--` terminates option parsing: `url` can never be read as an
        # option here even if a hostile value slipped past the source
        # layer's validation (final review, Major 1 — defence in depth,
        # the other half of the fix lives in boxbutler/sources/).
        "--", url,
    ]


def classify_ytdlp_failure(stderr: str) -> type[FetchError]:
    low = stderr.lower()
    for marker in _ITEM_UNAVAILABLE_MARKERS:
        if marker in low:
            return ItemUnavailable
    return ExtractionBroken


class YtDlpFetcher:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        js_runtime: str = "node",
    ):
        self._runner = runner
        self._js_runtime = js_runtime

    def supports(self, kind: ItemKind) -> bool:
        return kind in (ItemKind.YOUTUBE, ItemKind.PLAYLIST_ENTRY)

    def fetch(self, item: Item, cache_dir: Path) -> Path:
        target = cache_dir / cache_name(item.title, item.source_key, "m4a")
        if target.exists() and target.stat().st_size > 0:
            return target

        out_template = str(
            cache_dir / f"{sanitise_title(item.title)} [{item.source_key}].%(ext)s"
        )
        args = ytdlp_args(item.source_ref, out_template, self._js_runtime)
        result = self._runner(args, capture_output=True, text=True)

        if result.returncode != 0:
            stderr = result.stderr or ""
            raise classify_ytdlp_failure(stderr)(stderr[-500:])

        # Not `cache_dir.glob(...)` with the key interpolated directly: glob
        # treats `[` / `]` as a character-class, not a literal bracket, so
        # a pattern like `* [abc].*` silently matches nothing.
        #
        # A name must also *parse* as a real cache name via
        # `parse_cache_name` before it counts as a match. yt-dlp writes
        # `<name>.part` (and sometimes `.ytdl`) while a download is in
        # progress and only renames atomically on completion — but relying
        # on that rename as the *only* defence is too thin here: a
        # half-downloaded file mistaken for a finished one means a
        # half-length story lands on a child's tonie while the run reports
        # success. Requiring a parse excludes `.part`/`.ytdl` (they don't
        # match the `<title> [<key>].<ext>` shape) and any other stray
        # artifact in the cache directory, not just the ones we thought of.
        matches = []
        for p in cache_dir.iterdir():
            if not p.is_file() or p.stat().st_size == 0:
                continue
            try:
                _title, key, _ext = parse_cache_name(p.name)
            except ValueError:
                continue
            if key == item.source_key:
                matches.append(p)
        matches.sort(key=lambda p: p.stat().st_mtime)
        if not matches:
            raise ExtractionBroken("yt-dlp exited 0 but produced no file")
        return matches[-1]
