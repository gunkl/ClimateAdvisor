"""Tests for revisit scheduling in the AutomationEngine.

Verifies that HVAC actions trigger a 5-minute revisit timer, that
previous timers are cancelled, and that cleanup cancels pending revisits.

See: GitHub Issue #37
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import REVISIT_DELAY_SECONDS

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

class TestRevisitScheduling:
    """Verify revisit timer lifecycle around HVAC actions."""

    def test_revisit_scheduled_after_hvac_action(self):
        """When _revisit_callback is set, _set_hvac_mode triggers async_call_later."""
        from homeassistant.helpers.event import async_call_later

        engine = _make_automation_engine()
        engine._revisit_callback = AsyncMock()
        async_call_later.reset_mock()

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        async_call_later.assert_called()
        # First positional arg is hass, second is delay
        call_args = async_call_later.call_args
        assert call_args[0][1] == REVISIT_DELAY_SECONDS

    def test_revisit_cancels_previous_timer(self):
        """Scheduling a new revisit must cancel the previous one."""
        engine = _make_automation_engine()
        engine._revisit_callback = AsyncMock()
        old_cancel = MagicMock()
        engine._revisit_cancel = old_cancel

        engine._schedule_revisit()

        old_cancel.assert_called_once()

    def test_no_revisit_without_callback(self):
        """Without _revisit_callback, no timer should be scheduled."""
        engine = _make_automation_engine()
        # _revisit_callback is None by default
        asyncio.run(engine._set_hvac_mode("heat", reason="test"))

        assert engine._revisit_cancel is None

    def test_cleanup_cancels_revisit(self):
        """cleanup() must cancel any pending revisit timer."""
        engine = _make_automation_engine()
        cancel_mock = MagicMock()
        engine._revisit_cancel = cancel_mock

        engine.cleanup()

        cancel_mock.assert_called_once()
        assert engine._revisit_cancel is None
