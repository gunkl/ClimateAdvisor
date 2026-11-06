"""Issue #1378: an overnight pre-cool decided in the evening survives midnight and is judged on the day it banks for.

Occupant view: on a warm day before a hot one, Climate Advisor scheduled tonight's pre-cool (02:30) and the 23:59 day
reset cancelled it. After midnight it was re-decided on the new day's classification, whose "tomorrow" is the day
AFTER the hot one, so the house usually got no overnight banking before the hot day it was meant to prepare for.
The mirror: after midnight a night before a MILD day was pre-cooled because the day after that was hot (AC running
at 02:30 for nothing). Now a night is always judged on the day it wakes into.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import (  # noqa: E402
    HOT_DAY_PRE_COOL_MODIFIER,
    PreCoolDecision,
    pre_cool_waking_date,
    resolve_pre_cool_modifier_for_night,
)
from custom_components.climate_advisor.coordinator import _compute_pre_cool_trigger_time_pure  # noqa: E402

_CFG = {"comfort_cool": 74.0, "sleep_cool": 78.0, "sleep_time": "22:30", "wake_time": "06:30", "threshold_hot": 85}
_D = date(2026, 7, 15)  # warm day
_HOT = date(2026, 7, 16)  # the hot day the pre-cool banks for
_DEC = PreCoolDecision(_HOT, -3.0, 70.0, 55.0)  # decided on Jul 15 (forecast 70/55) for the night ending Jul 16


def _at(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=UTC)


def _c(today_high: float, tomorrow_high: float, setback_modifier: float = 0.0, window_close=None, today_low=60.0):
    return SimpleNamespace(
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=tomorrow_high,
        setback_modifier=setback_modifier,
        window_close_time=window_close,
    )


def _coord_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


# ---------------------------------------------------------------------------
# Which day a night wakes into
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (_at(_HOT, 0, 0), _HOT),  # just after midnight: wakes into today
        (_at(_HOT, 2, 30), _HOT),
        (_at(_HOT, 6, 29), _HOT),
        (_at(_HOT, 6, 30), date(2026, 7, 17)),  # from wake on: tonight wakes into tomorrow
        (_at(_D, 23, 59), _HOT),
    ],
)
def test_pre_cool_waking_date(at, expected):
    assert pre_cool_waking_date(at, _CFG) == expected


def test_pre_cool_waking_date_malformed_wake_uses_0630():
    assert pre_cool_waking_date(_at(_HOT, 5, 0), {"wake_time": "bad"}) == _HOT
    assert pre_cool_waking_date(_at(_HOT, 7, 0), {"wake_time": "bad"}) == date(2026, 7, 17)


# ---------------------------------------------------------------------------
# Eligibility matrix: (frame) x (today hot?) x (tomorrow hot?) x (latched decision)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "classification", "at", "now", "decided", "expected"),
    [
        # Before midnight / after wake: the night wakes into the classification's TOMORROW (unchanged behaviour).
        ("evening, tomorrow hot", _c(80, 95), _at(_HOT, 2, 30), _at(_D, 20), None, HOT_DAY_PRE_COOL_MODIFIER),
        ("evening, warming trend", _c(70, 80, -3.0), _at(_HOT, 2, 30), _at(_D, 20), None, -3.0),
        ("evening, tomorrow mild", _c(95, 75), _at(_HOT, 2, 30), _at(_D, 20), None, None),
        # After midnight: the night wakes into the classification's TODAY.
        ("after midnight, today hot, tomorrow mild", _c(95, 75), _at(_HOT, 2, 30), _at(_HOT, 0), None, -2.0),
        # Mirror: a night before a MILD day is not pre-cooled because the day after is hot.
        ("after midnight, today mild, tomorrow hot", _c(75, 95), _at(_HOT, 2, 30), _at(_HOT, 0), None, None),
        # The warming trend in today's classification is into TOMORROW: it does not qualify tonight.
        ("after midnight, trend into tomorrow", _c(75, 85, -3.0), _at(_HOT, 2, 30), _at(_HOT, 0), None, None),
        # A decision made the day before is RE-CHECKED on the fresh forecast for today (owner decision): the
        # warming trend is measured from the deciding day's high/low (70/55) to today's, with the classifier's
        # thresholds (avg >= 10 -> -3, >= 5 -> -2).
        ("latched, still a big warm-up", _c(82, 70, today_low=65), _at(_HOT, 2, 30), _at(_HOT, 0), _DEC, -3.0),
        ("latched, warm-up shrank to moderate", _c(77, 70, today_low=60), _at(_HOT, 2, 30), _at(_HOT, 0), _DEC, -2.0),
        # The forecast was revised overnight: no warm-up any more and not hot -> the night's pre-cool is dropped.
        ("latched, forecast revised away", _c(72, 70, today_low=56), _at(_HOT, 2, 30), _at(_HOT, 0), _DEC, None),
        # No warm-up but today hot -> the hot-day gate still holds.
        ("latched, no warm-up but hot", _c(88, 70, today_low=50), _at(_HOT, 2, 30), _at(_HOT, 0), _DEC, -2.0),
        # A decision without a base (made after midnight) can only use the hot gate.
        (
            "latched, no base",
            _c(82, 70, today_low=65),
            _at(_HOT, 2, 30),
            _at(_HOT, 0),
            PreCoolDecision(_HOT, -3.0),
            None,
        ),
        # A decision for another night is ignored.
        (
            "stale latch",
            _c(82, 70, today_low=65),
            _at(_HOT, 2, 30),
            _at(_HOT, 0),
            PreCoolDecision(_D, -3.0, 70, 55),
            None,
        ),
        # After midnight, a trigger for the NEXT night (scheduled before wake) is judged on tomorrow as before.
        ("05:00, next night, tomorrow hot", _c(75, 95), _at(date(2026, 7, 17), 2, 30), _at(_HOT, 5), None, -2.0),
    ],
)
def test_eligibility_is_judged_on_the_day_the_night_wakes_into(label, classification, at, now, decided, expected):
    got = resolve_pre_cool_modifier_for_night(classification, _CFG, at=at, now=now, decided=decided)
    assert got == expected, label


def test_no_classification_is_not_eligible():
    assert resolve_pre_cool_modifier_for_night(None, _CFG, at=_at(_HOT, 2), now=_at(_HOT, 0)) is None


# ---------------------------------------------------------------------------
# Trigger time (pure): the 00:00 re-decision after the reset
# ---------------------------------------------------------------------------


def test_after_midnight_a_hot_today_schedules_tonight():
    got = _compute_pre_cool_trigger_time_pure(_c(95, 75), _CFG, _at(_HOT, 0, 0))
    assert got == _at(_HOT, 2, 30)


def test_after_midnight_a_mild_today_before_a_hot_tomorrow_schedules_nothing_tonight():
    """Mirror of #1378: the night before a mild day used to be pre-cooled because the day after it was hot."""
    assert _compute_pre_cool_trigger_time_pure(_c(75, 95), _CFG, _at(_HOT, 0, 0)) is None


