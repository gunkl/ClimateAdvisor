"""Tests for warm-day setback fixes — Root Causes E and C from Issue #96.

Root Cause E: apply_classification() schedules a revisit via _record_action() →
_schedule_revisit() every time it runs, causing a 5-min re-trigger loop.
Fix: cancel any pending revisit at the end of apply_classification().

Root Cause C: On warm/hot days, hard HVAC-off triggers Ecobee side-effects.
Fix: read current thermostat mode and apply setback without changing modes.

GitHub Issue: #96
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    if asyncio.iscoroutine(coro):
        coro.close()


def _make_engine(
    comfort_heat: float = 70.0,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Build an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": 76.0,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(
    day_type: str = "warm",
    hvac_mode: str = "off",
) -> DayClassification:
    """Build a DayClassification via object.__new__ (no __init__ required)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 78.0
    obj.today_low = 58.0
    obj.tomorrow_high = 79.0
    obj.tomorrow_low = 59.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_climate_state(
    engine: AutomationEngine,
    thermostat_mode: str,
    indoor_temp: float = 72.0,
) -> None:
    """Configure hass.states.get to return a climate state with given mode and temp.

    indoor_temp defaults to 72°F (above default comfort_heat=70°F) so the
    comfort-gap guard does NOT fire — tests can verify setback behavior.
    """
    climate_state = MagicMock()
    climate_state.state = thermostat_mode
    climate_state.attributes.get.return_value = indoor_temp
    engine.hass.states.get.return_value = climate_state


def _hvac_calls(engine: AutomationEngine) -> list:
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_hvac_mode"]


def _temp_calls(engine: AutomationEngine) -> list:
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_temperature"]


# ---------------------------------------------------------------------------
# Root Cause E — Classification loop prevention via revisit cancel
# ---------------------------------------------------------------------------


class TestClassificationLoopPrevention:
    """After apply_classification() returns, any pending revisit must be canceled.

    The fix: at the very end of apply_classification(), call self._revisit_cancel()
    and set it to None so the 5-min re-trigger loop cannot fire.
    """

    def test_revisit_canceled_after_classification_applied_heat_mode(self):
        """Heat classification → _revisit_cancel is None after apply_classification."""
        engine = _make_engine()
        _set_climate_state(engine, "heat", indoor_temp=65.0)

        c = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.apply_classification(c))

        assert engine._revisit_cancel is None

    def test_revisit_canceled_after_classification_applied_off_mode(self):
        """Warm-day classification (hvac_mode=off) → _revisit_cancel is None after apply."""
        engine = _make_engine()
        _set_climate_state(engine, "heat", indoor_temp=72.0)  # above comfort floor

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert engine._revisit_cancel is None

    def test_revisit_canceled_after_classification_applied_cool_mode(self):
        """Cool classification → _revisit_cancel is None after apply_classification."""
        engine = _make_engine()
        _set_climate_state(engine, "cool", indoor_temp=72.0)

        c = _make_classification(day_type="hot", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))

        assert engine._revisit_cancel is None

    def test_classification_applied_event_only_emitted_on_first_call(self):
        """Same (day_type, hvac_mode) applied twice → classification_applied emitted once."""
        engine = _make_engine()
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))
        asyncio.run(engine.apply_classification(c))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 1

    def test_classification_applied_event_emitted_when_day_type_changes(self):
        """Apply warm then mild (same hvac_mode) → two classification_applied events."""
        engine = _make_engine()
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c_warm = _make_classification(day_type="warm", hvac_mode="off")
        c_mild = _make_classification(day_type="mild", hvac_mode="off")
        asyncio.run(engine.apply_classification(c_warm))
        asyncio.run(engine.apply_classification(c_mild))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 2

    def test_classification_applied_event_emitted_when_hvac_mode_changes(self):
        """Same day_type but different hvac_mode → two classification_applied events."""
        engine = _make_engine()
        _set_climate_state(engine, "cool", indoor_temp=72.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c_off = _make_classification(day_type="warm", hvac_mode="off")
        c_cool = _make_classification(day_type="warm", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c_off))
        asyncio.run(engine.apply_classification(c_cool))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 2


