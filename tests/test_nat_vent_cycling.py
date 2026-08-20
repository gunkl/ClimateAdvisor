"""Tests for Issue #698 (Epic #594 Phase R, Phase 2d): pure nat-vent mid-session
cycling decision.

Direct unit coverage of ``decide_nat_vent_cycling()`` — the two threshold
branches (cycle-off, cycle-on), the outdoor-warm reactivation guard (Decision
3's ``is_outdoor_rise_exit()`` delegation), the "no threshold crossed" hold
state, and the missing-indoor-reading defensive fallback. Mirrors
``test_nat_vent_exit.py``'s structure and conventions for the sibling pure
module.
"""

from __future__ import annotations

from custom_components.climate_advisor.nat_vent_cycling import (
    NatVentCyclingInputs,
    decide_nat_vent_cycling,
)


def _inputs(
    *,
    indoor: float | None = 72.0,
    outdoor: float | None = 65.0,
    comfort_heat_raw: float = 68.0,
    sleep_heat: float = 68.0,
    in_sleep_window: bool = False,
    comfort_cool: float = 76.0,
    hysteresis: float = 1.0,
    fan_hardware_active: bool = True,
) -> NatVentCyclingInputs:
    return NatVentCyclingInputs(
        indoor=indoor,
        outdoor=outdoor,
        comfort_heat_raw=comfort_heat_raw,
        sleep_heat=sleep_heat,
        in_sleep_window=in_sleep_window,
        comfort_cool=comfort_cool,
        hysteresis=hysteresis,
        fan_hardware_active=fan_hardware_active,
    )


class TestThresholdMath:
    def test_daytime_target_is_comfort_midpoint(self) -> None:
        # comfort_heat=68, comfort_cool=76 -> target=72; off=71, on=73.
        decision = decide_nat_vent_cycling(
            _inputs(comfort_heat_raw=68.0, comfort_cool=76.0, hysteresis=1.0, in_sleep_window=False, indoor=72.0)
        )
        assert decision.off_threshold == 71.0
        assert decision.on_threshold == 73.0

    def test_sleep_window_target_is_sleep_heat_plus_hysteresis(self) -> None:
        # sleep_heat=65, hysteresis=1 -> target=66; off=65, on=67 (matches
        # nat_vent_temperature_check()'s own sleep-window comment example).
        decision = decide_nat_vent_cycling(_inputs(sleep_heat=65.0, hysteresis=1.0, in_sleep_window=True, indoor=66.0))
        assert decision.off_threshold == 65.0
        assert decision.on_threshold == 67.0


class TestCycleOff:
    def test_fan_active_and_indoor_at_off_threshold_cycles_off(self) -> None:
        # off_threshold = 71.0
        decision = decide_nat_vent_cycling(_inputs(indoor=71.0, fan_hardware_active=True))
        assert decision.fan_should_be_active is False

    def test_fan_active_and_indoor_below_off_threshold_cycles_off(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=69.0, fan_hardware_active=True))
        assert decision.fan_should_be_active is False

    def test_fan_already_inactive_at_off_threshold_stays_inactive(self) -> None:
        # Fan is already off -- no cycle-off transition needed, just holds state.
        decision = decide_nat_vent_cycling(_inputs(indoor=69.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is False


class TestCycleOn:
    def test_fan_inactive_and_indoor_at_on_threshold_cycles_on(self) -> None:
        # on_threshold = 73.0; outdoor 65 < indoor 73, no outdoor-rise block.
        decision = decide_nat_vent_cycling(_inputs(indoor=73.0, outdoor=65.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is True
        assert decision.outdoor_rise_blocked is False

    def test_fan_inactive_and_indoor_above_on_threshold_cycles_on(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=75.0, outdoor=65.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is True

    def test_fan_already_active_at_on_threshold_stays_active(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=75.0, outdoor=65.0, fan_hardware_active=True))
        assert decision.fan_should_be_active is True


class TestOutdoorWarmGuardBlocksReactivation:
    def test_outdoor_equal_indoor_blocks_reactivation(self) -> None:
        # Non-strict >= boundary, delegated to is_outdoor_rise_exit() (Decision 3).
        decision = decide_nat_vent_cycling(_inputs(indoor=73.0, outdoor=73.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is False
        assert decision.outdoor_rise_blocked is True

    def test_outdoor_above_indoor_blocks_reactivation(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=73.0, outdoor=80.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is False
        assert decision.outdoor_rise_blocked is True

    def test_outdoor_below_indoor_does_not_block(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=73.0, outdoor=72.9, fan_hardware_active=False))
        assert decision.fan_should_be_active is True
        assert decision.outdoor_rise_blocked is False

    def test_outdoor_none_does_not_block(self) -> None:
        # is_outdoor_rise_exit() returns False when outdoor is None -- matches
        # legacy's own `outdoor is not None and outdoor >= current_temp` guard,
        # which also never blocks reactivation on missing outdoor data.
        decision = decide_nat_vent_cycling(_inputs(indoor=73.0, outdoor=None, fan_hardware_active=False))
        assert decision.fan_should_be_active is True
        assert decision.outdoor_rise_blocked is False

    def test_off_threshold_guard_ignores_outdoor_even_when_outdoor_warm(self) -> None:
        # The outdoor-warm guard only applies to the ON-direction (reactivation)
        # branch -- cycling off never consults outdoor at all, matching legacy.
        decision = decide_nat_vent_cycling(_inputs(indoor=69.0, outdoor=90.0, fan_hardware_active=True))
        assert decision.fan_should_be_active is False
        assert decision.outdoor_rise_blocked is False


class TestHoldState:
    def test_no_threshold_crossed_fan_off_stays_off(self) -> None:
        # Between off(71) and on(73): fan currently off, indoor 72 -- neither
        # threshold crossed, fan holds its current (off) state.
        decision = decide_nat_vent_cycling(_inputs(indoor=72.0, fan_hardware_active=False))
        assert decision.fan_should_be_active is False

    def test_no_threshold_crossed_fan_on_stays_on(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=72.0, fan_hardware_active=True))
        assert decision.fan_should_be_active is True


class TestMissingIndoor:
    def test_indoor_none_holds_current_fan_state_active(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=None, fan_hardware_active=True))
        assert decision.fan_should_be_active is True

    def test_indoor_none_holds_current_fan_state_inactive(self) -> None:
        decision = decide_nat_vent_cycling(_inputs(indoor=None, fan_hardware_active=False))
        assert decision.fan_should_be_active is False
