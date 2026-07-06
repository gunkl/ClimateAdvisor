"""Regression tests for the _fan_command_time race guard (Issue #239).

The guard prevents CA's own fan activation/deactivation from being falsely
detected as a manual override when the cloud thermostat echo arrives after
_fan_command_pending has already cleared.

Three scenarios:
1. Recent fan command (within 30 s) → thermostat echo (fan_mode "auto" → "on")
   → handle_fan_manual_override NOT called (guard suppresses it).
2. Stale fan command (45 s ago) → fan_mode changes → override correctly detected.
3. Recent fan command (deactivation) → fan_mode changes "on" → "auto"
   → NOT detected as override.

Also tests the _async_fan_entity_changed path for belt-and-suspenders coverage.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

_NOW = datetime(2026, 6, 11, 14, 0, 0)


@contextmanager
def _fixed_now(when: datetime):
    """Temporarily pin coordinator.dt_util.now() to a fixed datetime.

    coordinator.py resolves dt_util as sys.modules["homeassistant.util"].dt
    (the attribute on the parent MagicMock), NOT sys.modules["homeassistant.util.dt"].
    We patch only the coordinator module's name so other modules (chart_log, etc.)
    that share the same MagicMock are unaffected.
    """
    coordinator_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    with patch.object(coordinator_mod.dt_util, "now", return_value=when):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(state_value: str, fan_mode: str = "auto") -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = {
        "hvac_action": "idle",
        "temperature": 70.0,
        "fan_mode": fan_mode,
    }
    return s


def _make_thermostat_event(old_state: MagicMock, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


def _make_fan_entity_event(old_state_str: str, new_state_str: str) -> MagicMock:
    old_s = MagicMock()
    old_s.state = old_state_str
    new_s = MagicMock()
    new_s.state = new_state_str
    event = MagicMock()
    event.data = {"old_state": old_s, "new_state": new_s}
    return event


def _make_coord(*, fan_command_time=None):
    """Coordinator stub with real _async_thermostat_changed / _async_fan_entity_changed bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "fan_state_feedback": True,  # tests validate feedback-mode override detection
    }

    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._fan_active = False
    ae._natural_vent_active = False
    ae._fan_override_active = False
    ae._temp_command_pending = False
    ae._fan_command_time = fan_command_time
    ae._hvac_command_time = None
    ae._temp_command_time = None
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    ae.reconcile_fan_on_startup = AsyncMock()
    coord.automation_engine = ae

    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "mild",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 72,
            "today_low": 55,
            "tomorrow_high": 73,
            "tomorrow_low": 56,
            "hvac_mode": "off",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    coord._current_classification = c

    from custom_components.climate_advisor.learning import DailyRecord

    coord._today_record = DailyRecord(date="2026-06-11", day_type="mild", trend_direction="stable")
    coord._async_save_state = AsyncMock()
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = MagicMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._last_outdoor_temp = 65.0
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()
    coord._startup_coalesce_active = False  # Bug 1 (Issue #321)

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    coord._async_fan_entity_changed = types.MethodType(ClimateAdvisorCoordinator._async_fan_entity_changed, coord)
    coord._is_recent_hvac_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_hvac_command, coord)
    coord._is_recent_temp_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_temp_command, coord)
    coord._is_recent_fan_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_fan_command, coord)
    # Issue #423: archetype-aware fan-running derivation, bound for real so tests can mock
    # just _get_fan_physical_state/_fan_state_feedback_enabled underneath it.
    coord._derive_thermostat_fan_running_for_reconcile = types.MethodType(
        ClimateAdvisorCoordinator._derive_thermostat_fan_running_for_reconcile, coord
    )
    coord._fan_state_feedback_enabled = types.MethodType(ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord)
    coord._get_fan_physical_state = MagicMock(return_value=None)  # default: command-only / no feedback
    return coord


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFanCommandGuard:
    """_is_recent_fan_command suppresses false override on CA's own fan echo."""

    def test_recent_fan_command_suppresses_thermostat_echo(self):
        """fan_command_time=now → fan_mode echo auto→on → override NOT fired.

        Occupant effect: without this guard, CA's own fan activation would
        immediately cancel the fan (override detected → fan stopped), leaving
        the occupant with no ventilation even though CA just turned the fan on.
        """
        coord = _make_coord(fan_command_time=_NOW)

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_stale_fan_command_allows_override_detection(self):
        """fan_command_time=45 s ago → fan_mode changes → override correctly detected.

        Occupant effect: a user who manually adjusts the thermostat fan 45 seconds
        after CA last touched it should still have their preference respected.
        """
        stale_time = _NOW - timedelta(seconds=45)
        coord = _make_coord(fan_command_time=stale_time)
        coord.automation_engine._fan_override_active = False

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()

    def test_none_fan_command_time_allows_override_detection(self):
        """fan_command_time=None → override correctly detected (guard inactive at startup).

        Occupant effect: on a fresh start where CA has never run the fan, a manual
        fan change at the thermostat should always be respected.
        """
        coord = _make_coord(fan_command_time=None)

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()

    def test_recent_deactivation_echo_not_detected_as_override(self):
        """fan_command_time=now (deactivation) → fan_mode on→auto echo → NOT override.

        Occupant effect: when CA turns the fan off at bedtime, the thermostat echo
        (fan_mode "on" → "auto") should not re-trigger an override that would
        re-enable the fan and prevent the occupant from sleeping quietly.
        """
        coord = _make_coord(fan_command_time=_NOW)
        coord.automation_engine._fan_active = True  # CA had fan on

        old_state = _make_state("cool", fan_mode="on")
        new_state = _make_state("cool", fan_mode="auto")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()


