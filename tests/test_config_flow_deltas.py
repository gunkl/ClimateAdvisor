"""Issue #1061: config UI uniformity — delta-field unit display, initial-flow shape, zone rename."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tests.test_config_flow import _make_config_flow, _make_options_flow

DELTA_KEYS = (
    "max_setback_depth_f",
    "comfort_deadband_hot_f",
    "comfort_deadband_warm_f",
    "comfort_deadband_mild_f",
    "comfort_deadband_cool_f",
    "comfort_deadband_cold_f",
)

_MOVED_FROM_INITIAL = {
    "sleep_indoor_temp_entity",
    "sensor_debounce_seconds",
    "manual_grace_seconds",
    "automation_grace_seconds",
    "override_confirm_seconds",
    "hvac_fan_restrict_mode",
    "fan_state_entity",
    "fan_state_feedback",
    "fan_remote_entity",
    "fan_min_runtime_per_hour",
    "welcome_home_debounce_seconds",
}


@pytest.fixture(autouse=True)
def _need_voluptuous():
    # voluptuous is a required test dependency (uv group "test"); a stub means a broken
    # environment and must fail loudly rather than skip.
    import voluptuous as v

    assert not isinstance(v, MagicMock), "voluptuous is a stub - install the test dependency group"


class _CapturedSelector:
    """Callable (voluptuous accepts callables as validators) exposing the config it was built with."""

    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return value


@pytest.fixture
def real_number_selectors():
    """The ha_stubs ``selector`` module is a MagicMock; capture NumberSelector kwargs so tests can read them."""
    from custom_components.climate_advisor import config_flow

    with (
        patch.object(config_flow.selector, "NumberSelectorConfig", side_effect=lambda **kw: dict(kw)),
        patch.object(config_flow.selector, "NumberSelector", side_effect=_CapturedSelector),
    ):
        yield


def _schema_keys(result: dict) -> set[str]:
    return {str(k) for k in result["data_schema"].schema}


def _schema_marker(result: dict, name: str):
    return next(k for k in result["data_schema"].schema if str(k) == name)


def _field_config(result: dict, name: str):
    marker = _schema_marker(result, name)
    return marker, result["data_schema"].schema[marker].config


def _advanced(entry_data: dict, user_input=None):
    flow, captured = _make_options_flow(entry_data)
    result = asyncio.run(flow.async_step_advanced(user_input))
    return result, captured


def _form_values(entry_data: dict) -> dict:
    """The payload an untouched form would post (every field at its shown default)."""
    result, _ = _advanced(entry_data)
    return {str(k): k.default() for k in result["data_schema"].schema}


class TestDeltaFieldDisplay:
    def test_celsius_shows_converted_default_bounds_and_unit(self, real_number_selectors):
        result, _ = _advanced({"temp_unit": "celsius"})
        marker, cfg = _field_config(result, "max_setback_depth_f")
        assert marker.default() == 4.4  # 8.0 F delta -> 4.44 C
        assert cfg["unit_of_measurement"] == "°C"
        assert cfg["min"] == 0.5  # 1 F delta = 0.556 C, rounded outward
        assert cfg["max"] == 5.6
        assert cfg["step"] == 0.1
        marker, _ = _field_config(result, "comfort_deadband_hot_f")
        assert marker.default() == 2.8  # 5 F delta

    def test_fahrenheit_unchanged(self, real_number_selectors):
        result, _ = _advanced({"temp_unit": "fahrenheit", "max_setback_depth_f": 7.5})
        marker, cfg = _field_config(result, "max_setback_depth_f")
        assert marker.default() == 7.5
        assert (cfg["min"], cfg["max"], cfg["step"]) == (1.0, 10.0, 0.5)
        assert cfg["unit_of_measurement"] == "°F"

    def test_missing_key_uses_f_default(self):
        result, _ = _advanced({})  # no temp_unit -> Fahrenheit
        assert _field_config(result, "comfort_deadband_cold_f")[0].default() == 5.0


class TestDeltaFieldRoundTrip:
    @pytest.mark.parametrize("key", DELTA_KEYS)
    def test_untouched_celsius_resubmit_does_not_drift(self, key):
        data = {"temp_unit": "celsius", key: 4.3}
        _, captured = _advanced(data, _form_values(data))
        assert captured["data"][key] == 4.3

    def test_untouched_celsius_keeps_default_deltas_exact(self):
        data = {"temp_unit": "celsius", "max_setback_depth_f": 8.0}
        _, captured = _advanced(data, _form_values(data))
        assert captured["data"]["max_setback_depth_f"] == 8.0

    def test_changed_celsius_value_converts_to_fahrenheit_same_key(self):
        data = {"temp_unit": "celsius"}
        shown = _form_values(data)
        shown["max_setback_depth_f"] = 5.0  # C delta
        _, captured = _advanced(data, shown)
        assert captured["data"]["max_setback_depth_f"] == pytest.approx(9.0, abs=0.01)

    def test_celsius_submit_clamped_to_fahrenheit_bounds(self):
        data = {"temp_unit": "celsius"}
        shown = _form_values(data)
        shown["max_setback_depth_f"] = 0.5  # 0.9 F delta, below the 1.0 F floor
        shown["comfort_deadband_hot_f"] = 4.4  # 7.92 F: in range
        _, captured = _advanced(data, shown)
        assert captured["data"]["max_setback_depth_f"] == 1.0
        assert captured["data"]["comfort_deadband_hot_f"] == pytest.approx(7.92, abs=0.01)

    def test_fahrenheit_submit_stored_as_entered(self):
        data = {"temp_unit": "fahrenheit"}
        shown = _form_values(data)
        shown["max_setback_depth_f"] = 6.5
        _, captured = _advanced(data, shown)
        assert captured["data"]["max_setback_depth_f"] == 6.5


class TestAdvancedUnits:
    def test_unit_labels(self, real_number_selectors):
        result, _ = _advanced({})
        expected = {
            "min_preheat_minutes": "minutes",
            "max_preheat_minutes": "minutes",
            "default_preheat_minutes": "minutes",
            "default_tou_lead_minutes": "minutes",
            "comfort_family_recency_window_min": "minutes",
            "comfort_mode_switch_min_interval_s": "seconds",
        }
        for key, unit in expected.items():
            assert _field_config(result, key)[1]["unit_of_measurement"] == unit, key

    def test_ai_and_fan_units(self, real_number_selectors):
        flow, _ = _make_options_flow({})
        ai = asyncio.run(flow.async_step_ai_settings(None))
        assert _field_config(ai, "ai_max_tokens")[1]["unit_of_measurement"] == "tokens"
        assert _field_config(ai, "ai_monthly_budget")[1]["unit_of_measurement"] == "USD"
        sensors = asyncio.run(flow.async_step_sensors(None))
        assert _field_config(sensors, "fan_min_runtime_per_hour")[1]["unit_of_measurement"] == "minutes"


class TestInitialFlowShape:
    def test_initial_steps_omit_moved_fields(self):
        flow = _make_config_flow()
        sources = asyncio.run(flow.async_step_temperature_sources(None))
        sensors = asyncio.run(flow.async_step_sensors(None))
        occupancy = asyncio.run(flow.async_step_occupancy(None))
        schedule = asyncio.run(flow.async_step_schedule(None))
        for result in (sources, sensors, occupancy, schedule):
            assert not (_schema_keys(result) & _MOVED_FROM_INITIAL)
        assert _schema_keys(sensors) == {"door_window_sensors", "sensor_polarity_inverted", "fan_mode", "fan_entity"}
        assert len(_schema_keys(occupancy)) == 6
        assert "briefing_notifications_enabled" in _schema_keys(schedule)

    def test_initial_flow_creates_entry_with_title_and_briefing_choice(self):
        """Entry creation: title from zone_name, zone_name not stored, briefing choice captured.

        The stub flow does not validate against the schema, so this does NOT prove the moved fields are
        absent from the initial flow; ``test_initial_steps_omit_moved_fields`` (schema keys) does.
        """
        flow = _make_config_flow()

        async def _drive():
            await flow.async_step_sensors({"door_window_sensors": ["binary_sensor.door"], "fan_mode": "disabled"})
            await flow.async_step_occupancy({"home_toggle_invert": False})
            return await flow.async_step_schedule(
                {
                    "zone_name": "  Upstairs ",
                    "wake_time": "07:00:00",
                    "sleep_time": "23:00:00",
                    "briefing_time": "06:30:00",
                    "briefing_notifications_enabled": False,
                }
            )

        result = asyncio.run(_drive())
        assert result["title"] == "Upstairs"
        assert "zone_name" not in result["data"]
        assert result["data"]["briefing_notifications_enabled"] is False
        assert not (set(result["data"]) & _MOVED_FROM_INITIAL)

    def test_briefing_default_falls_back_when_omitted(self):
        flow = _make_config_flow()
        result = asyncio.run(
            flow.async_step_schedule(
                {"zone_name": "Z", "wake_time": "07:00:00", "sleep_time": "23:00:00", "briefing_time": "06:30:00"}
            )
        )
        assert result["data"]["briefing_notifications_enabled"] is True  # first zone


class TestZoneRename:
    def _flow(self):
        flow, _ = _make_options_flow({"weather_entity": "weather.x", "climate_entity": "climate.x"})
        flow.config_entry.title = "Old Zone"
        calls: list[dict] = []

        def _update(entry, **kwargs):
            calls.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "title" in kwargs:
                entry.title = kwargs["title"]

        flow.hass.config_entries.async_update_entry = MagicMock(side_effect=_update)
        return flow, calls

    @staticmethod
    def _core_input(zone_name):
        return {
            "zone_name": zone_name,
            "weather_entity": "weather.x",
            "climate_entity": "climate.x",
            "temp_unit": "fahrenheit",
            "notify_service": "notify.notify",
        }

    def test_default_is_current_title(self):
        flow, _ = self._flow()
        result = asyncio.run(flow.async_step_core(None))
        assert _schema_marker(result, "zone_name").default() == "Old Zone"

    def test_rename_updates_title_once_and_not_data(self):
        flow, calls = self._flow()
        asyncio.run(flow.async_step_core(self._core_input("  New Zone ")))
        assert [c for c in calls if "title" in c] == [{"title": "New Zone"}]
        assert "zone_name" not in flow.config_entry.data
        assert flow.config_entry.data["notify_service"] == "notify.notify"

    @pytest.mark.parametrize("name", ["Old Zone", "   "])
    def test_unchanged_or_empty_name_keeps_title(self, name):
        flow, calls = self._flow()
        asyncio.run(flow.async_step_core(self._core_input(name)))
        assert not [c for c in calls if "title" in c]

    def test_too_long_name_rejected(self):
        flow, calls = self._flow()
        result = asyncio.run(flow.async_step_core(self._core_input("x" * 51)))
        assert result["errors"]["zone_name"] == "zone_name_too_long"
        assert calls == []
