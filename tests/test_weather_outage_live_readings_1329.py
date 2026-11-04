"""Issue #1329: weather outage keeps live readings; only scheduled retries count; no half-loaded start-up chart row."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock


def _coord(config: dict, states: dict):
    cls = importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator
    coord = object.__new__(cls)
    coord.config = config
    coord.hass = MagicMock()

    def get(eid):
        if eid not in states:
            return None
        s = MagicMock()
        s.state = states[eid]
        s.attributes = {}
        return s

    coord.hass.states.get.side_effect = get
    return coord


class TestLiveDedicatedOutdoor:
    def test_a_sensor_reading_survives_the_weather_outage(self):
        coord = _coord({"outdoor_temp_source": "sensor", "outdoor_temp_entity": "sensor.out"}, {})
        coord._read_outdoor_sensor = lambda: (57.5, None)
        assert coord._live_dedicated_outdoor_temp() == 57.5

    def test_a_failed_sensor_read_is_unknown_never_a_placeholder(self):
        coord = _coord({"outdoor_temp_source": "sensor", "outdoor_temp_entity": "sensor.out"}, {})
        coord._read_outdoor_sensor = lambda: (None, "unavailable")
        assert coord._live_dedicated_outdoor_temp() is None

    def test_a_weather_service_install_has_no_live_value_without_the_weather(self):
        coord = _coord({"outdoor_temp_source": "weather_service"}, {})
        coord._read_outdoor_sensor = MagicMock(side_effect=AssertionError("must not read a sensor"))
        assert coord._live_dedicated_outdoor_temp() is None


class TestStartupChartEntry:
    def test_skipped_while_starting_up_before_the_thermostat_reports(self):
        coord = _coord({"climate_entity": "climate.t"}, {"climate.t": "unavailable"})
        coord._startup_coalesce_active = True
        assert coord._chart_poll_waits_for_startup(None) is True

    def test_skipped_while_starting_up_without_an_indoor_reading(self):
        coord = _coord({"climate_entity": "climate.t"}, {"climate.t": "heat"})
        coord._startup_coalesce_active = True
        assert coord._chart_poll_waits_for_startup(None) is True

    def test_written_once_the_thermostat_reports_during_start_up(self):
        coord = _coord({"climate_entity": "climate.t"}, {"climate.t": "heat"})
        coord._startup_coalesce_active = True
        assert coord._chart_poll_waits_for_startup(66.5) is False

    def test_a_real_thermostat_outage_after_start_up_is_still_charted(self):
        """Issue #1039: outside start-up an empty indoor reading is a real outage and stays in the chart."""
        coord = _coord({"climate_entity": "climate.t"}, {"climate.t": "unavailable"})
        coord._startup_coalesce_active = False
        assert coord._chart_poll_waits_for_startup(None) is False