def test_after_midnight_the_latched_decision_keeps_tonight_while_the_forecast_still_supports_it():
    got = _compute_pre_cool_trigger_time_pure(_c(82, 70, today_low=65), _CFG, _at(_HOT, 0, 0), decided=_DEC)
    assert got == _at(_HOT, 2, 30)
    assert _compute_pre_cool_trigger_time_pure(_c(72, 70, today_low=56), _CFG, _at(_HOT, 0, 0), decided=_DEC) is None


# ---------------------------------------------------------------------------
# Coordinator: midnight reset keeps the night's decision; the next night is decided after it
# ---------------------------------------------------------------------------


def _coord(classification, *, now: datetime):
    coord = object.__new__(_coord_class())
    coord.hass = MagicMock()
    coord.config = dict(_CFG)
    coord._current_classification = classification
    coord._pre_cool_trigger_scheduled = False
    coord._pre_cool_trigger_cancel = None
    coord._pre_cool_trigger_dt = None
    coord._pre_cool_status = None
    coord._pre_cool_target = None
    coord._pre_cool_decided = None
    coord._zone_scoped = lambda fn: fn
    return coord


def _schedule(coord, now):
    with (
        patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
        patch("custom_components.climate_advisor.coordinator.async_track_point_in_time") as track,
    ):
        coord._maybe_schedule_pre_cool()
    return track


