"""Minimal web settings (Task 8). The full config.py arrives in Task 29.

`admin_user` / `admin_password` are bootstrap-only: they seed the argon2
hash stored in `store.settings` on first start (see `auth.ensure_admin`)
and are not read again after that — the UI-changeable password lives in
the store, not in these env-derived settings.

Both default to `None` (Task 36): a container started against an empty
`/data` with no `BOXBUTLER_ADMIN_USER`/`PASSWORD` env vars set has no
admin account yet, and `boxbutler.web.app.create_app`'s setup-gate
middleware sends every route except `/setup` and static assets to the
first-run wizard until one is created — either this way (env vars) or by
finishing that wizard. `ensure_admin` only bootstraps from these fields
when both are present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WebSettings:
    secret_key: str
    admin_user: str | None = None
    admin_password: str | None = None  # bootstrap only (spec §5)
    secure_cookies: bool = False
    login_rate_limit: int = 5
    login_rate_window_s: int = 300
    data_dir: Path = field(default_factory=lambda: Path("/data"))
