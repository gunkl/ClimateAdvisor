"""Issue #1225 follow-ups to #1142 (REAL ``AutomationEngine``, state-following fake thermostat).

* 1  A quiet pause promises a fan session will take over. When the blower start is refused at runtime
     (restriction, fan-off hold, ...) the occupant must still be told the HVAC was paused.
* 2  No "close the windows" push while away or on vacation.
* 3  "Once per session" is re-armed when the NEXT session starts, however the last one ended.
* 5  After an Off-hold at a nat-vent exit nothing is left to restore.
* -  A blower session with every window shut does not keep the thermostat Off.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.climate_advisor.const import CONF_HVAC_FAN_RESTRICT_MODE
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
from tests.test_open_window_decision_owner_1142 import (
    _DAY,
    _at,
    _blower_engine,
    _notes,
    _open,
    _tick,
    _warm_up,
)


class TestQuietPauseOnlyWhenTheFanCanStart:
    def test_blower_restriction_keeps_the_pause_loud(self):
        # restrict "heat": the blower never runs while the thermostat is cooling, so the FSM's promise is empty.
        engine = _blower_engine(mode="cool", **{CONF_HVAC_FAN_RESTRICT_MODE: "heat"})
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True
        assert notify.called, "the blower cannot start, so the occupant is told the HVAC paused"

    def test_fan_off_hold_keeps_the_pause_loud(self):
        engine = _blower_engine(mode="cool")
        engine._fan_off_hold_until = _DAY + timedelta(minutes=30)
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True
        assert notify.called

    def test_control_unrestricted_blower_stays_quiet(self):
        engine = _blower_engine(mode="cool")
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True
        assert not notify.called, "the fan takes over on the next check"


class TestCloseWindowsAdviceAndReset:
    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_no_advice_while_away_or_on_vacation(self, occupancy):
        engine = _blower_engine()
        engine._occupancy_mode = occupancy
        notify = _notes(engine)
        _open(engine)
        _warm_up(engine)
        _tick(engine, 60)
        assert not notify.called, "closing the windows only re-arms the setback while nobody is home"

    def test_session_that_ends_without_the_reset_site_still_advises_next_time(self):
        engine = _blower_engine()
        notify = _notes(engine)
        _open(engine)
        _warm_up(engine)
        _tick(engine, 60)
        assert notify.await_count == 1
        # An end path that never touched the flag (occupant fan-off / reconcile no-fan / FSM writer).
        engine._natural_vent_active = False
        engine._apply_nat_vent_fsm_state(NatVentLifecycleState.ACTIVE_FULL_GATE)
        assert engine._close_windows_advised is False, "reset on the session-START edge"
        _tick(engine, 120)
        assert notify.await_count == 2

    def test_a_running_session_is_not_re_armed_by_a_repeat_write(self):
        engine = _blower_engine()
        _notes(engine)
        _open(engine)
        engine._close_windows_advised = True
        engine._apply_nat_vent_fsm_state(NatVentLifecycleState.ACTIVE_FULL_GATE)
        assert engine._close_windows_advised is True


class TestBlowerSessionWithNoOpenWindow:
    def test_thermostat_is_not_held_off_with_every_window_shut(self):
        engine = _blower_engine()
        _open(engine)
        assert engine._natural_vent_active
        engine._sensor_check_callback = lambda: False
        engine._test_state.state = "cool"
        _at(lambda: engine._apply_nat_vent_hvac_state(), 120)
        assert engine._test_state.state == "cool", "no window to hold Off for"

    def test_thermostat_is_held_off_while_a_window_is_open(self):
        engine = _blower_engine()
        _open(engine)
        engine._test_state.state = "cool"
        _at(lambda: engine._apply_nat_vent_hvac_state(), 120)
        assert engine._test_state.state == "off"

    def test_window_still_inside_its_delay_is_not_held_off(self):
        engine = _blower_engine()
        _open(engine)
        engine._sensor_debounce_pending_callback = lambda: True
        engine._test_state.state = "cool"
        _at(lambda: engine._apply_nat_vent_hvac_state(), 120)
        assert engine._test_state.state == "cool"


class TestExitOffHoldRecordsNothingToRestore:
    def test_pre_pause_mode_is_none_even_if_the_thermostat_state_lags(self):
        engine = _blower_engine(mode="cool")
        _open(engine)
        assert engine._natural_vent_active
        engine._test_state.state = "cool"  # armed again during the session
        real_set = engine._set_hvac_mode
        lagging_state = engine._test_state

        async def _lagging(mode, **kw):
            result = await real_set(mode, **kw)
            lagging_state.state = "cool"  # a cloud thermostat still reports the old mode
            return result

        engine._set_hvac_mode = _lagging
        _at(lambda: engine._exit_nat_vent(reason="test exit", set_outdoor_exit_time=True), 300)
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode is None, "the hold wrote Off: nothing to replay on close"
