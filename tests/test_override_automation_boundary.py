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
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


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
    coord._startup_coalesce_active = False  # Bug 1 (Issue #321)

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


# ---------------------------------------------------------------------------
# Helpers for expected-state suppression tests
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 6, 2, 10, 0, 0)


def _make_expected_state_stub(
    *,
    last_commanded_mode: str | None,
    last_commanded_seconds_ago: float | None,
    paused_by_door: bool = True,
    classification=None,
):
    """Build a coordinator stub with explicit _last_commanded_hvac_* fields.

    All three command-pending flags are False so the pending-guard is not active.
    _is_recent_hvac_command returns False so the race guard is not active.
    The _is_expected_confirmation logic is exercised purely through the
    _last_commanded_hvac_mode / _last_commanded_hvac_time values.
    """
    coord = _make_thermostat_coord_stub(
        hvac_command_pending=False,
        fan_command_pending=False,
        temp_command_pending=False,
        paused_by_door=paused_by_door,
        classification=classification,
    )

    ae = coord.automation_engine
    ae._last_commanded_hvac_mode = last_commanded_mode
    if last_commanded_seconds_ago is not None and last_commanded_mode is not None:
        ae._last_commanded_hvac_time = _FIXED_NOW - timedelta(seconds=last_commanded_seconds_ago)
    else:
        ae._last_commanded_hvac_time = None

    return coord


# ---------------------------------------------------------------------------
# TestExpectedStateSuppress
# ---------------------------------------------------------------------------


