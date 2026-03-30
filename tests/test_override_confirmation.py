"""Tests for override detection confirmation period (Issue #76).

Verifies that transient HVAC events (restart, fan cycles) are not immediately
treated as manual overrides.  A configurable confirmation window must elapse
before an override is formally accepted.  If the state resolves during the
window, the potential override is silently discarded.

See: GitHub Issue #76
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
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 19, 14, 30, 0)


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
    trend_direction="stable",
    trend_magnitude=2.0,
    setback_modifier=0.0,
    pre_condition=False,
    pre_condition_target=None,
    **kwargs,
):
    """Create a DayClassification with explicit fields (bypass __post_init__)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = trend_direction
    obj.trend_magnitude = trend_magnitude
    obj.today_high = kwargs.get("today_high", 78.0)
    obj.today_low = kwargs.get("today_low", 58.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 79.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 59.0)
    obj.hvac_mode = hvac_mode
    obj.pre_condition = pre_condition
    obj.pre_condition_target = pre_condition_target
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    return obj


def _start_confirmation_and_capture(engine, source="normal"):
    """Call start_override_confirmation and capture the timer callback.

    Patches @callback to be a no-op pass-through and async_call_later to
    capture the scheduled function so tests can fire it manually.

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
# Tests: bypass (CONF_OVERRIDE_CONFIRM_PERIOD = 0)
# ---------------------------------------------------------------------------


class TestConfirmationBypass:
    """When period == 0, override is accepted immediately (legacy behaviour)."""

    def test_immediate_override_when_period_zero(self):
        """With period=0, start_override_confirmation sets override at once."""
        engine = _make_automation_engine()  # default period=0

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        engine.start_override_confirmation(source="normal")

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"
        assert engine._override_confirm_pending is False

    def test_handle_manual_override_delegates_to_confirmation(self):
        """handle_manual_override() calls start_override_confirmation internally."""
        engine = _make_automation_engine()  # period=0 → immediate

        state = MagicMock()
        state.state = "heat"
        engine.hass.states.get.return_value = state

        engine.handle_manual_override()

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "heat"


# ---------------------------------------------------------------------------
# Tests: pending state
# ---------------------------------------------------------------------------


class TestConfirmationPending:
    """Verify the intermediate pending state while the window is open."""

    def test_pending_state_set_when_period_nonzero(self):
        """start_override_confirmation sets _override_confirm_pending while timer is running."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture(engine, source="normal")

        assert engine._override_confirm_pending is True
        assert engine._override_confirm_mode == "cool"
        assert engine._override_confirm_time is not None
        # Override not yet active
        assert engine._manual_override_active is False

    def test_apply_classification_skips_when_pending(self):
        """apply_classification must not change HVAC while confirmation is pending."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        # Simulate pending state (set directly — mirrors what the timer sets)
        engine._override_confirm_pending = True
        engine._override_confirm_mode = "heat"
        engine._override_confirm_time = "2026-03-19T14:20:00"

        c = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.apply_classification(c))

        engine.hass.services.async_call.assert_not_called()

    def test_apply_classification_still_stores_classification_when_pending(self):
        """Even during pending state, the new classification object is stored."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})
        engine._override_confirm_pending = True

        c = _make_classification(day_type="hot", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))

        assert engine._current_classification is c

    def test_restarting_confirmation_cancels_previous_timer(self):
        """A second call to start_override_confirmation cancels the first timer."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "heat"
        engine.hass.states.get.return_value = state

        # Plant a fake cancel function
        first_cancel = MagicMock()
        engine._override_confirm_cancel = first_cancel

        _start_confirmation_and_capture(engine, source="normal")

        first_cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: timer fires — confirmed
# ---------------------------------------------------------------------------


class TestConfirmationTimerConfirmed:
    """Override is confirmed when the timer fires and state is still divergent."""

    def test_timer_fires_divergent_sets_override(self):
        """Timer fires, HVAC still differs from classification → override confirmed."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        # Classification says "heat", thermostat is "cool"
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="normal")
        assert fired is not None, "async_call_later was not called"

        assert engine._override_confirm_pending is True

        # Fire the timer — state still "cool" (divergent from "heat")
        fired(None)

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"
        assert engine._override_confirm_pending is False

    def test_timer_fires_emits_override_confirmed_event(self):
        """Timer confirmation emits the override_confirmed event via callback."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        events = []
        engine._emit_event_callback = lambda event_type, data: events.append((event_type, data))

        fired = _start_confirmation_and_capture(engine, source="normal")
        fired(None)

        event_types = [e[0] for e in events]
        assert "override_confirmed" in event_types


# ---------------------------------------------------------------------------
# Tests: timer fires — self-resolved
# ---------------------------------------------------------------------------


class TestConfirmationSelfResolved:
    """Override is discarded when the thermostat returns to classification mode."""

    def test_timer_fires_resolved_clears_pending_no_override(self):
        """Timer fires, HVAC now matches classification → no override, pending cleared."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        # Initially show "cool" so confirmation starts
        divergent_state = MagicMock()
        divergent_state.state = "cool"
        engine.hass.states.get.return_value = divergent_state

        fired = _start_confirmation_and_capture(engine, source="normal")
        assert fired is not None

        # Now thermostat returned to "heat" (matches classification)
        resolved_state = MagicMock()
        resolved_state.state = "heat"
        engine.hass.states.get.return_value = resolved_state

        fired(None)

        assert engine._manual_override_active is False
        assert engine._override_confirm_pending is False
        assert engine._override_confirm_mode is None

    def test_timer_fires_resolved_emits_self_resolved_event(self):
        """Self-resolve emits the override_self_resolved event."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        divergent_state = MagicMock()
        divergent_state.state = "cool"
        engine.hass.states.get.return_value = divergent_state

        events = []
        engine._emit_event_callback = lambda event_type, data: events.append((event_type, data))

        fired = _start_confirmation_and_capture(engine, source="normal")

        resolved_state = MagicMock()
        resolved_state.state = "heat"
        engine.hass.states.get.return_value = resolved_state

        fired(None)

        event_types = [e[0] for e in events]
        assert "override_self_resolved" in event_types

    def test_timer_noop_when_pending_cleared(self):
        """If pending was already cleared before timer fires, callback is a no-op."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        fired = _start_confirmation_and_capture(engine, source="normal")
        assert fired is not None

        # Clear pending before timer fires (e.g., user called clear_manual_override)
        engine._override_confirm_pending = False

        fired(None)

        # Should not activate override
        assert engine._manual_override_active is False


