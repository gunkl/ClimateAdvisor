"""Tests for manual override protection in the AutomationEngine.

Verifies that user manual thermostat changes are respected: classification
is skipped when override is active, override is set during pause overrides,
and transition points (bedtime, morning) clear the override.

See: GitHub Issue #37
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

# Patch dt_util.now to return a real datetime (needed for isoformat() calls)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 19, 14, 30, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_automation_engine(config_overrides=None):
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock()
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
    obj.window_open_time = kwargs.get("window_open_time", None)
    obj.window_close_time = kwargs.get("window_close_time", None)
    obj.setback_modifier = setback_modifier
    return obj


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestManualOverrideProtection:
    """Verify that manual override blocks classification and is cleared at transitions."""

    def test_apply_classification_skips_when_override_active(self):
        """With override active, apply_classification must NOT call any service."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "heat"
        engine._manual_override_time = "2026-03-19T14:00:00"

        c = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.apply_classification(c))

        engine.hass.services.async_call.assert_not_called()

    def test_apply_classification_updates_classification_even_during_override(self):
        """Override skips HVAC changes but still stores the classification."""
        engine = _make_automation_engine()
        engine._manual_override_active = True

        c = _make_classification(day_type="hot", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))

        assert engine._current_classification is c

    def test_apply_classification_works_when_no_override(self):
        """Without override, apply_classification should call HVAC services."""
        engine = _make_automation_engine()

        c = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.apply_classification(c))

        engine.hass.services.async_call.assert_called()

    def test_handle_manual_override_during_pause_sets_override(self):
        """During a door/window pause, manual HVAC change sets override."""
        engine = _make_automation_engine()
        engine._paused_by_door = True

        state = MagicMock()
        state.state = "heat"
        engine.hass.states.get.return_value = state

        asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "heat"

    def test_clear_manual_override_resets_fields(self):
        """clear_manual_override() must reset all override fields."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        engine._manual_override_time = "2026-03-19T14:00:00"

        engine.clear_manual_override()

        assert engine._manual_override_active is False
        assert engine._manual_override_mode is None
        assert engine._manual_override_time is None

    def test_bedtime_clears_override(self):
        """handle_bedtime() should clear any active manual override."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        engine._manual_override_time = "2026-03-19T12:00:00"

        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        asyncio.run(engine.handle_bedtime())

        assert engine._manual_override_active is False
        assert engine._manual_override_mode is None

    def test_morning_wakeup_clears_override(self):
        """handle_morning_wakeup() should clear any active manual override."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "heat"
        engine._manual_override_time = "2026-03-19T06:00:00"

        c = _make_classification(day_type="hot", hvac_mode="cool")
        engine._current_classification = c

        asyncio.run(engine.handle_morning_wakeup())

        assert engine._manual_override_active is False
        assert engine._manual_override_mode is None

    def test_handle_manual_override_sets_override_and_starts_grace(self):
        """handle_manual_override() should set override and activate grace period."""
        engine = _make_automation_engine()

        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        engine.handle_manual_override()

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"
        assert engine._manual_override_time is not None
        assert engine._grace_active is True
