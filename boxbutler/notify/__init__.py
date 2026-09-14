"""Notifications (Task 25; spec §9.1, §10.16).

Configurable, not hardcoded — a public install has its own ntfy server or
none at all. Three independently-switchable classes (`on_failure` default
on, `on_success` / `on_debug` default off), ASCII-safe titles
(`ascii_title`), and a visible no-op (`NullNotifier`) rather than a silent
one when nothing is configured.
"""
from .none import NullNotifier, build_notifier
from .ntfy import NtfyNotifier
from .protocol import Notifier, NotifyPolicy, ascii_title

__all__ = [
    "NotifyPolicy",
    "Notifier",
    "ascii_title",
    "NtfyNotifier",
    "NullNotifier",
    "build_notifier",
]
