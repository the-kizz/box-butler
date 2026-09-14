"""The `Notifier` protocol and the three-class policy (Task 25; spec §9.1).

`NotifyPolicy` mirrors the Settings screen's `notify_on_failure` /
`notify_on_success` / `notify_on_debug` keys (`boxbutler/web/routes/settings.py`
`_Values`) exactly, field for field, so a caller never needs a second
translation layer between what the operator ticked and what a `Notifier`
enforces. `on_failure` defaults on (spec: "the one that matters" — a run
broke, or a tonie is `DEGRADED` and may be empty); the other two default off.

## `ascii_title` and the ntfy title-drop defect

The estate's Alertmanager -> ntfy bridge encodes the `Title` header as
latin-1 and **silently drops anything outside ASCII** (infra §13.2a). Story
titles routinely carry emoji or accents, so anything that reaches a `Title`
header must go through `ascii_title`, never a raw title. It is deliberately
*not* the same thing as calling `sanitise_title` and hoping the result is
ASCII: `sanitise_title` already replaces non-ASCII code points with spaces
and collapses whitespace, so its output is always ASCII already — the extra
`encode("ascii", "ignore")` pass is a second, cheap belt-and-braces line, not
the interesting part.

The interesting part is the fallback. `sanitise_title("")` (or a string that
is *entirely* non-ASCII, e.g. two emoji) already has its own fallback,
`"untitled"` — a sensible filename, a poor alert title ("untitled" tells the
operator nothing about which of many tonies broke). `ascii_title` treats
that exact sentinel as "sanitisation produced nothing usable" and falls back
again, to `"Box Butler"`, so an alert with no salvageable title still names
the product rather than showing a generic filename stem.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from boxbutler.domain.cache_name import sanitise_title

_EMPTY_SENTINEL = "untitled"  # sanitise_title's own fallback for "nothing survived"
FALLBACK_TITLE = "Box Butler"


@dataclass
class NotifyPolicy:
    on_failure: bool = True
    on_success: bool = False
    on_debug: bool = False


class Notifier(Protocol):
    def failure(self, title: str, body: str) -> None: ...
    def success(self, title: str, body: str) -> None: ...
    def debug(self, title: str, body: str) -> None: ...


def ascii_title(s: str) -> str:
    """A `Title` header that can never be dropped by a latin-1-encoding
    relay: `sanitise_title()` (which already maps every non-ASCII code point
    to a space), then a defensive `encode("ascii", "ignore")`, then fall back
    to `"Box Butler"` if nothing survives sanitisation at all.
    """
    t = sanitise_title(s)
    if t == _EMPTY_SENTINEL:
        return FALLBACK_TITLE
    ascii_t = t.encode("ascii", "ignore").decode("ascii").strip()
    return ascii_t or FALLBACK_TITLE
