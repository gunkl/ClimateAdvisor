"""Issue #1705 mirror: an economizer session never outlives its fan.

Occupant view: on a blower install, window cooling (the economizer) runs the HVAC blower. If anything other than the
economizer stops that blower or records it off (the fan reconcile, the occupant's own fan-off, the drift check, the
ownership self-heal), the economizer used to stay recorded "active" in cool-down with no fan. It only acts on a phase
change, so it never started the blower again for the rest of the window session: no free cooling.

These tests drive the real ``AutomationEngine`` methods; only the fan command itself (or the nat-vent exit choke point
that sends it) is stubbed, so each test fails if the matching ``_end_economizer_session_for_stopped_fan`` call is
deleted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.economizer_lifecycle import EconomizerLifecycleState
from tests.test_economizer import _make_automation_engine, _make_hot_classification


def _thermostat(engine, mode: str = "off") -> None:
    state = MagicMock()
    state.state = mode
    state.attributes = {"fan_modes": ["auto", "on"], "fan_mode": "on", "hvac_modes": ["off", "heat", "cool"]}
    engine.hass.states.get.return_value = state


def _engine_in_cool_down(fan_mode: str):
    engine = _make_automation_engine({CONF_FAN_MODE: fan_mode})
    engine._current_classification = _make_hot_classification()
    engine._economizer_active = True
    engine._economizer_phase = "cool-down"
    _thermostat(engine)
    return engine


def _fan_mode_calls(engine):
    return [c for c in engine.hass.services.async_call.call_args_list if c[0][1] == "set_fan_mode"]


def _reconcile(engine, *, fan_running: bool, outdoor: float = 80.0) -> None:
    asyncio.run(
        engine.reconcile_fan_on_startup(
            indoor=75.0, outdoor=outdoor, thermostat_fan_running=fan_running, any_sensor_open=True
        )
    )


class TestDeactivateFanIsTheHook:
    """The one hook: a ``_deactivate_fan()`` stop that went out, or found the fan already off, ends the session."""

    def test_executed_stop_ends_the_session(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True

        result = asyncio.run(engine._deactivate_fan(reason="min_runtime_cycle_complete"))

        assert result is FanCommandResult.EXECUTED
        assert [c[0][2]["fan_mode"] for c in _fan_mode_calls(engine)] == ["auto"]
        assert engine.economizer_lifecycle_state is EconomizerLifecycleState.INACTIVE

    def test_already_off_ends_the_session(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = False

        result = asyncio.run(engine._deactivate_fan(reason="bedtime"))

        assert result is FanCommandResult.ALREADY_IN_STATE
        assert engine._economizer_active is False

    def test_observe_only_stop_keeps_the_session(self):
        # DRY_RUN: nothing was sent, the fan keeps running, so its session stays recorded (#1304).
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        engine.dry_run = True

        result = asyncio.run(engine._deactivate_fan(reason="bedtime"))

        assert result is FanCommandResult.DRY_RUN
        assert engine._economizer_active is True

    def test_economizer_own_stop_passes_through_its_own_teardown(self):
        # _deactivate_economizer() opts out of the hook and clears its own flags (with its comfort restore).
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        engine._end_economizer_session_for_stopped_fan = MagicMock()

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        engine._end_economizer_session_for_stopped_fan.assert_not_called()
        assert engine._economizer_active is False

    def test_bedtime_runs_the_economizer_teardown(self):
        """Bedtime with window cooling running: the economizer's own teardown runs (fan stop, comfort restore, log),
        not just the hook's bookkeeping -- stopping the fan first used to end the session and skip it."""
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        engine._deactivate_economizer = AsyncMock(wraps=engine._deactivate_economizer)

        asyncio.run(engine.handle_bedtime())

        engine._deactivate_economizer.assert_awaited_once()
        assert engine._economizer_active is False
        assert engine._fan_active is False
        assert [c[0][2]["fan_mode"] for c in _fan_mode_calls(engine)][:1] == ["auto"]


