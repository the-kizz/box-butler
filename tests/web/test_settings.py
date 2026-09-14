"""Settings screen (Task 12; spec §5 screen 5, §7, §4.2, §3.6, §9.1, §3.1).

Cap default is **5395 s**, not the brief's sketched 5340 — see
boxbutler/domain/fitting.py:DEFAULT_CAP_SECONDS and spec §3.4: 5340 was a
minute of insurance against encoder drift that live measurement showed
does not happen, so the cap moved to 5395 (5 s under the hard 5400 s
cap). Tests assert 5395 throughout, not the brief's stale value.
"""
from __future__ import annotations

import io
import json
import os

import pytest


def test_settings_page_shows_defaults_and_advanced_block(seeded):
    html = seeded.get("/settings").text
    assert 'value="15:00"' in html
    assert 'value="5395"' in html
    assert "<details" in html and "Advanced" in html
    assert "BOXBUTLER_NOTIFY_TOKEN" in html
    assert 'name="notify_token"' not in html


def test_timezone_picker_lists_zones_and_saves(seeded, store):
    html = seeded.get("/settings").text
    assert "Australia/Melbourne" in html and "Europe/Berlin" in html

    r = seeded.post(
        "/settings",
        data={
            "schedule": "18:30",
            "timezone": "Europe/Berlin",
            "cap_seconds": "5000",
            "avoid_duplicates_across_tonies": "1",
            "repeat_cooldown_days": "14",
            "cache_budget_gb": "40",
            "prefetch_depth": "3",
            "notify_kind": "none",
        },
    )
    assert r.status_code == 303
    assert store.settings.get("schedule") == "18:30"
    assert store.settings.get("timezone") == "Europe/Berlin"
    assert store.settings.get("repeat_cooldown_days") == 14


def test_invalid_schedule_rejected(seeded, store):
    r = seeded.post(
        "/settings",
        data={"schedule": "25:99", "timezone": "UTC", "cap_seconds": "5395"},
    )
    assert r.status_code == 400
    assert store.settings.get("schedule", "15:00") == "15:00"
    # error-clarity: cause and fix both present, plain text (not a bare enum)
    assert "24-hour" in r.text or "HH:MM" in r.text


def test_invalid_timezone_rejected(seeded, store):
    r = seeded.post(
        "/settings",
        data={"schedule": "15:00", "timezone": "Not/AZone", "cap_seconds": "5395"},
    )
    assert r.status_code == 400
    assert store.settings.get("timezone") != "Not/AZone"


def test_cap_clamped_to_sink_max(seeded, store):
    r = seeded.post(
        "/settings",
        data={"schedule": "15:00", "timezone": "UTC", "cap_seconds": "99999"},
    )
    assert r.status_code == 303
    assert store.settings.get("cap_seconds") == 5400


def test_negative_cooldown_rejected(seeded, store):
    r = seeded.post(
        "/settings",
        data={
            "schedule": "15:00",
            "timezone": "UTC",
            "cap_seconds": "5395",
            "repeat_cooldown_days": "-3",
        },
    )
    assert r.status_code == 400
    assert store.settings.get("repeat_cooldown_days", 0) == 0


def test_save_shows_success_feedback(seeded):
    r = seeded.post(
        "/settings",
        data={"schedule": "16:00", "timezone": "UTC", "cap_seconds": "5395"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "saved" in r.text.lower()


def test_notify_token_never_rendered(seeded, store, monkeypatch):
    monkeypatch.setenv("BOXBUTLER_NOTIFY_TOKEN", "super-secret-token-value")
    html = seeded.get("/settings").text
    assert "super-secret-token-value" not in html
    assert "set" in html.lower()  # tells the operator a token IS configured


def test_export_and_import(seeded, store):
    r = seeded.get("/api/export")
    assert r.status_code == 200
    assert r.json()["version"] == 1

    r = seeded.post("/api/import", files={"file": ("x.json", r.content, "application/json")})
    assert r.status_code == 303


def test_import_replace_not_yet_supported_is_a_clean_error(seeded, store):
    doc = json.dumps({"version": 1, "libraries": [], "assignments": []}).encode()
    r = seeded.post(
        "/api/import",
        data={"replace": "1"},
        files={"file": ("x.json", io.BytesIO(doc), "application/json")},
    )
    assert r.status_code == 400


def test_import_bad_json_is_a_clean_error(seeded, store):
    r = seeded.post(
        "/api/import",
        files={"file": ("x.json", io.BytesIO(b"not json"), "application/json")},
    )
    assert r.status_code == 400


def test_notify_kind_none_warns_nothing_will_be_sent(seeded):
    # IMPORTANT 1 (Task 12 review round 1): default notify_kind is "none"
    # and notify_on_failure defaults True (spec-stated) — a checked box
    # that does nothing is the defect, so the page must say so plainly.
    html = seeded.get("/settings").text
    assert "nothing will be sent" in html.lower()


def test_notify_kind_ntfy_hides_the_none_warning(seeded, store):
    store.settings.set("notify_kind", "ntfy")
    html = seeded.get("/settings").text
    assert "nothing will be sent" not in html.lower()


def test_failure_alert_status_surfaced_above_advanced(seeded):
    # IMPORTANT 2 (Task 12 review round 1): whether a parent will be
    # told about a possibly-empty tonie is surfaced at the top level,
    # not buried inside the collapsed Advanced block.
    html = seeded.get("/settings").text
    status_idx = html.lower().find("failure alerts:")
    advanced_idx = html.find("<details")
    assert status_idx != -1 and advanced_idx != -1
    assert status_idx < advanced_idx
    # Default state: on_failure True, notify_kind "none" -> the honest
    # "on, but nothing will be sent" combination, not a bare "on".
    assert "on, but nothing will be sent" in html.lower()


def test_failure_alert_status_reflects_configured_channel(seeded, store):
    store.settings.set("notify_kind", "ntfy")
    store.settings.set("notify_on_failure", True)
    html = seeded.get("/settings").text
    assert "failure alerts: on via ntfy" in html.lower()


def test_failure_alert_status_reflects_off(seeded, store):
    store.settings.set("notify_on_failure", False)
    html = seeded.get("/settings").text
    assert "failure alerts: off" in html.lower()


def test_settings_requires_login(client):
    r = client.get("/settings")
    assert r.status_code in (302, 303)
    r = client.get("/api/export")
    assert r.status_code in (302, 303)
