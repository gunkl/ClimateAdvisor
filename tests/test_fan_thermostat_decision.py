"""Inline unit tests for the pure fan thermostatic stop check (architecture-reset Step 2).

Direct tests of decide_fan_thermostat_check() and its boundaries — mirrors the
test_nat_vent_gate.py pattern. Each boundary traces to the real production
comment/incident that motivated it (#327, #402), not an invented value.
"""

from __future__ import annotations

from custom_components.climate_advisor.fan_thermostat_decision import (
    FanThermostatInputs,
    FanThermostatOutcome,
    _resolve_vent_floor,
    decide_fan_thermostat_check,
    resolve_hard_exit_floor,
)

_BASE = {
    "indoor": 74.0,
    "outdoor": 70.0,
    "comfort_heat_raw": 70.0,
    "sleep_heat": 64.0,
    "in_sleep_window": False,
    "hysteresis": 1.0,
    "natural_vent_active": False,
}


def _inputs(**overrides) -> FanThermostatInputs:
    return FanThermostatInputs(**{**_BASE, **overrides})


class TestCheck1DirectionReversal:
    def test_favorable_direction_keeps(self):
        assert decide_fan_thermostat_check(_inputs(outdoor=70.0, indoor=74.0)) == FanThermostatOutcome.KEEP

    def test_boundary_equal_temps_stops(self):
        """Non-strict >=: outdoor == indoor already counts as reversed — unlike the
        reactivation gate's strict '<', this is deliberately a different boundary."""
        assert decide_fan_thermostat_check(_inputs(outdoor=74.0, indoor=74.0)) == FanThermostatOutcome.STOP_DEACTIVATE

    def test_no_hysteresis_on_stop_side(self):
        """Explicit production comment: subtracting hysteresis here would kill free
        cooling ~1F early — e.g. outdoor=71/indoor=72 must still stop, not be
        protected by the 1.0 hysteresis configured for the reactivation side."""
        assert (
            decide_fan_thermostat_check(_inputs(outdoor=71.0, indoor=72.0, hysteresis=1.0)) == FanThermostatOutcome.KEEP
        )
        assert (
            decide_fan_thermostat_check(_inputs(outdoor=72.0, indoor=72.0, hysteresis=1.0))
            == FanThermostatOutcome.STOP_DEACTIVATE
        )

    def test_reversal_during_nat_vent_routes_through_exit(self):
        """Issue #418: must route through the nat-vent exit path, not a plain
        deactivate, when a nat-vent session is active."""
        result = decide_fan_thermostat_check(_inputs(outdoor=75.0, indoor=74.0, natural_vent_active=True))
        assert result == FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT

    def test_reversal_without_nat_vent_deactivates_plainly(self):
        result = decide_fan_thermostat_check(_inputs(outdoor=75.0, indoor=74.0, natural_vent_active=False))
        assert result == FanThermostatOutcome.STOP_DEACTIVATE

    def test_none_outdoor_never_triggers_check1(self):
        assert decide_fan_thermostat_check(_inputs(outdoor=None, indoor=74.0)) == FanThermostatOutcome.KEEP

    def test_none_indoor_never_triggers_check1(self):
        assert decide_fan_thermostat_check(_inputs(outdoor=70.0, indoor=None)) == FanThermostatOutcome.KEEP


class TestCheck2CooledToFloor:
    def test_above_floor_keeps(self):
        assert (
            decide_fan_thermostat_check(_inputs(indoor=71.0, outdoor=65.0, comfort_heat_raw=70.0))
            == FanThermostatOutcome.KEEP
        )

    def test_boundary_at_floor_stops(self):
        """Non-strict <=: indoor exactly at the floor already stops."""
        assert (
            decide_fan_thermostat_check(_inputs(indoor=70.0, outdoor=65.0, comfort_heat_raw=70.0))
            == FanThermostatOutcome.STOP_COOLED_TO_FLOOR
        )

    def test_awake_floor_ignores_hysteresis(self):
        """Deliberate asymmetry: the awake branch does NOT subtract hysteresis from
        comfort_heat_raw (only the sleep branch subtracts it from sleep_heat)."""
        assert _resolve_vent_floor(_inputs(comfort_heat_raw=70.0, hysteresis=1.0, in_sleep_window=False)) == 70.0

    def test_sleep_window_uses_hysteresis_adjusted_sleep_floor(self):
        """Issue #402: the tick-level check must be sleep-aware — indoor sitting
        between sleep_heat and the flat daytime comfort_heat must NOT stop the fan
        prematurely during the sleep window, or it preempts nat_vent_temperature_check()'s
        correct sleep-window cycling before it ever runs."""
        floor = _resolve_vent_floor(_inputs(sleep_heat=64.0, hysteresis=1.0, in_sleep_window=True))
        assert floor == 63.0  # 64 - 1

        # Indoor at 67F: above the sleep-aware floor (63F) -> must KEEP running.
        assert (
            decide_fan_thermostat_check(
                _inputs(indoor=67.0, outdoor=60.0, sleep_heat=64.0, hysteresis=1.0, in_sleep_window=True)
            )
            == FanThermostatOutcome.KEEP
        )
        # Same indoor, same config, but AWAKE: comfort_heat_raw=70F floor -> must STOP.
        assert (
            decide_fan_thermostat_check(
                _inputs(
                    indoor=67.0,
                    outdoor=60.0,
                    comfort_heat_raw=70.0,
                    sleep_heat=64.0,
                    hysteresis=1.0,
                    in_sleep_window=False,
                )
            )
            == FanThermostatOutcome.STOP_COOLED_TO_FLOOR
        )

    def test_none_indoor_never_triggers_check2(self):
        assert decide_fan_thermostat_check(_inputs(indoor=None, outdoor=65.0)) == FanThermostatOutcome.KEEP


