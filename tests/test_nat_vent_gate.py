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
    NatVentSoftStartGateInputs,
    _resolve_ceiling_threshold,
    _resolve_comfort_heat,
    decide_nat_vent_gate,
    decide_nat_vent_soft_start_gate,
    resolve_comfort_cool,
    resolve_comfort_heat,
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


class TestResolveComfortHeatStandalone:
    """Issue #535: resolve_comfort_heat() extracted as a dependency-free function
    so callers outside nat_vent_gate.py (briefing.py's forecast-curve scan) can
    resolve the sleep-aware comfort floor without constructing a full
    NatVentGateInputs. Must match _resolve_comfort_heat(inputs) exactly."""

    def test_daytime_uses_raw_comfort_heat(self):
        assert resolve_comfort_heat(comfort_heat_raw=70.0, sleep_heat=64.0, in_sleep_window=False) == 70.0

    def test_sleep_window_uses_sleep_heat(self):
        assert resolve_comfort_heat(comfort_heat_raw=70.0, sleep_heat=64.0, in_sleep_window=True) == 64.0

    def test_matches_inputs_shaped_wrapper(self):
        for in_sleep_window in (True, False):
            inputs = _inputs(in_sleep_window=in_sleep_window)
            assert resolve_comfort_heat(inputs.comfort_heat_raw, inputs.sleep_heat, in_sleep_window) == (
                _resolve_comfort_heat(inputs)
            )


class TestResolveComfortCoolStandalone:
    """Issue #786: cool-side counterpart to resolve_comfort_heat(), added for the TOU
    scheduler's pre-conditioning heat-banking target (drives to comfort_cool ahead of a
    high-cost heating window)."""

    def test_daytime_uses_raw_comfort_cool(self):
        assert resolve_comfort_cool(comfort_cool_raw=76.0, sleep_cool=72.0, in_sleep_window=False) == 76.0

    def test_sleep_window_uses_sleep_cool(self):
        assert resolve_comfort_cool(comfort_cool_raw=76.0, sleep_cool=72.0, in_sleep_window=True) == 72.0


class TestResolveCeilingThreshold:
    def test_whole_house_fan_returns_none(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_WHOLE_HOUSE)) is None

    def test_both_returns_none(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_BOTH)) is None

    def test_hvac_fan_returns_comfort_cool(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_HVAC)) == 76.0

    def test_aggressive_savings_adds_margin(self):
        assert _resolve_ceiling_threshold(_inputs(fan_mode=FAN_MODE_HVAC, aggressive_savings=True)) == 78.0


_SOFT_START_BASE = {
    "outdoor": 75.0,
    "indoor": 76.0,
    "comfort_heat": 70.0,
    "comfort_cool": 74.0,
    "fan_mode": FAN_MODE_WHOLE_HOUSE,
    "outdoor_today_peak": 90.0,
    "outdoor_sample_count": 5,
    "peak_decline_margin": 1.0,
    "full_gate_active": False,
}


def _soft_start_inputs(**overrides) -> NatVentSoftStartGateInputs:
    return NatVentSoftStartGateInputs(**{**_SOFT_START_BASE, **overrides})


class TestDecideNatVentSoftStartGate:
    """Issue #540 (scoped from #533): WHF purge/comfort soft-start at outdoor/indoor
    parity once today is confirmed past its peak and declining."""

    def test_activates_when_all_conditions_met(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs()) is True

    def test_none_outdoor_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor=None)) is False

    def test_none_indoor_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(indoor=None)) is False

    def test_full_gate_already_active_stands_down(self):
        """Soft-start never competes with the full bulk-cooling gate for the same activation."""
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(full_gate_active=True)) is False

    def test_hvac_only_fan_mode_blocks(self):
        """Soft-start is a WHF-purge claim — HVAC-only fan archetype does not qualify."""
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(fan_mode=FAN_MODE_HVAC)) is False

    def test_disabled_fan_mode_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(fan_mode=FAN_MODE_DISABLED)) is False

    def test_both_fan_mode_qualifies(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(fan_mode=FAN_MODE_BOTH)) is True

    def test_parity_boundary_equal_temps_activates(self):
        """Core ask: outdoor <= indoor, not the full gate's strict outdoor < indoor."""
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor=76.0, indoor=76.0)) is True

    def test_parity_boundary_outdoor_above_indoor_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor=76.1, indoor=76.0)) is False

    def test_floor_boundary_indoor_at_comfort_heat_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(indoor=70.0, outdoor=68.0)) is False

    def test_floor_boundary_indoor_at_comfort_cool_blocks(self):
        """Strict '>' on comfort_cool too — indoor must be above, not just above comfort_heat."""
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(indoor=74.0, outdoor=70.0)) is False

    def test_past_peak_margin_boundary_exactly_at_margin_blocks(self):
        """Strict '<' — outdoor exactly (peak - margin) is not yet 'declining'."""
        assert (
            decide_nat_vent_soft_start_gate(
                _soft_start_inputs(outdoor_today_peak=90.0, peak_decline_margin=1.0, outdoor=89.0, indoor=89.5)
            )
            is False
        )

    def test_past_peak_margin_boundary_just_past_activates(self):
        assert (
            decide_nat_vent_soft_start_gate(
                _soft_start_inputs(outdoor_today_peak=90.0, peak_decline_margin=1.0, outdoor=88.9, indoor=89.5)
            )
            is True
        )

    def test_thin_sample_buffer_fails_safe(self):
        """Issue #540 timezone/restart sanity check: a thin post-restart buffer must not
        produce a false 'already past peak' read."""
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor_sample_count=0)) is False
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor_sample_count=2)) is False
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor_sample_count=3)) is True

    def test_none_peak_blocks(self):
        assert decide_nat_vent_soft_start_gate(_soft_start_inputs(outdoor_today_peak=None)) is False
