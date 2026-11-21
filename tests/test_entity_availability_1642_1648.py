"""Issues #1642 and #1648: Home Assistant quality scale, entity-unavailable and entity-device-class.

Occupant story (#1642): when the thermostat or the weather service drops out, Climate Advisor keeps controlling on
its fallbacks, and the Status card says what is down. Before, the Indoor / Outdoor Temperature and Forecast High / Low
sensors kept showing their last value as if it were current, so a dashboard showed a confident reading with nothing
behind it. They now show unavailable while their source is gone, and their value again as soon as it is back. The plan
and status sensors keep their values (Climate Advisor is still deciding), and the Automation switch stays usable, so
the occupant can always turn Climate Advisor to observe-only.

Occupant story (#1648): Last Action Time and Next Automation Time are timestamps, so a card shows them as "5 minutes
ago" / "in 2 hours" in the viewer's language and an automation can trigger on them. AI Status lists its fixed states.

Unit tier (lightweight stubs): the real sensor classes with a mocked coordinator. The real-HA behaviour (the state
written, the listener on the source entities) is in ``tests_ha/test_entity_unavailable_1642.py``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from homeassistant.components.sensor import SensorDeviceClass  # noqa: E402

from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor import sensor as sensor_platform  # noqa: E402
from custom_components.climate_advisor import switch as switch_platform  # noqa: E402
from custom_components.climate_advisor.const import DOMAIN  # noqa: E402
from custom_components.climate_advisor.status_text import CodedText  # noqa: E402

_READINGS = {"indoor_temp", "outdoor_temp", "forecast_high", "forecast_low"}


def _entities(platform, *, config=None, data=None):
    coord = MagicMock()
    coord.config = {"climate_entity": "climate.t", "weather_entity": "weather.home", **(config or {})}
    coord.data = {"indoor_temp": 70.0, "outdoor_temp": 50.0, "forecast_high": 60.0, "forecast_low": 40.0} | (data or {})
    coord.last_update_success = True
    coord._get_indoor_temp.return_value = 70.0
    coord._weather_attrs_and_down.return_value = ({}, False)
    coord._live_dedicated_outdoor_temp.return_value = None
    hass = MagicMock()
    hass.data = {DOMAIN: {"zone1": coord}}
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.title = "Living Room"
    added: list = []
    asyncio.run(platform.async_setup_entry(hass, entry, added.extend))
    return coord, {e._attr_translation_key: e for e in added}


def _available(sensors) -> set[str]:
    return {k for k, s in sensors.items() if s.available}


# --- #1642: coordinator update failure ---------------------------------------------------------------------------


def test_a_failed_coordinator_update_makes_every_sensor_unavailable_and_the_next_good_one_recovers():
    coord, sensors = _entities(sensor_platform)
    assert _available(sensors) == set(sensors)
    coord.last_update_success = False
    assert _available(sensors) == set()
    coord.last_update_success = True  # the mirror: the next good update
    assert _available(sensors) == set(sensors)


def test_the_automation_switch_stays_usable_through_a_failed_update():
    coord, switches = _entities(switch_platform)
    coord.last_update_success = False
    assert switches["automation_enabled"].available is True


# --- #1642: source lost ------------------------------------------------------------------------------------------


def test_thermostat_gone_with_no_other_indoor_reading_makes_only_indoor_temperature_unavailable():
    coord, sensors = _entities(sensor_platform)
    coord._get_indoor_temp.return_value = None  # thermostat missing, no dedicated sensor answering
    assert set(sensors) - _available(sensors) == {"indoor_temp"}
    coord._get_indoor_temp.return_value = 69.5  # back
    assert sensors["indoor_temp"].available is True


def test_indoor_reading_missing_from_the_cycle_is_unavailable_not_unknown():
    _coord, sensors = _entities(sensor_platform, data={"indoor_temp": None})
    assert sensors["indoor_temp"].available is False


def test_weather_service_down_makes_outdoor_and_forecast_unavailable_and_keeps_the_plan():
    coord, sensors = _entities(sensor_platform)
    coord._weather_attrs_and_down.return_value = ({}, True)
    assert set(sensors) - _available(sensors) == {"outdoor_temp", "forecast_high", "forecast_low"}
    coord._weather_attrs_and_down.return_value = ({}, False)  # the mirror: service back
    assert _available(sensors) == set(sensors)


def test_weather_down_with_a_live_dedicated_outdoor_sensor_keeps_outdoor_temperature():
    coord, sensors = _entities(
        sensor_platform, config={"outdoor_temp_source": "sensor", "outdoor_temp_entity": "sensor.out"}
    )
    coord._weather_attrs_and_down.return_value = ({}, True)
    coord._live_dedicated_outdoor_temp.return_value = 48.0
    assert sensors["outdoor_temp"].available is True
    assert sensors["forecast_high"].available is False
    coord._live_dedicated_outdoor_temp.return_value = None  # the sensor fails too
    assert sensors["outdoor_temp"].available is False


def test_source_entities_followed_per_sensor():
    _coord, sensors = _entities(
        sensor_platform,
        config={
            "indoor_temp_entity": "sensor.in",
            "sleep_indoor_temp_entity": "sensor.bedroom",
            "outdoor_temp_source": "weather_service",
            "outdoor_temp_entity": "sensor.out",  # saved but not in use: not followed
        },
    )
    assert sensors["indoor_temp"]._source_entities() == ["climate.t", "sensor.in", "sensor.bedroom"]
    assert [e for e in sensors["outdoor_temp"]._source_entities() if e] == ["weather.home"]
    assert sensors["forecast_low"]._source_entities() == ["weather.home"]
    others = set(sensors) - _READINGS
    assert all(sensors[k]._source_entities() == [] for k in others)


def test_a_failing_source_check_shows_the_value_rather_than_breaking_the_state_write():
    coord, sensors = _entities(sensor_platform)
    coord._get_indoor_temp.side_effect = RuntimeError("boom")
    assert sensors["indoor_temp"].available is True


def test_availability_listener_writes_only_when_availability_flips():
    coord, sensors = _entities(sensor_platform)
    s = sensors["indoor_temp"]
    s.entity_id = "sensor.living_room_indoor_temperature"
    s.hass = MagicMock()
    s.async_write_ha_state = MagicMock()
    shown = MagicMock()
    s.hass.states.get.return_value = shown

    shown.state = "70.0"
    s._async_source_changed(None)  # still available: no write
    assert s.async_write_ha_state.call_count == 0
    coord._get_indoor_temp.return_value = None
    s._async_source_changed(None)  # lost: write
    assert s.async_write_ha_state.call_count == 1
    shown.state = "unavailable"
    s._async_source_changed(None)  # still lost: no write
    assert s.async_write_ha_state.call_count == 1
    coord._get_indoor_temp.return_value = 70.0
    s._async_source_changed(None)  # recovered: write
    assert s.async_write_ha_state.call_count == 2


# --- #1648: device classes -----------------------------------------------------------------------------------------


def test_last_action_time_is_an_aware_timestamp():
    _coord, sensors = _entities(sensor_platform, data={"last_action_time": "2026-11-20T06:00:00.123456-08:00"})
    s = sensors["last_action_time"]
    assert s._attr_device_class == SensorDeviceClass.TIMESTAMP
    value = s.native_value
    assert value == datetime(2026, 11, 20, 14, 0, 0, 123456, tzinfo=UTC)
    assert value.tzinfo is not None


def test_last_action_time_naive_or_missing_or_garbage():
    _coord, sensors = _entities(sensor_platform, data={"last_action_time": "2026-11-20T06:00:00"})
    assert sensors["last_action_time"].native_value.tzinfo is not None  # naive (older saved state): local time
    for raw in (None, "", "not a time"):
        _coord, sensors = _entities(sensor_platform, data={"last_action_time": raw})
        assert sensors["last_action_time"].native_value is None, raw


def test_next_automation_time_is_the_clock_labels_datetime():
    at = datetime(2026, 11, 21, 6, 30, tzinfo=timezone(timedelta(hours=-8)))
    label = CodedText("clock", i18n.Msg("status.clock", t=i18n.Time(at)))
    _coord, sensors = _entities(sensor_platform, data={"next_automation_time": label})
    s = sensors["next_automation_time"]
    assert s._attr_device_class == SensorDeviceClass.TIMESTAMP
    assert s.native_value == at
    for raw in (CodedText("none", None, text=""), None, "6:30 AM"):
        _coord, sensors = _entities(sensor_platform, data={"next_automation_time": raw})
        assert sensors["next_automation_time"].native_value is None, raw


def test_ai_status_is_an_enum_of_its_reported_states():
    _coord, sensors = _entities(sensor_platform, data={"ai_status": "circuit_open"})
    s = sensors["ai_status"]
    assert s._attr_device_class == SensorDeviceClass.ENUM
    assert s.native_value == "circuit_open"
