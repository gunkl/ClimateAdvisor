"""Tests for Issue #263/#306: restore_state() must NOT restore _paused_by_door / _pre_pause_mode.

Clean-slate rule: HA restart clears override, grace, AND door/window pause state.
The door/window state-change listener re-detects open sensors within 30–90 s of startup
(None → "on" entity transition), so carry-over would only cause indefinite pause if
cloud weather or thermostat services are slow to reconnect.

Pattern: AutomationEngine direct with mocked HA dependencies, mirroring
tests/test_nat_vent_activation.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Patch dt_util.now so isoformat() calls inside the engine always work.
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 13, 10, 0, 0)

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"


def _make_engine(
    comfort_heat: float = 68.0,
    comfort_cool: float = 76.0,
) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    # Climate entity reports "cool" mode so apply_classification can arm a band.
    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {
        "current_temperature": 74.0,
        "hvac_modes": ["off", "cool", "heat", "heat_cool"],
        "supported_features": 0,
    }

    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 82,
        "notify_service": "notify.notify",
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    return engine


def _make_cool_classification() -> DayClassification:
    """Minimal warm-day classification that causes apply_classification to arm a cool band."""
    obj = object.__new__(DayClassification)
    obj.day_type = "warm"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 2.0
    obj.today_high = 88.0
    obj.today_low = 65.0
    obj.tomorrow_high = 88.0
    obj.tomorrow_low = 65.0
    obj.hvac_mode = "cool"
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


# ---------------------------------------------------------------------------
# Test 1: _paused_by_door not restored
# ---------------------------------------------------------------------------


class TestPausedByDoorNotRestoredOnRestart:
    """restore_state() with paused_by_door=True must leave _paused_by_door False."""

    def test_paused_by_door_not_restored_on_restart(self):
        """Core assertion: persisted paused_by_door=True is discarded on restore."""
        engine = _make_engine()

        # Confirm default is already False
        assert engine._paused_by_door is False

        engine.restore_state({"paused_by_door": True, "pre_pause_mode": "cool"})

        # Clean-slate: pause must NOT be carried across restart
        assert engine._paused_by_door is False, (
            "restore_state() must not restore _paused_by_door — clean-slate rule (Issue #263/#306)"
        )


# ---------------------------------------------------------------------------
# Test 2: _pre_pause_mode not restored
# ---------------------------------------------------------------------------


class TestPrePauseModeNotRestoredOnRestart:
    """restore_state() must leave _pre_pause_mode as None regardless of persisted value."""

    def test_pre_pause_mode_not_restored_on_restart(self):
        """Persisted pre_pause_mode is discarded — no stale mode to resume from."""
        engine = _make_engine()

        assert engine._pre_pause_mode is None

        engine.restore_state({"paused_by_door": True, "pre_pause_mode": "cool"})

        assert engine._pre_pause_mode is None, (
            "restore_state() must not restore _pre_pause_mode — clean-slate rule (Issue #263/#306)"
        )


# ---------------------------------------------------------------------------
# Test 3: other state fields ARE still restored
# ---------------------------------------------------------------------------


class TestOtherStateFieldsStillRestored:
    """Pause fields are cleared but other engine state must still be restored normally."""

    def test_fan_active_restored(self):
        """_fan_active is a legitimate carry-across field and must be restored."""
        engine = _make_engine()
        engine.restore_state({"fan_active": True})
        assert engine._fan_active is True

    def test_pre_condition_achieved_restored(self):
        """_pre_condition_achieved is persisted so a restart mid-day does not re-arm the ceiling offset."""
        engine = _make_engine()
        engine.restore_state({"pre_condition_achieved": True, "pre_condition_achieved_date": "2026-06-13"})
        assert engine._pre_condition_achieved is True

    def test_economizer_active_restored(self):
        """_economizer_active is a carry-across field and must be restored."""
        engine = _make_engine()
        engine.restore_state({"economizer_active": True})
        assert engine._economizer_active is True

    def test_last_action_reason_restored(self):
        """_last_action_reason is restored so briefing shows the last known action."""
        engine = _make_engine()
        engine.restore_state({"last_action_reason": "daily classification"})
        assert engine._last_action_reason == "daily classification"

    def test_pause_cleared_while_fan_restored_simultaneously(self):
        """Both rules apply together: pause cleared, fan_active restored."""
        engine = _make_engine()
        engine.restore_state(
            {
                "paused_by_door": True,
                "pre_pause_mode": "heat",
                "fan_active": True,
                "last_action_reason": "test",
            }
        )
        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        assert engine._fan_active is True
        assert engine._last_action_reason == "test"


# ---------------------------------------------------------------------------
# Test 4: apply_classification reaches _apply_comfort_band after clean-slate restart
# ---------------------------------------------------------------------------


class TestApplyClassificationAfterCleanSlateRestart:
    """After restore_state (which clears pause), apply_classification must arm the thermostat."""

    def test_apply_classification_reaches_comfort_band_after_restart(self):
        """With _paused_by_door=False (clean-slate), apply_classification must call set_temperature.

        Confirms that the clean-slate restore does not leave the engine in a state that
        blocks automation. The occupant's thermostat must be set after an HA restart,
        not left indefinitely paused.
        """
        engine = _make_engine()

        # Simulate what the coordinator does on startup: restore state from persisted JSON.
        # The persisted state had pause active, but restore_state must clear it.
        engine.restore_state({"paused_by_door": True, "pre_pause_mode": "cool"})

        assert engine._paused_by_door is False  # clean slate confirmed

        classification = _make_cool_classification()

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 6, 13, 10, 0, 0)):
            asyncio.run(engine.apply_classification(classification))

        # _apply_comfort_band must have called set_temperature → hass.services.async_call
        assert engine.hass.services.async_call.called, (
            "apply_classification must arm the thermostat after a clean-slate restart "
            "(no indefinite pause — Issue #263/#306)"
        )
        call_args = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in call_args if c[0][0] == "climate"]
        assert len(climate_calls) >= 1, "Expected at least one 'climate' service call from apply_classification"