class TestFanEntityChangedGuard:
    """_async_fan_entity_changed also respects _is_recent_fan_command (belt-and-suspenders)."""

    def test_recent_fan_command_suppresses_fan_entity_echo(self):
        """fan_command_time=now → fan entity state off→on → override NOT fired.

        Occupant effect: the whole-house fan entity echo after CA activates it
        must not be treated as a manual override.
        """
        coord = _make_coord(fan_command_time=_NOW)
        coord.automation_engine._fan_active = False

        event = _make_fan_entity_event("off", "on")
        with _fixed_now(_NOW):
            asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_stale_fan_command_fan_entity_override_detected(self):
        """fan_command_time=45 s ago → fan entity on → override correctly detected.

        Occupant effect: manual use of a whole-house fan switch 45 seconds after
        CA last touched it should still be recorded as a user override.
        """
        stale_time = _NOW - timedelta(seconds=45)
        coord = _make_coord(fan_command_time=stale_time)
        coord.automation_engine._fan_active = False

        event = _make_fan_entity_event("off", "on")
        with _fixed_now(_NOW):
            asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()


class TestPostStartupUntrackedFanReconcile:
    """Post-startup untracked fan reconcile (Issue #347).

    When hvac_action transitions to 'fan' while CA does not own the fan,
    reconcile_fan_on_startup is called to enforce the invariant: a running
    fan always has an explicit owner — adopt as nat-vent or turn off.
    """

    def _make_fan_action_event(self, old_action: str, new_action: str, hvac_mode: str = "cool") -> MagicMock:
        old_s = MagicMock()
        old_s.state = hvac_mode
        old_s.attributes = {"hvac_action": old_action, "temperature": 70.0, "fan_mode": "auto"}
        new_s = MagicMock()
        new_s.state = hvac_mode
        new_s.attributes = {"hvac_action": new_action, "temperature": 70.0, "fan_mode": "auto"}
        event = MagicMock()
        event.data = {"old_state": old_s, "new_state": new_s}
        return event

    def test_fan_action_start_unowned_triggers_reconcile(self):
        """hvac_action idle→fan while CA does not own fan → reconcile_fan_on_startup called.

        Occupant effect: thermostat starts fan autonomously (e.g. fan-circulation between
        AC cycles). CA must immediately decide to adopt it as nat-vent or turn it off —
        not leave the fan running untracked overnight.
        """
        coord = _make_coord()
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_awaited_once_with(
            indoor=72.0,
            outdoor=65.0,
            thermostat_fan_running=True,
            any_sensor_open=False,
        )

    def test_fan_action_start_ca_owned_skips_reconcile(self):
        """hvac_action idle→fan while CA already owns the fan → no reconcile triggered.

        Occupant effect: CA activated the fan for nat-vent; the thermostat then reports
        hvac_action='fan' as expected. Must not double-reconcile and accidentally turn
        the fan off.
        """
        coord = _make_coord()
        coord.automation_engine._fan_active = True
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_not_awaited()

    def test_fan_action_already_fan_no_retriggering(self):
        """hvac_action fan→fan (steady-state) → no reconcile triggered.

        Occupant effect: once the fan has been reconciled (adopted or turned off), the
        steady-state thermostat events must not keep calling reconcile on every tick.
        """
        coord = _make_coord()
        event = self._make_fan_action_event(old_action="fan", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_not_awaited()

    def test_fan_action_with_fan_mode_change_skips_reconcile(self):
        """hvac_action idle→fan AND fan_mode auto→on in same event → no reconcile.

        Occupant effect: user manually presses "fan on" at the thermostat — some
        thermostats couple fan_mode and hvac_action in a single state event. This is
        a manual override, not a thermostat-autonomous fan start. The §9b manual
        override detection path must handle it, not the post-startup reconcile.
        """
        coord = _make_coord()
        old_s = MagicMock()
        old_s.state = "cool"
        old_s.attributes = {"hvac_action": "idle", "temperature": 70.0, "fan_mode": "auto"}
        new_s = MagicMock()
        new_s.state = "cool"
        new_s.attributes = {"hvac_action": "fan", "temperature": 70.0, "fan_mode": "on"}
        event = MagicMock()
        event.data = {"old_state": old_s, "new_state": new_s}

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_not_awaited()

    def test_fan_action_start_with_pending_ca_command_skips_reconcile(self):
        """hvac_action idle→fan while a CA fan command is still in flight → no reconcile
        (Issue #417).

        Occupant effect: CA just issued its own nat-vent cycle-on service call; the
        thermostat's hvac_action briefly reports 'fan' before CA's own _fan_active /
        _natural_vent_active flags finish settling. Without this guard, the reconcile
        listener could see this transient window as "unowned" and immediately turn the
        fan back off, undoing the cycling decision CA just made.
        """
        coord = _make_coord()
        coord.automation_engine._fan_command_pending = True
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_not_awaited()

    def test_fan_action_start_with_recent_ca_command_skips_reconcile(self):
        """hvac_action idle→fan within 30s of a CA fan command (pending already cleared)
        → no reconcile (Issue #417)."""
        coord = _make_coord(fan_command_time=_NOW - timedelta(seconds=5))
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_not_awaited()

    def test_fan_action_start_with_stale_ca_command_still_triggers_reconcile(self):
        """hvac_action idle→fan more than 30s after any CA fan command → reconcile still
        fires normally — the guard must not suppress genuine untracked-fan detection."""
        coord = _make_coord(fan_command_time=_NOW - timedelta(seconds=45))
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Issue #423: archetype-aware fan-running derivation for reconcile_fan_on_startup()
# ---------------------------------------------------------------------------


class TestDeriveThermostatFanRunningForReconcile:
    """_derive_thermostat_fan_running_for_reconcile() — archetype-aware ground truth."""

    def test_hvac_mode_uses_thermostat_attrs_regardless_of_physical_state(self):
        """FAN_MODE_HVAC: thermostat attrs ARE the fan — trust them, ignore physical state."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_HVAC
        coord._get_fan_physical_state = MagicMock(return_value=False)

        assert coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="fan") is True
        coord._get_fan_physical_state.assert_not_called()

    def test_whole_house_mode_uses_physical_state_not_thermostat_attrs(self):
        """FAN_MODE_WHOLE_HOUSE regression test for Issue #423: thermostat attrs say the fan
        is running (a thermostat-internal blip), but the real configured WHF entity reads off
        — must return False, not adopt a fan that was never actually turned on."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        coord._get_fan_physical_state = MagicMock(return_value=False)

        result = coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="fan")

        assert result is False, "Issue #423: WHF physical state=off must win over a thermostat-only signal"

    def test_whole_house_mode_agrees_when_physical_state_true(self):
        """FAN_MODE_WHOLE_HOUSE: physical state confirms on -> True."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        coord._get_fan_physical_state = MagicMock(return_value=True)

        assert coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="idle") is True

    def test_command_only_mode_falls_back_to_thermostat_attrs(self):
        """FAN_MODE_WHOLE_HOUSE with fan_state_feedback disabled: _get_fan_physical_state()
        returns None (no ground truth) -> falls back to the thermostat-attribute signal
        (documented, explicit fallback, not an accidental fallthrough)."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        coord._get_fan_physical_state = MagicMock(return_value=None)

        assert coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="fan") is True
        assert coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="idle") is False

    def test_both_mode_ors_whf_and_thermostat_signals(self):
        """FAN_MODE_BOTH: ORs the real WHF physical state with the thermostat signal — a
        strict superset of the pre-#423 behavior, not a true per-device model (tracked as a
        follow-up gap, not solved here)."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_BOTH
        coord._get_fan_physical_state = MagicMock(return_value=False)

        # WHF physically off, but thermostat's own blower attrs say running -> True (OR).
        assert coord._derive_thermostat_fan_running_for_reconcile(fan_mode_attr="", hvac_action_attr="fan") is True


class TestReconcileSite3ArchetypeAware:
    """Issue #423 regression: the exact Issue #347/#417 runtime trigger site (Site 3) that
    misfired in the reported incident, now archetype-aware for FAN_MODE_WHOLE_HOUSE."""

    def _make_fan_action_event(self, old_action: str, new_action: str, hvac_mode: str = "cool") -> MagicMock:
        old_s = MagicMock()
        old_s.state = hvac_mode
        old_s.attributes = {"hvac_action": old_action, "temperature": 70.0, "fan_mode": "auto"}
        new_s = MagicMock()
        new_s.state = hvac_mode
        new_s.attributes = {"hvac_action": new_action, "temperature": 70.0, "fan_mode": "auto"}
        event = MagicMock()
        event.data = {"old_state": old_s, "new_state": new_s}
        return event

    def test_whf_thermostat_blip_with_whf_physically_off_does_not_adopt(self):
        """Issue #423 exact incident reproduction: FAN_MODE_WHOLE_HOUSE, hvac_action
        transitions idle->fan (a thermostat-internal blip, e.g. after a nat-vent proactive
        floor exit), but the real WHF entity reads physically off. Reconcile must NOT adopt
        — thermostat_fan_running must be False, not the pre-fix hardcoded True.
        """
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        coord._get_fan_physical_state = MagicMock(return_value=False)
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_awaited_once_with(
            indoor=72.0,
            outdoor=65.0,
            thermostat_fan_running=False,
            any_sensor_open=False,
        )

    def test_whf_thermostat_blip_with_whf_physically_on_does_adopt(self):
        """Same transition, but the real WHF entity confirms it's genuinely on -> adopt
        (thermostat_fan_running=True) — the fix must not make WHF adoption impossible, only
        correct when the signal disagrees with reality."""
        coord = _make_coord()
        coord.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        coord._get_fan_physical_state = MagicMock(return_value=True)
        event = self._make_fan_action_event(old_action="idle", new_action="fan")

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.reconcile_fan_on_startup.assert_awaited_once_with(
            indoor=72.0,
            outdoor=65.0,
            thermostat_fan_running=True,
            any_sensor_open=False,
        )
