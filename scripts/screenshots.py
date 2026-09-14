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
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    return 0


if __name__ == "__main__":
    sys.exit(main())
