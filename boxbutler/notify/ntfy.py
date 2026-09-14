"""ntfy-backed `Notifier` (Task 25; spec §9.1, §10.16).

Two invariants, both load-bearing:

1. **The title is always run through `ascii_title`** — never the raw
   caller-supplied title — because the estate's Alertmanager -> ntfy bridge
   silently drops a non-ASCII `Title` header (infra §13.2a). The body is
   left as-is: ntfy's body is UTF-8 clean, only the header is the trap.
2. **A notification failure must never fail a run.** `_send` catches every
   `httpx` transport/HTTP-status error, logs it (never the token — see
   below), and returns. There is nothing here for a caller to catch.

## The token is never logged

`self._token` is used exactly once, to build the `Authorization` header
dict passed straight to `httpx`. Nothing in this module ever calls
`str()`/`repr()` on that header dict, logs `self._token`, or includes it in
an exception message: the `except` branch below logs the exception object
from `httpx` (which carries the failing request's method and URL, not its
headers) and the notification kind, nothing else. `sent` — the list tests
inspect — is only ever appended to on a *successful* send and only stores
the (already-ASCII) title, body and priority, never headers.
"""
from __future__ import annotations

import logging

import httpx

from .protocol import NotifyPolicy, ascii_title

logger = logging.getLogger(__name__)


class NtfyNotifier:
    def __init__(
        self,
        server: str,
        topic: str,
        token: str | None,
        policy: NotifyPolicy,
        client: httpx.Client | None = None,
    ) -> None:
        self._server = server.rstrip("/")
        self._topic = topic
        self._token = token
        self._policy = policy
        self._client = client or httpx.Client(timeout=5.0)
        self.sent: list[dict] = []   # for tests: what was actually posted

    def failure(self, title: str, body: str) -> None:
        if self._policy.on_failure:
            self._send(title, body, priority="high", tags="warning")

    def success(self, title: str, body: str) -> None:
        if self._policy.on_success:
            self._send(title, body, priority="default", tags="tada")

    def debug(self, title: str, body: str) -> None:
        if self._policy.on_debug:
            self._send(title, body, priority="default", tags="mag")

    def _send(self, title: str, body: str, *, priority: str, tags: str) -> None:
        safe_title = ascii_title(title)
        headers = {"Title": safe_title, "Priority": priority, "Tags": tags}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._client.post(
                f"{self._server}/{self._topic}",
                content=body.encode("utf-8"),
                headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # Logged without the token: `e` is httpx's own exception, which
            # never carries request headers in its message, and this branch
            # never touches `self._token` or `headers` directly.
            logger.warning("ntfy notification failed (kind=%s): %s", tags, e)
            return
        self.sent.append({"title": safe_title, "body": body, "priority": priority})
