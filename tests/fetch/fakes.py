"""`FakeFetcher` — the test double for `FetcherProtocol` used by every
layer above the fetch layer (Tasks 14+), so their tests never touch the
network either.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from boxbutler.domain.cache_name import cache_name
from boxbutler.domain.models import Item, ItemKind


class FakeFetcher:
    def __init__(
        self,
        files: dict[str, Path] | None = None,
        fail: dict[str, Exception] | None = None,
    ):
        self.files = files or {}
        self.fail = fail or {}
        self.calls: list[str] = []

    def fetch(self, item: Item, cache_dir: Path) -> Path:
        self.calls.append(item.id)
        if item.id in self.fail:
            raise self.fail[item.id]
        src = self.files[item.id]
        target = cache_dir / cache_name(item.title, item.source_key, src.suffix.lstrip(".") or "bin")
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        return target

    def supports(self, kind: ItemKind) -> bool:
        return True
