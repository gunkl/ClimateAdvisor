"""Tests for action tracking in the AutomationEngine.

Verifies that _record_action correctly stores time/reason and that
state serialization/deserialization includes action and override fields.

See: GitHub Issue #37
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActionTracking:
    """Verify _record_action stores time and reason on HVAC actions."""

    def test_set_hvac_mode_records_action(self):
        """_set_hvac_mode should record action time and reason."""
        engine = _make_automation_engine()
        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        assert engine._last_action_time is not None
        assert "Set HVAC to cool" in engine._last_action_reason
        assert "test" in engine._last_action_reason

    def test_set_temperature_records_action(self):
        """_set_temperature should record action time and reason."""
        engine = _make_automation_engine()
        asyncio.run(engine._set_temperature(72, reason="test"))

        assert engine._last_action_time is not None
        assert "Set temp to 72" in engine._last_action_reason
        assert "test" in engine._last_action_reason

    def test_dry_run_does_not_record_action(self):
        """In dry-run mode, no service call happens and no action is recorded."""
        engine = _make_automation_engine()
        engine.dry_run = True
        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        assert engine._last_action_time is None

    def test_serializable_state_includes_action_fields(self):
        """get_serializable_state() must include last_action_time/reason."""
        engine = _make_automation_engine()
        engine._last_action_time = "2026-03-19T14:30:00"
        engine._last_action_reason = "Set HVAC to cool — test"

        state = engine.get_serializable_state()

        assert state["last_action_time"] == "2026-03-19T14:30:00"
        assert state["last_action_reason"] == "Set HVAC to cool — test"

    def test_restore_state_loads_action_fields(self):
        """restore_state() must populate action tracking fields."""
        engine = _make_automation_engine()
        engine.restore_state(
            {
                "last_action_time": "2026-03-19T14:00:00",
                "last_action_reason": "test",
            }
        )

        assert engine._last_action_time == "2026-03-19T14:00:00"
        assert engine._last_action_reason == "test"

    def test_serializable_state_omits_override_fields(self):
        """get_serializable_state() must NOT include manual override fields (clean-slate policy)."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "heat"
        engine._manual_override_time = "2026-03-19T14:00:00"

        state = engine.get_serializable_state()

        assert "manual_override_active" not in state
        assert "manual_override_mode" not in state
        assert "manual_override_time" not in state

    def test_restore_state_ignores_override_fields(self):
        """restore_state() must always clear override fields — clean slate on restart."""
        engine = _make_automation_engine()
        engine.restore_state(
            {
                "manual_override_active": True,
                "manual_override_mode": "cool",
                "manual_override_time": "2026-03-19T15:00:00",
            }
        )

        assert engine._manual_override_active is False
        assert engine._manual_override_mode is None
        assert engine._manual_override_time is None

    def test_restore_state_clears_override_grace_fsm_flags_legacy(self):
        """Issue #680: restore_state()'s clean-slate reset of the 3 FSM-modeled flags
        (_override_confirm_pending/_grace_active/_grace_protects_override) must still
        land on the same all-clear result now that it routes through
        _resolve_override_grace_fsm_state() instead of assigning them directly.
        This is a behavior-preservation test — the observable outcome (all 3 flags
        False after restore) must be identical to before the fix. Default engine
        config leaves _override_grace_fsm_authoritative False, so this exercises the
        legacy() closure branch.
        """
        engine = _make_automation_engine()
        # Pre-set the 3 flags to non-clean values, as if a real override/grace session
        # was somehow live at construction time — restore_state() must still land on
        # all-clear regardless of these starting values.
        engine._override_confirm_pending = True
        engine._grace_active = True
        engine._grace_protects_override = True

        engine.restore_state({})

        assert engine._override_confirm_pending is False
        assert engine._grace_active is False
        assert engine._grace_protects_override is False

    def test_restore_state_clears_override_grace_fsm_flags_authoritative(self):
        """Issue #680: with _override_grace_fsm_authoritative=True, restore_state()'s
        clean-slate reset must route through _apply_override_grace_fsm_state() (the FSM
        branch of _resolve_override_grace_fsm_state()) and produce the identical
        all-clear result as the legacy branch — proving the switch genuinely governs
        which computation determines these 3 flags, rather than the direct assignments
        existing independently of it.
        """
        engine = _make_automation_engine()
        engine._override_grace_fsm_authoritative = True
        engine._override_confirm_pending = True
        engine._grace_active = True
        engine._grace_protects_override = True

        with patch.object(
            engine, "_apply_override_grace_fsm_state", wraps=engine._apply_override_grace_fsm_state
        ) as spy:
            engine.restore_state({})

        spy.assert_called_once()
        assert engine._override_confirm_pending is False
        assert engine._grace_active is False
        assert engine._grace_protects_override is False
