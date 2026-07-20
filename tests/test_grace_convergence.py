"""Tests for grace-expiry → scheduled-state convergence (Issue #230).

When a grace period expires normally (sensors closed, not in a planned window),
the engine must apply the correct scheduled state rather than leaving HVAC at
the position set by the earlier manual override.

Covers:
- Grace expiry in bedtime window → handle_bedtime() called
- Grace expiry outside bedtime window → apply_classification() called
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    OCCUPANCY_AWAY,
    OCCUPANCY_VACATION,
)

# Provide a base dt_util.now — individual tests override this via patch
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 20, 23, 0, 0)

# Patch targets
_PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.automation.callback"
_PATCH_DT_NOW = "custom_components.climate_advisor.automation.dt_util.now"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thermostat_state(mode: str = "cool") -> MagicMock:
    """Return a mock thermostat state with heat+cool capabilities.

    Issue #505: apply_classification()/handle_bedtime()/handle_pre_cool() now call
    _apply_comfort_band() (via handle_occupancy_vacation()/handle_occupancy_away())
    on the DEFER_OCCUPANCY path — that reads hvac_modes/supported_features to decide
    whether it can arm the band. Without these the band no-ops and no service call
    is emitted.
    """
    s = MagicMock()
    s.state = mode
    s.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,
    }
    return s


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        """Close coroutine to prevent 'never awaited' warnings."""
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    hass.states.get.return_value = _make_thermostat_state("cool")

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_MANUAL_GRACE_PERIOD: 300,
        CONF_MANUAL_GRACE_NOTIFY: False,
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
    day_type: str = "mild",
    hvac_mode: str = "cool",
    **kwargs,
) -> DayClassification:
    """Create a DayClassification bypassing __post_init__ validation."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = "stable"
    obj.trend_magnitude = 2.0
    obj.today_high = kwargs.get("today_high", 78.0)
    obj.today_low = kwargs.get("today_low", 58.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 79.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 59.0)
    obj.hvac_mode = hvac_mode
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = kwargs.get("setback_modifier", 0.0)
    return obj


