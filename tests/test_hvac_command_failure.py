"""Tests for HVAC command failure containment (Issue #805).

``_set_hvac_mode()`` and ``_set_temperature()`` are the two single write
points (Fix 1b) for every thermostat command CA issues. Before this fix,
neither caught an exception from ``hass.services.async_call()`` — only a
bare ``try/finally`` wrapped the call. If the configured ``climate_entity``
is removed or otherwise invalid, HA raises when the service call targets it,
and that exception propagated uncaught out of both functions. Since these
are called from inside the coordinator's per-cycle update, an uncaught
exception here could abort the whole cycle (forecast/classification/learning
bookkeeping too), not just the HVAC write — turning a detectable failure
into a crash.

This test file proves the fix: both write points now catch any exception
from the service call, log it, emit an ``incident_detected`` event with
``incident_class="hvac_command_failed"``, and return cleanly without
re-raising.

See: GitHub Issue #805
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


def _consume_coroutine(coro):
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies (mirrors test_dry_run.py)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    climate_state = MagicMock()
    climate_state.state = "heat_cool"
    climate_state.attributes = {
        "hvac_modes": ["off", "heat", "cool", "heat_cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        "current_temperature": 72.0,
    }
    hass.states.get.return_value = climate_state

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )


class TestSetHvacModeCommandFailure:
    """_set_hvac_mode() must not raise when the service call fails."""

    def test_service_call_exception_is_caught_not_raised(self, caplog):
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("Entity climate.thermostat not found")

        with caplog.at_level(logging.ERROR, logger=AUTOMATION_LOGGER):
            # Must not raise — this is the whole point of the fix.
            asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("HVAC command failed" in m for m in error_msgs)
        assert any("climate.thermostat" in m for m in error_msgs)

    def test_emits_hvac_command_failed_incident(self):
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("boom")
        emitted = []
        engine._emit_event_callback = lambda event_type, data: emitted.append((event_type, data))

        asyncio.run(engine._set_hvac_mode("heat", reason="test"))

        assert len(emitted) == 1
        event_type, data = emitted[0]
        assert event_type == "incident_detected"
        assert data["incident_class"] == "hvac_command_failed"
        assert data["hvac_mode"] == "heat"
        assert data["climate_entity"] == "climate.thermostat"
        assert "boom" in data["error"]

    def test_hvac_command_pending_reset_after_failure(self):
        """The finally block must still run and clear the pending flag."""
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("boom")

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        assert engine._hvac_command_pending is False

    def test_success_path_unaffected(self):
        """Normal (non-raising) calls still work exactly as before."""
        engine = _make_automation_engine()

        asyncio.run(engine._set_hvac_mode("heat", reason="test"))

        engine.hass.services.async_call.assert_called()
        assert engine._hvac_command_pending is False


class TestSetTemperatureCommandFailure:
    """_set_temperature() must not raise when the service call fails."""

    def test_service_call_exception_is_caught_not_raised(self, caplog):
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("Entity climate.thermostat not found")

        with caplog.at_level(logging.ERROR, logger=AUTOMATION_LOGGER):
            asyncio.run(engine._set_temperature(72, reason="test"))

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("Temperature command failed" in m for m in error_msgs)

    def test_emits_hvac_command_failed_incident(self):
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("boom")
        emitted = []
        engine._emit_event_callback = lambda event_type, data: emitted.append((event_type, data))

        asyncio.run(engine._set_temperature(72, reason="test", mode="cool"))

        assert len(emitted) == 1
        event_type, data = emitted[0]
        assert event_type == "incident_detected"
        assert data["incident_class"] == "hvac_command_failed"
        assert data["hvac_mode"] == "cool"
        assert data["climate_entity"] == "climate.thermostat"
        assert "boom" in data["error"]

    def test_temp_command_pending_reset_after_failure(self):
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("boom")

        asyncio.run(engine._set_temperature(72, reason="test"))

        assert engine._temp_command_pending is False

    def test_returns_early_does_not_schedule_verification(self):
        """A failed write must not schedule the setpoint-verify/retry logic —
        that logic assumes the write succeeded and would misfire against a
        value never actually sent."""
        engine = _make_automation_engine()
        engine.hass.services.async_call.side_effect = Exception("boom")
        scheduled = []
        # async_call_later is imported into automation.py's namespace; patch it
        # there so we can prove it's never reached on the failure path.
        import custom_components.climate_advisor.automation as automation_module

        original = automation_module.async_call_later
        automation_module.async_call_later = lambda *a, **k: scheduled.append((a, k))
        try:
            asyncio.run(engine._set_temperature(72, reason="test"))
        finally:
            automation_module.async_call_later = original

        assert scheduled == []

    def test_success_path_unaffected(self):
        engine = _make_automation_engine()

        asyncio.run(engine._set_temperature(72, reason="test"))

        engine.hass.services.async_call.assert_called()
        assert engine._temp_command_pending is False
