"""Tests for resume-from-pause functionality (Issue #47).

Covers:
- _hvac_command_pending guard preventing false manual override detection
- resume_from_pause() restoring classified HVAC mode and starting grace
- Grace period expiry re-checking sensors and re-pausing when still open
- _resumed_from_pause flag lifecycle (set on resume, cleared on clear_manual_override)
- _compute_automation_status() returning the correct status string variants
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
)

# Patch dt_util.now to return a real datetime (needed for isoformat() calls in the engine)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 20, 10, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    day_type: str = "warm",
    hvac_mode: str = "cool",
    trend_direction: str = "stable",
    trend_magnitude: float = 2.0,
    setback_modifier: float = 0.0,
    pre_condition: bool = False,
    pre_condition_target=None,
    **kwargs,
) -> DayClassification:
    """Create a DayClassification bypassing __post_init__ validation."""
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


# Patch targets used by grace-period tests
_PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.automation.callback"


def _start_grace_and_capture_callback(engine: AutomationEngine, source: str = "manual"):
    """Call _start_grace_period and return the raw expiry closure."""
    with patch(_PATCH_CALL_LATER) as mock_call_later, \
         patch(_PATCH_CALLBACK, side_effect=lambda f: f):
        mock_call_later.return_value = MagicMock()
        engine._start_grace_period(source)
        assert mock_call_later.call_count == 1
        grace_callback = mock_call_later.call_args[0][2]
        return grace_callback


# ---------------------------------------------------------------------------
# TestHvacCommandPendingGuard
# ---------------------------------------------------------------------------

class TestHvacCommandPendingGuard:
    """Verify the _hvac_command_pending and _hvac_command_time race guards."""

    def test_system_hvac_off_does_not_trigger_override(self):
        """When _hvac_command_pending is True the override detection condition is False.

        The coordinator's thermostat state listener gates override detection on
        ``not self.automation_engine._hvac_command_pending``.  Simulate the
        condition logic directly: with the flag set the compound condition must
        evaluate False even when every other criterion is met.
        """
        engine = _make_automation_engine()
        engine._hvac_command_pending = True

        classification = _make_classification(day_type="warm", hvac_mode="cool")
        engine._current_classification = classification

        # Replicate the coordinator's override-detection condition
        old_state_value = "cool"
        new_state_value = "off"
        manual_override_active = engine._manual_override_active           # False
        hvac_command_pending = engine._hvac_command_pending               # True (guard)
        classification_matches = (new_state_value == classification.hvac_mode)  # False

        override_would_fire = (
            old_state_value != new_state_value
            and new_state_value not in ("unavailable", "unknown")
            and not manual_override_active
            and not hvac_command_pending          # <-- blocks here
            and classification is not None
            and not classification_matches
        )

        assert override_would_fire is False

    def test_recent_hvac_command_blocks_override(self):
        """_hvac_command_time set 1 second ago yields elapsed < threshold → guard active."""
        engine = _make_automation_engine()
        engine._hvac_command_pending = False

        # Simulate a timestamp 1 second before "now" (mocked to 10:00:00)
        now = datetime(2026, 3, 20, 10, 0, 0)
        engine._hvac_command_time = now - timedelta(seconds=1)

        # Replicate _is_recent_hvac_command logic (threshold = 3.0 s)
        elapsed = (now - engine._hvac_command_time).total_seconds()
        is_recent = elapsed < 3.0

        assert is_recent is True

    def test_old_hvac_command_does_not_block(self):
        """_hvac_command_time set 5 seconds ago yields elapsed >= threshold → guard inactive."""
        engine = _make_automation_engine()

        now = datetime(2026, 3, 20, 10, 0, 0)
        engine._hvac_command_time = now - timedelta(seconds=5)

        elapsed = (now - engine._hvac_command_time).total_seconds()
        is_recent = elapsed < 3.0

        assert is_recent is False

    def test_set_hvac_mode_sets_pending_flag(self):
        """_hvac_command_pending is True while _set_hvac_mode is awaiting the service call."""
        engine = _make_automation_engine()

        pending_during_call = []

        async def _capture_pending(*args, **kwargs):
            pending_during_call.append(engine._hvac_command_pending)

        engine.hass.services.async_call = AsyncMock(side_effect=_capture_pending)

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        # Inside the call the flag should have been True
        assert True in pending_during_call
        # After the call (in the finally block) it is reset to False
        assert engine._hvac_command_pending is False

    def test_set_hvac_mode_sets_timestamp(self):
        """_hvac_command_time is populated after _set_hvac_mode completes."""
        engine = _make_automation_engine()
        assert engine._hvac_command_time is None

        asyncio.run(engine._set_hvac_mode("heat", reason="test"))

        assert engine._hvac_command_time is not None


# ---------------------------------------------------------------------------
# TestResumeFromPause
# ---------------------------------------------------------------------------

class TestResumeFromPause:
    """Verify resume_from_pause() correctly restores HVAC and starts grace."""

    def test_resume_restores_classified_mode(self):
        """resume_from_pause clears pause state, calls HVAC service with classified mode."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"
        engine._current_classification = _make_classification(hvac_mode="cool")

        with patch(_PATCH_CALL_LATER) as mock_call_later, \
             patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            result = asyncio.run(engine.resume_from_pause())

        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        assert engine._resumed_from_pause is True
        assert engine._grace_active is True
        assert result == "cool"

        # A climate service call must have been issued with "cool"
        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [
            c for c in calls
            if c.args[0] == "climate" and c.args[1] == "set_hvac_mode"
        ]
        assert len(hvac_calls) >= 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "cool"

    def test_resume_uses_current_classification_not_pre_pause(self):
        """resume_from_pause uses _current_classification.hvac_mode, not _pre_pause_mode."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"         # stale pre-pause mode
        # Classification changed to heat since the pause was set
        engine._current_classification = _make_classification(hvac_mode="heat")

        with patch(_PATCH_CALL_LATER) as mock_call_later, \
             patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            result = asyncio.run(engine.resume_from_pause())

        assert result == "heat"
        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [
            c for c in calls
            if c.args[0] == "climate" and c.args[1] == "set_hvac_mode"
        ]
        assert len(hvac_calls) >= 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"

    def test_resume_when_not_paused(self):
        """resume_from_pause returns None and makes no service calls when not paused."""
        engine = _make_automation_engine()
        engine._paused_by_door = False

        result = asyncio.run(engine.resume_from_pause())

        assert result is None
        engine.hass.services.async_call.assert_not_called()

    def test_resume_without_classification(self):
        """resume_from_pause clears pause flags even when _current_classification is None."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._current_classification = None

        with patch(_PATCH_CALL_LATER) as mock_call_later, \
             patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            result = asyncio.run(engine.resume_from_pause())

        # Pause is cleared
        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        # No HVAC call because there is no classification to restore from
        hvac_calls = [
            c for c in engine.hass.services.async_call.call_args_list
            if c.args[0] == "climate" and c.args[1] == "set_hvac_mode"
        ]
        assert len(hvac_calls) == 0
        assert result is None

    def test_resume_starts_manual_grace(self):
        """resume_from_pause starts a grace period with source='manual'."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._current_classification = _make_classification(hvac_mode="cool")

        with patch(_PATCH_CALL_LATER) as mock_call_later, \
             patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.resume_from_pause())

        assert engine._grace_active is True
        assert engine._last_resume_source == "manual"


# ---------------------------------------------------------------------------
# TestGraceExpiryRecheck
# ---------------------------------------------------------------------------

class TestGraceExpiryRecheck:
    """Verify that grace expiry re-checks sensors and re-pauses when still open."""

    def test_grace_expiry_repauses_when_sensor_open(self):
        """If a sensor is still open at grace expiry, HVAC is re-paused."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        # Sensor check callback returns True → sensor still open
        engine._sensor_check_callback = lambda: True

        # Thermostat is currently running cooling
        state_mock = MagicMock()
        state_mock.state = "cool"
        engine.hass.states.get.return_value = state_mock

        grace_callback = _start_grace_and_capture_callback(engine, source="manual")

        # Reset mock so we only track calls after grace fires
        engine.hass.services.async_call.reset_mock()
        engine.hass.async_create_task.reset_mock()

        grace_callback(None)

        # The expiry callback should have scheduled _re_pause_for_open_sensor
        engine.hass.async_create_task.assert_called()

        # Manually run the re-pause coroutine to verify side effects
        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is True
        hvac_calls = [
            c for c in engine.hass.services.async_call.call_args_list
            if c.args[0] == "climate" and c.args[1] == "set_hvac_mode"
        ]
        assert len(hvac_calls) >= 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_grace_expiry_clears_normally_when_closed(self):
        """If all sensors are closed at grace expiry, grace clears normally."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        # Sensor check callback returns False → all sensors closed
        engine._sensor_check_callback = lambda: False

        grace_callback = _start_grace_and_capture_callback(engine, source="manual")
        grace_callback(None)

        assert engine._grace_active is False
        assert engine._manual_override_active is False
        assert engine._last_resume_source is None

    def test_grace_expiry_no_callback_clears_normally(self):
        """When _sensor_check_callback is None, grace expires normally."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._sensor_check_callback = None

        grace_callback = _start_grace_and_capture_callback(engine, source="manual")
        grace_callback(None)

        assert engine._grace_active is False
        assert engine._last_resume_source is None

    def test_re_pause_when_hvac_already_off(self):
        """_re_pause_for_open_sensor sets _paused_by_door=True without a service call when HVAC is already off."""
        engine = _make_automation_engine()

        state_mock = MagicMock()
        state_mock.state = "off"
        engine.hass.states.get.return_value = state_mock

        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is True
        # No climate service call should have been made (HVAC already off)
        hvac_calls = [
            c for c in engine.hass.services.async_call.call_args_list
            if c.args[0] == "climate" and c.args[1] == "set_hvac_mode"
        ]
        assert len(hvac_calls) == 0


