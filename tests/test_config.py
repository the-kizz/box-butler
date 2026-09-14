import os
import stat

import pytest
from pathlib import Path
from boxbutler.config import load_settings, ConfigError

REQ = {"BOXBUTLER_SINK_USER": "the-user-value", "BOXBUTLER_SINK_PASSWORD": "s3cret-sink", "BOXBUTLER_ADMIN_USER": "a",
       "BOXBUTLER_ADMIN_PASSWORD": "s3cret-admin", "BOXBUTLER_SECRET_KEY": "s3cret-key"}

# The two sink credentials only -- secret_key is no longer required, so
# these tests exercise the generate-and-persist path without it.
REQ_NO_KEY = {"BOXBUTLER_SINK_USER": "the-user-value", "BOXBUTLER_SINK_PASSWORD": "s3cret-sink"}

def test_defaults_match_spec(tmp_path):
    s = load_settings(None, {**REQ, "TZ": "Australia/Melbourne"})
    # cap_seconds is 5395, not the brief's sketched 5340 -- spec §3.4
    # explicitly revises 5340 up to 5395 (see boxbutler/domain/fitting.py
    # DEFAULT_CAP_SECONDS and Task 29 review MAJOR-3); a second, lower
    # default here would make the cap depend on which layer you asked.
    assert (s.schedule, s.cap_seconds, s.cache_budget_gb, s.prefetch_depth, s.timezone) == ("15:00", 5395, 40, 3, "Australia/Melbourne")
    assert s.avoid_duplicates_across_tonies and s.repeat_cooldown_days == 0 and s.loudnorm_default is False and s.notify_on_failure and not s.notify_on_success

def test_yaml_then_env_precedence(tmp_path):
    y = tmp_path / "c.yml"; y.write_text("app:\n  schedule: '18:00'\n  timezone: Europe/Berlin\ncap_seconds: 5000\n")
    s = load_settings(y, {**REQ, "BOXBUTLER_CAP_SECONDS": "5100"})
    assert s.schedule == "18:00" and s.timezone == "Europe/Berlin" and s.cap_seconds == 5100

def test_missing_secrets_named_not_valued():
    # secret_key is no longer a required secret -- it's generated and
    # persisted instead (see the secret-key generation tests below) -- so
    # only the two sink credentials can appear here.
    with pytest.raises(ConfigError) as e: load_settings(None, {"BOXBUTLER_SINK_USER": "the-user-value"})
    assert "BOXBUTLER_SINK_PASSWORD" in str(e.value) and "BOXBUTLER_SECRET_KEY" not in str(e.value) and "the-user-value" not in str(e.value)

def test_secrets_never_from_yaml(tmp_path):
    y = tmp_path / "c.yml"; y.write_text("sink:\n  password: nope\n")
    with pytest.raises(ConfigError): load_settings(y, {k: v for k, v in REQ.items() if k != "BOXBUTLER_SINK_PASSWORD"})

def test_example_config_loads_and_has_no_real_values():
    text = Path("config.example.yml").read_text()
    # These are forbidden *literals*, not sample values: the example config
    # must not carry the operator's own host, a real playlist URL, or a real
    # Creative-Tonie name. (Hence this file is a denylist owner in
    # tests/test_no_leaked_estate_data.py.)
    assert "kizz" not in text and "youtube.com" not in text and "koala" not in text.lower()
    s = load_settings(Path("config.example.yml"), REQ); assert s.schedule == "15:00"

def test_example_config_does_not_force_a_timezone_over_tz(tmp_path):
    # Task 29 review, MAJOR-2: the example used to hard-code
    # `timezone: "Etc/UTC"`, which *wins* over a correctly set `TZ` --
    # precisely the UTC-window-boundary defect this codebase documents as
    # destroying a morning's story. The example must leave `timezone`
    # unset so a real `TZ` reaches `Settings.timezone` unmolested.
    s = load_settings(Path("config.example.yml"), {**REQ, "TZ": "Australia/Melbourne"})
    assert s.timezone == "Australia/Melbourne"

def test_bad_tz_env_is_rejected_not_persisted_later(tmp_path):
    # Task 29 review, CRITICAL-2: `TZ=AEST` (an abbreviation ZoneInfo
    # can't load) used to be accepted raw, seeded into the store, and
    # then wedge every future `serve()` permanently. It must now be
    # rejected at load time, loudly and by name, before it ever reaches
    # a store.
    with pytest.raises(ConfigError) as e:
        load_settings(None, {**REQ, "TZ": "AEST"})
    assert "AEST" in str(e.value) and "TZ" in str(e.value)

def test_bad_boxbutler_timezone_env_is_rejected():
    with pytest.raises(ConfigError) as e:
        load_settings(None, {**REQ, "BOXBUTLER_TIMEZONE": "Australia/Melboune"})
    assert "Australia/Melboune" in str(e.value)

