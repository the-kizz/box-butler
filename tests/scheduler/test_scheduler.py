"""Task 28: scheduler — daily, timezone-aware, DST-safe (spec D7 §7).

Every test here drives a fake clock/sleep; nothing sleeps in real time.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from boxbutler.scheduler.scheduler import Scheduler, next_run_at, parse_hhmm

MEL = ZoneInfo("Australia/Melbourne")


def test_next_today_if_before_schedule():
    now = datetime(2026, 9, 11, 10, 0, tzinfo=MEL)
    assert next_run_at(now, "15:00", MEL) == datetime(2026, 9, 11, 15, 0, tzinfo=MEL)


def test_next_tomorrow_if_after_or_equal():
    now = datetime(2026, 9, 11, 15, 0, tzinfo=MEL)
    assert next_run_at(now, "15:00", MEL) == datetime(2026, 9, 12, 15, 0, tzinfo=MEL)


def test_dst_transition_keeps_wall_clock():
    # Melbourne DST starts 2026-10-04 02:00 -> 03:00. 15:00 local on the 3rd
    # and the 4th are 23 h apart in UTC, not 24 -- next_run_at must resolve
    # the offset via zoneinfo, never via `+ timedelta(days=1)` on an aware dt.
    before = next_run_at(datetime(2026, 10, 3, 16, 0, tzinfo=MEL), "15:00", MEL)
    assert before == datetime(2026, 10, 4, 15, 0, tzinfo=MEL)
    prev = datetime(2026, 10, 3, 15, 0, tzinfo=MEL)
    assert (before.astimezone(UTC) - prev.astimezone(UTC)) == timedelta(hours=23)


def test_utc_now_input_is_converted():
    now_utc = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)  # 10:00 Melbourne
    assert next_run_at(now_utc, "15:00", MEL).astimezone(UTC) == datetime(2026, 9, 11, 5, 0, tzinfo=UTC)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_hhmm("25:99")


def test_scheduler_fires_once_per_day():
    t = {"now": datetime(2026, 9, 11, 14, 59, 30, tzinfo=MEL)}
    fired = []
    sched = {"v": ("15:00", "Australia/Melbourne")}
    stop = threading.Event()

    def sleep(s):
        t["now"] += timedelta(seconds=s)
        (stop.set() if len(fired) >= 2 else None)

    s = Scheduler(
        lambda: sched["v"],
        lambda: fired.append(t["now"]),
        clock=lambda: t["now"].astimezone(UTC),
        sleep=sleep,
        stop=stop,
    )
    s.run_forever()
    assert len(fired) == 2 and (fired[1] - fired[0]) >= timedelta(hours=23)


def test_on_fire_exception_does_not_kill_loop():
    t = {"now": datetime(2026, 9, 11, 14, 59, 59, tzinfo=MEL)}
    stop = threading.Event()
    n = {"c": 0}

    def fire():
        n["c"] += 1
        (stop.set() if n["c"] == 2 else None)
        raise RuntimeError("boom")

    Scheduler(
        lambda: ("15:00", "Australia/Melbourne"),
        fire,
        clock=lambda: t["now"].astimezone(UTC),
        sleep=lambda s: t.__setitem__("now", t["now"] + timedelta(seconds=s)),
        stop=stop,
    ).run_forever()
    assert n["c"] == 2


# ------------------------------------------------------------- extra: DST edge times
#
# Melbourne DST forward transition (2026-10-04): local clocks jump
# 02:00 -> 03:00, so 02:00-02:59:59 does not exist that day.
# Melbourne DST backward transition (2026-04-05): local clocks fall back
# 03:00 -> 02:00, so 02:00-02:59:59 happens twice that day.
#
# `next_run_at` never special-cases these: it always builds the candidate
# via `datetime.combine(date, time, tzinfo=tz)`, which zoneinfo resolves
# with `fold=0` by default (PEP 495). That default already gives the
# behaviour this project wants ("when in doubt, fire once, late, rather
# than twice or not at all"):


def test_nonexistent_local_time_resolves_to_a_real_later_instant():
    # 02:30 does not exist on 2026-10-04 (the gap is 02:00-03:00).
    # fold=0 uses the pre-transition (standard) offset for the naive wall
    # time, which lands on a real UTC instant that -- once you look at the
    # *actual* Melbourne clock afterwards -- reads 03:30, not 02:30: the
    # scheduler fires an hour late that one day of the year, never early,
    # never twice, never silently skipped.
    now = datetime(2026, 10, 3, 20, 0, tzinfo=MEL)
    candidate = next_run_at(now, "02:30", MEL)
    assert candidate.astimezone(UTC) == datetime(2026, 10, 3, 16, 30, tzinfo=UTC)
    # Round-tripping through UTC (i.e. what a real clock/calendar would show)
    # reveals the "late" wall-clock reading.
    assert candidate.astimezone(UTC).astimezone(MEL) == datetime(2026, 10, 4, 3, 30, tzinfo=MEL)

    # And the loop only ever fires once for that day: the *next* candidate
    # computed from this instant lands on the following day, not a second
    # trigger for 2026-10-04.
    following = next_run_at(candidate, "02:30", MEL)
    assert following.date().isoformat() == "2026-10-05"


def test_doubled_local_time_fires_only_the_first_occurrence():
    # 02:30 happens twice on 2026-04-05 (clocks fall back 03:00 -> 02:00).
    # fold=0 resolves to the offset in effect *before* the transition
    # (AEDT, +11), which is chronologically the earlier of the two
    # instants -- so the scheduler commits to firing at the first
    # occurrence, deterministically, rather than picking arbitrarily.
    now = datetime(2026, 4, 4, 20, 0, tzinfo=MEL)
    first = next_run_at(now, "02:30", MEL)
    assert first.astimezone(UTC) == datetime(2026, 4, 4, 15, 30, tzinfo=UTC)

    # Advancing from that instant must not fire again for the second
    # (post-fallback) occurrence of the same wall-clock 02:30 later that
    # same day -- it must move on to the next calendar day.
    second = next_run_at(first, "02:30", MEL)
    assert second.date().isoformat() == "2026-04-06"


def test_settings_change_to_earlier_time_is_honoured_next_loop():
    # A Settings-page edit that moves the fire time *earlier* must not
    # leave the scheduler asleep until the old (later) time -- each loop
    # iteration re-reads get_schedule() and re-derives `next`.
    start = datetime(2026, 9, 11, 9, 0, tzinfo=MEL)
    t = {"now": start}
    sched = {"v": ("15:00", "Australia/Melbourne")}
    fired = []
    stop = threading.Event()

    def sleep(s):
        # After the first short slice, simulate an operator moving the
        # schedule much earlier -- to a time still ahead of "now", but
        # well before the originally-computed 15:00 target.
        if sched["v"] == ("15:00", "Australia/Melbourne"):
            sched["v"] = ("09:05", "Australia/Melbourne")
        t["now"] += timedelta(seconds=s)
        if fired:
            stop.set()

    s = Scheduler(
        lambda: sched["v"],
        lambda: fired.append(t["now"]),
        clock=lambda: t["now"].astimezone(UTC),
        sleep=sleep,
        stop=stop,
    )
    s.run_forever()
    assert len(fired) == 1
    # It fired around the new 09:05 target, nowhere near the stale 15:00
    # one -- proof the change was picked up without waiting out the old
    # target.
    assert fired[0] < start + timedelta(hours=1)


# ------------------------------------------------------ resilience: bad config must not kill the loop
#
# Critical review finding C1: `get_schedule()`/`_resolve_zone()`/
# `next_run_at()` failing *after* the loop is already running (an
# operator's Settings-page typo, or a transient settings-store error)
# used to propagate straight out of `run_forever`, killing the
# background thread with no log output. These tests drive the real
# `run_forever` loop (never real time) through each of those failures
# and assert it survives, logs, and keeps firing on the last-known-good
# schedule -- not a default, and not silence.


def test_bad_hhmm_mid_loop_survives_and_keeps_firing_on_last_good_schedule(caplog):
    t = {"now": datetime(2026, 9, 11, 14, 59, 30, tzinfo=MEL)}
    fired = []
    sched = {"v": ("15:00", "Australia/Melbourne")}
    stop = threading.Event()

    def sleep(s):
        t["now"] += timedelta(seconds=s)
        # Corrupt the schedule only *after* the first successful fire,
        # so this is unambiguously a "re-read after running" failure,
        # not a first-read failure (covered separately below).
        if len(fired) == 1:
            sched["v"] = ("notatime", "Australia/Melbourne")
        if len(fired) >= 2:
            stop.set()

    s = Scheduler(
        lambda: sched["v"],
        lambda: fired.append(t["now"]),
        clock=lambda: t["now"].astimezone(UTC),
        sleep=sleep,
        stop=stop,
    )
    with caplog.at_level(logging.ERROR, logger="boxbutler.scheduler.scheduler"):
        s.run_forever()

    # The loop kept going and fired a second time, ~24h later, using the
    # last-known-good "15:00" schedule -- not stopped, not a guessed
    # default time.
    assert len(fired) == 2
    assert fired[0] == datetime(2026, 9, 11, 15, 0, tzinfo=MEL)
    assert fired[1] == datetime(2026, 9, 12, 15, 0, tzinfo=MEL)
    assert any("re-read failed" in r.message for r in caplog.records)


def test_bad_zone_mid_loop_survives_and_keeps_firing_on_last_good_schedule(caplog):
    t = {"now": datetime(2026, 9, 11, 14, 59, 30, tzinfo=MEL)}
    fired = []
    sched = {"v": ("15:00", "Australia/Melbourne")}
    stop = threading.Event()

    def sleep(s):
        t["now"] += timedelta(seconds=s)
        if len(fired) == 1:
            sched["v"] = ("15:00", "Notareal/Zone")
        if len(fired) >= 2:
            stop.set()

    s = Scheduler(
        lambda: sched["v"],
        lambda: fired.append(t["now"]),
        clock=lambda: t["now"].astimezone(UTC),
        sleep=sleep,
        stop=stop,
    )
    with caplog.at_level(logging.ERROR, logger="boxbutler.scheduler.scheduler"):
        s.run_forever()

    assert len(fired) == 2
    assert fired[0] == datetime(2026, 9, 11, 15, 0, tzinfo=MEL)
    assert fired[1] == datetime(2026, 9, 12, 15, 0, tzinfo=MEL)
    assert any("re-read failed" in r.message for r in caplog.records)


def test_transient_get_schedule_failure_recovers_and_adopts_new_schedule(caplog):
    t = {"now": datetime(2026, 9, 11, 14, 59, 30, tzinfo=MEL)}
    fired = []
    state = {"fail_left": 3}
    stop = threading.Event()

    def get_schedule():
        # Before the first fire: a clean, good read. After the first
        # fire: the settings store is flaky for a few reads (simulating
        # a transient error), then recovers -- returning a *different*,
        # earlier schedule, so "resumed reading" is provable by the
        # fire time actually moving, not just by not-crashing.
        if not fired:
            return ("15:00", "Australia/Melbourne")
        if state["fail_left"] > 0:
            state["fail_left"] -= 1
            raise RuntimeError("settings store unavailable")
        return ("09:05", "Australia/Melbourne")

    def sleep(s):
        t["now"] += timedelta(seconds=s)
        if len(fired) >= 2:
            stop.set()

    s = Scheduler(
        get_schedule,
        lambda: fired.append(t["now"]),
        clock=lambda: t["now"].astimezone(UTC),
        sleep=sleep,
        stop=stop,
    )
    with caplog.at_level(logging.ERROR, logger="boxbutler.scheduler.scheduler"):
        s.run_forever()

    assert len(fired) == 2
    # First fire on the original good schedule.
    assert fired[0] == datetime(2026, 9, 11, 15, 0, tzinfo=MEL)
    # Once get_schedule() becomes readable again with the new, earlier
    # 09:05 schedule, the loop adopts it well before the stale 15:00
    # target it had been sleeping towards -- proof it actually resumed
    # reading, not merely that it didn't crash.
    assert fired[1] == datetime(2026, 9, 12, 9, 5, tzinfo=MEL)
    assert any("re-read failed" in r.message for r in caplog.records)


def test_first_read_failure_raises_and_never_starts_the_loop(caplog):
    # There is no last-known-good value on the very first read -- unlike
    # the mid-loop cases above, there is nothing safe to fall back to, so
    # this is allowed to raise and `on_fire` must never be called.
    fired = []
    stop = threading.Event()

    def get_schedule():
        raise RuntimeError("settings store unavailable")

    s = Scheduler(
        get_schedule,
        lambda: fired.append("fired"),
        clock=lambda: datetime(2026, 9, 11, 14, 59, 30, tzinfo=MEL).astimezone(UTC),
        sleep=lambda _seconds: None,
        stop=stop,
    )
    with caplog.at_level(logging.ERROR, logger="boxbutler.scheduler.scheduler"):
        with pytest.raises(RuntimeError, match="settings store unavailable"):
            s.run_forever()

    assert fired == []
    assert any("cannot start" in r.message for r in caplog.records)
