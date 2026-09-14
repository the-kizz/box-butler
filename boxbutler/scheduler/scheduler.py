"""Daily, timezone-aware, DST-safe trigger (Task 28; spec D7 §7,
"All schedule arithmetic is timezone-aware, so a DST change does not
drift the run time").

Two failure shapes this module exists to rule out:

- **Reading the wrong clock/zone.** The zone comes *only* from the
  settings string passed in (e.g. ``"Australia/Melbourne"``), resolved
  through :class:`~zoneinfo.ZoneInfo` by the caller. This module never
  reads ``/etc/timezone``, never calls bare ``datetime.now()`` and never
  assumes the process's ambient local time -- on at least one real
  deploy target, ``/etc/timezone`` disagrees with ``timedatectl``/
  ``/etc/localtime``, and trusting the wrong one would silently run a
  bedtime job at the wrong hour with no obvious symptom.
- **Naive day arithmetic across a DST boundary.** The next fire time is
  built with ``datetime.combine(date, time, tzinfo=tz)`` so
  :mod:`zoneinfo` resolves the UTC offset for that wall-clock date.
  Never ``+ timedelta(days=1)`` on an aware datetime and assume the wall
  clock holds -- across a Melbourne DST boundary that is off by an hour.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, time as time_cls, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: The loop never sleeps longer than this in one slice, so a schedule
#: edit made on the Settings page (a new HH:MM or a new zone) is picked
#: up promptly rather than only after the stale target is reached.
_MAX_SLEEP_SLICE_SECONDS = 60.0


def parse_hhmm(s: str) -> tuple[int, int]:
    """Parse a ``"HH:MM"`` string into ``(hour, minute)``.

    Raises ``ValueError`` on anything malformed -- including an
    out-of-range hour/minute -- rather than clamping or guessing. An
    unparsable schedule string is an unknown state, and this project's
    rule is that unknown states are raised, never silently coerced into
    a plausible-looking default.
    """
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"not an HH:MM time: {s!r}")
    hour_s, minute_s = parts
    if not (hour_s.isdigit() and minute_s.isdigit()):
        raise ValueError(f"not an HH:MM time: {s!r}")
    hour, minute = int(hour_s), int(minute_s)
    # time_cls() itself raises ValueError for an out-of-range hour/minute,
    # which is exactly the "raise on garbage" behaviour this function
    # promises -- so route through it rather than hand-rolling the bounds
    # check twice.
    time_cls(hour, minute)
    return hour, minute


def next_run_at(now: datetime, schedule: str, tz: ZoneInfo) -> datetime:
    """The next wall-clock ``HH:MM`` (per `schedule`) in `tz` that is
    strictly after `now`, as an aware datetime in `tz`.

    `now` may carry any tzinfo (including UTC, as the `Scheduler` clock
    does by default) -- it is converted to `tz` first so all comparison
    and arithmetic happens in the schedule's own wall clock.

    The candidate is built with ``datetime.combine(date, time,
    tzinfo=tz)``, never by adding a day to an already-aware datetime, so
    ``zoneinfo`` resolves the correct UTC offset for that calendar date
    even across a DST transition.

    Two local times a daily HH:MM can land on need a deliberate answer,
    not an accidental one (this project's guiding principle: prefer
    firing once, late, over firing twice or not at all):

    - a **nonexistent** local time (the hour skipped when DST starts,
      e.g. 02:30 on the day Melbourne jumps 02:00 -> 03:00): `combine`
      leaves `fold` at its default of 0 (PEP 495), which resolves the
      naive wall time using the *pre-transition* offset. Converted back
      through a real clock that only ever shows valid times, that
      instant reads as one hour *later* than the nominal HH:MM (e.g.
      "02:30" becomes an instant that shows as 03:30 on an actual
      Melbourne clock) -- i.e. it fires once, an hour late, that one day
      of the year. It never fires early and never fails to fire.
    - a **doubled** local time (the hour repeated when DST ends, e.g.
      02:30 occurring twice when Melbourne falls back 03:00 -> 02:00):
      `fold=0` resolves to the offset in effect *before* the transition,
      which is chronologically the *earlier* of the two instants that
      share that wall-clock reading. This is a deliberate, explicit
      choice (not an accident of whatever `fold` happened to default
      to): the scheduler commits to the first occurrence. The loop only
      ever advances to the following calendar date after firing, so the
      second (post-fallback) occurrence of the same wall-clock reading
      is never treated as a second trigger.
    """
    hour, minute = parse_hhmm(schedule)
    local = now.astimezone(tz)
    candidate_date = local.date()
    candidate = datetime.combine(candidate_date, time_cls(hour, minute), tzinfo=tz)
    if candidate <= local:
        candidate_date = candidate_date + timedelta(days=1)
        candidate = datetime.combine(candidate_date, time_cls(hour, minute), tzinfo=tz)
    return candidate


class Scheduler:
    """Fires `on_fire` once a day at the configured wall-clock time.

    `get_schedule` is re-read every loop iteration (not cached at
    construction or after each fire) so a change made on the Settings
    page -- a new time, or a new timezone -- takes effect without
    restarting the process. The loop sleeps in slices of at most
    `_MAX_SLEEP_SLICE_SECONDS` so such a change, including one that
    moves the next fire time *earlier*, is picked up promptly rather
    than only once the stale target arrives.

    `clock` and `sleep` are injected seams so tests can drive the loop
    without ever sleeping in real time; `stop` lets a test (or a real
    shutdown) end `run_forever` deterministically.

    An exception raised by `on_fire` is logged and swallowed -- it must
    never stop the loop. A scheduler that dies on one bad night stops
    every future night, which is worse than one missed run. The same
    principle applies to the config re-read itself: `get_schedule()` can
    return garbage (an operator's Settings-page typo) or simply raise (a
    transient settings-store error), and `_resolve_zone()`/`next_run_at()`
    can raise on a bad zone or bad time string. Once the loop has a
    last-known-good `(schedule, tz_name)` to fall back on, any of those
    failures is logged (via `logger.exception`, distinguishable in the
    logs from an `on_fire` failure by its own message) and the loop keeps
    firing on that last-known-good schedule rather than stopping or
    guessing a default. Only the very first read, before any
    last-known-good value exists, is allowed to raise out of
    `run_forever` -- there is nothing safe to fall back to, and starting
    up with no usable schedule at all is a different situation from
    losing a good one mid-flight.
    """

    def __init__(
        self,
        get_schedule: Callable[[], tuple[str, str]],
        on_fire: Callable[[], None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        stop: threading.Event | None = None,
    ) -> None:
        self._get_schedule = get_schedule
        self._on_fire = on_fire
        self._clock = clock
        self._sleep = sleep
        self._stop = stop if stop is not None else threading.Event()
        self._next: datetime | None = None

    def next(self) -> datetime:
        """The next scheduled fire time, recomputed from the current
        settings and the current clock."""
        schedule, tz_name = self._get_schedule()
        tz = _resolve_zone(tz_name)
        now = self._clock()
        self._next = next_run_at(now, schedule, tz)
        return self._next

    def _read_target(self, now: datetime) -> tuple[str, str, ZoneInfo, datetime]:
        """Read settings, resolve the zone, and compute the next fire time
        for `now`, as one unit.

        Raises whatever `get_schedule()`, `_resolve_zone()` (a bad IANA
        zone) or `next_run_at()`/`parse_hhmm()` (a bad `HH:MM` string)
        raise. Callers decide what "no good read" means for them: fatal
        at startup, or "keep the last-known-good value" once the loop is
        already running. Doing all three steps together means a bad
        `HH:MM` string can never slip through as a "successful" read just
        because `get_schedule()` and `_resolve_zone()` alone didn't
        object to it.
        """
        schedule, tz_name = self._get_schedule()
        tz = _resolve_zone(tz_name)
        target = next_run_at(now, schedule, tz)
        return schedule, tz_name, tz, target

    def run_forever(self) -> None:
        # There is no last-known-good schedule yet, so a bad or unreadable
        # config here is a different situation from losing a good one
        # mid-flight (below): there is nothing safe to fall back to, so
        # this is allowed to raise and the loop never starts. This mirrors
        # `parse_hhmm`'s own rule -- an unknown state is raised, never
        # coerced into a plausible-looking default (never a bare `15:00`,
        # never UTC).
        try:
            schedule, tz_name, tz, target = self._read_target(self._clock())
        except Exception:
            logger.exception(
                "scheduler: initial schedule/zone is unreadable or "
                "invalid and there is no last-known-good value to fall "
                "back on -- the loop cannot start"
            )
            raise
        self._next = target

        while not self._stop.is_set():
            # Sleep towards `target` in short slices. Settings are
            # re-read every slice so a Settings-page edit -- moving the
            # time later *or* earlier, or changing the zone -- is picked
            # up promptly. Crucially, `target` itself is only ever
            # adopted when the settings actually *changed*: re-running
            # `next_run_at` against an unchanged schedule the instant the
            # clock reaches `target` would treat "now == target" as
            # already-fired and jump the target to tomorrow, so the fire
            # would never happen. Adopting a new target only on an
            # observed change avoids that self-defeating loop while still
            # honouring a genuine edit immediately.
            while not self._stop.is_set():
                now = self._clock()
                remaining = (target - now).total_seconds()
                if remaining <= 0:
                    break
                self._sleep(min(remaining, _MAX_SLEEP_SLICE_SECONDS))
                # A typo'd HH:MM/zone saved on the Settings page, or a
                # transient settings-store error, must not kill the loop
                # -- log it (distinct message from an `on_fire` failure,
                # so an operator can tell "your new schedule is invalid,
                # still using the previous one" from "a run failed") and
                # keep sleeping towards the last-known-good `target`.
                try:
                    new_schedule, new_tz_name, new_tz, new_target = self._read_target(
                        self._clock()
                    )
                except Exception:
                    logger.exception(
                        "scheduler: schedule/zone re-read failed; keeping "
                        "the last-known-good schedule (%r, %r) and "
                        "retrying next cycle",
                        schedule,
                        tz_name,
                    )
                    continue
                if (new_schedule, new_tz_name) != (schedule, tz_name):
                    schedule, tz_name, tz, target = (
                        new_schedule,
                        new_tz_name,
                        new_tz,
                        new_target,
                    )
                    self._next = target
            if self._stop.is_set():
                return
            try:
                self._on_fire()
            except Exception:
                logger.exception("scheduler: on_fire raised; continuing to the next day")
            # Re-read for tomorrow's target. Same last-known-good fallback
            # as above: a bad re-read here must not stop the loop, it
            # just means tomorrow's run repeats tonight's schedule/zone.
            try:
                schedule, tz_name, tz, target = self._read_target(self._clock())
            except Exception:
                logger.exception(
                    "scheduler: schedule/zone re-read failed after firing; "
                    "continuing to fire on the last-known-good schedule "
                    "(%r, %r)",
                    schedule,
                    tz_name,
                )
                target = next_run_at(self._clock(), schedule, tz)
            self._next = target


def _resolve_zone(tz_name: str) -> ZoneInfo:
    """Resolve a settings timezone string via `ZoneInfo`.

    Raises whatever `ZoneInfo` raises (e.g. `ZoneInfoNotFoundError`) for
    an unrecognised zone -- never falls back to UTC or to the process's
    local time. An unknown zone is an unknown state, not a reason to
    guess.
    """
    return ZoneInfo(tz_name)


__all__ = ["Scheduler", "next_run_at", "parse_hhmm"]
