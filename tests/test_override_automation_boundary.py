"""Tests for Issue #206 — fan command pending flag not suppressing pause-path override.

Bug: _async_thermostat_changed() pause-path guard checks only _hvac_command_pending.
When _deactivate_fan() runs as the first step of nat vent exit, only
_fan_command_pending is True, so override detection fires falsely.

Fix (applied by Craftsman-A): guard checks
    not (hvac_command_pending OR fan_command_pending OR temp_command_pending)

Test classes:
- TestFanCommandPendingSuppress  — FAILS before fix, passes after
- TestNatVentExitNoFalseOverride — FAILS before fix, passes after
- TestCeilingGuardNoFalseOverride — regression: passes both before and after fix
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from datetime import datetime  # noqa: E402

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 2, 10, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class.

    test_occupancy.py deletes custom_components.climate_advisor.coordinator from
    sys.modules and re-imports it. Always import fresh so method __globals__
    point to the patched module, not a stale one.
    """
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__ validation."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 82,
        "today_low": 60,
        "tomorrow_high": 83,
        "tomorrow_low": 61,
        "hvac_mode": "off",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": True,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    s = MagicMock()
    s.state = state_value
    s.attributes = attributes or {}
    return s


def _make_event(old_state_value: str, new_state_value: str) -> MagicMock:
    """Create a mock HA thermostat state-change event."""
    event = MagicMock()
    event.data = {
        "old_state": _make_state(old_state_value),
        "new_state": _make_state(new_state_value),
    }
    return event


def _make_thermostat_coord_stub(
    *,
    hvac_command_pending: bool,
    fan_command_pending: bool,
    temp_command_pending: bool = False,
    paused_by_door: bool,
    classification=None,
):
    """Build a minimal coordinator stub for testing _async_thermostat_changed.

    Uses object.__new__ to skip __init__, then binds the real method under test
    via types.MethodType (same pattern as test_daily_record_accuracy.py).
    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    # Automation engine — MagicMock (NOT AsyncMock) per project convention.
    # Boolean flags must be set explicitly; unset MagicMock attrs are truthy.
    ae = MagicMock()
    ae.is_paused_by_door = paused_by_door
    ae._hvac_command_pending = hvac_command_pending
    ae._fan_command_pending = fan_command_pending
    ae._temp_command_pending = temp_command_pending
    ae._manual_override_active = False
    ae._fan_override_active = False
    ae._natural_vent_active = False
    ae._override_confirm_pending = False
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = classification or _make_classification()
    coord._async_save_state = AsyncMock()
    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._cancel_all_debounce_timers = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = AsyncMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)

    # Bind the real method under test
    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

    return coord


# ---------------------------------------------------------------------------
# TestFanCommandPendingSuppress
# ---------------------------------------------------------------------------


class TestFanCommandPendingSuppress:
    """Direct unit test: _fan_command_pending=True should suppress pause-path override.

    FAILS before the fix (guard only checks _hvac_command_pending).
    PASSES after the fix (guard checks all three pending flags).
    """

    def test_fan_command_pending_suppresses_pause_override(self):
        """When _fan_command_pending=True and _hvac_command_pending=False, no override fires.

        This is the core regression: deactivate_fan() sets _fan_command_pending=True
        but NOT _hvac_command_pending. The old guard let override detection through.
        """
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,  # not set by fan deactivation
            fan_command_pending=True,  # set by deactivate_fan()
            paused_by_door=True,
        )

        # State change: fan_only → cool (thermostat catching up after fan deactivation)
        event = _make_event("fan_only", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        # Override must NOT have been called
        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_fan_command_pending_only_suppresses_when_paused(self):
        """Complement: when not paused and _fan_command_pending=True, the pause branch
        is not entered at all — no override fires via the pause path regardless.
        """
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=True,
            paused_by_door=False,  # not paused
        )

        # Outside a pause, a classification-matching change should not fire override
        coord._current_classification = _make_classification(hvac_mode="cool")
        event = _make_event("off", "cool")  # matches classification → no override
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()


# ---------------------------------------------------------------------------
# TestNatVentExitNoFalseOverride
# ---------------------------------------------------------------------------


class TestNatVentExitNoFalseOverride:
    """Integration-style test: nat vent exit sequence must not emit false override.

    When _deactivate_fan() runs first during nat vent exit:
    - _fan_command_pending = True   (set by deactivate_fan)
    - _hvac_command_pending = False (not yet set — HVAC change comes later)

    The thermostat reports the mode change while _fan_command_pending=True.
    This must NOT trigger handle_manual_override_during_pause.

    FAILS before the fix, PASSES after.
    """

    def test_nat_vent_exit_fan_deactivation_no_override(self):
        """Thermostat state change while _fan_command_pending=True and paused must be suppressed."""
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,  # HVAC step hasn't fired yet
            fan_command_pending=True,  # deactivate_fan() set this
            paused_by_door=True,
        )
        coord.automation_engine._natural_vent_active = True

        # Thermostat changes: fan_only → cool (HVAC restoring after fan deactivation)
        event = _make_event("fan_only", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_nat_vent_exit_with_warm_day_classification(self):
        """Warm day classification (hvac_mode=off) + nat vent exit should not fire override."""
        classification = _make_classification(day_type="warm", hvac_mode="off")
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=True,
            paused_by_door=True,
            classification=classification,
        )
        coord.automation_engine._natural_vent_active = True

        # State change: fan_only → cool (user's thermostat coming back to cool after fan off)
        event = _make_event("fan_only", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_genuine_manual_override_during_pause_still_fires(self):
        """When both command flags are False, a real human override must still be detected.

        This is the complement: ensure suppression does not hide legitimate overrides.
        PASSES both before and after the fix.
        """
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=False,  # no automation command in flight
            paused_by_door=True,
        )

        # State change to "cool" while paused — no automation command pending → override fires
        event = _make_event("off", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_called_once()


# ---------------------------------------------------------------------------
# TestCeilingGuardNoFalseOverride
# ---------------------------------------------------------------------------


class TestCeilingGuardNoFalseOverride:
    """Regression guard: _hvac_command_pending=True must still suppress override.

    This test verifies the original ceiling-guard behavior is not broken by the fix.
    PASSES both before AND after the fix.
    """

    def test_hvac_command_pending_suppresses_pause_override(self):
        """When _hvac_command_pending=True, override is suppressed in pause path."""
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=True,  # ceiling guard correctly set this
            fan_command_pending=False,
            paused_by_door=True,
        )

        # State change: off → cool (automation's own command propagating)
        event = _make_event("off", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_hvac_and_fan_both_pending_suppresses_override(self):
        """When both _hvac_command_pending and _fan_command_pending are True, no override."""
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=True,
            fan_command_pending=True,
            paused_by_door=True,
        )

        event = _make_event("fan_only", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_temp_command_pending_suppresses_override(self):
        """When _temp_command_pending=True, override must also be suppressed.

        Included here as a second regression guard since it exercises
        the same combined-flag logic as the fan fix.
        PASSES both before and after the fix only if the fix also covers
        _temp_command_pending — otherwise this reveals an additional gap.
        """
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=False,
            temp_command_pending=True,  # setpoint command in flight
            paused_by_door=True,
        )

        event = _make_event("off", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()
