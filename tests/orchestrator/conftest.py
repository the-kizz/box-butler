"""Re-exports for the run loop's fixtures (Task 21; promoted in Task 27).

`world` (and the helpers built around it) now live in `tests/conftest.py`
so `tests/cli/` can see the same fixture — pytest only shares fixtures
down a conftest's own directory tree, and `tests/cli` and
`tests/orchestrator` are siblings, not one inside the other. See
`tests/conftest.py`'s "Promoted from tests/orchestrator/conftest.py"
section for the fixture itself (moved verbatim, no behaviour change).

This file stays only so `from tests.orchestrator.conftest import events`
(and `source_cache_name`, `rendition_cache_name`, `mutating_calls`) keeps
working for the several test modules in this directory that import those
names directly as functions, not as fixtures. `world` itself needs no
re-export here: as a fixture defined in an ancestor conftest, pytest
already makes it visible to every test under `tests/orchestrator/`.
"""
from __future__ import annotations

from tests.conftest import (  # noqa: F401
    CAP,
    SOURCE_SECONDS,
    events,
    mutating_calls,
    rendition_cache_name,
    source_cache_name,
)
