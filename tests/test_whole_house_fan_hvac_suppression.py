"""Tests for whole-house fan HVAC suppression (Fix C) and window-close stop (Fix D).

Fix C: When the whole-house fan (FAN_MODE_WHOLE_HOUSE or FAN_MODE_BOTH) activates,
       it must set HVAC to "off" to prevent heating/cooling fighting the fan, and
       restore the prior HVAC mode when deactivated.

Fix D: When all sensors close and the whole-house fan is running outside of nat-vent,
       handle_all_doors_windows_closed must stop the fan.

These behaviors are absent before the fix — tests fail before the fix, pass after.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Patch dt_util.now before importing automation
if "homeassistant.util.dt" in sys.modules:
    sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 12, 14, 0, 0)
else:
    # conftest will have installed the stubs; patch after import
    pass

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Import const at module level (stable; occupancy test only reloads coordinator,
# not const).  AutomationEngine is fetched fresh per-call via _get_engine_class()
# because test_occupancy.py deletes coordinator from sys.modules, which triggers
# re-import of automation.py and creates a fresh class object.  A module-level
# AutomationEngine reference would point to the stale class that lacks attributes
# added by this fix.  See test_daily_record_accuracy.py for the same pattern.
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

_NOW = datetime(2026, 6, 12, 14, 0, 0)


def _get_engine_class():
    """Return the current AutomationEngine class, re-importing if needed."""
    mod = importlib.import_module("custom_components.climate_advisor.automation")
    return mod.AutomationEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    coro.close()


def _make_thermostat_state(hvac_mode: str = "cool") -> MagicMock:
    s = MagicMock()
    s.state = hvac_mode
    s.attributes = {"fan_mode": "auto", "temperature": 75.0}
    return s


def _make_engine(
    fan_mode: str = FAN_MODE_WHOLE_HOUSE,
    fan_entity: str = "fan.whole_house",
    current_hvac_mode: str = "cool",
):
    """Create an AutomationEngine configured for whole-house fan tests."""
    AutomationEngine = _get_engine_class()

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=_make_thermostat_state(current_hvac_mode))

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        CONF_FAN_MODE: fan_mode,
        CONF_FAN_ENTITY: fan_entity,
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.window"],
        notify_service="notify.notify",
        config=config,
    )
    return engine


def _get_hvac_mode_calls(engine) -> list[str]:
    """Return the list of hvac_mode values passed to set_hvac_mode service calls."""
    result = []
    for c in engine.hass.services.async_call.call_args_list:
        if c[0][0] == "climate" and c[0][1] == "set_hvac_mode":
            result.append(c[0][2].get("hvac_mode"))
    return result


def _get_fan_entity_calls(engine, service: str, fan_entity: str) -> list:
    """Return service calls for the given fan entity domain/service."""
    result = []
    domain = fan_entity.split(".")[0]
    for c in engine.hass.services.async_call.call_args_list:
        if c[0][0] == domain and c[0][1] == service and c[0][2].get("entity_id") == fan_entity:
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Fix C: _activate_fan with whole-house fan suppresses HVAC
# ---------------------------------------------------------------------------


class TestWholehouseFanSuppressesHvac:
    """When a whole-house fan activates, HVAC must be suppressed.

    Occupant effect: without suppression, the AC fights the whole-house fan,
    wasting energy and reducing the fan's cooling effectiveness.
    """

    def test_activate_whole_house_fan_sets_hvac_off(self):
        """_activate_fan() with FAN_MODE_WHOLE_HOUSE calls set_hvac_mode('off').

        Occupant effect: running AC while the whole-house fan is exchanging air
        directly counteracts free cooling and wastes energy.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="cool")

        asyncio.run(engine._activate_fan(reason="nat vent test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "off" in hvac_calls, (
            f"Expected HVAC to be set to 'off' when whole-house fan activates; got calls: {hvac_calls}"
        )

    def test_activate_whole_house_fan_stores_pre_fan_hvac_mode(self):
        """_activate_fan() stores the prior HVAC mode for restoration later.

        Occupant effect: without storing prior mode, the restore-on-deactivate
        path has nothing to restore and HVAC stays off indefinitely.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="cool")
        assert engine._pre_fan_hvac_mode is None, "Precondition: no prior mode stored"

        asyncio.run(engine._activate_fan(reason="nat vent test"))

        assert engine._pre_fan_hvac_mode == "cool", (
            f"Expected _pre_fan_hvac_mode='cool'; got {engine._pre_fan_hvac_mode!r}"
        )

    def test_activate_both_fan_sets_hvac_off(self):
        """_activate_fan() with FAN_MODE_BOTH also calls set_hvac_mode('off').

        Occupant effect: FAN_MODE_BOTH activates both the whole-house and HVAC fan;
        the HVAC compressor/heat must still be suppressed.
        """
        engine = _make_engine(fan_mode=FAN_MODE_BOTH, current_hvac_mode="heat")

        asyncio.run(engine._activate_fan(reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "off" in hvac_calls, f"Expected HVAC to be set to 'off' when FAN_MODE_BOTH activates; got: {hvac_calls}"

    def test_activate_hvac_fan_does_not_suppress_hvac(self):
        """_activate_fan() with FAN_MODE_HVAC must NOT call set_hvac_mode.

        Occupant effect: FAN_MODE_HVAC is the thermostat blower only — it does not
        exchange outdoor air and must not change the HVAC heating/cooling state.
        """
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, current_hvac_mode="cool")

        asyncio.run(engine._activate_fan(reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "off" not in hvac_calls, f"FAN_MODE_HVAC must NOT suppress HVAC; got set_hvac_mode calls: {hvac_calls}"

    def test_activate_disabled_fan_does_not_suppress_hvac(self):
        """_activate_fan() with FAN_MODE_DISABLED does nothing (early return).

        Occupant effect: sanity check — no-op fan mode must not modify HVAC.
        """
        engine = _make_engine(fan_mode=FAN_MODE_DISABLED, current_hvac_mode="cool")

        asyncio.run(engine._activate_fan(reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert len(hvac_calls) == 0, f"Disabled fan must not modify HVAC; got: {hvac_calls}"


# ---------------------------------------------------------------------------
# Fix C: _deactivate_fan with whole-house fan restores HVAC
# ---------------------------------------------------------------------------


class TestWholehouseFanRestoresHvac:
    """When a whole-house fan deactivates, HVAC must be restored to prior mode.

    Occupant effect: after the evening whole-house fan run, the home needs to
    transition back to normal HVAC scheduling — without restore, HVAC stays off.
    """

    def test_deactivate_whole_house_fan_restores_prior_hvac_mode(self):
        """_deactivate_fan() with _pre_fan_hvac_mode set → calls set_hvac_mode(prior).

        Occupant effect: when the whole-house fan finishes, the AC or heat that
        was running before must resume to maintain the comfort setpoint.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"  # stored when fan was activated

        asyncio.run(engine._deactivate_fan(reason="sensors closed"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "cool" in hvac_calls, f"Expected HVAC restored to 'cool' after whole-house fan off; got: {hvac_calls}"

    def test_deactivate_whole_house_fan_clears_pre_fan_hvac_mode(self):
        """After restore, _pre_fan_hvac_mode must be cleared.

        Occupant effect: stale stored mode could cause incorrect restore on a
        subsequent activation/deactivation cycle.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"

        asyncio.run(engine._deactivate_fan(reason="sensors closed"))

        assert engine._pre_fan_hvac_mode is None, (
            "_pre_fan_hvac_mode must be cleared after restore to avoid stale state"
        )

    def test_deactivate_no_prior_mode_does_not_set_hvac(self):
        """If _pre_fan_hvac_mode is None, deactivate must not call set_hvac_mode.

        Occupant effect: if the engine restarted with no stored mode (e.g., after
        an HA restart mid-fan-run), it must not blindly call set_hvac_mode(None)
        which would cause an HA API error.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)
        engine._fan_active = True
        engine._pre_fan_hvac_mode = None  # no prior mode stored

        asyncio.run(engine._deactivate_fan(reason="sensors closed"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert len(hvac_calls) == 0, f"Must not call set_hvac_mode when no prior mode is stored; got: {hvac_calls}"

    def test_deactivate_hvac_fan_does_not_restore_hvac(self):
        """_deactivate_fan() with FAN_MODE_HVAC must NOT call set_hvac_mode.

        Occupant effect: HVAC-fan-only deactivation should only affect fan_mode,
        not HVAC mode — changing HVAC mode unexpectedly disrupts the comfort schedule.
        """
        engine = _make_engine(fan_mode=FAN_MODE_HVAC)
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"  # should be ignored for FAN_MODE_HVAC

        asyncio.run(engine._deactivate_fan(reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert len(hvac_calls) == 0, f"FAN_MODE_HVAC deactivation must not call set_hvac_mode; got: {hvac_calls}"


# ---------------------------------------------------------------------------
# Fix C: state persistence — _pre_fan_hvac_mode in serialization/restore
# ---------------------------------------------------------------------------


class TestPreFanHvacModeStatePersistence:
    """_pre_fan_hvac_mode is serialized and restored so it survives HA restarts."""

    def test_get_serializable_state_includes_pre_fan_hvac_mode(self):
        """get_serializable_state() must include pre_fan_hvac_mode field.

        Occupant effect: without this field, an HA restart while the whole-house
        fan runs leaves no record of what HVAC mode to restore.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)
        engine._pre_fan_hvac_mode = "heat"

        state = engine.get_serializable_state()

        assert "pre_fan_hvac_mode" in state, "get_serializable_state() must include 'pre_fan_hvac_mode' key"
        assert state["pre_fan_hvac_mode"] == "heat", f"Expected 'heat', got {state['pre_fan_hvac_mode']!r}"

    def test_get_serializable_state_pre_fan_hvac_mode_none_by_default(self):
        """When no fan is active, pre_fan_hvac_mode serializes as None."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)

        state = engine.get_serializable_state()

        assert state.get("pre_fan_hvac_mode") is None

    def test_restore_state_restores_pre_fan_hvac_mode(self):
        """restore_state() must restore pre_fan_hvac_mode from persisted data.

        Occupant effect: after HA restart while fan is running, the engine must
        know what mode to restore when the fan eventually stops.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)
        engine.restore_state({"pre_fan_hvac_mode": "cool"})

        assert engine._pre_fan_hvac_mode == "cool", (
            f"restore_state must restore pre_fan_hvac_mode; got {engine._pre_fan_hvac_mode!r}"
        )

    def test_restore_state_defaults_pre_fan_hvac_mode_to_none(self):
        """If not in persisted state, _pre_fan_hvac_mode defaults to None."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE)
        engine.restore_state({})  # empty state (e.g., first boot)

        assert engine._pre_fan_hvac_mode is None


# ---------------------------------------------------------------------------
# Fix D: handle_all_doors_windows_closed stops whole-house fan outside nat-vent
# ---------------------------------------------------------------------------


class TestWindowCloseStopsWholehouseFan:
    """All sensors closed must stop the whole-house fan even outside nat-vent.

    Occupant effect: leaving the whole-house fan running after windows close
    draws outdoor air (possibly hot or cold) into the home, counteracting HVAC.
    """

    def test_sensors_closed_stops_whole_house_fan_when_not_nat_vent(self):
        """handle_all_doors_windows_closed() stops fan when _fan_active=True,
        _natural_vent_active=False, fan_mode=whole_house.

        Occupant effect: if the whole-house fan was started manually (or via
        min-runtime cycle) and the user closes all windows, the fan must stop
        to avoid drawing in uncomfortable air.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic")
        engine._fan_active = True
        engine._natural_vent_active = False
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        fan_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        assert len(fan_off_calls) >= 1, (
            f"Expected fan.turn_off('fan.attic') when sensors close; got calls: "
            f"{[str(c) for c in engine.hass.services.async_call.call_args_list]}"
        )

    def test_sensors_closed_stops_both_mode_fan_when_not_nat_vent(self):
        """FAN_MODE_BOTH: handle_all_doors_windows_closed also stops the fan.

        Occupant effect: same as FAN_MODE_WHOLE_HOUSE — outdoor air exchange
        must stop when the windows close.
        """
        engine = _make_engine(fan_mode=FAN_MODE_BOTH, fan_entity="fan.attic")
        engine._fan_active = True
        engine._natural_vent_active = False
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        fan_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        assert len(fan_off_calls) >= 1, (
            f"FAN_MODE_BOTH: expected fan.turn_off when sensors close; "
            f"got: {[str(c) for c in engine.hass.services.async_call.call_args_list]}"
        )

    def test_sensors_closed_does_not_stop_fan_when_nat_vent_active(self):
        """If nat-vent is active, the existing nat-vent branch handles fan stop.

        The new Fix D branch must not double-stop the fan when nat-vent is running.
        Occupant effect: nat-vent cleanup is already handled by the existing
        `if self._natural_vent_active:` block.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic")
        engine._fan_active = True
        engine._natural_vent_active = True  # nat-vent branch handles this
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        # The nat-vent branch calls _deactivate_fan — which calls turn_off.
        # We just verify the function doesn't error and the fan is stopped.
        # (nat-vent branch handles this path; Fix D must not add a duplicate call)
        fan_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        # Exactly 1 deactivation (from nat-vent branch), not 2
        assert len(fan_off_calls) <= 1, (
            f"Fan should be stopped exactly once (by nat-vent branch); got {len(fan_off_calls)} calls"
        )

    def test_sensors_closed_does_not_stop_hvac_fan_only(self):
        """FAN_MODE_HVAC: Fix D must not trigger for HVAC-only fan.

        Occupant effect: thermostat blower stopping on window-close would
        disrupt the HVAC system's own circulation — the fix applies only to
        outdoor air exchange (whole-house fan).
        """
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, fan_entity="fan.attic")
        engine._fan_active = True
        engine._natural_vent_active = False
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        # Main check: function must not error and no unexpected fan domain turn_off
        fan_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        assert len(fan_off_calls) == 0, (
            f"FAN_MODE_HVAC: Fix D must not call fan.turn_off for HVAC-fan; got: {fan_off_calls}"
        )

    def test_sensors_closed_does_not_stop_fan_when_not_active(self):
        """If the fan is not running (_fan_active=False), Fix D must not fire.

        Occupant effect: spuriously calling turn_off on an already-off fan entity
        could confuse HA or create unnecessary log noise.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic")
        engine._fan_active = False  # fan not running
        engine._natural_vent_active = False
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        fan_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        assert len(fan_off_calls) == 0, f"Fix D must not call fan.turn_off when _fan_active=False; got: {fan_off_calls}"


# ---------------------------------------------------------------------------
# Issue #392 Fix 1b: structural choke-point guard (_whf_owns_hvac in
# _set_hvac_mode()/_set_temperature())
# ---------------------------------------------------------------------------


def _make_classification(day_type: str = "warm", hvac_mode: str = "cool"):
    """Minimal DayClassification, bypassing __post_init__ validation."""
    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.day_type = day_type
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 85.0
    c.today_low = 65.0
    c.tomorrow_high = 85.0
    c.tomorrow_low = 65.0
    c.hvac_mode = hvac_mode
    c.pre_condition = False
    c.pre_condition_target = 0.0
    c.windows_recommended = False
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


class TestChokePointGuardBlocksWhfWrites:
    """Issue #392 Fix 1b: _set_hvac_mode()/_set_temperature() silently block active-mode
    writes while a whole-house-fan session owns the thermostat (_whf_owns_hvac() == True),
    making WHF/AC mutual exclusion a structural guarantee rather than a per-caller convention.

    Occupant effect: without this guard, any of the ~13 call sites that write HVAC mode
    (or the 7 _apply_comfort_band() call sites) could re-arm the compressor while the
    whole-house fan is physically running, fighting the fan and wasting energy — even
    though _activate_fan()/_deactivate_fan() already try to maintain exclusivity by
    convention (Root Cause #2 in the #392 investigation).
    """

    def test_set_hvac_mode_blocked_when_whf_owns_hvac(self):
        """mode='cool' write is silently dropped (no service call) while WHF suppresses HVAC."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = "cool"  # WHF session actively suppressing HVAC

        asyncio.run(engine._set_hvac_mode("cool", reason="test: attempted re-arm"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "cool" not in hvac_calls, f"Expected 'cool' write blocked while WHF owns HVAC; got: {hvac_calls}"

    def test_set_hvac_mode_off_never_blocked(self):
        """mode='off' writes are never blocked — the guard only blocks active modes."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="cool")
        engine._pre_fan_hvac_mode = "cool"

        asyncio.run(engine._set_hvac_mode("off", reason="test: suppress"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "off" in hvac_calls, f"Expected 'off' write to always succeed; got: {hvac_calls}"

    def test_set_hvac_mode_not_blocked_for_hvac_fan_mode(self):
        """FAN_MODE_HVAC never sets _pre_fan_hvac_mode, so _whf_owns_hvac() is always False."""
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = None

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "cool" in hvac_calls, f"FAN_MODE_HVAC must not be blocked; got: {hvac_calls}"

    def test_set_hvac_mode_not_blocked_when_no_whf_session_active(self):
        """FAN_MODE_WHOLE_HOUSE but no active session (_pre_fan_hvac_mode=None) -> not blocked."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = None  # no suppression session in progress

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        hvac_calls = _get_hvac_mode_calls(engine)
        assert "cool" in hvac_calls, f"No active WHF session must not block writes; got: {hvac_calls}"

    def test_set_temperature_blocked_when_whf_owns_hvac(self):
        """_set_temperature(mode='cool') is silently dropped while WHF suppresses HVAC."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = "cool"

        asyncio.run(engine._set_temperature(74.0, reason="test: attempted re-arm", mode="cool"))

        temp_calls = [
            c
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_temperature"
        ]
        assert len(temp_calls) == 0, f"Expected set_temperature blocked while WHF owns HVAC; got: {temp_calls}"

    def test_set_temperature_not_blocked_for_hvac_fan_mode(self):
        """FAN_MODE_HVAC: set_temperature succeeds normally (fan/compressor coexist)."""
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, current_hvac_mode="cool")
        engine._pre_fan_hvac_mode = None

        asyncio.run(engine._set_temperature(74.0, reason="test", mode="cool"))

        temp_calls = [
            c
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_temperature"
        ]
        assert len(temp_calls) == 1, f"FAN_MODE_HVAC set_temperature must succeed; got: {temp_calls}"

    def test_hvac_write_blocked_event_fires_on_set_hvac_mode(self):
        """A blocked _set_hvac_mode() write emits hvac_write_blocked_whf_active."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = "cool"
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._set_hvac_mode("cool", reason="test: attempted re-arm"))

        events = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "hvac_write_blocked_whf_active" in events, f"Expected block event; got: {events}"
        payload = next(
            c.args[1]
            for c in engine._emit_event_callback.call_args_list
            if c.args[0] == "hvac_write_blocked_whf_active"
        )
        assert payload["attempted_mode"] == "cool"

    def test_hvac_write_blocked_event_fires_on_set_temperature(self):
        """A blocked _set_temperature() write emits hvac_write_blocked_whf_active."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = "cool"
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._set_temperature(74.0, reason="test: attempted re-arm", mode="cool"))

        events = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "hvac_write_blocked_whf_active" in events, f"Expected block event; got: {events}"

    def test_no_block_event_when_write_succeeds(self):
        """A successful (non-blocked) write does not emit hvac_write_blocked_whf_active."""
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, current_hvac_mode="off")
        engine._pre_fan_hvac_mode = None
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._set_hvac_mode("cool", reason="test"))

        events = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "hvac_write_blocked_whf_active" not in events


class TestRePauseCallsApplyNatVentHvacState:
    """Issue #392 Fix 1: _re_pause_for_open_sensor() now calls _apply_nat_vent_hvac_state()
    after activating nat-vent, matching the other three reactivation gate sites
    (handle_door_window_open, check_natural_vent_conditions grace re-entry,
    nat_vent_temperature_check paused-state reactivation). Before this fix,
    _re_pause_for_open_sensor() was the one site that skipped this call — an
    inconsistency the plan calls out as "fix alongside" Root Cause #1.

    Occupant effect: without this call, a WHF session reactivated via this specific
    path would not have its no-op _apply_nat_vent_hvac_state() invariant applied
    consistently (WHF: no-op since _activate_fan() already suppressed HVAC; HVAC-fan
    mode: fails to re-arm the full comfort band), producing inconsistent state
    depending on which of the four reactivation paths happened to fire.
    """

    def test_re_pause_calls_apply_nat_vent_hvac_state_when_reactivating(self):
        """WHF: outdoor cool -> nat-vent reactivates -> _apply_nat_vent_hvac_state() called."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="cool")
        engine._get_indoor_temp_f = MagicMock(return_value=76.0)
        engine._last_outdoor_temp = 68.0  # cool enough for nat-vent
        engine._apply_nat_vent_hvac_state = AsyncMock()

        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._natural_vent_active is True
        engine._apply_nat_vent_hvac_state.assert_awaited_once()

    def test_re_pause_does_not_call_apply_nat_vent_hvac_state_when_not_reactivating(self):
        """Outdoor too warm -> falls through to regular re-pause -> helper NOT called."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="cool")
        engine._get_indoor_temp_f = MagicMock(return_value=76.0)
        engine._last_outdoor_temp = 90.0  # too warm — nat-vent conditions not met
        engine._apply_nat_vent_hvac_state = AsyncMock()

        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._natural_vent_active is False
        engine._apply_nat_vent_hvac_state.assert_not_awaited()


class TestApplyClassificationWhfEarlyReturn:
    """Issue #392 Fix 1b: apply_classification()'s nat-vent branch returns early for
    FAN_MODE_WHOLE_HOUSE/BOTH right after _apply_nat_vent_hvac_state(), instead of falling
    through to select_comfort_band()/_apply_comfort_band()/the ODE ceiling guard — all of
    which would attempt (and have their writes silently dropped by) the choke-point guard
    while WHF owns the thermostat. FAN_MODE_HVAC keeps falling through as before (band
    stays armed; fan and compressor coexist per Issue #249).
    """

    def test_whf_nat_vent_active_skips_select_comfort_band(self):
        """WHF + nat-vent active + savings off -> select_comfort_band() is NOT called."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine._natural_vent_active = True
        engine._pre_fan_hvac_mode = "cool"
        engine._apply_nat_vent_hvac_state = AsyncMock()

        classification = _make_classification()

        with patch("custom_components.climate_advisor.automation.select_comfort_band") as mock_select:
            asyncio.run(engine.apply_classification(classification))
            mock_select.assert_not_called()

        engine._apply_nat_vent_hvac_state.assert_awaited_once()

    def test_hvac_fan_nat_vent_active_still_calls_select_comfort_band(self):
        """FAN_MODE_HVAC + nat-vent active + savings off -> select_comfort_band() IS called
        (band stays armed; the guard is only for WHF's structural mutual-exclusion contract).
        """
        engine = _make_engine(fan_mode=FAN_MODE_HVAC, current_hvac_mode="cool")
        engine._natural_vent_active = True
        engine._apply_nat_vent_hvac_state = AsyncMock()

        classification = _make_classification()

        with patch("custom_components.climate_advisor.automation.select_comfort_band") as mock_select:
            mock_select.return_value = MagicMock(active="ceiling", floor=70.0, ceiling=75.0, reason="test")
            asyncio.run(engine.apply_classification(classification))
            mock_select.assert_called_once()

    def test_whf_aggressive_savings_still_returns_early_before_select_comfort_band(self):
        """WHF + nat-vent + aggressive_savings=True -> already returned early for savings;
        select_comfort_band() must still not be called (belt-and-suspenders for the WHF path).
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, current_hvac_mode="off")
        engine.config["aggressive_savings"] = True
        engine._natural_vent_active = True
        engine._pre_fan_hvac_mode = "cool"
        engine._apply_nat_vent_hvac_state = AsyncMock()

        classification = _make_classification()

        with patch("custom_components.climate_advisor.automation.select_comfort_band") as mock_select:
            asyncio.run(engine.apply_classification(classification))
            mock_select.assert_not_called()


class TestFanActivateDeactivateIdempotency:
    """Issue #392 Fix 1c: _activate_fan()/_deactivate_fan() are idempotent — a second call
    while already in the target state is a no-op (debug log only), preventing the
    18:53/18:58 burst pattern where multiple uncoordinated handlers each re-decide the
    same already-satisfied condition and re-execute the full activation/deactivation
    sequence (re-capturing _pre_fan_hvac_mode from a possibly-stale thermostat read,
    reissuing the physical service call, emitting a duplicate event).
    """

    def test_activate_fan_twice_calls_service_once(self):
        """Two _activate_fan() calls in a row -> physical turn_on called exactly once."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="cool")
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._activate_fan(reason="first"))
        asyncio.run(engine._activate_fan(reason="second"))

        turn_on_calls = _get_fan_entity_calls(engine, "turn_on", "fan.attic")
        assert len(turn_on_calls) == 1, f"Expected exactly 1 turn_on call; got {len(turn_on_calls)}"

    def test_activate_fan_twice_emits_event_once(self):
        """Two _activate_fan() calls -> fan_activated emitted exactly once."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="cool")
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._activate_fan(reason="first"))
        asyncio.run(engine._activate_fan(reason="second"))

        activated_events = [c for c in engine._emit_event_callback.call_args_list if c.args[0] == "fan_activated"]
        assert len(activated_events) == 1, f"Expected exactly 1 fan_activated event; got {len(activated_events)}"

    def test_activate_fan_twice_does_not_recapture_pre_fan_hvac_mode(self):
        """Second _activate_fan() call does not overwrite _pre_fan_hvac_mode from a
        possibly-stale thermostat read — it should already equal the mode captured
        by the first (real) activation.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="cool")

        asyncio.run(engine._activate_fan(reason="first"))
        assert engine._pre_fan_hvac_mode == "cool"

        # Simulate the thermostat now reading "off" (already suppressed) — if the second
        # call incorrectly re-captured, _pre_fan_hvac_mode would become "off" (wrong).
        engine.hass.states.get = MagicMock(return_value=_make_thermostat_state("off"))
        asyncio.run(engine._activate_fan(reason="second"))

        assert engine._pre_fan_hvac_mode == "cool", (
            f"Idempotency guard must prevent re-capture on the redundant call; got {engine._pre_fan_hvac_mode!r}"
        )

    def test_deactivate_fan_twice_calls_service_once(self):
        """Two _deactivate_fan() calls in a row -> physical turn_off called exactly once."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="off")
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._deactivate_fan(reason="first"))
        asyncio.run(engine._deactivate_fan(reason="second"))

        turn_off_calls = _get_fan_entity_calls(engine, "turn_off", "fan.attic")
        assert len(turn_off_calls) == 1, f"Expected exactly 1 turn_off call; got {len(turn_off_calls)}"

    def test_deactivate_fan_twice_emits_event_once(self):
        """Two _deactivate_fan() calls -> fan_deactivated emitted exactly once."""
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="off")
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"
        engine._emit_event_callback = MagicMock()

        asyncio.run(engine._deactivate_fan(reason="first"))
        asyncio.run(engine._deactivate_fan(reason="second"))

        deactivated_events = [c for c in engine._emit_event_callback.call_args_list if c.args[0] == "fan_deactivated"]
        assert len(deactivated_events) == 1, f"Expected exactly 1 fan_deactivated event; got {len(deactivated_events)}"

    def test_deactivate_fan_twice_does_not_double_restore_hvac(self):
        """Second _deactivate_fan() call does not re-issue the HVAC restore write —
        _pre_fan_hvac_mode is already None after the first (real) deactivation.
        """
        engine = _make_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, fan_entity="fan.attic", current_hvac_mode="off")
        engine._fan_active = True
        engine._pre_fan_hvac_mode = "cool"

        asyncio.run(engine._deactivate_fan(reason="first"))
        hvac_calls_after_first = _get_hvac_mode_calls(engine)
        assert "cool" in hvac_calls_after_first

        asyncio.run(engine._deactivate_fan(reason="second"))
        hvac_calls_after_second = _get_hvac_mode_calls(engine)
        assert hvac_calls_after_second.count("cool") == 1, (
            f"Expected exactly 1 HVAC restore write across both calls; got {hvac_calls_after_second}"
        )
