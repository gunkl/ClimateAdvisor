"""Real-HA tests for Issue #1337: a weather outage no longer stops the work that does not need the weather service.

Occupant story (2026-11-04, Zone 1): Met.no was unavailable for 154 minutes. The dashboard and chart fell back to a
made-up cosine day although a real 48-hour forecast had arrived minutes earlier, and the 30-minute cycle skipped
everything behind "did the weather service answer": comfort minutes, the day's record, ventilation re-checks and the
end of the post-restart window that ignores the occupant's thermostat changes. On a weather-service install the
engine and thermal learning read a hard-coded 65°F as the outdoor temperature. Prove a test is not vacuous with
``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

OUTDOOR = "sensor.house_outdoor_f"


async def _zone(
    make_house, *, outdoor_sensor: bool, high_f: float = 66.0, low_f: float = 50.0, indoor_f: float = 67.0, **extra
):
    ca_config = {"outdoor_temp_source": "sensor", "outdoor_temp_entity": OUTDOOR} if outdoor_sensor else {}
    house = await make_house(
        contacts=["window"],
        startup="settled",
        start=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        initial_indoor_f=indoor_f,
        outdoor_f=(high_f + low_f) / 2,
        high_f=high_f,
        low_f=low_f,
        initial_hvac_mode="heat",
        ca_config=ca_config,
        **extra,
    )
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.weather is not None, "precondition: the house has a weather entity"
    return house


def _weather_down(house) -> None:
    house.weather._attr_available = False
    house.weather.async_write_ha_state()


async def test_the_cycle_keeps_running_on_the_kept_forecast(make_house) -> None:
    house = await _zone(make_house, outdoor_sensor=True)
    coord = house.coordinator
    fetched_at = coord._classification_fetched_at
    assert coord._daily_forecast_full and coord._hourly_forecast_temps, "precondition: a forecast was fetched"
    _weather_down(house)
    house.hass.states.async_set(OUTDOOR, "57.5")
    await coord.async_refresh()

    assert coord._daily_forecast_full, "the outage wiped the daily forecast"
    assert coord._hourly_forecast_temps, "the outage wiped the hourly forecast"
    assert coord._current_classification is not None
    assert coord._classification_fetched_at == fetched_at, "a kept forecast is not a fresh fetch"
    assert coord._weather_retry_pending, "the weather service is still retried while the kept forecast is used"
    assert coord.data.get("outdoor_temp") == pytest.approx(57.5)


async def test_comfort_minutes_are_counted_while_the_weather_service_is_down(make_house) -> None:
    house = await _zone(make_house, outdoor_sensor=True)
    coord = house.coordinator
    _weather_down(house)
    house.hass.states.async_set(OUTDOOR, "57.5")
    await coord.async_refresh()
    before = coord._today_record.comfort_violations_minutes
    house.set_indoor(50.0)  # far below the comfort floor: no furnace closes 18°F in half an hour
    await house.advance(minutes=31)
    await coord.async_refresh()
    assert coord._today_record.comfort_violations_minutes > before, "time spent too cold during the outage was lost"


async def test_a_weather_service_install_never_reads_the_65f_placeholder(make_house) -> None:
    house = await _zone(make_house, outdoor_sensor=False, high_f=95.0, low_f=85.0, indoor_f=76.0)
    coord = house.coordinator
    _weather_down(house)
    await house.advance(minutes=6)  # the 5-minute outdoor refresh runs during the outage
    assert 84.0 <= coord._last_outdoor_temp <= 96.0, f"engine outdoor was {coord._last_outdoor_temp}, not the forecast"
    sample = coord._get_current_sample(elapsed_minutes=0)
    assert sample is not None and 84.0 <= sample["outdoor_temp_f"] <= 96.0, "thermal learning sampled a fake outdoor"
    await coord.async_refresh()
    assert coord.data.get("outdoor_temp") is not None, "the dashboard lost outdoor although the forecast covers now"


async def test_a_restart_during_the_outage_still_ends_the_startup_window(make_house) -> None:
    # "scripted": the thermostat simply takes every command (as in test_second_change_p4_1325), so the only question
    # is whether Climate Advisor sees the occupant's change.
    house = await _zone(make_house, outdoor_sensor=True, profile="scripted")
    _weather_down(house)
    house.hass.states.async_set(OUTDOOR, "57.5")
    await house.coordinator.async_refresh()  # the kept forecast is saved with the state
    await house.restart()  # comes back while the weather service is still down; settles past 300 s
    coord = house.coordinator
    assert coord._daily_forecast_full, "the kept forecast did not survive the restart"
    assert coord._startup_coalesce_active is False, "the post-restart window never ended during the outage"

    engine = coord.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append(event_type)
        forward(event_type, data)

    engine._emit_event_callback = _tee
    for _ in range(15):  # >120 s after the coalesce's own command, so the change cannot be read as its echo
        await house.advance(seconds=10)
    await house.occupant_sets_thermostat(mode="cool", setpoint_f=74.0)
    for _ in range(40):
        await house.advance(seconds=10)
    assert "override_detected" in seen, f"the occupant's change after the window was ignored: {seen}"
