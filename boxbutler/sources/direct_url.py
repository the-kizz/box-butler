"""Direct-audio-URL resolver (spec §3.5) — the "someone pasted a link to an
mp3" ingest method, distinct from RSS (a feed of many) and YouTube (needs
extraction).

`source_key` = `sha1(url)[:16]`. A direct URL has no feed-supplied guid and
no platform-assigned id to key off, so we mint one from the one thing that
is genuinely stable about it: the URL string itself. (If the operator later
moves the file to a new URL, that's a new item — this resolver has no way
to know it's "the same" story, and inventing that link would be exactly
the kind of guessed identity Task 15's NaN lesson warns against.)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from boxbutler.domain.models import ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError

_AUDIO_EXTS = {
    "mp3", "m4a", "m4b", "aac", "flac", "wav", "ogg", "oga", "opus", "wma", "aif", "aiff",
}


def _ext(ref: str) -> str:
    return Path(urlsplit(ref).path).suffix.lstrip(".").lower()


class DirectUrlSource:
    def __init__(self, client: httpx.Client):
        self._client = client

    def matches(self, ref: str) -> bool:
        if not ref.startswith(("http://", "https://")):
            return False
        if _ext(ref) in _AUDIO_EXTS:
            return True
        try:
            resp = self._client.head(ref)
        except httpx.HTTPError:
            return False
        content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return content_type.startswith("audio/")

    def resolve(self, ref: str) -> list[ResolvedItem]:
        if not self.matches(ref):
            raise SourceError(f"not recognisable as a direct audio URL: {ref!r}")
        key = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:16]
        title = Path(urlsplit(ref).path).stem or ref
        return [ResolvedItem(ItemKind.URL, ref, key, title)]
