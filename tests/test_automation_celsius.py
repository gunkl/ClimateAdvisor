"""Tests for Celsius temperature unit support in AutomationEngine.

These tests verify the critical conversion in _set_temperature():
internal °F values must be converted to the user's unit before
being sent to the HA climate.set_temperature service.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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

        # Double-write (Issue #299): pre-write + target-write = 2 calls
        assert engine.hass.services.async_call.call_count == 2
        call_args = engine.hass.services.async_call.call_args  # last call = target write
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

        # Double-write (Issue #299): pre-write + target-write = 2 calls
        assert engine.hass.services.async_call.call_count == 2
        call_args = engine.hass.services.async_call.call_args  # last call = target write
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


# ---------------------------------------------------------------------------
# Tests: _set_temperature_dual() service payload
# ---------------------------------------------------------------------------


class TestSetTemperatureDual:
    """Verify _set_temperature_dual() sends a well-formed service call.

    The service data must include:
      - hvac_mode: "heat_cool"  (so Ecobee accepts the setpoints atomically)
      - target_temp_low / target_temp_high in the user's unit
      - entity_id

    Regression for Issue #286: missing hvac_mode caused Ecobee to snap back to
    its internal hold values within 1 second of CA's write.
    """

    def test_dual_includes_hvac_mode_in_pre_write_only(self):
        """_set_temperature_dual issues two calls when a mode switch is needed (Issue #299).

        Fix P1 (Issue #299): hvac_mode='heat_cool' is only included in the pre-write when
        the thermostat is not already in heat_cool mode. The target write omits hvac_mode to
        prevent the Ecobee from evaluating its comfort program and overwriting CA's setpoints.

        When the thermostat state is unknown/non-heat_cool (as in the test harness):
        - Call 1 (pre-write): includes hvac_mode='heat_cool' + offset setpoints
        - Call 2 (target write): NO hvac_mode + exact setpoints
        """
        engine = _make_automation(temp_unit="fahrenheit", comfort_heat=68.0, comfort_cool=74.0)
        # hass.states.get() returns a MagicMock whose .state != "heat_cool" → mode switch needed
        asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="test"))

        assert engine.hass.services.async_call.call_count == 2
        pre_write_args = engine.hass.services.async_call.call_args_list[0][0]
        target_write_args = engine.hass.services.async_call.call_args_list[1][0]
        # Pre-write must include hvac_mode so the mode switch reaches the Ecobee
        assert pre_write_args[2].get("hvac_mode") == "heat_cool", (
            "pre-write must include hvac_mode='heat_cool' to trigger the mode switch"
        )
        # Target write must NOT include hvac_mode — omitting it prevents Ecobee comfort-program lookup
        assert "hvac_mode" not in target_write_args[2], (
            "target write must omit hvac_mode to prevent Ecobee comfort-program reassertion (Fix P1)"
        )

    def test_dual_fahrenheit_passthrough(self):
        """Fahrenheit config: service receives the exact float values passed in."""
        engine = _make_automation(temp_unit="fahrenheit")

        asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="test"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert data["target_temp_low"] == 68.0
        assert data["target_temp_high"] == 74.0

    def test_dual_celsius_conversion(self):
        """Celsius config: service receives °C-converted values.

        68.0°F → 20.0°C, 74.0°F → 23.333...°C.
        """
        engine = _make_automation(temp_unit="celsius")

        asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="test"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert abs(data["target_temp_low"] - 20.0) < 0.01, f"Expected 20.0°C, got {data['target_temp_low']}"
        assert abs(data["target_temp_high"] - 23.333) < 0.01, f"Expected 23.333°C, got {data['target_temp_high']}"

    def test_dual_entity_id_forwarded(self):
        """The correct climate entity ID is always included in dual service call."""
        engine = _make_automation(temp_unit="fahrenheit")

        asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="entity check"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert data["entity_id"] == "climate.test_thermostat"

    def test_dual_dry_run_skips_service_call(self):
        """In dry_run mode, _set_temperature_dual never calls climate.set_temperature."""
        engine = _make_automation(temp_unit="fahrenheit")
        engine.dry_run = True

        asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="dry run"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: post-command setpoint validation (Fix 3, Issue #290)
# ---------------------------------------------------------------------------


def _make_automation_with_task_runner(
    temp_unit: str = "fahrenheit",
    comfort_cool: float = 76.0,
    comfort_heat: float = 68.0,
) -> AutomationEngine:
    """Create an AutomationEngine whose async_create_task actually runs coroutines.

    This lets validation callback tests exercise _check_dual_setpoint_accepted /
    _check_single_setpoint_accepted directly without needing a real event loop.
    """
    hass = MagicMock()
    hass.services.async_call = AsyncMock()

    def _run_coroutine(coro):
        asyncio.run(coro)

    hass.async_create_task = MagicMock(side_effect=_run_coroutine)

    config: dict = {
        "climate_entity": "climate.test_thermostat",
        "temp_unit": temp_unit,
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity=config["climate_entity"],
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


class TestSetpointValidation:
    """Post-command thermostat validation fires 10 s after _set_temperature_dual/single.

    Pattern: patch async_call_later to capture the scheduled lambda, then invoke
    it with a mock _now to run the validation coroutine synchronously.
    """

    # ── dual setpoint: MISMATCH ───────────────────────────────────────────────

    def test_setpoint_validation_mismatch_logs_error(self):
        """Dual setpoint validation fires and logs error when thermostat reports wrong values.

        Occupant impact: if the thermostat silently rejects a setpoint command,
        the home may heat or cool to the wrong temperature all day with no alert.

        Setup: command low=68.0 high=74.0, thermostat reports low=67.0 high=77.0
        (both outside ±0.6°F tolerance).
        Expected: _LOGGER.error called, 'setpoint_rejected' event emitted.
        """
        engine = _make_automation_with_task_runner()
        emitted_events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: emitted_events.append((name, data))

        # Thermostat state reports wrong values after the command
        wrong_state = MagicMock()
        wrong_state.attributes = {
            "target_temp_low": 67.0,
            "target_temp_high": 77.0,
        }
        engine.hass.states.get = MagicMock(return_value=wrong_state)

        captured_callbacks: list = []

        def fake_call_later(hass, delay, callback):
            captured_callbacks.append((delay, callback))
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=fake_call_later,
            ),
            patch("custom_components.climate_advisor.automation._LOGGER") as mock_logger,
        ):
            asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="comfort band"))

            assert len(captured_callbacks) == 1, "Expected exactly one async_call_later call"
            delay, callback = captured_callbacks[0]
            assert delay == 10, f"Validation delay should be 10 s, got {delay}"

            # Fire the callback inside the patch context so _LOGGER is still mocked
            callback(None)

        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "FAILED" in error_msg or "validation" in error_msg.lower(), (
            f"Expected validation failure message, got: {error_msg}"
        )

        rejected = [e for e in emitted_events if e[0] == "setpoint_rejected"]
        assert len(rejected) == 1, f"Expected 'setpoint_rejected' event, got: {emitted_events}"
        payload = rejected[0][1]
        assert payload["commanded_low"] == 68.0
        assert payload["commanded_high"] == 74.0
        assert payload["reported_low"] == 67.0
        assert payload["reported_high"] == 77.0

    # ── dual setpoint: MATCH ─────────────────────────────────────────────────

    def test_setpoint_validation_match_logs_info(self):
        """Dual setpoint validation succeeds when thermostat reports matching values.

        Occupant impact: confirmation that the thermostat accepted the setpoint
        means heating/cooling will proceed as intended.

        Setup: command low=68.0 high=74.0, thermostat reports exactly that.
        Expected: _LOGGER.info called with 'confirmed', no error, no event.
        """
        engine = _make_automation_with_task_runner()
        emitted_events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: emitted_events.append((name, data))

        # Thermostat state reports matching values
        ok_state = MagicMock()
        ok_state.attributes = {
            "target_temp_low": 68.0,
            "target_temp_high": 74.0,
        }
        engine.hass.states.get = MagicMock(return_value=ok_state)

        captured_callbacks: list = []

        def fake_call_later(hass, delay, callback):
            captured_callbacks.append((delay, callback))
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=fake_call_later,
            ),
            patch("custom_components.climate_advisor.automation._LOGGER") as mock_logger,
        ):
            asyncio.run(engine._set_temperature_dual(68.0, 74.0, reason="comfort band"))

            assert len(captured_callbacks) == 1
            _, callback = captured_callbacks[0]

            # Fire the callback inside the patch context so _LOGGER is still mocked
            callback(None)

        mock_logger.error.assert_not_called()
        rejected = [e for e in emitted_events if e[0] == "setpoint_rejected"]
        assert len(rejected) == 0, f"No rejection event expected, got: {rejected}"

        # Info log must mention confirmation
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        confirmed = any("confirmed" in c.lower() for c in info_calls)
        assert confirmed, f"Expected 'confirmed' in info log, calls: {info_calls}"

    # ── single setpoint: MISMATCH ─────────────────────────────────────────────

    def test_single_setpoint_validation_mismatch_logs_error(self):
        """Single setpoint validation fires and logs error when thermostat reports wrong value.

        Occupant impact: if the thermostat rejects a heat/cool setpoint command,
        the home stays at the wrong temperature without any operator alert.

        Setup: command 72.0°F, thermostat reports temperature=69.0 (outside ±0.6°F).
        Expected: _LOGGER.error called, 'setpoint_rejected' event emitted.
        """
        engine = _make_automation_with_task_runner()
        emitted_events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: emitted_events.append((name, data))

        wrong_state = MagicMock()
        wrong_state.state = "heat"
        wrong_state.attributes = {"temperature": 69.0, "hvac_action": "idle", "fan_mode": "auto"}
        engine.hass.states.get = MagicMock(return_value=wrong_state)

        captured_callbacks: list = []

        def fake_call_later(hass, delay, callback):
            captured_callbacks.append((delay, callback))
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=fake_call_later,
            ),
            patch("custom_components.climate_advisor.automation._LOGGER") as mock_logger,
        ):
            asyncio.run(engine._set_temperature(72.0, reason="heat setback"))

            assert len(captured_callbacks) == 1
            _, callback = captured_callbacks[0]

            # Fire the callback inside the patch context so _LOGGER is still mocked
            callback(None)

        mock_logger.error.assert_called()
        rejected = [e for e in emitted_events if e[0] == "setpoint_rejected"]
        assert len(rejected) == 1, f"Expected 'setpoint_rejected' event, got: {emitted_events}"
        payload = rejected[0][1]
        assert payload["commanded"] == 72.0
        assert payload["reported"] == 69.0

    # ── single setpoint: MATCH ────────────────────────────────────────────────

    def test_single_setpoint_validation_match_logs_info(self):
        """Single setpoint validation succeeds when thermostat reports matching value.

        Setup: command 72.0°F, thermostat reports temperature=72.0 (within ±0.6°F).
        Expected: no error, no event, info log contains 'confirmed'.
        """
        engine = _make_automation_with_task_runner()
        emitted_events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: emitted_events.append((name, data))

        ok_state = MagicMock()
        ok_state.state = "heat"
        ok_state.attributes = {"temperature": 72.0, "hvac_action": "idle", "fan_mode": "auto"}
        engine.hass.states.get = MagicMock(return_value=ok_state)

        captured_callbacks: list = []

        def fake_call_later(hass, delay, callback):
            captured_callbacks.append((delay, callback))
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=fake_call_later,
            ),
            patch("custom_components.climate_advisor.automation._LOGGER") as mock_logger,
        ):
            asyncio.run(engine._set_temperature(72.0, reason="heat setback"))

            assert len(captured_callbacks) == 1
            _, callback = captured_callbacks[0]

            # Fire the callback inside the patch context so _LOGGER is still mocked
            callback(None)

        mock_logger.error.assert_not_called()
        rejected = [e for e in emitted_events if e[0] == "setpoint_rejected"]
        assert len(rejected) == 0

        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        confirmed = any("confirmed" in c.lower() for c in info_calls)
        assert confirmed, f"Expected 'confirmed' in info log, calls: {info_calls}"
