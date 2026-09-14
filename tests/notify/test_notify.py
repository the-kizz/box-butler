"""Notifier tests (Task 25; spec §9.1, §10.16).

Every `NtfyNotifier` here is built on `httpx.MockTransport` — **no test in
this file makes a network call**, and `*.invalid` (RFC 2606) stands in for
a real ntfy server throughout.
"""
import httpx

from boxbutler.notify.none import NullNotifier, build_notifier
from boxbutler.notify.ntfy import NtfyNotifier
from boxbutler.notify.protocol import NotifyPolicy, ascii_title


def capture():
    posted = []

    def handler(req):
        posted.append((str(req.url), dict(req.headers), req.content.decode()))
        return httpx.Response(200)

    return posted, httpx.Client(transport=httpx.MockTransport(handler))


def test_failure_notifies_by_default_success_and_debug_do_not():
    posted, c = capture()
    n = NtfyNotifier("https://ntfy.example.invalid", "bb", None, NotifyPolicy(), c)
    n.failure("Tonie may be empty", "Green Tonie DEGRADED")
    n.success("ok", "x")
    n.debug("plan", "y")
    assert len(posted) == 1 and posted[0][1]["priority"] == "high"


def test_success_and_debug_when_enabled():
    posted, c = capture()
    n = NtfyNotifier("https://ntfy.example.invalid", "bb", "tok", NotifyPolicy(True, True, True), c)
    n.success("ok", "x")
    n.debug("plan", "y")
    assert len(posted) == 2 and posted[0][1]["authorization"] == "Bearer tok"


def test_titles_are_ascii_even_when_story_title_was_not():
    posted, c = capture()
    n = NtfyNotifier("https://ntfy.example.invalid", "bb", None, NotifyPolicy(), c)
    n.failure("Green Tonie now has The Spaghetti Yeti \N{SPAGHETTI} — Café", "body \N{SPAGHETTI} may stay unicode")
    title = posted[0][1]["title"]
    assert title.isascii() and "Spaghetti Yeti" in title and "Cafe" in title
    assert ascii_title("\N{SPAGHETTI}\N{SPAGHETTI}") == "Box Butler"


def test_kind_none_sends_nothing():
    n = build_notifier("none", None, None, None, NotifyPolicy(True, True, True))
    n.failure("a", "b")
    assert isinstance(n, NullNotifier) and len(n.calls) == 1


def test_network_error_is_swallowed():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    NtfyNotifier("https://ntfy.example.invalid", "bb", None, NotifyPolicy(), c).failure("t", "b")   # no raise


def test_incomplete_ntfy_config_is_a_visible_noop_not_a_silent_one():
    """The Settings trap's flip side (spec §9.1, brief note): `kind: ntfy`
    with no server/topic configured must still record that a notification
    was due, never silently vanish."""
    n = build_notifier("ntfy", "", "", None, NotifyPolicy(True, True, True))
    n.failure("a", "b")
    assert isinstance(n, NullNotifier) and n.calls == [("failure", "a", "b")]


def test_token_never_appears_in_sent_or_calls():
    posted, c = capture()
    n = NtfyNotifier("https://ntfy.example.invalid", "bb", "super-secret-token", NotifyPolicy(), c)
    n.failure("t", "b")
    assert "super-secret-token" not in repr(n.sent)
    assert posted[0][1]["authorization"] == "Bearer super-secret-token"
