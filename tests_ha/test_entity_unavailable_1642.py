"""Real-HA test for Issues #1642 and #1648: entity-unavailable and the timestamp sensors.

Occupant story (#1642): the weather service or the thermostat drops out. Climate Advisor keeps going on its fallbacks
and the Status card names the outage; before, the Outdoor / Indoor Temperature and Forecast High / Low sensors kept
showing a confident value with nothing behind it. Now they show unavailable at once, and their value again as soon as
the source is back. A failed update makes every sensor unavailable until the next good one, but the Automation switch
stays usable, so observe-only can always be turned on.

Occupant story (#1648): Last Action Time and Next Automation Time are Home Assistant timestamps, so a card shows
"5 minutes ago" / "in 2 hours" and an automation can trigger on them.

Real Home Assistant, the real coordinator and the real platforms. Explicit asserts only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_READINGS_FROM_WEATHER = ("outdoor_temp", "forecast_high", "forecast_low")


async def _house(make_house):
    house = await make_house(
        startup="settled",
        start=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        initial_indoor_f=67.0,
        outdoor_f=58.0,
        high_f=66.0,
        low_f=50.0,
        initial_hvac_mode="heat",
    )
    await house.advance(minutes=6)  # past the start-up window
    assert house.weather is not None, "precondition: the house has a weather entity"
    return house


def _set_weather_available(house, available: bool) -> None:
    house.weather._attr_available = available
    house.weather.async_write_ha_state()


def _thermostat(house):
    return house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]


async def test_weather_outage_makes_its_readings_unavailable_at_once_and_they_return(make_house) -> None:
    house = await _house(make_house)
    for key in (*_READINGS_FROM_WEATHER, "indoor_temp", "day_type", "automation_status"):
        assert house.sensor(key).state != "unavailable", key

    _set_weather_available(house, False)
    await house.hass.async_block_till_done()  # no cycle needed: the sensors follow the weather entity
    for key in _READINGS_FROM_WEATHER:
        assert house.sensor(key).state == "unavailable", key
    # The thermostat's reading and the plan are not the weather's: they stay.
    for key in ("indoor_temp", "day_type", "automation_status", "next_automation_action"):
        assert house.sensor(key).state != "unavailable", key

    await house.advance(minutes=31)  # a cycle on the kept forecast does not bring the kept value back
    for key in _READINGS_FROM_WEATHER:
        assert house.sensor(key).state == "unavailable", key

    _set_weather_available(house, True)  # the mirror
    await house.hass.async_block_till_done()
    for key in _READINGS_FROM_WEATHER:
        state = house.sensor(key)
        assert state.state != "unavailable", key
        float(state.state)  # a number again, not unknown


async def test_thermostat_gone_makes_indoor_temperature_unavailable_and_it_returns(make_house) -> None:
    house = await _house(make_house)
    thermostat = _thermostat(house)
    assert house.sensor("indoor_temp").state != "unavailable"

    thermostat._attr_available = False
    thermostat.async_write_ha_state()
    await house.hass.async_block_till_done()
    assert house.sensor("indoor_temp").state == "unavailable"
    assert house.sensor("outdoor_temp").state != "unavailable"

    thermostat._attr_available = True  # the mirror
    thermostat.async_write_ha_state()
    await house.hass.async_block_till_done()
    assert house.sensor("indoor_temp").state != "unavailable"


async def test_failed_update_makes_sensors_unavailable_but_not_the_switch_and_the_next_good_update_recovers(
    make_house,
) -> None:
    from homeassistant.helpers import entity_registry as er

    house = await _house(make_house)
    coord = house.coordinator
    hass, entry_id = house.hass, house.ca_entry.entry_id
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    sensors = [e.entity_id for e in entries if e.domain == "sensor" and e.disabled_by is None]
    (switch,) = [e.entity_id for e in entries if e.domain == "switch"]

    original = coord._async_update_data_impl

    async def _fail():
        raise RuntimeError("simulated update failure")

    coord._async_update_data_impl = _fail
    try:
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert coord.last_update_success is False, "precondition: the update failed"
        assert [s for s in sensors if hass.states.get(s).state != "unavailable"] == []
        assert hass.states.get(switch).state in ("on", "off"), "the Automation switch went unavailable"
    finally:
        coord._async_update_data_impl = original

    await coord.async_refresh()  # the mirror: the next good update
    await hass.async_block_till_done()
    assert coord.last_update_success is True
    assert [s for s in sensors if hass.states.get(s).state == "unavailable"] == []


async def test_last_action_and_next_automation_times_are_timestamps(make_house) -> None:
    from homeassistant.util import dt as dt_util

    house = await _house(make_house)
    coord = house.coordinator
    await coord.async_refresh()
    await house.hass.async_block_till_done()

    recorded = coord.automation_engine._last_action_time  # the engine's ISO string (local, with microseconds)
    assert isinstance(recorded, str), "precondition: the settled house has taken an action"
    last = house.sensor("last_action_time")
    assert last.attributes["device_class"] == "timestamp"
    # Home Assistant's timestamp state: the same moment, in UTC, to the second.
    assert last.state == dt_util.parse_datetime(recorded).astimezone(dt_util.UTC).isoformat(timespec="seconds")

    nxt = house.sensor("next_automation_time")
    assert nxt.attributes["device_class"] == "timestamp"
    if nxt.state != "unknown":  # "no more actions today" reads as unknown
        when = dt_util.parse_datetime(nxt.state)
        assert when is not None and when.tzinfo is not None, nxt.state
        assert when > dt_util.utcnow()
    ai = house.sensor("ai_status")
    assert ai.attributes["device_class"] == "enum"
    assert ai.state in ai.attributes["options"]
