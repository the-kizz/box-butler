"""The visible no-op (Task 25; spec §9.1).

Settings already hit this trap once: `notify_on_failure` ticked while
`notify_kind` was `"none"` — a control that reads as "on" but sends
nothing. That was fixed on the Settings screen by surfacing "Failure
alerts: on, but nothing will be sent — no notification server is
configured" (`boxbutler/web/routes/settings.py`).

`NullNotifier` is this module's side of the same contract: it never raises
and never contacts a network, but it also never pretends to be silent
because nothing is wrong. Every call is recorded in `.calls` as
`(kind, title, body)` so a caller (or a test) can always tell the
difference between "quiet because healthy" and "quiet because
unconfigured" — Task 26/39's history and health surfaces are expected to
read `.calls` (or an equivalent run-event) rather than infer silence from
the absence of any outbound request.

`build_notifier` is the one place `kind` is interpreted: `"ntfy"` with a
complete config (`server` and `topic` both set) builds a real
`NtfyNotifier`; `"none"`, an unrecognised kind, or an incomplete `"ntfy"`
config (missing server/topic) all fall back to `NullNotifier` — visibly
recording that a notification was due, never a silent drop.
"""
from __future__ import annotations

from .ntfy import NtfyNotifier
from .protocol import Notifier, NotifyPolicy


class NullNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def failure(self, title: str, body: str) -> None:
        self.calls.append(("failure", title, body))

    def success(self, title: str, body: str) -> None:
        self.calls.append(("success", title, body))

    def debug(self, title: str, body: str) -> None:
        self.calls.append(("debug", title, body))


def build_notifier(
    kind: str,
    server: str | None,
    topic: str | None,
    token: str | None,
    policy: NotifyPolicy,
) -> Notifier:
    if kind == "ntfy" and server and topic:
        return NtfyNotifier(server, topic, token, policy)
    return NullNotifier()