class TestExpectedStateSuppress:
    """Tests for expected-state override suppression (Issue #206 — Settings column / cloud lag).

    When the thermostat is confirming an automation command (same mode, within 2 minutes),
    the state-change must NOT be treated as a user override. This covers cloud-thermostat
    lag where _hvac_command_pending may already be cleared by the time the HA event fires.
    """

    def test_expected_state_suppresses_pause_path(self):
        """Thermostat confirms automation command within grace window → no override during pause.

        _last_commanded_hvac_mode="cool", commanded 5s ago, new_state="cool" → matches.
        Expected: _is_expected_confirmation=True → handle_manual_override_during_pause NOT called.
        """
        coord = _make_expected_state_stub(
            last_commanded_mode="cool",
            last_commanded_seconds_ago=5,
            paused_by_door=True,
        )

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch.object(mod.dt_util, "now", return_value=_FIXED_NOW):
            event = _make_event("off", "cool")
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_expected_state_window_expires(self):
        """After the 120-second grace window, the confirmation is no longer expected → override fires.

        _last_commanded_hvac_time = now - 130s → total_seconds()=130 >= 120 → _is_expected_confirmation=False.
        With all pending flags False and no recent command, the pause-path override must fire.
        """
        coord = _make_expected_state_stub(
            last_commanded_mode="cool",
            last_commanded_seconds_ago=130,
            paused_by_door=True,
        )

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch.object(mod.dt_util, "now", return_value=_FIXED_NOW):
            event = _make_event("off", "cool")
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_called_once()

    def test_user_override_different_mode_detected(self):
        """Commanded mode was "cool" but thermostat changed to "heat" → not an expected state.

        _is_expected_confirmation=False because new_state.state != _last_commanded_hvac_mode.
        With paused_by_door=True and no pending flags, the override must be detected.
        """
        coord = _make_expected_state_stub(
            last_commanded_mode="cool",
            last_commanded_seconds_ago=10,
            paused_by_door=True,
        )

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch.object(mod.dt_util, "now", return_value=_FIXED_NOW):
            event = _make_event("off", "heat")  # different from commanded "cool"
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_called_once()

    def test_expected_state_suppresses_normal_path(self):
        """Not paused; classification wants "off" but automation commanded "cool" 5s ago.

        Thermostat confirms "cool" — this is automation-expected, NOT a user override.
        The non-pause elif block checks `and not _is_expected_confirmation`, so it must not fire.
        """
        classification = _make_classification(hvac_mode="off")
        coord = _make_expected_state_stub(
            last_commanded_mode="cool",
            last_commanded_seconds_ago=5,
            paused_by_door=False,
            classification=classification,
        )

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch.object(mod.dt_util, "now", return_value=_FIXED_NOW):
            event = _make_event("off", "cool")  # cool != classification.hvac_mode="off"
            asyncio.run(coord._async_thermostat_changed(event))

        # Non-pause path: handle_manual_override must NOT be called
        coord.automation_engine.handle_manual_override.assert_not_called()

    def test_no_last_commanded_mode_falls_through(self):
        """When _last_commanded_hvac_mode is None, _is_expected_confirmation is False.

        With paused_by_door=True, all pending flags False, and no recent command,
        the genuine override path fires normally.
        """
        coord = _make_expected_state_stub(
            last_commanded_mode=None,
            last_commanded_seconds_ago=None,
            paused_by_door=True,
        )

        # No dt_util patch needed — _is_expected_confirmation short-circuits at None check
        event = _make_event("off", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_called_once()

    def test_hvac_action_change_does_not_trigger_pause_override(self):
        """Attribute-only event (hvac_action idle→cooling, mode stays "cool") must NOT override.

        Root cause of Round 3 regression: pause path lacked old_state.state != new_state.state
        guard. HA emits state_changed for hvac_action attribute changes even when HVAC mode
        is unchanged. After the 120s expected-state window expired, these events fired false
        overrides (~20 min after ceiling guard).

        old_state.state = "cool", new_state.state = "cool" → mode didn't change → skip.
        """
        coord = _make_expected_state_stub(
            last_commanded_mode=None,  # window expired / no active suppression
            last_commanded_seconds_ago=None,
            paused_by_door=True,
        )

        # Simulate hvac_action attribute change: mode stays "cool" throughout
        event = _make_event("cool", "cool")
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()


# ---------------------------------------------------------------------------
# TestWarmDayHvacActionCycling (Regression test for commit 5b3edbe)
# ---------------------------------------------------------------------------


class TestWarmDayHvacActionCycling:
    """Regression test for warm_day_state_confirmed split (commit 5b3edbe).

    Issue: Removing the heartbeat _set_hvac_mode("off") call on warm days when mode
    is already off stales _hvac_command_time. The audit claims this could cause false
    override detection when hvac_action cycles (idle→cooling) with mode still off.

    Counter-hypothesis: The old_state.state != new_state.state guard (commit 56ba2a7)
    blocks hvac_action attribute changes from reaching the 3-second guard, because
    mode stays "off" during hvac_action cycling, making old_state.state == new_state.state.

    This test validates the counter-hypothesis:
    - PASS: old_state != new_state guard works, no false override on attribute-only changes
    - FAIL: regression is real, heartbeat timer refresh fix needed
    """

    def test_warm_day_hvac_action_cycling_no_false_override(self):
        """Warm day, thermostat mode stays 'off', but hvac_action cycles (idle→cooling).

        Simulates the scenario from commit 5b3edbe regression audit:
        - _hvac_command_time is stale (10 minutes ago, no recent service call)
        - _last_commanded_hvac_mode is stale or None
        - Thermostat emits state_changed with old_state.state == new_state.state == "off"
          but different hvac_action attribute (attribute-only event)

        Expected: NO override detected.
        Mechanism: old_state.state == new_state.state prevents override paths from firing.
        """
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=False,
            temp_command_pending=False,
            paused_by_door=True,  # pause path active
        )

        # Stale _hvac_command_time (10 minutes ago)
        ae = coord.automation_engine
        ae._hvac_command_time = datetime(2026, 6, 2, 9, 50, 0)  # 10 min before _FIXED_NOW (10:00)
        ae._last_commanded_hvac_mode = None  # stale
        ae._last_commanded_hvac_time = None

        # State change: mode stays "off", hvac_action changes (attribute-only event)
        # old_state.state == new_state.state == "off" — should skip override detection
        event = _make_event("off", "off")  # both old and new state are "off"
        asyncio.run(coord._async_thermostat_changed(event))

        # Should NOT detect override — old_state.state == new_state.state guards both paths
        coord.automation_engine.handle_manual_override_during_pause.assert_not_called()

    def test_warm_day_state_off_to_off_attribute_change_normal_path(self):
        """Complement: normal path (not paused) also skips attribute-only changes."""
        classification = _make_classification(hvac_mode="off")
        coord = _make_thermostat_coord_stub(
            hvac_command_pending=False,
            fan_command_pending=False,
            temp_command_pending=False,
            paused_by_door=False,  # normal path
            classification=classification,
        )

        ae = coord.automation_engine
        ae._hvac_command_time = datetime(2026, 6, 2, 9, 50, 0)  # stale
        ae._last_commanded_hvac_mode = None
        ae._last_commanded_hvac_time = None

        # Attribute-only event: mode stays "off"
        event = _make_event("off", "off")
        asyncio.run(coord._async_thermostat_changed(event))

        # Normal-path override check also requires old_state != new_state, so it must not fire
        coord.automation_engine.handle_manual_override.assert_not_called()
