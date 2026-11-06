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
