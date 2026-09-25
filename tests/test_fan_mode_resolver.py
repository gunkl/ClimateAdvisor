"""Tests for fan_mode_resolver.py (Issue #968)."""

from __future__ import annotations

from custom_components.climate_advisor.fan_mode_resolver import (
    is_thermostat_fan_physically_active,
    resolve_fan_mode_command,
)


class TestResolveFanModeCommand:
    def test_on_prefers_literal_on(self):
        assert resolve_fan_mode_command(["auto", "on"], "on") == "on"

    def test_on_falls_back_to_highest_named_speed(self):
        assert resolve_fan_mode_command(["auto", "low", "medium", "high"], "on") == "high"

    def test_on_falls_back_to_medium_when_no_high(self):
        assert resolve_fan_mode_command(["auto", "low", "medium"], "on") == "medium"

    def test_on_returns_none_when_no_valid_value(self):
        assert resolve_fan_mode_command(["auto"], "on") is None
        assert resolve_fan_mode_command([], "on") is None
        assert resolve_fan_mode_command(None, "on") is None

    def test_off_prefers_auto(self):
        assert resolve_fan_mode_command(["auto", "on"], "off") == "auto"

    def test_off_falls_back_to_off_literal(self):
        assert resolve_fan_mode_command(["off", "on"], "off") == "off"

    def test_off_falls_back_to_low_when_no_auto_or_off(self):
        assert resolve_fan_mode_command(["low", "medium", "high"], "off") == "low"

    def test_case_insensitive_match_preserves_original_casing(self):
        assert resolve_fan_mode_command(["Auto", "On"], "on") == "On"
        assert resolve_fan_mode_command(["Auto", "On"], "off") == "Auto"


class TestIsThermostatFanPhysicallyActive:
    def test_named_speed_is_active(self):
        assert is_thermostat_fan_physically_active("low", "idle") is True
        assert is_thermostat_fan_physically_active("high", "") is True

    def test_auto_is_not_active_without_fan_hvac_action(self):
        assert is_thermostat_fan_physically_active("auto", "idle") is False

    def test_empty_is_not_active(self):
        assert is_thermostat_fan_physically_active("", "") is False

    def test_hvac_action_fan_makes_auto_active(self):
        assert is_thermostat_fan_physically_active("auto", "fan") is True

    def test_literal_on_is_active(self):
        assert is_thermostat_fan_physically_active("on", "") is True
