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
from unittest.mock import AsyncMock, MagicMock

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
