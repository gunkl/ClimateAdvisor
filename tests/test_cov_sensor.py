"""Coverage of the sensor platform's remaining paths (quality scale Silver, test-coverage).

Unit tier: the REAL sensor classes built by ``sensor.async_setup_entry`` over a mocked coordinator (the pattern of
``test_entity_availability_1642_1648.py``), checking what the occupant's dashboard and automations read: the state,
the attributes, availability.

Occupant view: a sensor that crashes on an odd value takes its card off the dashboard; one that shows a stale reading
as current misleads the person deciding whether to open a window.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor import sensor as sensor_platform

_LOGGER_NAME = "custom_components.climate_advisor.sensor"


def _sensors(*, config=None, data=None):
    coord = MagicMock()
    coord.config = {"climate_entity": "climate.t", "weather_entity": "weather.home", **(config or {})}
    coord.data = {"indoor_temp": 70.0, "outdoor_temp": 50.0} if data is None else data
    coord.last_update_success = True
    coord._get_indoor_temp.return_value = 70.0
    coord._weather_attrs_and_down.return_value = ({}, False)
    coord._live_dedicated_outdoor_temp.return_value = None
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.runtime_data = coord
    entry.title = "Living Room"
    added: list = []
    asyncio.run(sensor_platform.async_setup_entry(MagicMock(), entry, added.extend))
    return coord, {e._attr_translation_key: e for e in added}


class TestAvailability:
    def test_a_failing_source_check_shows_available_and_warns_once(self, caplog):
        """A bug in the source check must not hide the reading; it is logged once, not every state write."""
        coord, sensors = _sensors()
        coord._get_indoor_temp.side_effect = RuntimeError("broken check")
        indoor = sensors["indoor_temp"]

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert indoor.available is True
            assert indoor.available is True

        assert caplog.text.count("Source check failed") == 1

    def test_outdoor_reading_missing_is_unavailable(self):
        """No outdoor value at all: the Outdoor Temperature card shows unavailable, not an old number."""
        coord, sensors = _sensors(data={"indoor_temp": 70.0, "outdoor_temp": None})
        assert sensors["outdoor_temp"].available is False
        assert sensors["indoor_temp"].available is True

    def test_source_entities_are_followed_once_added(self):
        """The Indoor Temperature sensor follows its thermostat and sensors, so a lost source shows at once."""
        coord, sensors = _sensors(config={"indoor_temp_entity": "sensor.room", "sleep_indoor_temp_entity": None})
        indoor = sensors["indoor_temp"]
        indoor.hass = MagicMock()
        with patch.object(sensor_platform, "async_track_state_change_event", return_value="unsub") as track:
            asyncio.run(indoor.async_added_to_hass())

        track.assert_called_once()
        assert track.call_args.args[1] == ["climate.t", "sensor.room"]
        assert indoor.__dict__["_on_remove"] == ["unsub"]

    def test_plan_sensors_follow_nothing(self):
        coord, sensors = _sensors()
        day_type = sensors["day_type"]
        with patch.object(sensor_platform, "async_track_state_change_event") as track:
            asyncio.run(day_type.async_added_to_hass())
        track.assert_not_called()

    def test_source_change_before_the_first_state_write_does_nothing(self):
        coord, sensors = _sensors()
        indoor = sensors["indoor_temp"]
        indoor.hass = MagicMock()
        indoor.hass.states.get.return_value = None
        indoor.entity_id = "sensor.climate_advisor_indoor_temperature"
        indoor.async_write_ha_state = MagicMock()

        indoor._async_source_changed(None)

        indoor.async_write_ha_state.assert_not_called()

    def test_source_change_writes_only_when_availability_flips(self):
        coord, sensors = _sensors()
        indoor = sensors["indoor_temp"]
        indoor.hass = MagicMock()
        indoor.entity_id = "sensor.climate_advisor_indoor_temperature"
        indoor.async_write_ha_state = MagicMock()

        indoor.hass.states.get.return_value = MagicMock(state="70.0")
        indoor._async_source_changed(None)  # shown available, still available: no write
        indoor.async_write_ha_state.assert_not_called()

        coord._get_indoor_temp.return_value = None  # the thermostat stopped answering
        indoor._async_source_changed(None)
        indoor.async_write_ha_state.assert_called_once()


class TestValues:
    def test_a_coded_value_renders_in_the_homes_language_with_a_stable_code(self):
        """A coded reason shows as text in HA's language, while automations get the language-free code."""
        code = i18n.Code("settings.temp_source.sensor")
        coord, sensors = _sensors(data={"last_action_reason": code})
        reason = sensors["last_action_reason"]
        reason.hass = MagicMock()
        reason.hass.config.language = "de"

        assert reason.native_value == "Eigener Sensor"
        attrs = reason.extra_state_attributes
        assert attrs["full_reason"] == "Eigener Sensor"
        assert (attrs["code"], attrs["params"]) == ("settings.temp_source.sensor", {})

    def test_an_off_list_enum_value_reads_unknown_and_warns_once(self, caplog):
        """HA rejects a state outside ``options``; the sensor reads unknown instead of breaking."""
        coord, sensors = _sensors(data={"day_type": "scorching"})
        day_type = sensors["day_type"]
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert day_type.native_value is None
            assert day_type.native_value is None
        assert caplog.text.count("is not one of") == 1

    def test_no_coordinator_data_yet_reads_none(self, caplog):
        coord, sensors = _sensors(data={})
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert sensors["day_type"].native_value is None
            assert sensors["day_type"].native_value is None
        assert caplog.text.count("Coordinator data empty for sensor day_type") == 1

    def test_compliance_is_a_percentage_and_none_without_a_score(self):
        coord, sensors = _sensors(data={"compliance_score": 0.876})
        assert sensors["compliance_score"].native_value == 87.6
        coord.data = {"compliance_score": None, "indoor_temp": 70.0}
        assert sensors["compliance_score"].native_value is None

    def test_fan_status_without_data_has_no_attributes(self):
        coord, sensors = _sensors(data={})
        assert sensors["fan_status"].extra_state_attributes == {}