def test_evening_decision_is_recorded_for_the_night_it_banks_for():
    coord = _coord(_c(80, 95), now=_at(_D, 14))
    track = _schedule(coord, _at(_D, 14))
    assert track.call_count == 1
    assert coord._pre_cool_trigger_dt == _at(_HOT, 2, 30)
    assert coord._pre_cool_decided == PreCoolDecision(_HOT, HOT_DAY_PRE_COOL_MODIFIER, 80, 60)


async def _end_of_day(coord, now):
    coord._today_record = None
    coord._hvac_session_mode = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord.automation_engine = MagicMock()
    coord._refresh_hourly_forecast = AsyncMock()
    coord._async_save_state = AsyncMock()
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
        await _coord_class()._async_end_of_day(coord, now)


def test_day_reset_keeps_a_pre_cool_decided_for_the_coming_night():
    """The 23:59 reset used to cancel the 02:30 trigger and lose the night's banking (generated day 101000063)."""
    coord = _coord(_c(80, 95), now=_at(_D, 14))
    _schedule(coord, _at(_D, 14))
    cancel = MagicMock()
    coord._pre_cool_trigger_cancel = cancel
    asyncio.run(_end_of_day(coord, _at(_D, 23, 59)))
    cancel.assert_not_called()
    assert coord._pre_cool_trigger_dt == _at(_HOT, 2, 30)
    assert coord._pre_cool_trigger_scheduled is True
    assert coord._pre_cool_decided == PreCoolDecision(_HOT, HOT_DAY_PRE_COOL_MODIFIER, 80, 60)

    # 00:00 cycle on the hot day: its classification looks at a mild day after. No second schedule, no cancel.
    coord._current_classification = _c(95, 75)
    track = _schedule(coord, _at(_HOT, 0, 0))
    track.assert_not_called()
    cancel.assert_not_called()


def test_day_reset_still_clears_a_night_that_is_over():
    coord = _coord(_c(80, 95), now=_at(_D, 14))
    coord._pre_cool_trigger_scheduled = True
    coord._pre_cool_decided = PreCoolDecision(_D, -2.0)  # fired at 02:30 this morning
    coord._pre_cool_status = "pre-cool active (76°F ceiling)"
    asyncio.run(_end_of_day(coord, _at(_D, 23, 59)))
    assert coord._pre_cool_trigger_scheduled is False
    assert coord._pre_cool_decided is None
    assert coord._pre_cool_status is None


def test_day_reset_clears_when_nothing_was_decided():
    coord = _coord(_c(70, 70), now=_at(_D, 14))
    asyncio.run(_end_of_day(coord, _at(_D, 23, 59)))
    assert coord._pre_cool_trigger_scheduled is False and coord._pre_cool_decided is None


def test_next_night_is_decided_after_this_nights_trigger_fired():
    """After a 02:30 pre-cool fires, the next night is decided after wake on today's classification (whose
    tomorrow IS that night's day, warming trend included) instead of waiting for the 23:59 reset."""
    coord = _coord(_c(80, 92, -3.0), now=_at(_HOT, 3))
    coord._pre_cool_trigger_scheduled = True
    coord._pre_cool_decided = PreCoolDecision(_HOT, -2.0)

    # 03:00, same night: nothing new.
    assert _schedule(coord, _at(_HOT, 3)).call_count == 0
    # 09:00: the night ending Jul 17 is decided now, with the warming trend.
    assert _schedule(coord, _at(_HOT, 9)).call_count == 1
    assert coord._pre_cool_trigger_dt == _at(date(2026, 7, 17), 2, 30)
    assert coord._pre_cool_decided == PreCoolDecision(date(2026, 7, 17), -3.0, 80, 60)