def test_bad_yaml_timezone_is_rejected(tmp_path):
    y = tmp_path / "c.yml"; y.write_text("app:\n  timezone: Not/AZone\n")
    with pytest.raises(ConfigError) as e:
        load_settings(y, REQ)
    assert "Not/AZone" in str(e.value)

def test_bad_schedule_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(None, {**REQ, "BOXBUTLER_SCHEDULE": "25:99"})
    assert "25:99" in str(e.value)

def test_seed_db_settings_refuses_to_persist_an_unresolvable_timezone(store):
    # Belt-and-suspenders: even if a `Settings` bypassing `load_settings`
    # somehow carried a bad timezone, `seed_db_settings` must not write
    # it -- the "never persist a value that cannot be used" half of
    # CRITICAL-2, independent of the "validate before accepting" half
    # above.
    from boxbutler.config import Settings, seed_db_settings
    bad = Settings(timezone="AEST", sink_user="u", sink_password="p", admin_user="a", admin_password="b", secret_key="k")
    with pytest.raises(ConfigError):
        seed_db_settings(store, bad)
    assert store.settings.get("timezone", "absent") == "absent"

def test_non_integer_env_raises_config_error_not_bare_value_error():
    # Task 29 review, Minor 2.
    with pytest.raises(ConfigError):
        load_settings(None, {**REQ, "BOXBUTLER_CAP_SECONDS": "lots"})

def test_negative_int_env_is_rejected_not_clamped_upward():
    # Task 29 review, Minor 3: a negative cap used to be silently
    # coerced *upward* to the sink's hard maximum by clamp_cap's
    # "<= 0 -> sink max" semantics, eliminating the safety margin.
    with pytest.raises(ConfigError):
        load_settings(None, {**REQ, "BOXBUTLER_CAP_SECONDS": "-5"})

def test_repr_and_str_hide_secrets():
    s = load_settings(None, REQ)
    assert "s3cret" not in repr(s) and "s3cret" not in str(s)          # secret fields use field(repr=False)


# ---------------------------------------------------- session key generation


def test_secret_key_env_wins_and_nothing_is_written(tmp_path):
    # An operator-supplied key always wins, and load_settings must not
    # touch the data dir at all in that case -- no file, generated or
    # otherwise, should appear next to it.
    data_dir = tmp_path / "data"
    env = {**REQ_NO_KEY, "BOXBUTLER_SECRET_KEY": "an-operators-own-key", "BOXBUTLER_DATA_DIR": str(data_dir)}
    s = load_settings(None, env)
    assert s.secret_key == "an-operators-own-key"
    assert not (data_dir / "secret_key").exists()


def test_secret_key_generated_and_persisted_at_0600(tmp_path):
    data_dir = tmp_path / "data"
    env = {**REQ_NO_KEY, "BOXBUTLER_DATA_DIR": str(data_dir)}
    s = load_settings(None, env)
    key_file = data_dir / "secret_key"
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert s.secret_key == key_file.read_text().strip()
    # secrets.token_urlsafe(48) -- long enough that a typo'd "changeme"
    # could never appear here.
    assert len(s.secret_key) > 32


def test_secret_key_persists_identically_across_a_second_start(tmp_path):
    data_dir = tmp_path / "data"
    env = {**REQ_NO_KEY, "BOXBUTLER_DATA_DIR": str(data_dir)}
    first = load_settings(None, env)
    second = load_settings(None, env)
    # The whole point: a restart must not invalidate every session, so
    # the second load has to read back the exact value the first wrote,
    # not generate a fresh one.
    assert first.secret_key == second.secret_key


def test_unwritable_data_dir_raises_config_error_not_ephemeral_key(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o500)  # read + execute, no write
    try:
        env = {**REQ_NO_KEY, "BOXBUTLER_DATA_DIR": str(data_dir)}
        with pytest.raises(ConfigError):
            load_settings(None, env)
    finally:
        data_dir.chmod(0o700)  # let tmp_path clean up


def test_empty_key_file_is_treated_as_absent_and_regenerated(tmp_path):
    # A zero-byte or whitespace-only file is corruption, not a real key --
    # using it verbatim would sign cookies with an empty/blank secret.
    # Decision: treat it the same as "absent" and regenerate, so a
    # damaged file self-heals on the next start instead of wedging
    # startup forever (the file is not something an operator is expected
    # to go delete by hand).
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "secret_key").write_text("   \n")
    env = {**REQ_NO_KEY, "BOXBUTLER_DATA_DIR": str(data_dir)}
    s = load_settings(None, env)
    assert s.secret_key.strip() != ""
    assert s.secret_key == (data_dir / "secret_key").read_text().strip()


def test_secret_key_never_logged_during_generation(tmp_path, caplog):
    data_dir = tmp_path / "data"
    env = {**REQ_NO_KEY, "BOXBUTLER_DATA_DIR": str(data_dir)}
    with caplog.at_level("DEBUG"):
        s = load_settings(None, env)
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert s.secret_key not in log_text
