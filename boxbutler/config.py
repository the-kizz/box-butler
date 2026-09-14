"""Configuration — env + YAML in, a typed `Settings` out (Task 29; spec §7,
§3.1, §11 P5).

Two rules this module exists to enforce structurally, not by convention:

1. **Secrets are env-only, never YAML.** `sink_user`/`sink_password`/
   `admin_user`/`admin_password`/`secret_key`/`notify_token` are read
   exclusively from `BOXBUTLER_*` environment variables. Nothing in
   `_flatten_yaml`/`_YAML_KEYS` below even looks at a YAML key that could
   hold one of them, so a YAML file that *tries* to set `sink.password`
   is silently ignored rather than accidentally honoured — see
   `test_secrets_never_from_yaml`.
2. **No sentinel for an unknown state.** A missing required secret is a
   `ConfigError` naming every missing variable (never a value), raised
   at startup rather than discovered later as an inexplicable 401 against
   the real cloud, or a silently-empty admin account.

Precedence, low to high: dataclass defaults <- YAML (nested, as in
`config.example.yml`) <- `BOXBUTLER_*` env overrides (flat). Every field
has a working default (spec §7's "zero-config" requirement) except the
three required secrets (`sink_user`, `sink_password`, `secret_key`),
which have none — this project's binding rule is that an unset
requirement is raised, never guessed. `admin_user`/`admin_password` are
secrets too but are deliberately optional: their absence is what makes
the first-run setup wizard (`web/routes/setup.py`) reachable at all —
see `_REQUIRED_SECRET_FIELDS` below. `notify_token` is likewise optional
(`build_notifier` falls back to a no-op notifier without it).

`Settings` itself is the *env+YAML* configuration only (spec §3.1): a
handful of these fields (`schedule`, `timezone`, `cap_seconds`,
`avoid_duplicates_across_tonies`, `repeat_cooldown_days`,
`cache_budget_gb`, `prefetch_depth`, `loudnorm_default`, and the five
`notify_*` fields) are also UI-editable on the Settings screen and live,
once seeded, in `store.settings` (the database) — the database owns them
from first start onward, per §1.1b. `seed_db_settings` performs that
one-time seed; `effective_rotation_settings` is what the orchestrator
actually reads at run time, and it reads the database, never `Settings`
directly, so a change made on the Settings screen takes effect on the
next run with no restart.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from boxbutler.domain.fitting import DEFAULT_CAP_SECONDS, clamp_cap
from boxbutler.orchestrator.run import RotationSettings, default_timezone_name
from boxbutler.scheduler.scheduler import parse_hhmm
from boxbutler.sinks.protocol import SinkLimits
from boxbutler.store.db import Store


class ConfigError(Exception):
    """Configuration is invalid or incomplete. The message names keys,
    never values — see the module docstring, rule 2."""


# ---------------------------------------------------------------- Settings


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    cache_dir: Path = field(default_factory=lambda: Path("/cache"))
    media_root: Path | None = field(default_factory=lambda: Path("/media"))
    timezone: str = field(default_factory=default_timezone_name)
    schedule: str = "15:00"
    # spec §3.4 revised 5340 (89 min; a minute of insurance against
    # encoder-rounding drift) up to 5395 (5 s under the sink's hard 5400 s
    # cap) after live measurement showed the cloud's transcode preserves
    # source duration exactly -- the insurance was against a risk that
    # doesn't exist. `RotationSettings`/`web/routes/settings.py` both
    # already use `DEFAULT_CAP_SECONDS`; this was the one remaining place
    # a fresh install could seed the stale, lower value (Task 29 review,
    # MAJOR-3).
    cap_seconds: int = DEFAULT_CAP_SECONDS
    cache_budget_gb: int = 40
    prefetch_depth: int = 3
    avoid_duplicates_across_tonies: bool = True
    repeat_cooldown_days: int = 0
    loudnorm_default: bool = False
    # "none" matches the Settings screen's own documented zero-config
    # default (`web/routes/settings.py`'s `DEFAULTS["notify_kind"]`) -- a
    # public install has no ntfy server of its own. `build_notifier` falls
    # back to `NullNotifier` either way, but a fresh install's Settings
    # screen showing "ntfy" pre-selected with no server configured was a
    # brief defect (Task 29 review, Minor 10 / B2), not a deliberate choice.
    notify_kind: str = "none"
    notify_server: str | None = None
    notify_topic: str | None = None
    notify_on_failure: bool = True
    notify_on_success: bool = False
    notify_on_debug: bool = False
    sink_kind: str = "tonies_cloud"
    js_runtime: str = "node"
    listen_port: int = 8410
    secure_cookies: bool = False
    # secrets — env only, never YAML, never logged (field(repr=False) so
    # neither `repr()` nor the dataclass's own `__str__` can leak one).
    sink_user: str | None = field(default=None, repr=False)
    sink_password: str | None = field(default=None, repr=False)
    admin_user: str | None = field(default=None, repr=False)
    admin_password: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    notify_token: str | None = field(default=None, repr=False)


# ---------------------------------------------------------------- loading

# Fields that live under a nested YAML section, as `config.example.yml`
# lays them out — "app" for the schedule/timezone pair the Settings
# screen keeps together, "notify" and "sink" mirroring their own
# sections there. Every other field is a flat top-level YAML key.
_YAML_KEYS: dict[str, str] = {
    "schedule": "app.schedule",
    "timezone": "app.timezone",
    "cap_seconds": "cap_seconds",
    "cache_budget_gb": "cache_budget_gb",
    "prefetch_depth": "prefetch_depth",
    "avoid_duplicates_across_tonies": "avoid_duplicates_across_tonies",
    "repeat_cooldown_days": "repeat_cooldown_days",
    "loudnorm_default": "loudnorm_default",
    "notify_kind": "notify.kind",
    "notify_server": "notify.server",
    "notify_topic": "notify.topic",
    "notify_on_failure": "notify.on_failure",
    "notify_on_success": "notify.on_success",
    "notify_on_debug": "notify.on_debug",
    "sink_kind": "sink.kind",
    "js_runtime": "js_runtime",
    "listen_port": "listen_port",
    "secure_cookies": "secure_cookies",
    "data_dir": "data_dir",
    "cache_dir": "cache_dir",
    "media_root": "media_root",
}

_BOOL_FIELDS = {
    "avoid_duplicates_across_tonies", "loudnorm_default",
    "notify_on_failure", "notify_on_success", "notify_on_debug",
    "secure_cookies",
}
_INT_FIELDS = {"cap_seconds", "cache_budget_gb", "prefetch_depth", "repeat_cooldown_days", "listen_port"}
_PATH_FIELDS = {"data_dir", "cache_dir", "media_root"}

# Secret fields: env variable name only. Deliberately absent from
# `_YAML_KEYS` above — see the module docstring, rule 1.
_SECRET_ENV_VARS: dict[str, str] = {
    "sink_user": "BOXBUTLER_SINK_USER",
    "sink_password": "BOXBUTLER_SINK_PASSWORD",
    "admin_user": "BOXBUTLER_ADMIN_USER",
    "admin_password": "BOXBUTLER_ADMIN_PASSWORD",
    "secret_key": "BOXBUTLER_SECRET_KEY",
    "notify_token": "BOXBUTLER_NOTIFY_TOKEN",
}
# notify_token is optional — see build_notifier's own fallback to
# NullNotifier when a "ntfy" config is incomplete. admin_user/
# admin_password are also optional (Task 36 review / final coherence
# review, Critical-1): Task 36 built a three-step setup wizard whose
# entire reason to exist is letting an operator create the admin account
# *without* an env var to write first (spec §8.1: "a setup wizard, not a
# config file to write") — `web/auth.ensure_admin` and
# `web/routes/setup.py` were already written to treat both as optional
# (a no-op bootstrap when absent, falling back to the wizard). Requiring
# them here, at the one gate every one of those call sites sits behind,
# made the wizard unreachable dead code: a fresh install could never
# start without *already* having done the config-file-shaped thing the
# wizard exists to replace. `ensure_admin` still logs a loud warning if
# exactly one of the pair is set (a likely typo), and `seed_db_settings`/
# the setup POST gate are unchanged — an existing deployment with both
# set keeps bootstrapping exactly as before, with no wizard ever shown.
_REQUIRED_SECRET_FIELDS = tuple(
    f for f in _SECRET_ENV_VARS if f not in ("notify_token", "admin_user", "admin_password")
)

_YAML_SECTIONS = ("app", "notify", "sink")


def _flatten_yaml(doc: dict) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in (doc or {}).items():
        if key in _YAML_SECTIONS and isinstance(value, dict):
            for subkey, subval in value.items():
                flat[f"{key}.{subkey}"] = subval
        else:
            flat[key] = value
    return flat


def _coerce_env(field_name: str, raw: str) -> Any:
    if field_name in _BOOL_FIELDS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if field_name in _INT_FIELDS:
        env_name = f"BOXBUTLER_{field_name.upper()}"
        try:
            value = int(raw)
        except ValueError as exc:
            # Task 29 review, Minor 2: a bare `ValueError` here bypassed
            # this module's own rule-2 contract (every configuration
            # problem surfaces as `ConfigError`) and `main()`'s
            # `except ConfigError -> SystemExit`, so the operator saw a
            # traceback instead of `boxbutler: configuration error: ...`.
            raise ConfigError(f"{env_name} must be a whole number, got {raw!r}") from exc
        # Task 29 review, Minor 3: a negative value was silently accepted
        # and then coerced *upward* to the sink's hard maximum by
        # `clamp_cap`'s pre-existing "<= 0 -> sink max" domain semantics --
        # eliminating the cap's safety margin instead of erroring. Reject
        # negatives here, at the one point that turns operator input into
        # a number, rather than let a garbage value ride downstream.
        if value < 0:
            raise ConfigError(f"{env_name} must not be negative, got {value}")
        return value
    if field_name in _PATH_FIELDS:
        return Path(raw)
    return raw


def load_settings(yaml_path: Path | None, env: Mapping[str, str]) -> Settings:
    """Build a `Settings` from `yaml_path` (may be `None`) overridden by
    `env`. Raises `ConfigError` listing every missing required secret by
    name if any of the five are absent.
    """
    doc: dict = {}
    if yaml_path is not None:
        path = Path(yaml_path)
        if path.exists():
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    flat_yaml = _flatten_yaml(doc)

    values: dict[str, Any] = {}
    for field_name, yaml_key in _YAML_KEYS.items():
        if yaml_key in flat_yaml and flat_yaml[yaml_key] is not None:
            v = flat_yaml[yaml_key]
            values[field_name] = Path(v) if field_name in _PATH_FIELDS else v
        env_key = f"BOXBUTLER_{field_name.upper()}"
        if env.get(env_key):
            values[field_name] = _coerce_env(field_name, env[env_key])

    # YAML-sourced numeric fields skip `_coerce_env`'s validation (that
    # only runs for env overrides), so give them the same "reject
    # non-int, reject negative" treatment here -- a `cap_seconds: -5` in
    # a config file would otherwise ride all the way to `clamp_cap`'s
    # "<= 0 -> sink max" fallback and eliminate the cap's safety margin
    # (Task 29 review, Minor 3).
    for field_name in _INT_FIELDS:
        if field_name in values:
            v = values[field_name]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ConfigError(
                    f"{_YAML_KEYS[field_name]} must be a non-negative whole number, got {v!r}"
                )

    # Timezone (spec §7): an explicit value (`BOXBUTLER_TIMEZONE` or YAML
    # `app.timezone`) wins; otherwise `TZ` from the *passed* env mapping;
    # otherwise the host's own zone via `default_timezone_name()` (which
    # itself checks the real process environment/`/etc/localtime` as the
    # final fallback, validating every candidate against `ZoneInfo` before
    # returning one).
    #
    # Task 29 review, CRITICAL-2: the one candidate `default_timezone_name`
    # never sees is an *explicit* configured value -- `TZ` on a real host
    # can read an abbreviation like "AEST" that `ZoneInfo` can't load, and
    # a YAML/`BOXBUTLER_TIMEZONE` typo is exactly as likely. Bypassing
    # validation for the input most likely to be wrong, and then
    # persisting it (`seed_db_settings`, below), wedged startup
    # permanently with no configuration-level recovery path. Validate
    # here, before anything is built or persisted, and name the
    # offending variable and value -- a zone name is not a secret, and
    # naming it is what makes the error actionable.
    if "timezone" in values:
        tz_source = "BOXBUTLER_TIMEZONE" if env.get("BOXBUTLER_TIMEZONE") else "config YAML (app.timezone)"
        tz_value = values["timezone"]
    else:
        tz_env = env.get("TZ")
        if tz_env:
            tz_source, tz_value = "TZ", tz_env
        else:
            tz_source, tz_value = None, default_timezone_name()
    if tz_source is not None:
        try:
            ZoneInfo(tz_value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ConfigError(
                f"invalid timezone {tz_value!r} from {tz_source} -- not a "
                "recognised IANA zone (e.g. 'Australia/Melbourne'); an "
                "abbreviation like 'AEST' is not enough information for "
                "ZoneInfo to resolve"
            ) from exc
    values["timezone"] = tz_value

    # Schedule (spec §7): the same "validate before accepting" rule, for
    # the same reason -- `Scheduler`'s own `parse_hhmm` already raises on
    # a bad `HH:MM`, but only once a real run reaches it. Catching it
    # here turns a typo'd schedule into a startup `ConfigError` instead
    # of the same "wedged forever" shape as the timezone case above.
    if "schedule" in values:
        try:
            parse_hhmm(values["schedule"])
        except ValueError as exc:
            sched_source = (
                "BOXBUTLER_SCHEDULE" if env.get("BOXBUTLER_SCHEDULE") else "config YAML (app.schedule)"
            )
            raise ConfigError(
                f"invalid schedule {values['schedule']!r} from {sched_source} "
                "-- expected a 24-hour HH:MM time, e.g. '15:00'"
            ) from exc

    missing = [
        _SECRET_ENV_VARS[f] for f in _REQUIRED_SECRET_FIELDS if not env.get(_SECRET_ENV_VARS[f])
    ]
    if missing:
        raise ConfigError(
            "missing required secret environment variable(s): " + ", ".join(missing)
        )
    for field_name, env_name in _SECRET_ENV_VARS.items():
        values[field_name] = env.get(env_name) or None

    return Settings(**values)


# --------------------------------------------------------------- DB seeding

# The UI-editable subset (spec §1.1b, §3.1): env+YAML seeds these into
# `store.settings` once, on first start; after that the database owns
# them and this module never overwrites a value already present.
_DB_SEEDED_KEYS: tuple[str, ...] = (
    "schedule",
    "timezone",
    "cap_seconds",
    "avoid_duplicates_across_tonies",
    "repeat_cooldown_days",
    "cache_budget_gb",
    "prefetch_depth",
    "loudnorm_default",
    "notify_kind",
    "notify_server",
    "notify_topic",
    "notify_on_failure",
    "notify_on_success",
    "notify_on_debug",
)

_ABSENT = object()


def seed_db_settings(store: Store, s: Settings) -> None:
    """Copy the UI-editable keys from `s` into `store.settings`, but only
    where the key is currently absent — a fresh store on first start. A
    store that already has a value (an operator's own edit on the
    Settings screen, or a previous container's seed) is left alone.

    Task 29 review, CRITICAL-2: `load_settings` is the normal way a
    `Settings` is built, and it now rejects an unresolvable timezone or
    schedule before this function ever sees it (see above) — but this
    function's own contract ("never persist a value that cannot be
    used") should not depend on every caller having gone through
    `load_settings` first. Re-validate the two fields whose failure mode
    is a wedged, unrecoverable startup (an invalid `store.settings` value
    read back and crashing every future `serve()`, with the Settings
    screen that could fix it unreachable) rather than trust the caller.
    """
    for key in _DB_SEEDED_KEYS:
        if store.settings.get(key, _ABSENT) is not _ABSENT:
            continue
        value = getattr(s, key)
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
                raise ConfigError(
                    f"refusing to seed an unresolvable timezone into the store: {value!r}"
                ) from exc
        elif key == "schedule":
            try:
                parse_hhmm(value)
            except ValueError as exc:
                raise ConfigError(
                    f"refusing to seed an invalid schedule into the store: {value!r}"
                ) from exc
        store.settings.set(key, value)


def effective_rotation_settings(store: Store, sink_limits: SinkLimits) -> RotationSettings:
    """The orchestrator's actual run-time knobs (spec §7, §3.6): read
    live from `store.settings` (the database, which owns these values
    from first start onward — see `seed_db_settings`), never from
    `Settings` directly, so a Settings-screen edit applies on the very
    next run with no restart. `cap_seconds` is clamped to the sink's own
    reported `maxSeconds` regardless of what the operator configured.
    """
    get = store.settings.get
    cap = clamp_cap(get("cap_seconds", DEFAULT_CAP_SECONDS), sink_limits.max_seconds)
    return RotationSettings(
        cap_seconds=cap,
        avoid_duplicates=get("avoid_duplicates_across_tonies", True),
        repeat_cooldown_days=get("repeat_cooldown_days", 0),
        prefetch_depth=get("prefetch_depth", 3),
        cache_budget_bytes=get("cache_budget_gb", 40) * 1024**3,
        loudnorm_default=get("loudnorm_default", False),
        schedule=get("schedule", "15:00"),
        timezone=get("timezone", default_timezone_name()),
    )


__all__ = [
    "ConfigError",
    "Settings",
    "load_settings",
    "seed_db_settings",
    "effective_rotation_settings",
]
