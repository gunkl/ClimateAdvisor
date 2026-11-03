"""Pure decision tests for the WHF safety enforcer (Issue #1144). No mocks: the leaf has no dependencies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.whf_safety_decision import (
    SensorReading,
    WhfSafetyAction,
    WhfSafetyInputs,
    decide_whf_safety,
)

T0 = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
CLOSED = SensorReading(is_open=False, stable_for_s=3600.0)
OPEN = SensorReading(is_open=True, stable_for_s=3600.0)


def _inputs(**kw) -> WhfSafetyInputs:
    base = {
        "fan_mode": "whole_house_fan",
        "fan_on": True,
        "fan_believed_on": False,
        "fan_on_for_s": 120.0,
        "sensors": (CLOSED,),
        "sealed_confirmed": False,
        "thermostat_mode": "off",
        "occupant_fan_on_at": None,
        "occupant_hvac_on_at": None,
        "whf_owns_hvac": False,
    }
    base.update(kw)
    return WhfSafetyInputs(**base)


class TestRule1Sealed:
    def test_running_fan_on_sealed_house_is_stopped(self):
        assert decide_whf_safety(_inputs()).action is WhfSafetyAction.STOP_SEALED

    def test_open_window_allows_the_fan(self):
        assert decide_whf_safety(_inputs(sensors=(CLOSED, OPEN))).action is WhfSafetyAction.NONE

    def test_fan_off_is_nothing_to_do(self):
        assert decide_whf_safety(_inputs(fan_on=False)).action is WhfSafetyAction.NONE

    def test_zero_sensors_rule_does_not_apply(self):
        # Issue #1200: a whole-house fan with zero door/window sensors is an unsupported configuration;
        # this pins today's unchanged behaviour, not a supported promise.
        assert decide_whf_safety(_inputs(sensors=())).action is WhfSafetyAction.NONE

    def test_not_a_whf_install(self):
        assert decide_whf_safety(_inputs(fan_mode="hvac_fan")).action is WhfSafetyAction.NONE
        assert decide_whf_safety(_inputs(fan_mode="disabled")).action is WhfSafetyAction.NONE

    def test_both_mode_is_covered(self):
        assert decide_whf_safety(_inputs(fan_mode="both")).action is WhfSafetyAction.STOP_SEALED

    def test_settle_defers_a_fan_that_just_came_on(self):
        d = decide_whf_safety(_inputs(fan_on_for_s=3.0))
        assert d.action is WhfSafetyAction.DEFER
        assert d.retry_in_s == 7.0

    def test_settle_over_stops(self):
        assert decide_whf_safety(_inputs(fan_on_for_s=10.0)).action is WhfSafetyAction.STOP_SEALED

    def test_fresh_close_is_confirmed_before_stopping(self):
        fresh = SensorReading(is_open=False, stable_for_s=4.0)
        d = decide_whf_safety(_inputs(sensors=(fresh,)))
        assert d.action is WhfSafetyAction.DEFER and d.retry_in_s == 6.0

    def test_debounced_close_event_skips_the_confirm(self):
        fresh = SensorReading(is_open=False, stable_for_s=0.5)
        d = decide_whf_safety(_inputs(sensors=(fresh,), sealed_confirmed=True))
        assert d.action is WhfSafetyAction.STOP_SEALED

    def test_unknown_sensor_counts_open_for_60s(self):
        s = SensorReading(is_open=False, unknown_for_s=59.0)
        assert decide_whf_safety(_inputs(sensors=(s,))).action is WhfSafetyAction.NONE

    def test_unknown_sensor_counts_closed_after_60s_and_is_reported(self):
        s = SensorReading(is_open=False, unknown_for_s=61.0, stable_for_s=61.0)
        d = decide_whf_safety(_inputs(sensors=(s,)))
        assert d.action is WhfSafetyAction.STOP_SEALED
        assert d.stale_sensor_count == 1

    def test_unknown_plus_open_sensor_keeps_the_fan(self):
        s = SensorReading(is_open=False, unknown_for_s=500.0)
        assert decide_whf_safety(_inputs(sensors=(s, OPEN))).action is WhfSafetyAction.NONE

    def test_unknown_physical_state_falls_back_to_believed_flags(self):
        assert decide_whf_safety(_inputs(fan_on=None, fan_believed_on=False)).action is WhfSafetyAction.NONE
        d = decide_whf_safety(_inputs(fan_on=None, fan_believed_on=True, fan_on_for_s=None))
        assert d.action is WhfSafetyAction.STOP_SEALED


class TestRule2Mutex:
    OPEN_HOUSE = (OPEN,)

    def test_newer_occupant_hvac_stops_the_fan(self):
        d = decide_whf_safety(
            _inputs(
                sensors=self.OPEN_HOUSE,
                thermostat_mode="heat",
                occupant_fan_on_at=T0,
                occupant_hvac_on_at=T0 + timedelta(minutes=5),
            )
        )
        assert d.action is WhfSafetyAction.STOP_MUTEX_FAN

    def test_newer_occupant_fan_suppresses_hvac(self):
        d = decide_whf_safety(
            _inputs(
                sensors=self.OPEN_HOUSE,
                thermostat_mode="cool",
                occupant_fan_on_at=T0 + timedelta(minutes=5),
                occupant_hvac_on_at=T0,
            )
        )
        assert d.action is WhfSafetyAction.SUPPRESS_HVAC

    def test_fan_only_occupant_action_suppresses(self):
        d = decide_whf_safety(_inputs(sensors=self.OPEN_HOUSE, thermostat_mode="heat_cool", occupant_fan_on_at=T0))
        assert d.action is WhfSafetyAction.SUPPRESS_HVAC

    def test_hvac_only_occupant_action_stops_the_fan(self):
        d = decide_whf_safety(_inputs(sensors=self.OPEN_HOUSE, thermostat_mode="heat", occupant_hvac_on_at=T0))
        assert d.action is WhfSafetyAction.STOP_MUTEX_FAN

    def test_ca_only_commands_are_never_occupant_actions(self):
        d = decide_whf_safety(_inputs(sensors=self.OPEN_HOUSE, thermostat_mode="heat"))
        assert d.action is WhfSafetyAction.NONE

    def test_hvac_off_is_no_conflict(self):
        d = decide_whf_safety(_inputs(sensors=self.OPEN_HOUSE, occupant_fan_on_at=T0, occupant_hvac_on_at=T0))
        assert d.action is WhfSafetyAction.NONE

    def test_already_suppressing_does_not_suppress_again(self):
        d = decide_whf_safety(
            _inputs(sensors=self.OPEN_HOUSE, thermostat_mode="cool", occupant_fan_on_at=T0, whf_owns_hvac=True)
        )
        assert d.action is WhfSafetyAction.NONE

    def test_sealed_rule_outranks_the_mutex(self):
        d = decide_whf_safety(_inputs(thermostat_mode="heat", occupant_hvac_on_at=T0))
        assert d.action is WhfSafetyAction.STOP_SEALED


class TestHouseIsSealed:
    """The one definition of a sealed house, shared by the stop decision and Climate Advisor's own start gate."""

    def test_no_sensors_is_never_sealed(self):
        from custom_components.climate_advisor.whf_safety_decision import house_is_sealed

        assert house_is_sealed(()) is False

    def test_all_closed_is_sealed_and_any_open_is_not(self):
        from custom_components.climate_advisor.whf_safety_decision import SensorReading, house_is_sealed

        closed, opened = SensorReading(is_open=False), SensorReading(is_open=True)
        assert house_is_sealed((closed, closed)) is True
        assert house_is_sealed((closed, opened)) is False

    def test_an_unreadable_sensor_counts_open_for_sixty_seconds_then_closed(self):
        from custom_components.climate_advisor.whf_safety_decision import SensorReading, house_is_sealed

        assert house_is_sealed((SensorReading(is_open=False, unknown_for_s=30.0),)) is False
        assert house_is_sealed((SensorReading(is_open=False, unknown_for_s=61.0),)) is True


