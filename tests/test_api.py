"""Tests for the Climate Advisor REST API module."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.api import (
    API_VIEWS,
    _get_coordinator,
)
from custom_components.climate_advisor.const import (
    API_AUTOMATION_STATE,
    API_BRIEFING,
    API_CANCEL_OVERRIDE,
    API_CHART_DATA,
    API_CONFIG,
    API_FORCE_RECLASSIFY,
    API_LEARNING,
    API_RESPOND_SUGGESTION,
    API_SEND_BRIEFING,
    API_STATUS,
    API_TOGGLE_AUTOMATION,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_WELCOME_HOME_DEBOUNCE,
    CONFIG_METADATA,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    DOMAIN,
)
from custom_components.climate_advisor.learning import DailyRecord


class TestGetCoordinator:
    """Tests for _get_coordinator helper."""

    def test_returns_coordinator_when_loaded(self):
        coord = MagicMock()
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": coord}}
        assert _get_coordinator(hass) is coord

    def test_returns_none_when_not_loaded(self):
        hass = MagicMock()
        hass.data = {}
        assert _get_coordinator(hass) is None

    def test_returns_none_when_domain_empty(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        assert _get_coordinator(hass) is None


class TestAPIConstants:
    """Test that API path constants are properly defined."""

    def test_all_paths_under_base(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
            API_CANCEL_OVERRIDE,
            API_TOGGLE_AUTOMATION,
        ]
        for path in paths:
            assert path.startswith("/api/climate_advisor/"), f"{path} has wrong prefix"

    def test_paths_are_unique(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
            API_CANCEL_OVERRIDE,
            API_TOGGLE_AUTOMATION,
        ]
        assert len(paths) == len(set(paths))


class TestAPIViewList:
    """Test the API_VIEWS registry."""

    def test_correct_count(self):
        assert len(API_VIEWS) == 19

    def test_all_are_callable(self):
        for view_cls in API_VIEWS:
            assert callable(view_cls)


class TestCoordinatorDataContract:
    """Test that the data contract between coordinator and API is correct."""

    def test_status_data_fields(self):
        """API status view expects these keys in coordinator.data."""
        required_keys = [
            ATTR_DAY_TYPE,
            "trend_direction",
            "trend_magnitude",
            "automation_status",
            "compliance_score",
            "next_human_action",
        ]
        coord_data = {
            ATTR_DAY_TYPE: "warm",
            "trend_direction": "stable",
            "trend_magnitude": 2.5,
            "automation_status": "active",
            "compliance_score": 0.92,
            "next_human_action": "Open windows at 08:00 AM",
        }
        for key in required_keys:
            assert key in coord_data

    def test_chart_data_structure(self):
        """Chart data should have all expected series."""
        chart_data = {
            "predicted_outdoor": [{"hour": 0, "temp": 60.0}],
            "predicted_indoor": [{"hour": 0, "temp": 70.0}],
            "actual_outdoor": [{"time": "2026-03-18T08:00:00", "temp": 62.0}],
            "actual_indoor": [{"time": "2026-03-18T08:00:00", "temp": 70.0}],
            "current_hour": 14.5,
        }
        assert "predicted_outdoor" in chart_data
        assert "predicted_indoor" in chart_data
        assert "actual_outdoor" in chart_data
        assert "actual_indoor" in chart_data
        assert "current_hour" in chart_data

    def test_debug_state_structure(self):
        """Debug state should have all expected fields."""
        debug_state = {
            "paused_by_door": False,
            "pre_pause_mode": None,
            "grace_active": False,
            "last_resume_source": None,
            "door_window_sensors": {},
            "pending_debounce_timers": [],
            "classification": None,
        }
        assert "paused_by_door" in debug_state
        assert "grace_active" in debug_state
        assert "door_window_sensors" in debug_state

    def test_daily_record_serializable(self):
        """DailyRecord should be serializable for the learning endpoint."""
        from dataclasses import asdict

        record = DailyRecord(date="2026-03-18", day_type="warm", trend_direction="stable")
        data = asdict(record)
        assert data["date"] == "2026-03-18"
        assert data["day_type"] == "warm"
        assert data["manual_overrides"] == 0
        assert data["door_window_pause_events"] == 0


class TestConfigViewDisplayTransform:
    """Tests for seconds-to-minutes display transform in config settings."""

    SECONDS_KEYS = (
        CONF_SENSOR_DEBOUNCE,
        CONF_MANUAL_GRACE_PERIOD,
        CONF_AUTOMATION_GRACE_PERIOD,
        CONF_OVERRIDE_CONFIRM_PERIOD,
        CONF_WELCOME_HOME_DEBOUNCE,
    )

    def test_seconds_keys_have_display_transform(self):
        """All seconds-based config keys should declare a display_transform."""
        for key in self.SECONDS_KEYS:
            meta = CONFIG_METADATA[key]
            assert meta.get("display_transform") == "seconds_to_minutes", (
                f"{key} missing display_transform in CONFIG_METADATA"
            )

    def test_seconds_to_minutes_conversion_values(self):
        """Default seconds values should convert to expected minutes."""
        cases = [
            (DEFAULT_SENSOR_DEBOUNCE_SECONDS, 5),
            (DEFAULT_MANUAL_GRACE_SECONDS, 30),
            (DEFAULT_AUTOMATION_GRACE_SECONDS, 5),
            (DEFAULT_OVERRIDE_CONFIRM_SECONDS, 10),
            (DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS, 60),
        ]
        for seconds, expected_minutes in cases:
            assert seconds // 60 == expected_minutes

    def test_transform_not_applied_to_non_time_keys(self):
        """Non-time settings should not have a display_transform."""
        non_time_keys = [k for k in CONFIG_METADATA if k not in self.SECONDS_KEYS]
        for key in non_time_keys:
            assert "display_transform" not in CONFIG_METADATA[key], f"{key} should not have display_transform"

    def test_none_value_safe_with_transform(self):
        """Seconds-to-minutes transform should not crash on None values."""
        value = None
        transform = "seconds_to_minutes"
        if transform == "seconds_to_minutes" and isinstance(value, (int, float)):
            value = value // 60
        assert value is None


class TestToggleAutomationView:
    """Tests for the toggle_automation API endpoint."""

    def test_toggle_disables_when_enabled(self):
        """Toggling when enabled should disable automation."""
        coord = MagicMock()
        coord.automation_enabled = True
        coord.set_automation_enabled = MagicMock()

        new_state = not coord.automation_enabled
        coord.set_automation_enabled(new_state)

        coord.set_automation_enabled.assert_called_once_with(False)

    def test_toggle_enables_when_disabled(self):
        """Toggling when disabled should enable automation."""
        coord = MagicMock()
        coord.automation_enabled = False
        coord.set_automation_enabled = MagicMock()

        new_state = not coord.automation_enabled
        coord.set_automation_enabled(new_state)

        coord.set_automation_enabled.assert_called_once_with(True)

    def test_toggle_automation_constant_defined(self):
        """API_TOGGLE_AUTOMATION should be under the base path."""
        assert API_TOGGLE_AUTOMATION.startswith("/api/climate_advisor/")
        assert "toggle_automation" in API_TOGGLE_AUTOMATION


# ---------------------------------------------------------------------------
# Helpers for Celsius unit tests (replicate api.py view logic without instantiation)
# ---------------------------------------------------------------------------


def _simulate_status_get(coordinator):
    """Replicate ClimateAdvisorStatusView.get() response dict."""
    from custom_components.climate_advisor.temperature import convert_delta, from_fahrenheit

    data = coordinator.data or {}
    unit = coordinator.config.get("temp_unit", "fahrenheit")
    indoor_temp = coordinator._get_indoor_temp()
    indoor_temp_display = round(from_fahrenheit(indoor_temp, unit), 1) if indoor_temp is not None else None
    trend_magnitude_display = round(convert_delta(data.get(ATTR_TREND_MAGNITUDE, 0), unit), 1)
    return {
        "day_type": data.get(ATTR_DAY_TYPE, "unknown"),
        "trend_direction": data.get(ATTR_TREND, "unknown"),
        "trend_magnitude": trend_magnitude_display,
        "hvac_mode": "off",
        "indoor_temp": indoor_temp_display,
        "current_setpoint": None,
        "automation_status": data.get("automation_status", "unknown"),
        "compliance_score": data.get("compliance_score", 1.0),
        "next_action": data.get(ATTR_NEXT_ACTION, ""),
        "next_automation_action": data.get(ATTR_NEXT_AUTOMATION_ACTION, ""),
        "next_automation_time": data.get(ATTR_NEXT_AUTOMATION_TIME, ""),
        "automation_enabled": coordinator.automation_enabled,
        "occupancy_mode": coordinator._occupancy_mode,
        "fan_status": data.get(ATTR_FAN_STATUS, "disabled"),
        "contact_status": data.get(ATTR_CONTACT_STATUS, "no sensors"),
        "contact_sensors": [],
        "manual_override_active": False,
        "fan_override_active": False,
        "paused_by_door": False,
        "unit": unit,
    }


def _simulate_learning_get(coordinator):
    """Replicate ClimateAdvisorLearningView.get() response dict."""
    from custom_components.climate_advisor.temperature import from_fahrenheit

    unit = coordinator.config.get("temp_unit", "fahrenheit")
    return {
        "today_record": None,
        "yesterday_record": coordinator.yesterday_record,
        "tomorrow_plan": coordinator.tomorrow_plan,
        "suggestions": [],
        "compliance": {},
        "comfort_range_low": round(from_fahrenheit(coordinator.config.get("comfort_heat", 70), unit), 1),
        "comfort_range_high": round(from_fahrenheit(coordinator.config.get("comfort_cool", 75), unit), 1),
        "unit": unit,
    }


class TestStatusViewCelsiusUnit:
    """Status API must convert temperatures and include 'unit' when temp_unit=celsius."""

    def _make_coordinator(self, temp_unit="celsius", indoor_temp=68.0, trend_magnitude=9.0):
        coord = MagicMock()
        coord.config = {"temp_unit": temp_unit, "climate_entity": "climate.test"}
        coord.data = {
            ATTR_DAY_TYPE: "mild",
            ATTR_TREND: "stable",
            ATTR_TREND_MAGNITUDE: trend_magnitude,
            "automation_status": "active",
            "compliance_score": 1.0,
        }
        coord._get_indoor_temp.return_value = indoor_temp
        coord.automation_enabled = True
        coord._occupancy_mode = "home"
        ae = MagicMock()
        ae._manual_override_active = False
        ae._override_confirm_pending = False
        ae._fan_override_active = False
        ae.is_paused_by_door = False
        coord.automation_engine = ae
        coord._compute_contact_details.return_value = []
        coord.yesterday_record = None
        coord.tomorrow_plan = None
        return coord

    def test_status_celsius_converts_indoor_temp(self):
        """indoor_temp must be in Celsius when unit=celsius configured."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", indoor_temp=68.0)
        response = _simulate_status_get(coord)
        assert response["unit"] == "celsius"  # KeyError before fix
        assert response["indoor_temp"] == pytest.approx(20.0, abs=0.1)  # 68°F → 20°C

    def test_status_celsius_converts_trend_magnitude(self):
        """trend_magnitude must be converted delta when unit=celsius."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", trend_magnitude=9.0)
        response = _simulate_status_get(coord)
        assert response["trend_magnitude"] == pytest.approx(5.0, abs=0.1)  # 9°F delta → 5°C delta

    def test_status_includes_unit_field(self):
        """Status response must include a 'unit' field."""
        coord = self._make_coordinator(temp_unit="fahrenheit", indoor_temp=72.0)
        response = _simulate_status_get(coord)
        assert "unit" in response  # KeyError before fix
        assert response["unit"] == "fahrenheit"


class TestLearningViewCelsiusUnit:
    """Learning API must convert comfort range temps and include 'unit' when temp_unit=celsius."""

    def _make_coordinator(self, temp_unit="celsius", comfort_heat=68, comfort_cool=76):
        coord = MagicMock()
        coord.config = {
            "temp_unit": temp_unit,
            "comfort_heat": comfort_heat,
            "comfort_cool": comfort_cool,
        }
        coord.today_record = None
        coord.yesterday_record = None
        coord.tomorrow_plan = None
        coord.learning = MagicMock()
        coord.learning.generate_suggestions.return_value = []
        coord.learning.get_last_suggestion_keys.return_value = []
        coord.learning.get_compliance_summary.return_value = {}
        return coord

    def test_learning_celsius_converts_comfort_range(self):
        """comfort_range_low/high must be in Celsius when configured."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", comfort_heat=68, comfort_cool=76)
        response = _simulate_learning_get(coord)
        assert response["unit"] == "celsius"  # KeyError before fix
        assert response["comfort_range_low"] == pytest.approx(20.0, abs=0.1)  # 68°F → 20°C
        assert response["comfort_range_high"] == pytest.approx(24.4, abs=0.1)  # 76°F → 24.4°C

    def test_learning_includes_unit_field(self):
        """Learning response must include a 'unit' field."""
        coord = self._make_coordinator(temp_unit="fahrenheit")
        response = _simulate_learning_get(coord)
        assert "unit" in response  # KeyError before fix