# ---------------------------------------------------------------------------
# Tests: clear_manual_override cancels pending timer
# ---------------------------------------------------------------------------


class TestClearCancelsPending:
    """clear_manual_override() must also cancel any pending confirmation timer."""

    def test_clear_cancels_pending_confirmation(self):
        """clear_manual_override() cancels the pending timer and resets state."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        _start_confirmation_and_capture(engine, source="normal")
        assert engine._override_confirm_pending is True

        # Plant a trackable cancel stub (the real one was replaced by the capture helper)
        cancel_stub = MagicMock()
        engine._override_confirm_cancel = cancel_stub

        engine.clear_manual_override()

        cancel_stub.assert_called_once()
        assert engine._override_confirm_pending is False
        assert engine._override_confirm_time is None
        assert engine._override_confirm_mode is None

    def test_clear_noop_when_no_pending(self):
        """clear_manual_override() is safe when no confirmation is pending."""
        engine = _make_automation_engine()

        assert engine._override_confirm_pending is False

        # Should not raise
        engine.clear_manual_override()

        assert engine._override_confirm_pending is False


# ---------------------------------------------------------------------------
# Tests: serializable state includes pending fields
# ---------------------------------------------------------------------------


class TestSerializableState:
    """get_serializable_state() exposes the confirmation pending fields."""

    def test_pending_fields_in_serializable_state(self):
        """Pending fields appear in get_serializable_state() output."""
        engine = _make_automation_engine(config_overrides={CONF_OVERRIDE_CONFIRM_PERIOD: 600})

        state_obj = MagicMock()
        state_obj.state = "cool"
        engine.hass.states.get.return_value = state_obj

        _start_confirmation_and_capture(engine, source="normal")

        serialized = engine.get_serializable_state()
        assert "override_confirm_pending" in serialized
        assert serialized["override_confirm_pending"] is True
        assert "override_confirm_time" in serialized
        assert serialized["override_confirm_time"] is not None

    def test_pending_false_in_serializable_state_when_inactive(self):
        """When no confirmation is pending, fields are False/None."""
        engine = _make_automation_engine()

        serialized = engine.get_serializable_state()
        assert serialized.get("override_confirm_pending") is False
        assert serialized.get("override_confirm_time") is None
