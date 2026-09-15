#!/usr/bin/env python3
"""Capture README screenshots of all five screens (Task 34; spec §11 P7, §5).

Starts `boxbutler.web.dev:app` — the dev entrypoint that seeds obviously
fake dashboard data into a throwaway SQLite store and a `FakeSink`,
**never** the real Tonies cloud (see `boxbutler/web/dev.py` and
`boxbutler/web/fake_data.py`) — on a free localhost port in a background
thread, then drives it with Playwright/chromium to capture every screen
at 1280x800 in both `light` and `dark` (`page.emulate_media`), plus the
dashboard at 375x812 for the mobile shot required by spec §5's 375px
contract.

Logs in **once** per browser context and reuses the session for every
screen. Logging in per-screen trips `LoginRateLimiter`
(`boxbutler/web/auth.py`) and silently produces screenshots of the login
page instead of the intended screen — if a captured PNG looks like a
login form, that rate limit is almost certainly why.

Usage::

    .venv/bin/python -m playwright install chromium  # once
    .venv/bin/python scripts/screenshots.py

Writes to `docs/screenshots/`. Never run against a real deployment or
real credentials — this script only ever talks to the in-process dev app
on 127.0.0.1.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "screenshots"

DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
MOBILE_VIEWPORT = {"width": 375, "height": 812}

# (screen name, path template) — "library" is filled in at runtime with
# the id of the first seeded library ("Bedtime"), since library ids are
# generated, not fixed. Kept as literal strings (rather than built with
# str.format at module scope) so tests/test_screenshots_script.py can
# assert their shape by reading this source without importing/running it.
SCREENS = [
    ("dashboard", "/"),
    ("library", "/libraries/{library_id}"),
    ("history", "/history"),
    ("runs", "/runs"),
    ("settings", "/settings"),
]

COLOR_SCHEMES = ["light", "dark"]

# The flow shots: the three screens where a library gets made and filled.
# They are captured separately from SCREENS because two of them need an
# app that has *not* been set up yet (the wizard redirects to / once an
# admin account exists), and because each one is a fragment of a page
# rather than a whole viewport -- a full-page shot of the library screen
# tells you nothing about the "add media" form at the bottom of it.
FLOW_SHOTS = [
    ("setup-library-folder.png", "wizard step 2: the library folder picker"),
    ("create-library.png", "Libraries: the New library form with its folder picker"),
    ("add-media.png", "a library's folder line and the Add to this library form"),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(port: int, timeout_s: float = 15.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"dev app never came up on port {port}")


def _serve(app, port: int):
    """Run `app` on `port` in a daemon thread. Returns (server, thread) so
    the caller can shut it down."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_until_up(port)
    return server, thread