class TestResolveHardExitFloorConsolidation:
    """Issue #456: resolve_hard_exit_floor() is the single source of truth for the
    nat-vent hard-exit floor, consolidating this module's own _resolve_vent_floor(),
    automation.py's check_natural_vent_conditions() (formerly inline _vent_floor),
    and automation.py's nat_vent_temperature_check() (formerly inline _hard_floor).
    These tests reproduce the exact old inline formulas from both automation.py
    sites to prove the shared function is behavior-identical to what it replaced.
    """

    def _old_check_natural_vent_conditions_formula(
        self, comfort_heat: float, sleep_heat: float, hysteresis: float, in_sleep_window: bool
    ) -> float:
        """The exact pre-#456 inline formula from check_natural_vent_conditions()."""
        if in_sleep_window:
            return sleep_heat - hysteresis
        return comfort_heat

    def _old_nat_vent_temperature_check_formula(
        self, comfort_heat: float, sleep_heat: float, hysteresis: float, in_sleep_window: bool
    ) -> float:
        """The exact pre-#456 inline formula from nat_vent_temperature_check()."""
        if in_sleep_window:
            return sleep_heat - hysteresis
        return comfort_heat

    def test_matches_old_check_natural_vent_conditions_formula(self):
        for comfort_heat, sleep_heat, hysteresis, in_sleep_window in [
            (70.0, 64.0, 1.0, True),
            (70.0, 64.0, 1.0, False),
            (68.0, 60.0, 2.0, True),
            (68.0, 60.0, 2.0, False),
            (72.0, 72.0, 0.0, True),
        ]:
            expected = self._old_check_natural_vent_conditions_formula(
                comfort_heat, sleep_heat, hysteresis, in_sleep_window
            )
            actual = resolve_hard_exit_floor(
                comfort_heat_raw=comfort_heat,
                sleep_heat=sleep_heat,
                in_sleep_window=in_sleep_window,
                hysteresis=hysteresis,
            )
            assert actual == expected

    def test_matches_old_nat_vent_temperature_check_formula(self):
        for comfort_heat, sleep_heat, hysteresis, in_sleep_window in [
            (70.0, 64.0, 1.0, True),
            (70.0, 64.0, 1.0, False),
            (68.0, 60.0, 2.0, True),
            (68.0, 60.0, 2.0, False),
            (72.0, 72.0, 0.0, True),
        ]:
            expected = self._old_nat_vent_temperature_check_formula(
                comfort_heat, sleep_heat, hysteresis, in_sleep_window
            )
            actual = resolve_hard_exit_floor(
                comfort_heat_raw=comfort_heat,
                sleep_heat=sleep_heat,
                in_sleep_window=in_sleep_window,
                hysteresis=hysteresis,
            )
            assert actual == expected

    def test_resolve_vent_floor_delegates_to_resolve_hard_exit_floor(self):
        """_resolve_vent_floor() (this module's own consumer) must produce the
        identical value as calling resolve_hard_exit_floor() directly."""
        inputs = _inputs(comfort_heat_raw=70.0, sleep_heat=64.0, hysteresis=1.0, in_sleep_window=True)
        assert _resolve_vent_floor(inputs) == resolve_hard_exit_floor(
            comfort_heat_raw=inputs.comfort_heat_raw,
            sleep_heat=inputs.sleep_heat,
            in_sleep_window=inputs.in_sleep_window,
            hysteresis=inputs.hysteresis,
        )


class TestCheckOrdering:
    def test_check1_takes_priority_over_check2(self):
        """If both a direction reversal AND a below-floor condition are true
        simultaneously, Check 1 fires first (matches real code's if/return order)."""
        result = decide_fan_thermostat_check(
            _inputs(outdoor=80.0, indoor=65.0, comfort_heat_raw=70.0, natural_vent_active=False)
        )
        assert result == FanThermostatOutcome.STOP_DEACTIVATE  # Check 1's outcome, not Check 2's