class TestMinRuntimeCycleOffLeavesATakenOverFan:
    """Mirror: a min-runtime cycle's off phase must not stop a blower window cooling or nat-vent took over."""

    def test_economizer_took_the_fan_cycle_off_leaves_it_running(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        engine._fan_min_runtime_active = True

        asyncio.run(engine._fan_cycle_off())

        assert _fan_mode_calls(engine) == []
        assert engine._fan_active is True
        assert engine._fan_min_runtime_active is False
        assert engine.economizer_lifecycle_state is EconomizerLifecycleState.COOL_DOWN

    def test_nat_vent_took_the_fan_cycle_off_leaves_it_running(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        _thermostat(engine)
        engine._fan_active = True
        engine._fan_min_runtime_active = True
        engine._natural_vent_active = True

        asyncio.run(engine._fan_cycle_off())

        assert _fan_mode_calls(engine) == []
        assert engine._fan_active is True
        assert engine._natural_vent_active is True

    def test_nothing_took_it_cycle_off_stops_the_fan(self):
        """Control: the cycle's own run is stopped at its end, as before."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        _thermostat(engine)
        engine._fan_active = True
        engine._fan_min_runtime_active = True

        asyncio.run(engine._fan_cycle_off())

        assert [c[0][2]["fan_mode"] for c in _fan_mode_calls(engine)] == ["auto"]
        assert engine._fan_active is False
        assert engine._fan_min_runtime_active is False


class TestReconcileStopEndsTheEconomizerSession:
    """The reconcile's turn-off branch (``nat_vent_reconcile_exit``) and its fan-observed-off branch, real stops."""

    def test_executed_stop_ends_the_session(self):
        # Outdoor warmer than indoor: no nat-vent warrant, so the reconcile turns the running blower off.
        engine = _engine_in_cool_down(FAN_MODE_HVAC)

        _reconcile(engine, fan_running=True)

        assert [c[0][2]["fan_mode"] for c in _fan_mode_calls(engine)] == ["auto"]
        assert engine.economizer_lifecycle_state is EconomizerLifecycleState.INACTIVE

    def test_rate_limited_stop_keeps_the_session(self):
        # The toggle floor deferred the stop: the fan still runs, so the session stays recorded (the #1390 retry
        # re-runs the reconcile when the floor lets the command land).
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)
        engine._arm_deferred_reconcile_retry = MagicMock()

        _reconcile(engine, fan_running=True)

        engine._arm_deferred_reconcile_retry.assert_called_once()
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"

    def test_no_session_is_a_no_op(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        _thermostat(engine)

        _reconcile(engine, fan_running=True)

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_fan_observed_off_ends_the_session(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)

        _reconcile(engine, fan_running=False)

        assert engine._economizer_active is False


class TestEndedSessionRestartsTheFan:
    """The point of ending the session: the economizer's next tick sees INACTIVE -> COOL_DOWN and starts the fan."""

    def test_next_tick_restarts_the_blower(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        _reconcile(engine, fan_running=True)
        assert engine._economizer_active is False

        engine._activate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0, indoor_temp=78.0, windows_physically_open=True, current_hour=20
            )
        )

        assert result is True
        engine._activate_fan.assert_awaited_once()
        assert engine._economizer_phase == "cool-down"

    def test_stranded_session_never_restarts_the_blower(self):
        """Control (the #1705 symptom): a session left recorded with no fan is 'unchanged' and sends nothing."""
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._activate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0, indoor_temp=78.0, windows_physically_open=True, current_hour=20
            )
        )

        engine._activate_fan.assert_not_awaited()


class TestOtherFanOffRecordsEndTheSession:
    """Symmetry: every engine path that records the fan off outside the economizer ends its session."""

    def test_fan_flags_cleared_ends_the_session(self):
        # The occupant's fan-off and the drift correction both clear the fan through this helper.
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        engine._start_grace_period = MagicMock()

        engine._clear_fan_flags_and_start_grace(reason="test", trigger_label="fan_off")

        assert engine._fan_active is False
        assert engine._economizer_active is False

    def test_drift_correction_preserving_nat_vent_still_ends_the_economizer(self):
        engine = _engine_in_cool_down(FAN_MODE_WHOLE_HOUSE)
        engine._fan_active = True
        engine._start_grace_period = MagicMock()

        engine._clear_fan_flags_and_start_grace(
            reason="drift", trigger_label="physical_drift_correction", preserve_nat_vent_session=True
        )

        assert engine._economizer_active is False

    def test_rf_timer_settle_clear_ends_the_session(self):
        """A fan-off inside the RF-timer boundary settle window with no nat-vent session clears the flags directly
        (on_fan_turned_off's settle branch); the economizer session must end with them."""
        from datetime import timedelta  # noqa: PLC0415

        from homeassistant.util import dt as dt_util  # noqa: PLC0415

        engine = _engine_in_cool_down(FAN_MODE_WHOLE_HOUSE)
        engine._fan_active = True
        engine._timer_boundary_settle_until = dt_util.now() + timedelta(seconds=60)

        engine.on_fan_turned_off(fan_before="on", fan_after="off")

        assert engine._timer_boundary_settle_until is None  # the settle branch ran (consumed one-shot)
        assert engine._fan_active is False
        assert engine._economizer_active is False

    def test_ownership_self_heal_ends_the_session(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True

        engine.clear_stale_fan_ownership()

        assert engine._fan_active is False
        assert engine._economizer_active is False

    def test_economizer_session_active_view(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        assert engine.economizer_session_active is False
        engine._economizer_active = True
        engine._economizer_phase = "maintain"
        assert engine.economizer_session_active is True


class TestHvacOffKeepsTheEconomizerBlower:
    """Mirror of #134: CA's own Off (e.g. a door/window pause landing after window cooling started) must not stop
    the economizer's blower by asserting fan_mode=auto -- nothing would tell the economizer its fan stopped."""

    def test_economizer_session_skips_the_fan_auto_assert(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        _thermostat(engine, "cool")

        asyncio.run(engine._set_hvac_mode("off", reason="door or window opened"))

        assert _fan_mode_calls(engine) == []
        assert engine._economizer_active is True

    def test_no_session_still_asserts_fan_auto(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        _thermostat(engine, "cool")

        asyncio.run(engine._set_hvac_mode("off", reason="door or window opened"))

        calls = _fan_mode_calls(engine)
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "auto"
