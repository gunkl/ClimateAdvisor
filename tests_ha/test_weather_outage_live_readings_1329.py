"""Real-HA test for Issue #1329: a weather outage no longer blanks a dedicated outdoor sensor or the thermostat.

Occupant story (2026-11-04, Zone 1): outdoor temperature comes from the zone's own sensor and indoor from the
thermostat, both fine all night. Every time the weather service (Met.no) went unavailable the dashboard showed outdoor
"unknown" and the chart log recorded no indoor and no outdoor reading, leaving gaps in the chart. And a door opening
during the weather retries used up a retry, collapsing the 30/60/120/240/480 s backoff into seconds. Prove a test is not
vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

OUTDOOR = "sensor.house_outdoor_f"


async def _zone_with_outdoor_sensor(make_house):
    house = await make_house(
        contacts=["window"],
        startup="settled",
        start=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        initial_indoor_f=67.0,
        outdoor_f=58.0,
        high_f=66.0,
        low_f=50.0,
        initial_hvac_mode="heat",
        ca_config={"outdoor_temp_source": "sensor", "outdoor_temp_entity": OUTDOOR},
    )
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.weather is not None, "precondition: the house has a weather entity"
    return house


def _weather_down(house) -> None:
    house.weather._attr_available = False
    house.weather.async_write_ha_state()


async def test_dashboard_and_chart_keep_live_readings_while_the_weather_service_is_down(make_house) -> None:
    house = await _zone_with_outdoor_sensor(make_house)
    coord = house.coordinator
    _weather_down(house)
    house.hass.states.async_set(OUTDOOR, "57.5")
    await house.advance(minutes=6)  # the 5-minute refresh has the sensor value
    await coord.async_refresh()  # a 30-minute cycle runs with no forecast
    # Read straight after the cycle: before #1329 the cycle blanked the dashboard value ("unknown" on the occupant's
    # displays) until the next 5-minute refresh put the sensor reading back.
    assert coord.data.get("outdoor_temp") == pytest.approx(57.5), "the cycle blanked the outdoor sensor reading"
    await house.advance(minutes=31)

    assert coord._current_classification is not None
    polls = [e for e in coord._chart_log._entries if e.get("hvac") is not None][-3:]
    assert polls, "precondition: the cycle wrote chart entries"
    last = polls[-1]
    assert last["outdoor"] == pytest.approx(57.5), "the chart log blanked the outdoor sensor during the outage"
    assert last["indoor"] is not None, "the chart log blanked the thermostat's reading during the outage"


async def test_a_door_event_during_the_weather_retries_does_not_use_one_up(make_house) -> None:
    house = await _zone_with_outdoor_sensor(make_house)
    coord = house.coordinator
    _weather_down(house)
    await coord.async_refresh()  # the outage is seen: retry 1 of 5 scheduled
    await house.advance(seconds=1)
    remaining = coord._startup_retries_remaining
    assert coord._weather_retry_pending, "precondition: a retry is scheduled"

    await house.open("window")  # unrelated refreshes before the 30 s retry fires
    await house.advance(seconds=5)
    await house.close_contact("window")
    await house.advance(seconds=5)
    assert coord._startup_retries_remaining == remaining, "a door event used up a weather retry"

    await house.advance(seconds=30)  # the scheduled retry fires and is the one that counts
    assert coord._startup_retries_remaining == remaining - 1
