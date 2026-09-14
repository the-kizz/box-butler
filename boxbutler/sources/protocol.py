"""`SourceProtocol` — the seam between a pasted reference and a library item
(spec §3.5, §10.14).

"It's not only YouTube. The YouTube download is just a tool to get media
into a library. People could add their own media to a library however they
please." (operator, brainstorming). A library is a list of media; ingest
methods are just ways to put media into it. The library, the rotation and
the sink must never care which resolver produced an `Item` — every
resolver here yields the same `ResolvedItem`, and only the resolver
differs.

Identity rule (load-bearing): `source_key` is the idempotency key, scoped
per `ItemKind`. It is what `ItemRepo.add`'s `UNIQUE(library_id, source_key)`
constraint dedupes on, and it is embedded in cache filenames by
`boxbutler.domain.cache_name`. Never derive it from a title — titles get
edited upstream, and two different stories can share one.
"""
from dataclasses import dataclass
from typing import Protocol

from boxbutler.domain.models import ItemKind


@dataclass(frozen=True)
class ResolvedItem:
    kind: ItemKind
    source_ref: str
    source_key: str
    title: str
    seconds: float | None = None
    local_path: str | None = None


class SourceError(Exception):
    """A resolver could not establish an item's identity or match a
    reference. Fail closed here rather than invent a key — an item with a
    guessed identity can silently dedupe against (or fail to dedupe
    against) the wrong thing forever.
    """


class SourceProtocol(Protocol):
    def matches(self, ref: str) -> bool:
        """Whether this source recognises `ref` as something it can
        resolve. Cheap/best-effort: false positives are caught by
        `resolve()` raising; false negatives just mean `Ingestor.add_ref`
        tries the next source."""
        ...

    def resolve(self, ref: str) -> list[ResolvedItem]:
        """Turn `ref` into one or more `ResolvedItem`s. Must not download
        or render audio — that is `FetcherProtocol` / `RendererProtocol`'s
        job (Tasks 13/14). Raises `SourceError` if identity can't be
        established."""
        ...
