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
# Tests: _set_temperature() unit conversion — single call with hvac_mode
# ---------------------------------------------------------------------------


class TestSetTemperatureCelsius:
    """Verify _set_temperature() sends the correct unit to the HA climate service.

    Issue #301: _set_temperature() now issues ONE call with both hvac_mode and
    temperature in the payload (no pre-write, no double-write).
    """

    def test_set_temperature_sends_celsius_to_ha(self):
        """_set_temperature with celsius config converts °F value to °C before service call.

        comfort_cool is stored as 75.2°F internally.
        75.2°F → (75.2 − 32) × 5/9 ≈ 24.0°C  → service must receive 24.0.
        Single call (Issue #301): one set_temperature call with hvac_mode + temperature.
        """
        engine = _make_automation(temp_unit="celsius", comfort_cool=75.2)

        asyncio.run(engine._set_temperature(75.2, reason="test"))

        # Single call (Issue #301): one set_temperature call
        assert engine.hass.services.async_call.call_count == 1
        call_args = engine.hass.services.async_call.call_args
        domain, service, data = call_args[0]
        assert domain == "climate"
        assert service == "set_temperature"
        sent_temp = data["temperature"]
        # 75.2°F → 24.0°C (within rounding tolerance)
        assert abs(sent_temp - 24.0) < 0.1
        # hvac_mode is always included in the single call
        assert "hvac_mode" in data

    def test_set_temperature_fahrenheit_passthrough(self):
        """_set_temperature with fahrenheit config sends °F value unchanged.

        75.0°F → service must receive 75.0.
        Single call (Issue #301): one set_temperature call with hvac_mode + temperature.
        """
        engine = _make_automation(temp_unit="fahrenheit", comfort_cool=75.0)

        asyncio.run(engine._set_temperature(75.0, reason="test"))

        # Single call (Issue #301)
        assert engine.hass.services.async_call.call_count == 1
        call_args = engine.hass.services.async_call.call_args
        domain, service, data = call_args[0]
        assert domain == "climate"
        assert service == "set_temperature"
        assert data["temperature"] == 75.0
        assert "hvac_mode" in data

    def test_set_temperature_includes_hvac_mode_cool(self):
        """_set_temperature with mode='cool' includes hvac_mode='cool' in service call."""
        engine = _make_automation(temp_unit="fahrenheit", comfort_cool=75.0)

        asyncio.run(engine._set_temperature(75.0, reason="test", mode="cool"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert data["hvac_mode"] == "cool"
        assert data["temperature"] == 75.0

    def test_set_temperature_includes_hvac_mode_heat(self):
        """_set_temperature with mode='heat' includes hvac_mode='heat' in service call."""
        engine = _make_automation(temp_unit="fahrenheit", comfort_heat=68.0)

        asyncio.run(engine._set_temperature(68.0, reason="test", mode="heat"))

        call_args = engine.hass.services.async_call.call_args
        data = call_args[0][2]
        assert data["hvac_mode"] == "heat"
        assert data["temperature"] == 68.0

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
# Tests: post-command setpoint validation (Fix 3, Issue #290)
# ---------------------------------------------------------------------------


def _make_automation_with_task_runner(
    temp_unit: str = "fahrenheit",
    comfort_cool: float = 76.0,
    comfort_heat: float = 68.0,
) -> AutomationEngine:
    """Create an AutomationEngine whose async_create_task actually runs coroutines.

    This lets validation callback tests exercise _check_single_setpoint_accepted
    directly without needing a real event loop.
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
    """Post-command thermostat validation fires 10 s after _set_temperature.

    Pattern: patch async_call_later to capture the scheduled lambda, then invoke
    it with a mock _now to run the validation coroutine synchronously.
    """

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


# ---------------------------------------------------------------------------
# Tests: retry scheduler (Issue #301)
# ---------------------------------------------------------------------------


class TestSetpointRetry:
    """_check_single_setpoint_accepted schedules a 15-minute retry on mismatch.

    Three scenarios:
    1. Mismatch → async_call_later called with delay=900
    2. Retry callback calls _set_temperature with correct args when seq matches
    3. Retry callback skips when _write_seq changed (newer command)
    """

    def _make_engine_stub(self) -> AutomationEngine:
        """Minimal AutomationEngine with controllable seq and pending state."""
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        config: dict = {
            "climate_entity": "climate.test_thermostat",
            "temp_unit": "fahrenheit",
            "comfort_heat": 68.0,
            "comfort_cool": 76.0,
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

    def _fire_validation_cb(self, engine: AutomationEngine, validation_cb, captured_call_later: list) -> None:
        """Invoke the 10s validation lambda: extract the inner coroutine via async_create_task and run it.

        The validation callback is ``lambda _now: hass.async_create_task(coro)``.
        We intercept async_create_task to grab the coroutine, then run it directly.
        """
        coros: list = []

        def capture_task(coro):
            coros.append(coro)

        engine.hass.async_create_task = MagicMock(side_effect=capture_task)
        validation_cb(None)
        assert len(coros) == 1, "validation lambda must call async_create_task once"
        # Run the captured _check_single_setpoint_accepted coroutine
        asyncio.run(coros[0])

    def test_mismatch_fires_retry_scheduler(self):
        """Thermostat reports wrong temp → async_call_later called with delay=900.

        Occupant impact: without retry scheduling, a rejected setpoint leaves the
        thermostat at the wrong temperature indefinitely until the next 30-min cycle.
        With retry, the command is re-sent within 15 minutes.
        """
        engine = self._make_engine_stub()

        # Thermostat reports wrong value (mismatch > 0.6°F tolerance)
        wrong_state = MagicMock()
        wrong_state.state = "cool"
        wrong_state.attributes = {"temperature": 72.0, "hvac_action": "idle", "fan_mode": "auto"}
        engine.hass.states.get = MagicMock(return_value=wrong_state)

        captured_call_later: list = []

        def fake_call_later(hass, delay, callback):
            captured_call_later.append((delay, callback))
            return MagicMock()

        # Run _set_temperature so validation is scheduled (10s), then fire it to trigger retry
        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            asyncio.run(engine._set_temperature(75.0, reason="test", mode="cool"))

        # First call_later = 10s validation; fire it to get the 900s retry scheduled
        assert len(captured_call_later) >= 1
        validation_delay, validation_cb = captured_call_later[0]
        assert validation_delay == 10

        # Reset so we can capture the retry call_later
        captured_call_later.clear()

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            # Fire the validation lambda — mismatch detected, should schedule 900s retry
            self._fire_validation_cb(engine, validation_cb, captured_call_later)

        # 900s retry must have been scheduled
        retry_calls = [(d, cb) for d, cb in captured_call_later if d == 900]
        assert len(retry_calls) == 1, (
            f"Expected async_call_later(delay=900) for retry, got delays: {[d for d, _ in captured_call_later]}"
        )

    def test_retry_callback_calls_set_temperature_when_seq_matches(self):
        """Retry callback re-issues _set_temperature with correct args when write_seq unchanged.

        Occupant impact: the retry must send the SAME temp and mode as the original
        command so the thermostat ends up at the intended setpoint.
        """
        engine = self._make_engine_stub()

        wrong_state = MagicMock()
        wrong_state.state = "cool"
        wrong_state.attributes = {"temperature": 72.0, "hvac_action": "idle", "fan_mode": "auto"}
        engine.hass.states.get = MagicMock(return_value=wrong_state)

        captured_call_later: list = []

        def fake_call_later(hass, delay, callback):
            captured_call_later.append((delay, callback))
            return MagicMock()

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            asyncio.run(engine._set_temperature(75.0, reason="test", mode="cool"))

        # Fire 10s validation to get 900s retry registered
        validation_cb = captured_call_later[0][1]
        captured_call_later.clear()

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            self._fire_validation_cb(engine, validation_cb, captured_call_later)

        # Capture retry callback (the lambda wrapping _retry_callback)
        assert len(captured_call_later) >= 1
        _, retry_lambda = captured_call_later[0]

        # Now invoke the retry lambda — it fires _retry_callback via async_create_task
        retry_coros: list = []

        def capture_retry_task(coro):
            retry_coros.append(coro)

        engine.hass.async_create_task = MagicMock(side_effect=capture_retry_task)
        engine.hass.services.async_call.reset_mock()

        # The lambda calls hass.async_create_task(_retry_callback(_now))
        retry_lambda(None)

        # Run the captured coroutine
        assert len(retry_coros) == 1
        asyncio.run(retry_coros[0])

        # _set_temperature must have been called with 75.0 and mode="cool"
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) >= 1
        last_call_data = calls[-1][0][2]
        assert abs(last_call_data["temperature"] - 75.0) < 0.1, (
            f"Retry must send original temp 75.0, got {last_call_data['temperature']}"
        )
        assert last_call_data["hvac_mode"] == "cool", (
            f"Retry must send original mode 'cool', got {last_call_data.get('hvac_mode')}"
        )

    def test_retry_callback_skips_when_write_seq_changed(self):
        """Retry callback skips when a newer command has superseded the original.

        Occupant impact: without this guard, a stale retry could overwrite a more
        recent CA command (e.g. a bedtime setback issued since the validation fired).
        """
        engine = self._make_engine_stub()

        wrong_state = MagicMock()
        wrong_state.state = "cool"
        wrong_state.attributes = {"temperature": 72.0, "hvac_action": "idle", "fan_mode": "auto"}
        engine.hass.states.get = MagicMock(return_value=wrong_state)

        captured_call_later: list = []

        def fake_call_later(hass, delay, callback):
            captured_call_later.append((delay, callback))
            return MagicMock()

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            asyncio.run(engine._set_temperature(75.0, reason="test", mode="cool"))

        # Fire 10s validation to register 900s retry
        validation_cb = captured_call_later[0][1]
        captured_call_later.clear()

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            side_effect=fake_call_later,
        ):
            self._fire_validation_cb(engine, validation_cb, captured_call_later)

        assert len(captured_call_later) >= 1
        _, retry_lambda = captured_call_later[0]

        # Simulate a newer command superseding the original
        engine._write_seq += 1

        retry_coros: list = []

        def capture_retry_task(coro):
            retry_coros.append(coro)

        engine.hass.async_create_task = MagicMock(side_effect=capture_retry_task)
        engine.hass.services.async_call.reset_mock()

        retry_lambda(None)

        if retry_coros:
            asyncio.run(retry_coros[0])

        # No new service call must have been issued
        assert engine.hass.services.async_call.call_count == 0, (
            "Retry callback must NOT call set_temperature when _write_seq has been incremented "
            "(a newer command superseded the original)."
        )