# ---------------------------------------------------------------------------
# Root Cause C — Warm-day setback instead of hard off
# ---------------------------------------------------------------------------


class TestWarmDaySetbackInsteadOfOff:
    """On warm/hot days, apply setback without changing HVAC mode.

    The fix reads the current thermostat mode and calls _set_temperature()
    (or _set_temperature_dual() for heat_cool/auto) instead of _set_hvac_mode("off").
    This avoids Ecobee side-effects from a hard off command.
    """

    def test_heat_mode_thermostat_sets_setback_heat_no_mode_change(self):
        """Thermostat in 'heat' → set_temperature(setback_heat), no mode change."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        assert calls[0].args[2]["temperature"] == 60.0  # setback_heat

    def test_cool_mode_thermostat_sets_setback_cool_no_mode_change(self):
        """Thermostat in 'cool' → set_temperature(setback_cool), no mode change."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "cool", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        assert calls[0].args[2]["temperature"] == 82.0  # setback_cool

    def test_heat_cool_mode_sets_dual_setbacks_no_mode_change(self):
        """Thermostat in 'heat_cool' → dual setback via target_temp_low/high, no mode change."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat_cool", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        data = calls[0].args[2]
        assert data["target_temp_low"] == 60.0  # setback_heat
        assert data["target_temp_high"] == 82.0  # setback_cool

    def test_auto_mode_sets_dual_setbacks_no_mode_change(self):
        """Thermostat in 'auto' → dual setback via target_temp_low/high, no mode change."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "auto", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        data = calls[0].args[2]
        assert data["target_temp_low"] == 60.0  # setback_heat
        assert data["target_temp_high"] == 82.0  # setback_cool

    def test_unknown_mode_falls_back_to_hard_off(self):
        """Thermostat in 'unknown' mode → falls back to set_hvac_mode('off')."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "unknown", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        hvac = _hvac_calls(engine)
        assert len(hvac) == 1
        assert hvac[0].args[2]["hvac_mode"] == "off"

    def test_warm_day_setback_emits_event(self):
        """Setback path emits warm_day_setback_applied event with day_type and thermostat_mode."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        setback_events = [e for e in events if e[0] == "warm_day_setback_applied"]
        assert len(setback_events) == 1
        payload = setback_events[0][1]
        assert payload["day_type"] == "warm"
        assert payload["thermostat_mode"] == "heat"

    def test_comfort_gap_path_unaffected(self):
        """Indoor (65°F) < comfort_heat (70°F) → comfort-gap path fires, NOT setback."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat", indoor_temp=65.0)  # below comfort floor

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        # Comfort-gap path: heats to comfort_heat
        hvac = _hvac_calls(engine)
        assert len(hvac) == 1
        assert hvac[0].args[2]["hvac_mode"] == "heat"

        calls = _temp_calls(engine)
        assert len(calls) == 1
        assert calls[0].args[2]["temperature"] == 70.0  # comfort_heat, not setback

    def test_hot_day_heat_mode_uses_setback_heat(self):
        """day_type='hot' with thermostat in 'heat' → same setback behavior as 'warm'."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        c = _make_classification(day_type="hot", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        assert calls[0].args[2]["temperature"] == 60.0  # setback_heat

    def test_indoor_unavailable_still_applies_setback(self):
        """Indoor temp unavailable → setback still applied (comfort-gap guard checks None)."""
        engine = _make_engine(comfort_heat=70.0)
        # Return None for states.get so indoor temp is unavailable,
        # but we still need a way to know the thermostat mode.
        # When indoor temp is None, guard does not fire → setback path executes.
        # For this test, the climate state returns None for current_temperature
        # but we still need state.state for the thermostat mode.
        climate_state = MagicMock()
        climate_state.state = "heat"

        # Make attributes.get return None for current_temperature
        def _attr_get(key, default=None):
            if key == "current_temperature":
                return None
            return default

        climate_state.attributes.get.side_effect = _attr_get
        engine.hass.states.get.return_value = climate_state

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        # Comfort-gap guard: indoor_temp is None → guard does not fire → setback applied
        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        assert calls[0].args[2]["temperature"] == 60.0  # setback_heat

    def test_celsius_setback_heat_converted_to_user_unit(self):
        """temp_unit='celsius', setback_heat=60°F → from_fahrenheit() yields ~15.6°C in service call."""
        # Internal storage is in Fahrenheit; setback_heat=60°F → from_fahrenheit(60, "celsius") = 15.6°C
        engine = _make_engine(
            config_overrides={
                "temp_unit": "celsius",
                "setback_heat": 60.0,  # 60°F internal → 15.6°C after unit conversion
                "comfort_heat": 70.0,  # 70°F internal → guard won't fire with indoor=72°F
            },
        )
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        calls = _temp_calls(engine)
        assert len(calls) == 1
        # 60°F → 15.6°C (rounded to 1dp by from_fahrenheit)
        service_temp = calls[0].args[2]["temperature"]
        # from_fahrenheit(60, "celsius") = (60-32)*5/9 = 15.555... ≈ 15.6
        assert abs(service_temp - 15.6) < 0.1


