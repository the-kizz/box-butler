import pytest
from pathlib import Path
from boxbutler.config import load_settings, ConfigError

REQ = {"BOXBUTLER_SINK_USER": "the-user-value", "BOXBUTLER_SINK_PASSWORD": "s3cret-sink", "BOXBUTLER_ADMIN_USER": "a",
       "BOXBUTLER_ADMIN_PASSWORD": "s3cret-admin", "BOXBUTLER_SECRET_KEY": "s3cret-key"}

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
    with pytest.raises(ConfigError) as e: load_settings(None, {"BOXBUTLER_SINK_USER": "the-user-value"})
    assert "BOXBUTLER_SINK_PASSWORD" in str(e.value) and "BOXBUTLER_SECRET_KEY" in str(e.value) and "the-user-value" not in str(e.value)

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
