"""Plain-HTTP fetcher (spec §3.5) — podcast RSS enclosures and direct audio
URLs. No JS challenge, no SABR, no format pinning: "cheaper and far more
robust than YouTube" per the spec, and the preferred path when the content
exists as a feed.

## A short body is not a complete download (final safety review, M2)

This fetcher used to stream the response to a `.part` file and
`os.replace()` it into place without ever comparing what it wrote against
what the server said it was sending. A truncated body — a CDN that closes
mid-stream, a proxy that drops the connection on a response with no
`Content-Length` to make httpx complain about — therefore landed in the
cache as an ordinary, successful fetch. Worse, it stayed: the cache-hit
check below is `exists() and st_size > 0`, so every later run reused the
short file, and the rendition row re-verified against its *own* probe
forever. The store said 3600 s, 900 s was on disk, and the run reported
success.

So: when the server states a `Content-Length`, the bytes written must match
it exactly or the fetch raises `ItemUnavailable` and the partial file is
removed — nothing incomplete is ever promoted into the cache, which is what
makes a cache hit trustworthy. When the server states nothing (an HTTP/1.0
or connection-close-delimited response), that is an *unknown*, not an
agreement: it is recorded as such on the instance (`last_length_unverified`)
and the only remaining independent check on that item's completeness is the
feed's own `<itunes:duration>` cross-check in
`Orchestrator._probe_source`.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from boxbutler.domain.cache_name import cache_name
from boxbutler.domain.models import Item, ItemKind
from boxbutler.fetch.protocol import ItemUnavailable

_CONTENT_TYPE_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/x-ms-wma": "wma",
}


def _content_length(value: str | None, content_encoding: str | None = None) -> int | None:
    """The number of bytes we should end up having written, or `None` when
    the server did not state a usable one. A malformed or negative header is
    `None` (unknown), never 0 (which would read as "an empty body is
    correct") — final safety review, M2.

    A compressed response is also `None`: `Content-Length` then describes the
    *encoded* body while httpx hands us the decoded bytes, so the two are not
    comparable and pretending otherwise would turn a correct download into a
    spurious short-read failure. Unknown stays unknown.
    """
    if content_encoding and content_encoding.strip().lower() not in ("", "identity"):
        return None
    if value is None:
        return None
    try:
        length = int(value.strip())
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _guess_ext(url: str, content_type: str | None) -> str:
    suffix = Path(urlsplit(url).path).suffix.lstrip(".")
    if suffix:
        return suffix
    if content_type:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type in _CONTENT_TYPE_EXT:
            return _CONTENT_TYPE_EXT[media_type]
    return "bin"


class HttpFetcher:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client()
        #: Set by `fetch` to record whether the last completed download
        #: could be checked against a stated `Content-Length`. `True` means
        #: the server gave no length and the byte count proved nothing — an
        #: unknown kept as an unknown (see the module docstring), never
        #: presented as a verified complete file.
        self.last_length_unverified: bool = False

    def supports(self, kind: ItemKind) -> bool:
        return kind in (ItemKind.RSS, ItemKind.URL)

    def fetch(self, item: Item, cache_dir: Path) -> Path:
        # Extension isn't known until we've at least seen headers, so probe
        # for an existing cache hit by URL suffix first, then fall back to
        # a HEAD-less GET and settle the real name from the response.
        url_ext = Path(urlsplit(item.source_ref).path).suffix.lstrip(".")
        if url_ext:
            candidate = cache_dir / cache_name(item.title, item.source_key, url_ext)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate

        try:
            with self._client.stream("GET", item.source_ref) as response:
                if response.status_code >= 400:
                    raise ItemUnavailable(
                        f"HTTP {response.status_code} fetching {item.source_ref!r}"
                    )
                ext = _guess_ext(item.source_ref, response.headers.get("content-type"))
                target = cache_dir / cache_name(item.title, item.source_key, ext)
                if target.exists() and target.stat().st_size > 0:
                    return target
                expected = _content_length(
                    response.headers.get("content-length"),
                    response.headers.get("content-encoding"),
                )
                self.last_length_unverified = expected is None
                part = target.with_suffix(target.suffix + ".part")
                cache_dir.mkdir(parents=True, exist_ok=True)
                written = 0
                with open(part, "wb") as f:
                    for chunk in response.iter_bytes():
                        written += f.write(chunk)
                if expected is not None and written != expected:
                    # A short (or over-long) body is an incomplete download,
                    # whatever the status code said. Refuse it, and take the
                    # partial file with it: promoting it would make every
                    # later run a cache hit on a truncated file, and
                    # `verify_rendition` cannot catch that — its expectation
                    # descends from this same file (M2).
                    part.unlink(missing_ok=True)
                    raise ItemUnavailable(
                        f"short read fetching {item.source_ref!r}: wrote {written} bytes, "
                        f"Content-Length said {expected}"
                    )
                os.replace(part, target)
                return target
        except httpx.HTTPError as exc:
            raise ItemUnavailable(f"error fetching {item.source_ref!r}: {exc}") from exc