# ---------------------------------------------------------------------------
# Issue #216 — warm_day_setback_applied event payload fields
# ---------------------------------------------------------------------------


class TestWarmDaySetbackEventPayload:
    """Verify warm_day_setback_applied event includes setpoint fields for cool mode
    and omits them for other modes (Issue #216).
    """

    def _set_climate_state_with_setpoint(
        self,
        engine: AutomationEngine,
        thermostat_mode: str,
        indoor_temp: float = 72.0,
        setpoint: float = 75.0,
    ) -> None:
        """Configure climate state with both current_temperature and temperature setpoint."""
        climate_state = MagicMock()
        climate_state.state = thermostat_mode

        def _attr_get(key, default=None):
            if key == "current_temperature":
                return indoor_temp
            if key == "temperature":
                return setpoint
            return default

        climate_state.attributes.get.side_effect = _attr_get
        engine.hass.states.get.return_value = climate_state

    def test_warm_day_setback_includes_setpoint_when_cool(self):
        """Thermostat in 'cool' mode → event has old_setpoint_f and new_setpoint_f fields."""
        engine = _make_engine(comfort_heat=70.0, config_overrides={"setback_cool": 82.0})
        self._set_climate_state_with_setpoint(engine, "cool", indoor_temp=72.0, setpoint=75.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        setback_events = [e for e in events if e[0] == "warm_day_setback_applied"]
        assert len(setback_events) == 1
        payload = setback_events[0][1]
        assert payload["thermostat_mode"] == "cool"
        assert "old_setpoint_f" in payload, "old_setpoint_f must be present for cool mode"
        assert "new_setpoint_f" in payload, "new_setpoint_f must be present for cool mode"
        # old setpoint was 75°F (read from thermostat); new is setback_cool=82°F
        assert payload["old_setpoint_f"] == 75.0
        assert payload["new_setpoint_f"] == 82.0

    def test_warm_day_setback_no_setpoint_when_already_off(self):
        """Thermostat in 'off' mode (unknown path) → event does NOT have setpoint fields."""
        engine = _make_engine(comfort_heat=70.0)
        # 'off' falls through to the else/unknown branch → hard-off, no setpoint fields
        climate_state = MagicMock()
        climate_state.state = "off"
        climate_state.attributes.get.return_value = 72.0  # indoor temp
        engine.hass.states.get.return_value = climate_state

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        setback_events = [e for e in events if e[0] == "warm_day_setback_applied"]
        assert len(setback_events) == 1
        payload = setback_events[0][1]
        assert "old_setpoint_f" not in payload, "old_setpoint_f must NOT be present for non-cool mode"
        assert "new_setpoint_f" not in payload, "new_setpoint_f must NOT be present for non-cool mode"

    def test_warm_day_setback_no_setpoint_when_heat_mode(self):
        """Thermostat in 'heat' mode → event does NOT have setpoint fields."""
        engine = _make_engine(comfort_heat=70.0)
        _set_climate_state(engine, "heat", indoor_temp=72.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        setback_events = [e for e in events if e[0] == "warm_day_setback_applied"]
        assert len(setback_events) == 1
        payload = setback_events[0][1]
        assert "old_setpoint_f" not in payload
        assert "new_setpoint_f" not in payload
