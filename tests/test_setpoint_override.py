"""Tests for setpoint-only manual override detection.

Verifies that when a user changes only the thermostat setpoint (temperature)
while the HVAC mode still matches the classification, the override confirmation
and grace period mechanisms fire correctly.

Source: automation.py handle_manual_override / start_override_confirmation /
        _confirm_override_expired; coordinator.py setpoint-change block.

See: GitHub Issue #196
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_OVERRIDE_CONFIRM_PERIOD,
)

# Patch dt_util.now to return a real datetime (needed for isoformat() calls)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 1, 17, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides=None):
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_OVERRIDE_CONFIRM_PERIOD: 0,  # bypass confirmation by default
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


def _make_classification(
    day_type="warm",
    hvac_mode="cool",
    **kwargs,
):
    """Create a DayClassification with explicit fields (bypass __post_init__)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = kwargs.get("trend_direction", "stable")
    obj.trend_magnitude = kwargs.get("trend_magnitude", 2.0)
    obj.today_high = kwargs.get("today_high", 82.0)
    obj.today_low = kwargs.get("today_low", 60.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 83.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 61.0)
    obj.hvac_mode = hvac_mode
    obj.pre_condition = kwargs.get("pre_condition", False)
    obj.pre_condition_target = kwargs.get("pre_condition_target")
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = kwargs.get("setback_modifier", 0.0)
    return obj


def _start_confirmation_and_capture(engine, source="normal"):
    """Call start_override_confirmation and capture the timer callback.

    Returns the captured callback (callable) or None if not captured.
    """
    captured = []

    def _fake_async_call_later(hass, delay, fn):
        captured.append(fn)
        return MagicMock()  # cancel function stub

    with (
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
        patch("custom_components.climate_advisor.automation.async_call_later", side_effect=_fake_async_call_later),
    ):
        engine.start_override_confirmation(source=source)

    return captured[0] if captured else None


# ---------------------------------------------------------------------------
# TestSetpointOverrideConfirmation — PATH A always fires for setpoint source
# ---------------------------------------------------------------------------


class TestSetpointOverrideConfirmation:
    """Setpoint overrides always take PATH A regardless of mode match."""

    def test_setpoint_source_always_confirms_even_when_mode_matches(self):
        """source='setpoint' with mode matching classification → PATH A (override confirmed)."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        # Classification says "cool" — thermostat is also "cool" (mode matches)
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"  # mode matches classification — would normally PATH B
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="setpoint")
        assert fired is not None

        # Fire the timer — mode still "cool" (matches classification "cool")
        fired(None)

        # Must confirm the override (PATH A), not self-resolve (PATH B)
        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"

    def test_setpoint_source_confirm_sets_override_active(self):
        """After PATH A fires for setpoint source, _manual_override_active is True."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="setpoint")
        fired(None)

        assert engine._manual_override_active is True
        assert engine._override_confirm_pending is False

    def test_setpoint_source_confirm_emits_override_confirmed_event(self):
        """source='setpoint' timer fires → 'override_confirmed' event emitted."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        emitted = []
        engine._emit_event_callback = lambda event_type, payload: emitted.append((event_type, payload))

        fired = _start_confirmation_and_capture(engine, source="setpoint")
        fired(None)

        event_types = [e[0] for e in emitted]
        assert "override_confirmed" in event_types

    def test_setpoint_source_stored_on_confirmation_start(self):
        """_override_confirm_source is 'setpoint' while confirmation is pending."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture(engine, source="setpoint")

        assert engine._override_confirm_source == "setpoint"
        assert engine._override_confirm_pending is True

    def test_setpoint_source_cleared_after_confirm(self):
        """_override_confirm_source is None after the timer fires and override is confirmed."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="setpoint")
        fired(None)

        assert engine._override_confirm_source is None


# ---------------------------------------------------------------------------
# TestNormalModeOverrideUnchanged — regression: mode-change path still works
# ---------------------------------------------------------------------------


class TestNormalModeOverrideUnchanged:
    """Normal mode-change override behavior is not affected by the setpoint changes."""

    def test_normal_source_still_path_a_when_divergent(self):
        """source='normal', mode diverges from classification → PATH A confirmed."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        # Classification says "heat", thermostat is "cool"
        c = _make_classification(hvac_mode="heat")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"  # diverges from classification "heat"
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="normal")
        fired(None)

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"

    def test_normal_source_still_path_b_when_resolved(self):
        """source='normal', mode matches classification → PATH B self-resolved."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        # Classification says "cool", thermostat also "cool"
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"  # matches classification — PATH B
        engine.hass.states.get.return_value = state

        emitted = []
        engine._emit_event_callback = lambda event_type, payload: emitted.append((event_type, payload))

        fired = _start_confirmation_and_capture(engine, source="normal")
        fired(None)

        # Must self-resolve, NOT confirm
        assert engine._manual_override_active is False
        event_types = [e[0] for e in emitted]
        assert "override_self_resolved" in event_types
        assert "override_confirmed" not in event_types


# ---------------------------------------------------------------------------
# TestHandleManualOverrideSourceKwarg — handle_manual_override accepts source
# ---------------------------------------------------------------------------


class TestHandleManualOverrideSourceKwarg:
    """handle_manual_override accepts source kwarg and passes it through."""

    def test_handle_manual_override_default_source_is_normal(self):
        """handle_manual_override() with no source arg uses source='normal'."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "heat"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture_via_handle(engine, source=None)

        assert engine._override_confirm_source == "normal"

    def test_handle_manual_override_setpoint_source_stored(self):
        """handle_manual_override(source='setpoint') stores 'setpoint' in confirm source."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture_via_handle(engine, source="setpoint")

        assert engine._override_confirm_source == "setpoint"


def _start_confirmation_and_capture_via_handle(engine, source):
    """Call handle_manual_override and capture the timer callback."""
    captured = []

    def _fake_async_call_later(hass, delay, fn):
        captured.append(fn)
        return MagicMock()

    kwargs = {}
    if source is not None:
        kwargs["source"] = source

    with (
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
        patch("custom_components.climate_advisor.automation.async_call_later", side_effect=_fake_async_call_later),
    ):
        engine.handle_manual_override(**kwargs)

    return captured[0] if captured else None


# ---------------------------------------------------------------------------
# TestOverrideGateClassification — apply_classification blocked after setpoint override
# ---------------------------------------------------------------------------


class TestOverrideGateClassification:
    """apply_classification is blocked while setpoint override confirmation is pending."""

    def test_apply_classification_blocked_after_setpoint_override(self):
        """start_override_confirmation(source='setpoint') sets pending; apply_classification skips."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture(engine, source="setpoint")

        assert engine._override_confirm_pending is True

        # Now try to apply a new classification
        c = _make_classification(hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))

        # No HA service call should have been made
        engine.hass.services.async_call.assert_not_called()

    def test_clear_manual_override_clears_source_field(self):
        """clear_manual_override() resets _override_confirm_source to None."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture(engine, source="setpoint")

        assert engine._override_confirm_source == "setpoint"

        engine.clear_manual_override()

        assert engine._override_confirm_source is None
        assert engine._override_confirm_pending is False
