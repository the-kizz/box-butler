"""`FetcherProtocol` — the seam between the ingest pipeline and the outside
world (spec §2 step 2, §3.5).

Nothing above this layer may know whether audio came from yt-dlp, HTTP, or
a local file: everything talks to a `FetcherProtocol.fetch()` that returns
a cached `Path`.

Failure classification (spec §3.5.1) matters as much as success:

- `ExtractionBroken` — yt-dlp or the solver itself is the problem. It will
  hit every item, so it is worth alerting on and worth stopping the run's
  fetch attempts early.
- `ItemUnavailable` — this one item is private, deleted or geo-blocked.
  Skip to the next item in the queue and carry on.

Conflating the two means either alerting on a single dead video, or
silently skipping every item when the extractor breaks.
"""
from pathlib import Path
from typing import Protocol

from boxbutler.domain.models import Item, ItemKind


class FetchError(Exception):
    """Base class for every fetch failure."""


class ExtractionBroken(FetchError):
    """yt-dlp / solver broke — will hit every item; alert on it."""


class ItemUnavailable(FetchError):
    """This one item is private/deleted/geo-blocked — skip it."""


class FetcherProtocol(Protocol):
    def fetch(self, item: Item, cache_dir: Path) -> Path:
        """Return the cached source file for `item`, fetching it if needed.

        Idempotent by `cache_name` — an existing cached file is returned
        without re-fetching.
        """
        ...

    def supports(self, kind: ItemKind) -> bool:
        """Whether this fetcher can handle items of `kind`."""
        ...