def _capture_flow(playwright) -> None:
    """The three flow shots.

    Builds its own throwaway app for the wizard, because `/setup` is only
    reachable while no admin account exists -- the dev app already has
    one. Both apps are in-process, on localhost, against a `FakeSink`;
    neither can reach a real tonie or a real media directory.
    """
    import tempfile

    from boxbutler.sinks.fake import FakeSink
    from boxbutler.store.db import Store
    from boxbutler.web import dev as dev_module
    from boxbutler.web.app import create_app
    from boxbutler.web.fake_data import FakeRunner
    from boxbutler.web.settings import WebSettings

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- the wizard, on an app with no admin account yet ---------------
    tmp = Path(tempfile.mkdtemp(prefix="bb-"))
    media_root = tmp / "media"
    # Folders a real operator would already have. The picker browsing an
    # empty directory would show the feature doing nothing.
    for name in ("Audiobook", "Car Trips", "Songs"):
        (media_root / name).mkdir(parents=True, exist_ok=True)
    store = Store.open(tmp / "wizard.sqlite")
    sink = FakeSink()
    sink.add_target("fake-amber-07", "Amber Tonie", [])
    # No bootstrap admin: `ensure_admin` would write one and the setup
    # gate would then send /setup straight to /, which is exactly what a
    # real first start does *not* do.
    settings = WebSettings(secret_key="dev", admin_user="", admin_password="", data_dir=tmp)
    wizard_app = create_app(
        store, sink, settings, FakeRunner(store, sink), media_root=media_root
    )
    port = _free_port()
    server, thread = _serve(wizard_app, port)
    browser = playwright.chromium.launch()
    try:
        page = browser.new_context(viewport=DESKTOP_VIEWPORT).new_page()
        page.emulate_media(color_scheme="light")
        page.goto(f"http://127.0.0.1:{port}/setup")
        page.wait_for_load_state("networkidle")
        # Step 1 asks for the tonie account; the FakeSink accepts any
        # credentials, so this is a click-through to reach step 2.
        page.fill("input[name=tonie_username]", "you@example.com")
        page.fill("input[name=tonie_password]", "not-a-real-password")
        page.click("button.setup-next")
        page.wait_for_load_state("networkidle")
        page.locator(".setup-wrap").screenshot(path=str(OUT_DIR / "setup-library-folder.png"))
        print(f"wrote {OUT_DIR / 'setup-library-folder.png'}")
    finally:
        browser.close()
        server.should_exit = True
        thread.join(timeout=10)

    # --- the two library screens, on the seeded dev app ----------------
    dev_store = dev_module._store  # noqa: SLF001 -- dev-only introspection
    dev_settings = dev_module._settings  # noqa: SLF001
    library_id = dev_store.libraries.list()[0].id
    port = _free_port()
    server, thread = _serve(dev_module.app, port)
    base_url = f"http://127.0.0.1:{port}"
    browser = playwright.chromium.launch()
    try:
        page = browser.new_context(viewport=DESKTOP_VIEWPORT).new_page()
        page.emulate_media(color_scheme="light")
        page.goto(f"{base_url}/login")
        page.fill("#username", dev_settings.admin_user)
        page.fill("#password", dev_settings.admin_password)
        page.click("button.login-submit")
        page.wait_for_load_state("networkidle")

        page.goto(f"{base_url}/libraries")
        page.wait_for_load_state("networkidle")
        page.locator(".create-library").screenshot(path=str(OUT_DIR / "create-library.png"))
        print(f"wrote {OUT_DIR / 'create-library.png'}")

        page.goto(f"{base_url}/libraries/{library_id}")
        page.wait_for_load_state("networkidle")
        # The folder line and the add form together: "here is the folder,
        # and here is how things get into it" is one idea, not two.
        top = page.locator(".scan-form").bounding_box()
        bottom = page.locator(".add-item").bounding_box()
        page.screenshot(
            path=str(OUT_DIR / "add-media.png"),
            clip={
                "x": top["x"] - 8,
                "y": top["y"] - 8,
                "width": max(top["width"], bottom["width"]) + 16,
                "height": (bottom["y"] + bottom["height"]) - top["y"] + 16,
            },
        )
        print(f"wrote {OUT_DIR / 'add-media.png'}")
    finally:
        browser.close()
        server.should_exit = True
        thread.join(timeout=10)


def main() -> int:
    # Imported lazily, after sys.path is set up by running this file
    # directly, and so that importing this module for the test never
    # starts a server or touches a browser.
    from playwright.sync_api import sync_playwright

    from boxbutler.web import dev as dev_module

    app = dev_module.app
    store = dev_module._store  # noqa: SLF001 -- dev-only introspection
    settings = dev_module._settings  # noqa: SLF001

    first_library = store.libraries.list()[0]
    library_id = first_library.id

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_until_up(port)

        base_url = f"http://127.0.0.1:{port}"
        OUT_DIR.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context(viewport=DESKTOP_VIEWPORT)
                page = context.new_page()

                # Log in once; every subsequent navigation in this
                # context reuses the session cookie.
                page.goto(f"{base_url}/login")
                page.fill("#username", settings.admin_user)
                page.fill("#password", settings.admin_password)
                page.click("button.login-submit")
                page.wait_for_load_state("networkidle")

                for name, path_template in SCREENS:
                    path = path_template.format(library_id=library_id)
                    for scheme in COLOR_SCHEMES:
                        page.emulate_media(color_scheme=scheme)
                        page.goto(f"{base_url}{path}")
                        page.wait_for_load_state("networkidle")
                        out_path = OUT_DIR / f"{name}-{scheme}.png"
                        page.screenshot(path=str(out_path))
                        print(f"wrote {out_path}")

                # Mobile dashboard shot (light scheme, 375x812 — spec §5).
                mobile_page = context.new_page()
                mobile_page.set_viewport_size(MOBILE_VIEWPORT)
                mobile_page.emulate_media(color_scheme="light")
                mobile_page.goto(base_url + "/")
                mobile_page.wait_for_load_state("networkidle")
                mobile_out = OUT_DIR / "dashboard-mobile.png"
                mobile_page.screenshot(path=str(mobile_out), full_page=True)
                print(f"wrote {mobile_out}")
                mobile_page.close()

                context.close()
            finally:
                browser.close()
            _capture_flow(p)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    return 0


if __name__ == "__main__":
    sys.exit(main())
