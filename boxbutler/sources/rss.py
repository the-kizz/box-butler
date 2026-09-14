"""Podcast RSS resolver (spec §3.5).

Preferred over YouTube where the content exists as a feed: no JS
challenge, no SABR, no format pinning, stable enclosure URLs — and it
matters in practice, not just in theory, since YouTube extraction broke
twice on the prototype's first night.

`source_key` = `sha1(guid or enclosure-url)[:16]` (review round 1,
Critical): the raw guid/URL is hashed, never stored verbatim as the key.
`<guid>` is very commonly a full URL (`isPermaLink="true"` is the RSS spec
default) and the no-guid fallback is *always* a URL, so an unhashed key
routinely contains `/` and `.`. `cache_name()`/`parse_cache_name()` treat
the bracketed `[<key>]` segment as an opaque token with no `/` in it — an
unhashed key breaks that round trip (`parse_cache_name` raises
`ValueError`), which means Task 13's fetcher never recognises its own
cached file as a hit and silently re-downloads on every run, and a raw `/`
in the key is a literal path-separator collision when the fetcher writes
the file. Task 16 has no test that calls `cache_name` on an RSS key (that
integration only happens in Task 13's fetcher), which is exactly how two
correct-in-isolation decisions combined into a silent failure — see
`test_rss_key_round_trips_through_cache_name` below for the regression
guard, and `tests/sources/test_resolvers.py::test_all_resolver_keys_round_trip_cache_name`
for the same invariant applied to every resolver so it can't recur
elsewhere.

`source_ref` stays the enclosure URL (not the guid) — it's what
`HttpFetcher.fetch()` actually downloads from `item.source_ref`, so it
must remain a fetchable URL. The hash is deterministic, so re-resolving
the same feed later reproduces the same key from the same guid and still
dedupes against the existing `Item` via `UNIQUE(library_id, source_key)`;
nothing about the raw guid needs to be retained separately for that to
keep working.

Parsed with `xml.etree.ElementTree` per the brief; no feed-parsing
dependency added for something this shape-constrained (an `<item>` with an
audio `<enclosure>`).

`matches()`/`resolve()` split (review round 1, Important): `matches()` is
now a syntactic, no-I/O check ("this is an http(s) URL") rather than
fetching the URL to sniff its root tag. Fetching twice per feed (once in
`matches()`, again in `resolve()`) doubled the round trips for no benefit
and let the two calls disagree if the feed changed in between. Because
`matches()` is now a blanket "any http(s) URL", **`RssSource` must be
ordered last** among URL-based sources in whatever list is handed to
`Ingestor` — it exists to catch what no cheaper, more specific matcher
(`YouTubeUrlSource`, `YouTubePlaylistSource`, `DirectUrlSource`) claimed
first. `resolve()` does the only network fetch and is the only place that
can determine "not actually a feed", raising `SourceError` in that case.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

import hashlib

import httpx

from boxbutler.domain.models import ItemKind
from boxbutler.sources.protocol import ResolvedItem, SourceError

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ITUNES_DURATION = f"{{{_ITUNES_NS}}}duration"


def _key_for(raw: str) -> str:
    """Hash a raw guid/URL into a `source_key` safe for `cache_name()`
    (see module docstring, review round 1 Critical)."""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_itunes_duration(text: str) -> float | None:
    """`<itunes:duration>` is either plain seconds ("125") or
    `HH:MM:SS`/`MM:SS`. Returns None rather than raising on anything else
    — a malformed duration shouldn't break ingest of an otherwise-good
    entry, it should just leave `seconds` unknown.
    """
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds


class RssSource:
    def __init__(self, client: httpx.Client):
        self._client = client

    def matches(self, ref: str) -> bool:
        # Syntactic only, no I/O (review round 1, Important): this is the
        # catch-all for "some http(s) URL nothing more specific claimed".
        # See the module docstring for why RssSource must therefore be
        # ordered last among URL-based sources.
        return ref.startswith(("http://", "https://"))

    def resolve(self, ref: str) -> list[ResolvedItem]:
        try:
            resp = self._client.get(ref)
        except httpx.HTTPError as exc:
            raise SourceError(f"error fetching feed {ref!r}: {exc}") from exc
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} fetching {ref!r}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise SourceError(f"not a parseable feed: {exc}") from exc
        tag = root.tag.rsplit("}", 1)[-1]
        if tag not in ("rss", "feed"):
            raise SourceError(f"not a recognisable RSS/Atom feed (root tag {root.tag!r})")

        items: list[ResolvedItem] = []
        for item_el in root.iter("item"):
            enclosure = None
            for enc in item_el.findall("enclosure"):
                # Minor (review round 1): an enclosure with a missing or
                # empty `type` is skipped here along with genuinely
                # non-audio ones. Real feeds sometimes omit `type` on an
                # audio enclosure. This is a deliberate, narrow choice for
                # now — a missing type is ambiguous, and skipping is the
                # fail-closed side of that ambiguity per this task's
                # identity rule — not yet revisited with a content-type
                # sniff or extension fallback.
                if enc.get("type", "").lower().startswith("audio/"):
                    enclosure = enc
                    break
            if enclosure is None:
                continue
            url = enclosure.get("url")
            if not url:
                continue

            guid_el = item_el.find("guid")
            guid_text = guid_el.text.strip() if guid_el is not None and guid_el.text else ""
            key = _key_for(guid_text or url)

            title_el = item_el.find("title")
            title = title_el.text.strip() if title_el is not None and title_el.text else url

            duration_el = item_el.find(_ITUNES_DURATION)
            seconds = (
                _parse_itunes_duration(duration_el.text)
                if duration_el is not None and duration_el.text
                else None
            )

            items.append(ResolvedItem(ItemKind.RSS, url, key, title, seconds))
        return items
