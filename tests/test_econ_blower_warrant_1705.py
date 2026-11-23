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


def _engine_in_cool_down(fan_mode: str):
    engine = _make_automation_engine({CONF_FAN_MODE: fan_mode})
    engine._current_classification = _make_hot_classification()
    engine._economizer_active = True
    engine._economizer_phase = "cool-down"
    return engine


def _reconcile(engine, *, fan_running: bool, outdoor: float = 80.0) -> None:
    asyncio.run(
        engine.reconcile_fan_on_startup(
            indoor=75.0, outdoor=outdoor, thermostat_fan_running=fan_running, any_sensor_open=True
        )
    )


class TestReconcileStopEndsTheEconomizerSession:
    """The reconcile's turn-off branch (``automation.py`` ``nat_vent_reconcile_exit``) stopping the economizer's fan."""

    def test_executed_stop_ends_the_session(self):
        # Outdoor warmer than indoor: no nat-vent warrant, so the reconcile turns the running fan off.
        engine = _engine_in_cool_down(FAN_MODE_WHOLE_HOUSE)
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.EXECUTED)

        _reconcile(engine, fan_running=True)

        engine._exit_nat_vent.assert_awaited_once()
        assert engine.economizer_lifecycle_state is EconomizerLifecycleState.INACTIVE
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_rate_limited_stop_keeps_the_session(self):
        # The toggle floor deferred the stop: the fan still runs, so the session stays recorded (the #1390 retry
        # re-runs the reconcile when the floor lets the command land).
        engine = _engine_in_cool_down(FAN_MODE_WHOLE_HOUSE)
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)
        engine._arm_deferred_reconcile_retry = MagicMock()

        _reconcile(engine, fan_running=True)

        engine._arm_deferred_reconcile_retry.assert_called_once()
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"

    def test_no_session_is_a_no_op(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.EXECUTED)

        _reconcile(engine, fan_running=True)

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_fan_observed_off_ends_the_session(self):
        # The no-fan branch: the reconcile reads the fan off while the economizer still claims it.
        engine = _engine_in_cool_down(FAN_MODE_WHOLE_HOUSE)
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.ALREADY_IN_STATE)

        _reconcile(engine, fan_running=False)

        engine._deactivate_fan.assert_awaited_once()
        assert engine._economizer_active is False


class TestEndedSessionRestartsTheFan:
    """The point of ending the session: the economizer's next tick sees INACTIVE -> COOL_DOWN and starts the fan."""

    def test_next_tick_restarts_the_blower(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.EXECUTED)
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

    @staticmethod
    def _fan_mode_calls(engine):
        return [c for c in engine.hass.services.async_call.call_args_list if c[0][1] == "set_fan_mode"]

    @staticmethod
    def _thermostat_in_cool(engine):
        state = MagicMock()
        state.state = "cool"
        state.attributes = {"fan_modes": ["auto", "on"], "hvac_modes": ["off", "heat", "cool"]}
        engine.hass.states.get.return_value = state

    def test_economizer_session_skips_the_fan_auto_assert(self):
        engine = _engine_in_cool_down(FAN_MODE_HVAC)
        engine._fan_active = True
        self._thermostat_in_cool(engine)

        asyncio.run(engine._set_hvac_mode("off", reason="door or window opened"))

        assert self._fan_mode_calls(engine) == []
        assert engine._economizer_active is True

    def test_no_session_still_asserts_fan_auto(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        self._thermostat_in_cool(engine)

        asyncio.run(engine._set_hvac_mode("off", reason="door or window opened"))

        calls = self._fan_mode_calls(engine)
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "auto"