def test_a_flag_set_without_a_decision_still_blocks_scheduling():
    """Partial stubs that set only the old once-per-day flag keep their meaning."""
    coord = _coord(_c(80, 95), now=_at(_D, 14))
    coord._pre_cool_trigger_scheduled = True
    assert _schedule(coord, _at(_D, 14)).call_count == 0


# ---------------------------------------------------------------------------
# Coordinator: the trigger fires with the night's own judgment
# ---------------------------------------------------------------------------


def _fire(coord, now):
    coord.automation_engine = MagicMock()
    coord.automation_engine.natural_vent_active = False
    coord.automation_engine.handle_pre_cool = AsyncMock(return_value="applied: 76.0")
    coord._get_indoor_temp = MagicMock(return_value=77.0)
    coord.async_refresh = AsyncMock()
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
        asyncio.run(coord._async_pre_cool_trigger(now))
    return coord.automation_engine.handle_pre_cool


def test_trigger_after_midnight_applies_the_evening_decision():
    """02:30 on the hot day: today's classification sees a mild tomorrow; the warm-up the evening decided on (70/55
    -> 95/70) still holds on the fresh forecast, so the pre-cool runs with the trend modifier."""
    coord = _coord(_c(95, 75, today_low=70), now=_at(_HOT, 2, 30))
    coord._pre_cool_decided = _DEC
    handle = _fire(coord, _at(_HOT, 2, 30))
    handle.assert_awaited_once()
    assert handle.await_args.kwargs["modifier"] == -3.0


def test_trigger_after_midnight_skips_when_the_forecast_was_revised_away():
    """Re-checked at fire time: overnight the hot day was revised to 72/56 (barely warmer than the deciding day's
    70/55, and not hot), so the AC does not run at 02:30."""
    coord = _coord(_c(72, 70, today_low=56), now=_at(_HOT, 2, 30))
    coord._pre_cool_decided = _DEC
    handle = _fire(coord, _at(_HOT, 2, 30))
    handle.assert_not_awaited()


def test_trigger_after_midnight_before_a_mild_day_is_skipped():
    """Mirror: nothing decided for this night, today is mild -> no AC at 02:30 even though tomorrow is hot."""
    coord = _coord(_c(75, 95), now=_at(_HOT, 2, 30))
    handle = _fire(coord, _at(_HOT, 2, 30))
    handle.assert_not_awaited()


def test_trigger_before_midnight_still_judges_tomorrow():
    coord = _coord(_c(80, 95), now=_at(_D, 23, 0))
    handle = _fire(coord, _at(_D, 23, 0))
    assert handle.await_args.kwargs["modifier"] == HOT_DAY_PRE_COOL_MODIFIER


# ---------------------------------------------------------------------------
# Engine: an explicit modifier is used as given; without one the old judgment applies (harness/goldens)
# ---------------------------------------------------------------------------


def test_handle_pre_cool_uses_the_callers_modifier():
    from custom_components.climate_advisor import automation as am

    eng = object.__new__(am.AutomationEngine)
    eng._current_classification = _c(75, 70)  # not eligible on its own
    eng.config = dict(_CFG)
    with (
        patch.object(am, "_in_sleep_window", return_value=False),
        patch("custom_components.climate_advisor.automation.dt_util.now", return_value=_at(_HOT, 2, 30)),
    ):
        # It gets past the eligibility gate (to the awake-hours backstop) only with the caller's modifier.
        assert asyncio.run(eng.handle_pre_cool(77.0, True, modifier=-3.0)) == "skipped: awake hours"
        assert asyncio.run(eng.handle_pre_cool(77.0, True)) == "skipped: not eligible"
