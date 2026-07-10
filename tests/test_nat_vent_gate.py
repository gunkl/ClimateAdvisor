"""Inline unit tests for the pure nat-vent reactivation gate (architecture-reset Step 2).

Direct tests of decide_nat_vent_gate() and its helpers — the "proper inline code
testing" the plan called for, as opposed to relying solely on the differential
harness. Mirrors the existing test_temperature.py pattern for free_cooling_direction_ok().
"""

from __future__ import annotations

from custom_components.climate_advisor.nat_vent_gate import (
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    NatVentGateInputs,
    _resolve_ceiling_threshold,
    _resolve_comfort_heat,
    decide_nat_vent_gate,
)

_BASE = {
    "outdoor": 70.0,
    "indoor": 76.0,
    "comfort_heat_raw": 70.0,
    "sleep_heat": 64.0,
    "in_sleep_window": False,
    "comfort_cool": 76.0,
    "nat_vent_delta": 3.0,
    "hysteresis": 0.0,
    "fan_mode": FAN_MODE_HVAC,
    "aggressive_savings": False,
}


def _inputs(**overrides) -> NatVentGateInputs:
    return NatVentGateInputs(**{**_BASE, **overrides})


class TestDecideNatVentGate:
    def test_activates_when_all_four_conditions_met(self):
        assert decide_nat_vent_gate(_inputs(outdoor=70.0, indoor=76.0)) is True

    def test_none_outdoor_blocks(self):
        assert decide_nat_vent_gate(_inputs(outdoor=None)) is False

    def test_none_indoor_blocks(self):
        assert decide_nat_vent_gate(_inputs(indoor=None)) is False

    def test_direction_boundary_equal_temps_blocks(self):
        """Strict '<' — outdoor == indoor is not favorable (mirrors free_cooling_direction_ok)."""
        assert decide_nat_vent_gate(_inputs(outdoor=76.0, indoor=76.0)) is False

    def test_direction_boundary_one_tenth_degree_favorable(self):
        assert decide_nat_vent_gate(_inputs(outdoor=75.9, indoor=76.0)) is True

    def test_hysteresis_shifts_direction_boundary(self):
        """outdoor < indoor - hysteresis: a 1F hysteresis requires outdoor 1F below indoor."""
        assert decide_nat_vent_gate(_inputs(outdoor=75.5, indoor=76.0, hysteresis=1.0)) is False
        assert decide_nat_vent_gate(_inputs(outdoor=74.9, indoor=76.0, hysteresis=1.0)) is True

    def test_floor_boundary_indoor_at_comfort_heat_blocks(self):
        """Strict '>' on the floor — indoor == comfort_heat is not above the floor."""
        assert decide_nat_vent_gate(_inputs(indoor=70.0, outdoor=65.0)) is False

    def test_floor_boundary_just_above_activates(self):
        assert decide_nat_vent_gate(_inputs(indoor=70.1, outdoor=65.0)) is True

    def test_sleep_window_uses_sleep_heat_floor(self):
        """Issue #417 — indoor between sleep_heat and comfort_heat must activate during sleep."""
        assert decide_nat_vent_gate(_inputs(indoor=67.0, outdoor=60.0, in_sleep_window=False)) is False
        assert decide_nat_vent_gate(_inputs(indoor=67.0, outdoor=60.0, in_sleep_window=True)) is True

    def test_threshold_boundary_outdoor_at_threshold_blocks(self):
        """threshold = comfort_cool(76) + nat_vent_delta(3) = 79; strict '<'.
        fan_mode=WHOLE_HOUSE isolates this from the (separate) ceiling gate."""
        assert decide_nat_vent_gate(_inputs(outdoor=79.0, indoor=80.0, fan_mode=FAN_MODE_WHOLE_HOUSE)) is False

    def test_threshold_boundary_just_under_activates(self):
        assert decide_nat_vent_gate(_inputs(outdoor=78.9, indoor=80.0, fan_mode=FAN_MODE_WHOLE_HOUSE)) is True

    def test_whole_house_fan_has_no_ceiling_gate(self):
        """Issue #392 — WHF archetype: ceiling never blocks, only direction matters."""
        assert decide_nat_vent_gate(_inputs(indoor=90.0, outdoor=70.0, fan_mode=FAN_MODE_WHOLE_HOUSE)) is True
        assert decide_nat_vent_gate(_inputs(indoor=90.0, outdoor=70.0, fan_mode=FAN_MODE_BOTH)) is True

    def test_hvac_fan_ceiling_blocks_above_comfort_cool(self):
        assert decide_nat_vent_gate(_inputs(indoor=76.1, outdoor=70.0, fan_mode=FAN_MODE_HVAC)) is False

    def test_hvac_fan_ceiling_boundary_at_comfort_cool_allows(self):
        """Non-strict '<=' on the ceiling check."""
        assert decide_nat_vent_gate(_inputs(indoor=76.0, outdoor=70.0, fan_mode=FAN_MODE_HVAC)) is True

    def test_aggressive_savings_widens_ceiling(self):
        assert decide_nat_vent_gate(_inputs(indoor=77.5, outdoor=70.0, aggressive_savings=False)) is False
        assert decide_nat_vent_gate(_inputs(indoor=77.5, outdoor=70.0, aggressive_savings=True)) is True

    def test_fan_disabled_does_not_affect_the_gate_itself(self):
        """FAN_MODE_DISABLED isn't special-cased in the gate — the caller (_activate_fan)
        is what no-ops for disabled; the gate is a pure eligibility question."""
        assert decide_nat_vent_gate(_inputs(fan_mode=FAN_MODE_DISABLED)) is True


class TestResolveComfortHeat:
    def test_daytime_uses_raw_comfort_heat(self):
        assert _resolve_comfort_heat(_inputs(in_sleep_window=False)) == 70.0

    def test_sleep_window_uses_sleep_heat(self):
        assert _resolve_comfort_heat(_inputs(in_sleep_window=True)) == 64.0


class TestResolveCeilingThreshold:
    def test_whole_house_fan_returns_none(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_WHOLE_HOUSE)) is None

    def test_both_returns_none(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_BOTH)) is None

    def test_hvac_fan_returns_comfort_cool(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_HVAC)) == 76.0

    def test_aggressive_savings_adds_margin(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_HVAC, aggressive_savings=True)) == 78.0