def _overlap(**kw) -> WhfSafetyInputs:
    """Issue #1288: the fan reads on with a window open and the thermostat heats beside it; no press stamps at all."""
    base = {
        "sensors": (OPEN,),
        "thermostat_mode": "heat",
        "hvac_action": "heating",
        "overlap_for_s": 12.0,
        "fan_on_for_s": 120.0,
        "thermostat_mode_for_s": 30.0,
        "whf_owns_hvac": True,
    }
    base.update(kw)
    return _inputs(**base)


class TestRule2StateBackstop:
    """Issue #1288: a missed heat/AC press must not leave the fan and the heating/cooling running together."""

    def test_below_the_overlap_threshold_it_waits_and_asks_again_in_time(self):
        d = decide_whf_safety(_overlap(overlap_for_s=4.0))
        assert d.action is WhfSafetyAction.DEFER and d.backstop
        assert d.retry_in_s == 6.0

    def test_heating_pressed_after_the_fan_stops_the_fan(self):
        d = decide_whf_safety(_overlap(thermostat_mode_for_s=30.0, fan_on_for_s=120.0))
        assert d.action is WhfSafetyAction.STOP_MUTEX_FAN and d.backstop

    def test_a_fan_that_came_on_after_the_heating_suppresses_the_heating_even_when_already_owned(self):
        d = decide_whf_safety(_overlap(thermostat_mode_for_s=600.0, fan_on_for_s=120.0, whf_owns_hvac=True))
        assert d.action is WhfSafetyAction.SUPPRESS_HVAC and d.backstop

    def test_unknown_ages_suppress_the_hvac(self):
        d = decide_whf_safety(_overlap(thermostat_mode_for_s=None, fan_on_for_s=None))
        assert d.action is WhfSafetyAction.SUPPRESS_HVAC

    def test_cooling_counts_too(self):
        d = decide_whf_safety(_overlap(thermostat_mode="cool", hvac_action="cooling"))
        assert d.action is WhfSafetyAction.STOP_MUTEX_FAN

    def test_a_stop_in_flight_is_not_decided_again(self):
        d = decide_whf_safety(_overlap(stop_in_flight_for_s=5.0))
        assert d.action is WhfSafetyAction.NONE and d.in_flight

    def test_climate_advisors_own_recent_off_is_given_time_to_land(self):
        d = decide_whf_safety(_overlap(own_hvac_off_for_s=3.0))
        assert d.action is WhfSafetyAction.DEFER and d.retry_in_s == 7.0
        assert decide_whf_safety(_overlap(own_hvac_off_for_s=10.0)).action is WhfSafetyAction.STOP_MUTEX_FAN

    def test_a_command_only_fan_is_blind_by_design(self):
        d = decide_whf_safety(_overlap(fan_on=None, fan_believed_on=True))
        assert d.action is WhfSafetyAction.NONE

    def test_idle_or_blower_only_is_no_overlap(self):
        for action in ("idle", "fan", "off", None):
            assert decide_whf_safety(_overlap(hvac_action=action)).action is WhfSafetyAction.NONE, action

    def test_a_thermostat_in_off_or_fan_only_is_no_overlap_even_if_it_reports_heating(self):
        for mode in ("off", "fan_only"):
            assert decide_whf_safety(_overlap(thermostat_mode=mode)).action is WhfSafetyAction.NONE, mode

    def test_no_overlap_clock_is_no_overlap(self):
        assert decide_whf_safety(_overlap(overlap_for_s=None)).action is WhfSafetyAction.NONE

    def test_the_sealed_house_rule_still_comes_first(self):
        assert decide_whf_safety(_overlap(sensors=(CLOSED,))).action is WhfSafetyAction.STOP_SEALED

    def test_a_press_stamp_still_decides_first(self):
        # The occupant's own fan press is newer than heat: the press-based rule suppresses, not the backstop.
        d = decide_whf_safety(_overlap(whf_owns_hvac=False, occupant_fan_on_at=T0))
        assert d.action is WhfSafetyAction.SUPPRESS_HVAC and not d.backstop

    def test_both_mode_is_covered(self):
        assert decide_whf_safety(_overlap(fan_mode="both")).action is WhfSafetyAction.STOP_MUTEX_FAN
