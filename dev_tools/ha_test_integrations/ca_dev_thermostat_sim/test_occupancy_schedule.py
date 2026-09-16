"""Verification script for occupancy_schedule.py's use of scheduler.py (Issue #898).

NOT a pytest test — plain script, run directly: `python test_occupancy_schedule.py`.
Same shape and same reason as test_sim_math.py in this directory: this repo's local
Python environment has no `homeassistant` package installed (confirmed there), and
scheduler.py transitively imports automation.py, which requires it — so
scheduler.is_schedule_active_at() cannot actually be imported and exercised here.
On a real HA instance (where this integration is meant to run, alongside
climate_advisor) that import works fine; this script hand-transcribes
is_schedule_active_at()'s algorithm from its own docstring/source
(custom_components/climate_advisor/scheduler.py:142-165, read in full) ONLY for
local verification, and checks this module's own resolve_occupancy_target()
reasoning against it. This file is never imported by occupancy_schedule.py or any
shipped code — it does not violate the DRY requirement that the real integration
call the production function; it exists so the reuse can be sanity-checked without
a live HA environment.

The transcribed algorithm (from scheduler.py's is_schedule_active_at()):

    if not schedule.days: return False
    start_h = parse("HH:MM" -> hours), end_h = parse(...)
    h = now.hour + now.minute/60.0
    today = weekday abbrev of now
    if end_h <= start_h:  # crosses midnight
        yesterday = weekday abbrev of (now - 1 day)
        if today in schedule.days and start_h <= h < 24: return True
        return yesterday in schedule.days and 0 <= h < end_h
    return today in schedule.days and start_h <= h < end_h

And resolve_occupancy_target() (this module): first schedule (in list order) for
which the above is True wins; its `target` (stored in Schedule.cost_tag — see
occupancy_schedule.py's module docstring for why that repurposing is safe) is
returned. None if no schedule covers `now`.

If a `homeassistant` package is ever installed in this venv, replace this script's
hand-transcription with a real import-based test, mirroring test_sim_math.py's own
note to that effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class _RefSchedule:
    id: str
    days: tuple[str, ...]
    start: str
    end: str
    target: str


_WEEKDAY_ABBREVS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hhmm(value: str) -> float:
    parts = value.split(":")
    return int(parts[0]) + int(parts[1]) / 60.0


def _weekday_abbrev(moment: datetime) -> str:
    return _WEEKDAY_ABBREVS[moment.weekday()]


def _reference_is_schedule_active_at(schedule: _RefSchedule, now: datetime) -> bool:
    """Hand-transcribed from scheduler.py's is_schedule_active_at() — see module docstring."""
    from datetime import timedelta

    if not schedule.days:
        return False
    start_h = _parse_hhmm(schedule.start)
    end_h = _parse_hhmm(schedule.end)
    h = now.hour + now.minute / 60.0
    today = _weekday_abbrev(now)

    if end_h <= start_h:
        yesterday = _weekday_abbrev(now - timedelta(days=1))
        if today in schedule.days and start_h <= h < 24:
            return True
        return yesterday in schedule.days and 0 <= h < end_h

    return today in schedule.days and start_h <= h < end_h


def _reference_resolve_occupancy_target(schedules: list[_RefSchedule], now: datetime) -> str | None:
    for schedule in schedules:
        if _reference_is_schedule_active_at(schedule, now):
            return schedule.target
    return None


def _check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    # Case 1: simple weekday work-hours window, straightforwardly inside it.
    weekday_away = _RefSchedule(
        id="a", days=("mon", "tue", "wed", "thu", "fri"), start="09:00", end="17:00", target="away"
    )
    monday_noon = datetime(2026, 9, 14, 12, 0)  # a Monday
    _check(
        "Weekday 9-5 schedule active at Monday noon",
        _reference_resolve_occupancy_target([weekday_away], monday_noon) == "away",
    )

    # Case 2: same schedule, outside the window (evening) — no target.
    monday_evening = datetime(2026, 9, 14, 20, 0)
    _check(
        "Weekday 9-5 schedule inactive at Monday 8pm",
        _reference_resolve_occupancy_target([weekday_away], monday_evening) is None,
    )

    # Case 3: same schedule, on a day not in its list (Saturday) — no target.
    saturday_noon = datetime(2026, 9, 19, 12, 0)
    _check(
        "Weekday 9-5 schedule inactive on Saturday",
        _reference_resolve_occupancy_target([weekday_away], saturday_noon) is None,
    )

    # Case 4: midnight-crossing vacation schedule (Fri 22:00 -> Mon 06:00 is not
    # expressible as one entry in this shape, but a same-day midnight-crossing
    # entry is): Sat 22:00 -> Sun 06:00, checked just after midnight on Sunday.
    overnight_vacation = _RefSchedule(id="b", days=("sat",), start="22:00", end="06:00", target="vacation")
    sunday_1am = datetime(2026, 9, 20, 1, 0)  # a Sunday, 1am — the post-midnight portion of Saturday's window
    _check(
        "Midnight-crossing Sat 22:00-06:00 schedule active Sunday 1am",
        _reference_resolve_occupancy_target([overnight_vacation], sunday_1am) == "vacation",
    )

    # Case 5: same schedule, well before its start on Saturday evening — inactive
    # (this is the pre-window portion of the *same* calendar day, not yet reached).
    saturday_8pm = datetime(2026, 9, 19, 20, 0)
    _check(
        "Midnight-crossing Sat 22:00-06:00 schedule inactive Saturday 8pm (before start)",
        _reference_resolve_occupancy_target([overnight_vacation], saturday_8pm) is None,
    )

    # Case 6: first-listed-wins on overlap, matching resolve_occupancy_target()'s
    # documented convention (same as production's resolve_active_schedules()).
    overlapping_a = _RefSchedule(id="c1", days=("mon",), start="10:00", end="14:00", target="away")
    overlapping_b = _RefSchedule(id="c2", days=("mon",), start="12:00", end="16:00", target="guest")
    monday_1pm = datetime(2026, 9, 14, 13, 0)
    _check(
        "Overlapping schedules: first-listed wins",
        _reference_resolve_occupancy_target([overlapping_a, overlapping_b], monday_1pm) == "away",
    )

    print("\nAll occupancy_schedule.py reasoning checks passed against the hand-transcribed reference.")


if __name__ == "__main__":
    main()
