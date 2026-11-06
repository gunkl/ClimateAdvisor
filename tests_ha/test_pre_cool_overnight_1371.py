"""Issue #1371 against real Home Assistant: the overnight pre-cool is scheduled at night, not mid-morning.

Occupant view: on a warm day before a hot one, Climate Advisor used to schedule its "overnight" pre-cool for
10:30 AM (the day's window-close time plus 30 minutes). That flipped the thermostat cool, then heat, then cool
while the house was comfortable. Now the pre-cool is scheduled inside the sleep window, and nothing pre-cools
during the day.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.automation import _in_sleep_window, pre_cool_sleep_window_config


async def _hot_tomorrow(house) -> None:
    house.weather.set_conditions(tomorrow_high_f=99.0)


async def test_warm_day_before_a_hot_one_schedules_the_pre_cool_at_night(make_house) -> None:
    house = await make_house(
        units="us",
        profile="scripted",
        start=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
        initial_indoor_f=72.0,
        high_f=84.0,
        low_f=62.0,
        before_ca_setup=_hot_tomorrow,
    )
    await house.advance(minutes=10)
    coordinator = house.coordinator
    classification = coordinator.automation_engine._current_classification
    assert classification is not None and classification.day_type == "warm", classification
    assert classification.window_close_time is not None  # the 10:00 close that used to drive the trigger

    trigger = coordinator._pre_cool_trigger_dt
    assert trigger is not None, "a warm day before a 99 F day is pre-cool eligible"
    assert _in_sleep_window(trigger, pre_cool_sleep_window_config(coordinator.config)), trigger

    # Through the old 10:30 trigger time and past noon: nothing pre-cools while people are awake.
    await house.advance(hours=5)
    events = [e for e in coordinator._event_log if e.get("type") == "pre_cool_applied"]
    assert events == [], events


async def test_pre_cool_decided_in_the_evening_survives_midnight(make_house) -> None:
    """Issue #1378: the 02:30 pre-cool decided on the warm day for the hot day still runs after the 23:59 reset.

    After midnight the forecast rolls: today is the 99 F day and tomorrow is mild. Judged on that new day's
    "tomorrow" the night used to be dropped, so the house went into the hot day without its overnight banking.
    """
    house = await make_house(
        units="us",
        profile="scripted",
        start=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
        initial_indoor_f=80.0,
        high_f=84.0,
        low_f=62.0,
        before_ca_setup=_hot_tomorrow,
    )
    await house.advance(minutes=10)
    coordinator = house.coordinator
    trigger = coordinator._pre_cool_trigger_dt
    assert trigger == datetime(2026, 7, 16, 2, 30, tzinfo=UTC), trigger

    # Through the 23:59 day reset.
    await house.advance_to(datetime(2026, 7, 15, 23, 59, 30, tzinfo=UTC))
    assert coordinator._pre_cool_trigger_dt == trigger, "the day reset must keep the night's pre-cool"

    # The forecast rolls at midnight: the hot day is now today, the day after it is mild.
    house.weather.set_conditions(high_f=99.0, low_f=70.0, tomorrow_high_f=75.0)
    house.weather.async_write_ha_state()
    await house.advance_to(datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    classification = coordinator.automation_engine._current_classification
    assert classification.today_high >= 95 and classification.tomorrow_high < 80, classification

    applied = [e for e in coordinator._event_log if e.get("type") == "pre_cool_applied"]
    assert len(applied) == 1, [e.get("type") for e in coordinator._event_log][-30:]