class TestDetailAttributes:
    def test_contact_status_counts_open_sensors(self):
        coord, sensors = _sensors(data={"contact_status": "1 open", "indoor_temp": 70.0})
        details = [
            {"entity_id": "binary_sensor.door", "open": True, "unreadable": False},
            {"entity_id": "binary_sensor.window", "open": False, "unreadable": False},
        ]
        coord._compute_contact_details.return_value = details
        coord.automation_engine.is_paused_by_door = True
        coord.automation_engine.natural_vent_active = False

        attrs = sensors["contact_status"].extra_state_attributes

        assert attrs["sensor_count"] == 2
        assert attrs["open_count"] == 1
        assert attrs["paused_by_door"] is True
        assert attrs["natural_vent_active"] is False
        assert attrs["sensors"] == details

    def test_ai_status_with_a_client_reports_its_numbers(self):
        coord, sensors = _sensors()
        coord.claude_client.get_status.return_value = {
            "error_count": 2,
            "total_requests": 9,
            "model": "m",
            "provider": "ai_task",
            "circuit_breaker_state": "open",
        }
        attrs = sensors["ai_status"].extra_state_attributes
        assert attrs["error_count"] == 2
        assert attrs["total_requests"] == 9
        assert attrs["model_in_use"] == "m"
        assert attrs["provider"] == "ai_task"
        assert attrs["ai_task_entity"] == ""
        assert attrs["circuit_breaker"] == "open"
        assert attrs["monthly_cost_estimate"] == 0.0

    def test_ai_status_without_a_client_is_disabled(self):
        coord, sensors = _sensors()
        coord.claude_client = None
        assert sensors["ai_status"].extra_state_attributes == {"status": "disabled"}
