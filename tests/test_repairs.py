"""Tests for the weather entity repairs flow and setup-time auto-resolution.

Covers:
- Fixable repair issue creation when weather entity is missing
- Auto-resolution at setup time when exactly one weather entity exists
- Repair issue creation when auto-resolution is ambiguous (2+ entities)
- WeatherEntityRepairFlow: form display and entity selection
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.repairs import (
    WeatherEntityRepairFlow,
    async_create_fix_flow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config_entry(data: dict, version: int = 7) -> MagicMock:
    """Create a mock ConfigEntry."""
    entry = MagicMock()
    entry.data = dict(data)
    entry.entry_id = "test_entry_id"
    entry.version = version
    return entry


def _make_hass(weather_entities: list[str] | None = None) -> MagicMock:
    """Create a minimal mock hass for repair tests.

    Args:
        weather_entities: entity IDs that exist in HA. If None, no entities.
    """
    hass = MagicMock()
    existing = set(weather_entities or [])

    def mock_states_get(entity_id):
        if entity_id in existing:
            state = MagicMock()
            state.entity_id = entity_id
            return state
        return None

    hass.states.get = mock_states_get

    # async_all returns list of state objects for a domain
    def mock_async_all(domain=None):
        if domain == "weather":
            return [
                MagicMock(entity_id=eid)
                for eid in existing
                if eid.startswith("weather.")
            ]
        return []

    hass.states.async_all = mock_async_all
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    return hass


FULL_CONFIG = {
    "weather_entity": "weather.forecast_home",
    "climate_entity": "climate.living_room",
    "comfort_heat": 70,
    "comfort_cool": 76,
    "setback_heat": 62,
    "setback_cool": 78,
    "notify_service": "notify.mobile_app_phone",
    "outdoor_temp_source": "weather_service",
    "indoor_temp_source": "climate_fallback",
    "door_window_sensors": ["binary_sensor.front_door"],
    "sensor_polarity_inverted": False,
    "sensor_debounce_seconds": 300,
    "manual_grace_seconds": 1800,
    "manual_grace_notify": False,
    "automation_grace_seconds": 3600,
    "automation_grace_notify": True,
    "email_notify": True,
    "wake_time": "06:30:00",
    "sleep_time": "22:30:00",
    "briefing_time": "06:00:00",
    "learning_enabled": True,
    "aggressive_savings": False,
}


# ---------------------------------------------------------------------------
# async_create_fix_flow
# ---------------------------------------------------------------------------

class TestAsyncCreateFixFlow:
    """Test the HA entry point for repair flows."""

    def test_returns_weather_repair_flow(self):
        hass = _make_hass()
        flow = asyncio.run(async_create_fix_flow(hass, "weather_entity_not_found", None))
        assert isinstance(flow, WeatherEntityRepairFlow)

    def test_returns_base_flow_for_unknown_issue(self):
        hass = _make_hass()
        flow = asyncio.run(async_create_fix_flow(hass, "some_other_issue", None))
        assert not isinstance(flow, WeatherEntityRepairFlow)


# ---------------------------------------------------------------------------
# Setup-time auto-resolution (tests the __init__.py logic)
# ---------------------------------------------------------------------------

class TestSetupAutoResolution:
    """Test auto-resolution of stale weather entity at setup time."""

    def test_auto_resolves_when_one_weather_entity_exists(self):
        """If exactly one weather entity exists, _resolve_weather_entity returns it."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.home"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result == "weather.home"

    def test_returns_none_when_no_weather_entities(self):
        """If no weather entities exist, returns None (ambiguous)."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass([])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result is None

    def test_returns_none_when_multiple_weather_entities(self):
        """If 2+ weather entities exist, returns None (ambiguous)."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.home", "weather.openweathermap"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result is None

    def test_returns_configured_when_it_exists(self):
        """If the configured entity still exists, returns it unchanged."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.forecast_home", "weather.other"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result == "weather.forecast_home"


# ---------------------------------------------------------------------------
# WeatherEntityRepairFlow
# ---------------------------------------------------------------------------

class TestWeatherEntityRepairFlow:
    """Test the repair flow step logic."""

    def test_shows_form_when_no_input(self):
        """First call shows entity selector form."""
        flow = WeatherEntityRepairFlow()
        flow.hass = _make_hass(["weather.home"])

        result = asyncio.run(flow.async_step_init(user_input=None))

        assert result["type"] == "form"
        assert result["step_id"] == "init"

    def test_updates_config_on_valid_selection(self):
        """Selecting a valid entity updates config, deletes issue, reloads."""
        flow = WeatherEntityRepairFlow()
        hass = _make_hass(["weather.home"])
        entry = _make_config_entry(FULL_CONFIG)
        hass.config_entries.async_entries = MagicMock(return_value=[entry])
        flow.hass = hass

        with patch(
            "custom_components.climate_advisor.repairs.ir.async_delete_issue"
        ) as mock_delete:
            result = asyncio.run(
                flow.async_step_init(
                    user_input={"weather_entity": "weather.home"}
                )
            )

        assert result["type"] == "create_entry"
        # Verify config entry was updated
        hass.config_entries.async_update_entry.assert_called_once()
        call_kwargs = hass.config_entries.async_update_entry.call_args
        new_data = call_kwargs[1]["data"] if "data" in call_kwargs[1] else call_kwargs[0][1]
        assert new_data["weather_entity"] == "weather.home"
        # Verify issue was deleted
        mock_delete.assert_called_once()
        # Verify integration was reloaded
        hass.config_entries.async_reload.assert_awaited_once_with("test_entry_id")

    def test_shows_error_on_invalid_entity(self):
        """Selecting an entity that doesn't exist shows error."""
        flow = WeatherEntityRepairFlow()
        flow.hass = _make_hass([])  # No entities exist

        result = asyncio.run(
            flow.async_step_init(
                user_input={"weather_entity": "weather.nonexistent"}
            )
        )

        assert result["type"] == "form"
        assert result["errors"]["weather_entity"] == "entity_not_found"

    def test_no_config_entries_graceful(self):
        """If no config entries exist, flow completes without error."""
        flow = WeatherEntityRepairFlow()
        hass = _make_hass(["weather.home"])
        hass.config_entries.async_entries = MagicMock(return_value=[])
        flow.hass = hass

        result = asyncio.run(
            flow.async_step_init(
                user_input={"weather_entity": "weather.home"}
            )
        )

        assert result["type"] == "create_entry"
        hass.config_entries.async_update_entry.assert_not_called()
