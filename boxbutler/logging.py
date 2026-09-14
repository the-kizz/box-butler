"""Structured JSON logging (Task 27; spec §6).

One JSON object per line, to `stdout` by default so `docker logs`/dozzle
can tail it with no extra plumbing: `{"ts": ..., "event": ..., **fields}`.
`ts` and `event` are always the first two keys, in that order, because
`tests/test_logging.py` asserts on key order (and because a human `tail -f
| jq` benefits from the timestamp and event name coming first on every
line regardless of what fields a given event happens to carry).

## Secrets must never reach a log line

This build's binding constraint (see the Task 27 brief) is that a bearer
token or password must never be logged. Two structural defences, not a
convention someone has to remember at each call site:

1. **Key-based redaction.** Every field is inspected recursively (through
   dicts and lists); any key whose name contains a marker like
   `password`, `token`, `secret`, `bearer`, `api_key`, `authorization` or
   `cookie` (case-insensitive) has its value replaced with a fixed
   redaction marker *before* the JSON is built. This catches the common
   case: a caller who plucks a config/settings object apart and passes
   `password=creds.password` (or forwards a whole `dict` from a request)
   never gets that value serialised, because the field name itself is
   the giveaway and the redaction happens centrally, in the one function
   every event passes through, rather than at each of the (many) call
   sites.
2. **Value-based scrubbing for the cases key-based redaction can't see.**
   Key-based redaction (1) only inspects the keys of a `Mapping` — a
   non-mapping object such as `Creds("admin", "hunter2")` has no keys for
   it to look at at all, and would otherwise reach the log line untouched
   via its `repr()`. So every string value (including the repr any
   non-JSON-native value falls back to — see `_sanitise` below) is also
   scanned for value *shapes* that are secrets regardless of what key, if
   any, they were filed under:
   - an `Authorization: Bearer <token>` / bare `Bearer <token>` pattern,
   - credentials embedded in a URL (`scheme://user:pass@host`), and
   - a `<marker>=<value>` / `<marker>: <value>` pair where `<marker>` is
     one of the same sensitive-key markers used in (1) — this is what
     catches `repr(Creds(...))` producing `"Creds(user='admin',
     password='hunter2')"`, since `password=` never appears as an actual
     dict key there.
   Each matched substring is replaced before logging.

Neither pass is a promise that *no* secret can ever reach a log line —
that would require understanding the shape of every future field — but
between them, a secret has to be filed under an innocuous key, formatted
in a way that isn't a bearer token or a credentialed URL, *and* not
written in any `marker=value` shape to slip through, which is a much
narrower gap than "everyone remembers not to log it".
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

# Substring markers (case-insensitive) that mark a field as sensitive by
# its *name* alone, regardless of what layer of the app produced it.
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "bearer",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_header",
    "cookie",
    "private_key",
)

# Value-shaped secrets that can hide inside an otherwise-fine field (most
# commonly a stringified exception).
_BEARER_VALUE_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_URL_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+:[^/\s:@]+@")

# A `<marker>=<value>` / `<marker>: <value>` pair anywhere in a string —
# this is what redacts something like `repr(Creds("admin", "hunter2"))`
# == `"Creds(user='admin', password='hunter2')"`: key-based redaction (1)
# only ever sees `Mapping` keys, and this object has none, so `password`
# never appears as an actual dict key for it to catch. Value is whatever
# sits inside a matching quote pair, or a run of non-separator characters
# if unquoted.
#
# Excludes "authorization" / "auth_header" / "bearer" / "token": those are
# already handled by `_BEARER_VALUE_RE` above (an `Authorization: Bearer
# <token>` header, or a bare `token=Bearer <x>` shape), which runs first —
# including them here would let this regex re-match the literal word
# "Bearer" that `_BEARER_VALUE_RE` deliberately leaves in place and mangle
# it into e.g. `token=***REDACTED***` (losing "Bearer" entirely) instead
# of the intended `token=Bearer ***REDACTED***`.
_KV_SENSITIVE_MARKERS = tuple(
    m for m in _SENSITIVE_KEY_MARKERS
    if m not in ("authorization", "auth_header", "bearer", "token")
)
_SENSITIVE_KV_RE = re.compile(
    r"(?i)\b(" + "|".join(_KV_SENSITIVE_MARKERS) + r")\b(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,)}\]]+)"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _scrub_string(value: str) -> str:
    value = _BEARER_VALUE_RE.sub(f"Bearer {REDACTED}", value)
    value = _URL_CREDENTIALS_RE.sub(f"://{REDACTED}@", value)
    value = _SENSITIVE_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
    return value


def _safe_repr(value: Any) -> str:
    """`repr(value)` that cannot itself raise. A field whose `__repr__`
    (or whatever `repr()` ends up calling) blows up must still produce a
    log line — see `_sanitise`'s fallback branch and the module docstring
    on Critical 3 (Task 27 review): a diagnostic that crashes the thing it
    observes is strictly worse than one that records a degraded
    description of a field."""
    try:
        return repr(value)
    except Exception:
        try:
            return f"<{type(value).__name__} (repr failed)>"
        except Exception:
            return "<unrepresentable value>"


def _sanitise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if _is_sensitive_key(k) else _sanitise(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitise(v) for v in value]
    if isinstance(value, str):
        return _scrub_string(value)
    if value is None or isinstance(value, (int, float, bool)):
        # The remaining JSON-native leaf types; nothing further to do.
        return value
    # Anything else — an exception object, a domain object, a set, a
    # dataclass, whatever a future call site passes without casting to
    # `str` first — is not something `json.dumps` can be trusted to
    # serialise. Falling back to a (scrubbed) repr is deliberate: this is
    # not a case for "raise on the unknown". `log_event` is the
    # diagnostic channel; a diagnostic that destroys the run it was
    # logging about is strictly worse than one that records a degraded
    # description of the offending field. Do not "fix" this back toward
    # raising.
    #
    # The repr still goes through `_scrub_string`, which is *not* a
    # promise that nothing here can leak — only the bearer/URL/`marker=`
    # value shapes it recognises are caught, same as any other string
    # (see the module docstring's "Two structural defences" section). A
    # secret embedded in some other textual shape inside a custom
    # `__repr__` can still slip through; this is a best-effort net, not a
    # guarantee.
    return _scrub_string(_safe_repr(value))


def log_event(
    event: str,
    stream=None,
    clock: Callable[[], datetime] = utcnow,
    **fields: Any,
) -> None:
    """Write one JSON line: `{"ts": <iso seconds Z>, "event": event,
    **fields}`. `ts`/`event` are always first, in that order.  Every field
    is passed through `_sanitise` first (see module docstring) — there is
    no way to call this function and have a field bypass redaction.

    `stream` defaults to the *current* `sys.stdout` at call time, not the
    one that happened to be bound when this module was imported — a
    mutable default (`stream=sys.stdout` in the signature) would freeze
    in whatever `sys.stdout` was at import time, which silently stops
    matching pytest's `capsys` (and anything else that swaps `sys.stdout`
    after import) since the default is evaluated exactly once.
    """
    if stream is None:
        stream = sys.stdout
    ts = clock()
    if ts.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed here; pass a timezone-aware datetime "
            "(e.g. datetime.now(UTC))"
        )
    base: dict[str, Any] = {"ts": ts.isoformat(timespec="seconds"), "event": event}
    # `_sanitise` itself — not just the `json.dumps` below — must be inside
    # this try/except (final review, Minor/3): iterating a hostile or
    # self-referential mapping/list can raise `RuntimeError` /
    # `RecursionError` before `_sanitise` ever returns, and
    # `Orchestrator._event` calls `log_event` with no try/except of its
    # own, so that would take down the run being logged about — the exact
    # failure this module exists to prevent. Degrade to a minimal, still-
    # valid line instead; do not narrow this to "raise louder".
    try:
        record: dict[str, Any] = dict(base)
        sanitised = _sanitise(fields)
        for key, value in sanitised.items():
            if key in ("ts", "event"):
                # A caller-supplied field can never displace ts/event from
                # the first two positions the brief's test asserts on.
                continue
            record[key] = value
        line = json.dumps(record, ensure_ascii=False)
    except Exception:
        # Belt-and-suspenders for `json.dumps` itself too: `_sanitise`
        # already converts anything it can see into JSON-native types, so
        # a `json.dumps` failure here should be unreachable in practice —
        # but "should be unreachable" is exactly the class of assumption
        # this fallback exists to not depend on.
        record = {**base, "log_error": "unserialisable or unsanitisable field"}
        line = json.dumps(record, ensure_ascii=False)
    stream.write(line + "\n")
    stream.flush()


def make_logger(stream=None) -> Callable[[str, dict], None]:
    """Adapter matching `orchestrator.run.Deps.log`'s signature
    (`Callable[[str, dict], None]`) — `Orchestrator._event` calls it as
    `deps.log(event, {...})`, a positional dict, not `**kwargs`.

    `stream=None` (resolved to `sys.stdout` per call inside `log_event`)
    for the same reason `log_event` itself avoids a mutable default —
    `main()` calls `make_logger()` with no stream, and that must still
    write to whatever `sys.stdout` is *at the time each event is
    logged*, not whatever it was when `make_logger` happened to run.
    """

    def _log(event: str, fields: dict) -> None:
        log_event(event, stream=stream, **(fields or {}))

    return _log


__all__ = ["REDACTED", "log_event", "make_logger", "utcnow"]