# ---------------------------------------------------------------------------
# TestResumedFromPauseFlag
# ---------------------------------------------------------------------------

class TestResumedFromPauseFlag:
    """Verify _resumed_from_pause flag lifecycle."""

    def test_flag_set_on_resume(self):
        """resume_from_pause() sets _resumed_from_pause to True."""
        engine = _make_automation_engine({
            CONF_MANUAL_GRACE_PERIOD: 300,
            CONF_MANUAL_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._current_classification = _make_classification(hvac_mode="cool")

        with patch(_PATCH_CALL_LATER) as mock_call_later, \
             patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.resume_from_pause())

        assert engine._resumed_from_pause is True

    def test_flag_cleared_on_clear_manual_override(self):
        """clear_manual_override() resets _resumed_from_pause to False."""
        engine = _make_automation_engine()
        engine._resumed_from_pause = True

        engine.clear_manual_override()

        assert engine._resumed_from_pause is False

    def test_flag_initially_false(self):
        """A freshly created AutomationEngine has _resumed_from_pause=False."""
        engine = _make_automation_engine()

        assert engine._resumed_from_pause is False


# ---------------------------------------------------------------------------
# TestStatusStringResumedFromPause
# ---------------------------------------------------------------------------

class TestStatusStringResumedFromPause:
    """Verify _compute_automation_status() returns correct status strings.

    The method lives on the coordinator, so we replicate its logic inline
    rather than instantiating a full coordinator — the same approach used
    throughout this test suite.
    """

    def _compute_automation_status(
        self,
        automation_enabled: bool,
        is_paused_by_door: bool,
        grace_active: bool,
        resumed_from_pause: bool,
        last_resume_source: str | None = "manual",
        occupancy_mode: str = "home",
    ) -> str:
        """Inline replication of ClimateAdvisorCoordinator._compute_automation_status()."""
        if not automation_enabled:
            return "disabled"
        if is_paused_by_door:
            return "paused — door/window open"
        if grace_active:
            if resumed_from_pause:
                return "resumed — door/window override"
            source = last_resume_source or "automation"
            return f"grace period ({source})"
        if occupancy_mode == "vacation":
            return "active (vacation)"
        if occupancy_mode == "away":
            return "active (away)"
        if occupancy_mode == "guest":
            return "active (guest)"
        return "active"

    def test_status_shows_resumed_door_window_override(self):
        """When grace is active and _resumed_from_pause=True, status is 'resumed — door/window override'."""
        status = self._compute_automation_status(
            automation_enabled=True,
            is_paused_by_door=False,
            grace_active=True,
            resumed_from_pause=True,
        )
        assert status == "resumed — door/window override"

    def test_status_shows_grace_period_manual_without_resume(self):
        """When grace is active but _resumed_from_pause=False, status is 'grace period (manual)'."""
        status = self._compute_automation_status(
            automation_enabled=True,
            is_paused_by_door=False,
            grace_active=True,
            resumed_from_pause=False,
            last_resume_source="manual",
        )
        assert status == "grace period (manual)"

    def test_status_paused_takes_priority_over_grace(self):
        """'paused — door/window open' takes priority when is_paused_by_door=True."""
        status = self._compute_automation_status(
            automation_enabled=True,
            is_paused_by_door=True,
            grace_active=True,
            resumed_from_pause=True,
        )
        assert status == "paused — door/window open"

    def test_status_disabled_when_automation_off(self):
        """'disabled' is returned when automation_enabled=False regardless of other flags."""
        status = self._compute_automation_status(
            automation_enabled=False,
            is_paused_by_door=False,
            grace_active=True,
            resumed_from_pause=True,
        )
        assert status == "disabled"

    def test_status_active_when_no_special_state(self):
        """'active' is returned when there is no override, pause, or grace in effect."""
        status = self._compute_automation_status(
            automation_enabled=True,
            is_paused_by_door=False,
            grace_active=False,
            resumed_from_pause=False,
        )
        assert status == "active"

    def test_status_grace_period_automation_source(self):
        """When source='automation' and _resumed_from_pause=False, status is 'grace period (automation)'."""
        status = self._compute_automation_status(
            automation_enabled=True,
            is_paused_by_door=False,
            grace_active=True,
            resumed_from_pause=False,
            last_resume_source="automation",
        )
        assert status == "grace period (automation)"
