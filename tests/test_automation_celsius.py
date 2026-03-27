"""Tests for Celsius temperature unit support in AutomationEngine.

These tests verify the critical conversion in _set_temperature():
internal °F values must be converted to the user's unit before
being sent to the HA climate.set_temperature service.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_automation(
    temp_unit: str,
    comfort_cool: float = 75.2,
    comfort_heat: float = 68.0,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Create an AutomationEngine with the given temperature unit config."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        """Close coroutine to prevent 'never awaited' warnings."""
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config: dict = {
        "climate_entity": "climate.test_thermostat",
        "temp_unit": temp_unit,
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity=config["climate_entity"],
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


# ---------------------------------------------------------------------------
# Tests: _set_temperature() unit conversion
# ---------------------------------------------------------------------------


class TestSetTemperatureCelsius:
    """Verify _set_temperature() sends the correct unit to the HA climate service."""

    def test_set_temperature_sends_celsius_to_ha(self):
        """_set_temperature with celsius config converts °F value to °C before service call.

        comfort_cool is stored as 75.2°F internally.
        75.2°F → (75.2 − 32) × 5/9 ≈ 24.0°C  → service must receive 24.0.
        """
        engine = _make_automation(temp_unit="celsius", comfort_cool=75.2)

        asyncio.run(engine._set_temperature(75.2, reason="test"))

        engine.hass.services.async_call.assert_called_once()
        call_args = engine.hass.services.async_call.call_args
        domain, service, data = call_args[0]
        assert domain == "climate"
        assert service == "set_temperature"
        sent_temp = data["temperature"]
        # 75.2°F → 24.0°C (within rounding tolerance)
        assert abs(sent_temp - 24.0) < 0.1

    def test_set_temperature_fahrenheit_passthrough(self):
        """_set_temperature with fahrenheit config sends °F value unchanged.

        75.0°F → service must receive 75.0.
        """
        engine = _make_automation(temp_unit="fahrenheit", comfort_cool=75.0)

        asyncio.run(engine._set_temperature(75.0, reason="test"))

        engine.hass.services.async_call.assert_called_once()
        call_args = engine.hass.services.async_call.call_args
        domain, service, data = call_args[0]
        assert domain == "climate"
        assert service == "set_temperature"
        assert data["temperature"] == 75.0

    def test_celsius_comfort_cool_service_value(self):
        """End-to-end: comfort_cool stored as 75.2°F, celsius user → service gets ~24.0."""
        engine = _make_automation(temp_unit="celsius", comfort_cool=75.2)

        asyncio.run(engine._set_temperature(engine.config["comfort_cool"], reason="comfort_cool"))

        call_args = engine.hass.services.async_call.call_args
        sent_temp = call_args[0][2]["temperature"]
        assert abs(sent_temp - 24.0) < 0.1

    def test_celsius_comfort_heat_service_value(self):
        """End-to-end: comfort_heat stored as 68°F (= 20°C), celsius user → service gets 20.0."""
        engine = _make_automation(temp_unit="celsius", comfort_heat=68.0)

        asyncio.run(engine._set_temperature(engine.config["comfort_heat"], reason="comfort_heat"))

        call_args = engine.hass.services.async_call.call_args
        sent_temp = call_args[0][2]["temperature"]
        # 68°F → 20.0°C
        assert abs(sent_temp - 20.0) < 0.01

    def test_celsius_setback_heat_service_value(self):
        """Setback heat: 60°F → 15.56°C sent to service in celsius mode."""
        engine = _make_automation(temp_unit="celsius")

        asyncio.run(engine._set_temperature(60.0, reason="setback"))

        call_args = engine.hass.services.async_call.call_args
        sent_temp = call_args[0][2]["temperature"]
        # (60 − 32) × 5/9 = 15.555...°C
        assert abs(sent_temp - 15.556) < 0.01

    def test_fahrenheit_setback_heat_passthrough(self):
        """Setback heat: 60°F sent unchanged in fahrenheit mode."""
        engine = _make_automation(temp_unit="fahrenheit")

        asyncio.run(engine._set_temperature(60.0, reason="setback"))

        call_args = engine.hass.services.async_call.call_args
        sent_temp = call_args[0][2]["temperature"]
        assert sent_temp == 60.0

    def test_dry_run_skips_service_call(self):
        """In dry_run mode, climate.set_temperature is never called."""
        engine = _make_automation(temp_unit="celsius", comfort_cool=75.2)
        engine.dry_run = True

        asyncio.run(engine._set_temperature(75.2, reason="dry run test"))

        engine.hass.services.async_call.assert_not_called()

    def test_entity_id_forwarded_correctly(self):
        """The correct climate entity ID is always included in the service call data."""
        engine = _make_automation(temp_unit="fahrenheit", comfort_cool=74.0)

        asyncio.run(engine._set_temperature(74.0, reason="entity check"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert data["entity_id"] == "climate.test_thermostat"