def _fire_grace_expiry(engine: AutomationEngine) -> None:
    """Start a grace period and synchronously fire the expiry callback."""
    with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda f: f):
        mock_call_later.return_value = MagicMock()
        engine._start_grace_period("manual")
        assert mock_call_later.call_count == 1
        grace_callback = mock_call_later.call_args[0][2]

    # Sensors are closed (no re-pause) and not within a planned window
    engine._sensor_check_callback = None
    engine._is_within_planned_window_period = MagicMock(return_value=False)

    # Fire the expiry callback directly (it's a sync @callback that schedules async work)
    grace_callback(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraceConvergence:
    """Grace period expiry must apply the correct scheduled state."""

    def test_grace_expiry_applies_bedtime_setback_in_bedtime_window(self):
        """Grace expires at 23:00 — inside bedtime window (22:30–07:00) → handle_bedtime() called."""
        engine = _make_automation_engine(
            {
                "sleep_time": "22:30",
                "wake_time": "07:00",
            }
        )
        engine._current_classification = _make_classification(hvac_mode="cool")

        handle_bedtime_called = []

        async def _fake_handle_bedtime():
            handle_bedtime_called.append(True)

        engine.handle_bedtime = _fake_handle_bedtime

        apply_classification_called = []

        async def _fake_apply_classification(c):
            apply_classification_called.append(c)

        engine.apply_classification = _fake_apply_classification

        mock_now = datetime(2026, 3, 20, 23, 0, 0)  # 23:00 — inside bedtime window

        with patch(_PATCH_DT_NOW, return_value=mock_now):
            asyncio.run(engine._apply_current_scheduled_state())

        assert handle_bedtime_called, "handle_bedtime() should have been called (in bedtime window)"
        assert not apply_classification_called, "apply_classification() should NOT be called in bedtime window"

    def test_grace_expiry_applies_classification_outside_bedtime_window(self):
        """Grace expires at 14:00 — outside bedtime window → apply_classification() called."""
        engine = _make_automation_engine(
            {
                "sleep_time": "22:30",
                "wake_time": "07:00",
            }
        )
        classification = _make_classification(hvac_mode="cool")
        engine._current_classification = classification

        handle_bedtime_called = []

        async def _fake_handle_bedtime():
            handle_bedtime_called.append(True)

        engine.handle_bedtime = _fake_handle_bedtime

        apply_classification_called = []

        async def _fake_apply_classification(c):
            apply_classification_called.append(c)

        engine.apply_classification = _fake_apply_classification

        mock_now = datetime(2026, 3, 20, 14, 0, 0)  # 14:00 — daytime, not in bedtime window

        with patch(_PATCH_DT_NOW, return_value=mock_now):
            asyncio.run(engine._apply_current_scheduled_state())

        assert apply_classification_called, "apply_classification() should have been called outside bedtime window"
        assert not handle_bedtime_called, "handle_bedtime() should NOT be called outside bedtime window"
        assert apply_classification_called[0] is classification

    def test_grace_expiry_no_classification_no_crash(self):
        """_apply_current_scheduled_state does nothing gracefully when no classification set."""
        engine = _make_automation_engine(
            {
                "sleep_time": "22:30",
                "wake_time": "07:00",
            }
        )
        engine._current_classification = None

        handle_bedtime_called = []

        async def _fake_handle_bedtime():
            handle_bedtime_called.append(True)

        engine.handle_bedtime = _fake_handle_bedtime

        mock_now = datetime(2026, 3, 20, 14, 0, 0)  # outside bedtime window

        with patch(_PATCH_DT_NOW, return_value=mock_now):
            # Should not raise
            asyncio.run(engine._apply_current_scheduled_state())

        assert not handle_bedtime_called

    def test_grace_expiry_no_sleep_wake_config_applies_classification(self):
        """Without sleep_time/wake_time config, falls through to classification."""
        engine = _make_automation_engine()  # no sleep_time or wake_time in config
        classification = _make_classification(hvac_mode="heat")
        engine._current_classification = classification

        apply_classification_called = []

        async def _fake_apply_classification(c):
            apply_classification_called.append(c)

        engine.apply_classification = _fake_apply_classification

        mock_now = datetime(2026, 3, 20, 23, 0, 0)

        with patch(_PATCH_DT_NOW, return_value=mock_now):
            asyncio.run(engine._apply_current_scheduled_state())

        assert apply_classification_called, "apply_classification() should be called when no sleep/wake config"
        assert apply_classification_called[0] is classification


class TestOccupancyReapplyOnDeferOccupancy:
    """Issue #505: vacation mode never re-armed its deep setback after the initial
    mode-entry — apply_classification()/handle_bedtime()/handle_pre_cool() must all
    actively reapply the away/vacation setback on DEFER_OCCUPANCY, not just log and
    return on the (sometimes false) assumption that it's "already active"."""

    def test_apply_classification_vacation_reapplies_setback(self):
        """A vacation-mode classification cycle must re-arm the 83°F deep setback
        (setback_cool=80 + VACATION_SETBACK_EXTRA=3), matching the live-log evidence
        that this never happened for issue #505's real 5-day vacation."""
        engine = _make_automation_engine()
        engine._occupancy_mode = OCCUPANCY_VACATION
        classification = _make_classification(hvac_mode="cool")

        asyncio.run(engine.apply_classification(classification))

        engine.hass.services.async_call.assert_called()
        args, _ = engine.hass.services.async_call.call_args
        assert args[0] == "climate"
        assert args[1] == "set_temperature"
        assert args[2]["temperature"] == 83.0
        assert args[2]["hvac_mode"] == "cool"

    def test_apply_classification_away_still_reapplies_setback(self):
        """Regression guard: away mode's existing correct reapply behavior (via
        handle_occupancy_away()) must be unchanged by the vacation fix."""
        engine = _make_automation_engine()
        engine._occupancy_mode = OCCUPANCY_AWAY
        classification = _make_classification(hvac_mode="cool")

        asyncio.run(engine.apply_classification(classification))

        engine.hass.services.async_call.assert_called()
        args, _ = engine.hass.services.async_call.call_args
        assert args[2]["temperature"] == 80.0

    def test_handle_bedtime_vacation_reapplies_setback_in_sleep_window(self):
        """Grace expiry landing inside the sleep window routes to handle_bedtime()
        instead of apply_classification() — that path must also reapply vacation's
        setback rather than silently assuming it's already active."""
        engine = _make_automation_engine({"sleep_time": "22:30", "wake_time": "07:00"})
        engine._occupancy_mode = OCCUPANCY_VACATION
        engine._current_classification = _make_classification(hvac_mode="cool")

        asyncio.run(engine.handle_bedtime())

        engine.hass.services.async_call.assert_called()
        args, _ = engine.hass.services.async_call.call_args
        assert args[2]["temperature"] == 83.0

    def test_handle_bedtime_away_reapplies_setback_in_sleep_window(self):
        """Same sibling gap, away mode: grace expiry inside the sleep window must
        also reapply away's setback, not just vacation's."""
        engine = _make_automation_engine({"sleep_time": "22:30", "wake_time": "07:00"})
        engine._occupancy_mode = OCCUPANCY_AWAY
        engine._current_classification = _make_classification(hvac_mode="cool")

        asyncio.run(engine.handle_bedtime())

        engine.hass.services.async_call.assert_called()
        args, _ = engine.hass.services.async_call.call_args
        assert args[2]["temperature"] == 80.0

    def test_handle_pre_cool_vacation_reapplies_setback(self):
        """handle_pre_cool()'s DEFER_OCCUPANCY branch must also reapply the setback."""
        engine = _make_automation_engine()
        engine._occupancy_mode = OCCUPANCY_VACATION
        classification = _make_classification(hvac_mode="cool", setback_modifier=-2.0)
        engine._current_classification = classification

        asyncio.run(engine.handle_pre_cool(indoor_temp=78.0, nat_vent_just_closed=False))

        engine.hass.services.async_call.assert_called()
        args, _ = engine.hass.services.async_call.call_args
        assert args[2]["temperature"] == 83.0


class TestGraceNormalExpirySchedulesConvergence:
    def test_grace_normal_expiry_schedules_convergence_task(self):
        """Normal grace expiry (sensors closed) schedules _apply_current_scheduled_state via async_create_task."""
        engine = _make_automation_engine(
            {
                "sleep_time": "22:30",
                "wake_time": "07:00",
            }
        )
        engine._current_classification = _make_classification(hvac_mode="cool")

        scheduled_coros: list = []

        def _capture_task(coro):
            scheduled_coros.append(coro)
            # We must close the coro to avoid 'never awaited' warnings
            coro.close()

        engine.hass.async_create_task = MagicMock(side_effect=_capture_task)

        mock_now = datetime(2026, 3, 20, 23, 0, 0)

        with patch(_PATCH_DT_NOW, return_value=mock_now):
            _fire_grace_expiry(engine)

        # At least one coroutine should have been submitted — the convergence task
        assert len(scheduled_coros) >= 1, (
            "async_create_task should have been called to schedule _apply_current_scheduled_state"
        )
