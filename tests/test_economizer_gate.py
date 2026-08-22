"""Inline unit tests for the pure economizer eligibility/phase gate
(strangler-fig completion program, Phase 5, Issue #746).

Direct tests of decide_economizer_transition() — mirrors test_nat_vent_gate.py's
pattern.
"""

from __future__ import annotations

from custom_components.climate_advisor.economizer_gate import (
    PHASE_COOL_DOWN,
    PHASE_INACTIVE,
    PHASE_MAINTAIN,
    EconomizerGateInputs,
    decide_economizer_transition,
)

_BASE = {
    "outdoor": 70.0,
    "indoor": 78.0,
    "comfort_cool": 76.0,
    "delta": 3.0,
    "windows_physically_open": True,
    "in_window": True,
    "aggressive_savings": False,
}


def _inputs(**overrides) -> EconomizerGateInputs:
    return EconomizerGateInputs(**{**_BASE, **overrides})


class TestEligibility:
    def test_eligible_cool_down_when_indoor_above_comfort(self):
        decision = decide_economizer_transition(_inputs())
        assert decision.eligible is True
        assert decision.phase == PHASE_COOL_DOWN
        assert decision.direction_ok is True

    def test_windows_closed_blocks(self):
        decision = decide_economizer_transition(_inputs(windows_physically_open=False))
        assert decision.eligible is False
        assert decision.phase == PHASE_INACTIVE

    def test_outdoor_above_comfort_plus_delta_blocks(self):
        decision = decide_economizer_transition(_inputs(outdoor=80.0))
        assert decision.eligible is False

    def test_outdoor_at_comfort_plus_delta_boundary_is_eligible(self):
        # comfort_cool=76, delta=3 -> boundary 79.0, uses <=. indoor raised above
        # outdoor so the free-cooling direction guard doesn't also reject this.
        decision = decide_economizer_transition(_inputs(outdoor=79.0, indoor=85.0))
        assert decision.eligible is True

    def test_out_of_time_window_blocks(self):
        decision = decide_economizer_transition(_inputs(in_window=False))
        assert decision.eligible is False

    def test_direction_not_ok_blocks(self):
        # outdoor >= indoor -> direction rejected
        decision = decide_economizer_transition(_inputs(outdoor=79.0, indoor=70.0))
        assert decision.direction_ok is False
        assert decision.eligible is False

    def test_indoor_none_direction_ok_fails_open(self):
        decision = decide_economizer_transition(_inputs(indoor=None, outdoor=70.0))
        assert decision.direction_ok is True


class TestPhaseSelection:
    def test_aggressive_savings_always_maintain_even_when_indoor_above_comfort(self):
        decision = decide_economizer_transition(_inputs(aggressive_savings=True, indoor=90.0))
        assert decision.eligible is True
        assert decision.phase == PHASE_MAINTAIN

    def test_indoor_above_comfort_selects_cool_down(self):
        decision = decide_economizer_transition(_inputs(indoor=78.0, comfort_cool=76.0))
        assert decision.phase == PHASE_COOL_DOWN

    def test_indoor_at_or_below_comfort_selects_maintain(self):
        decision = decide_economizer_transition(_inputs(indoor=75.0, comfort_cool=76.0))
        assert decision.phase == PHASE_MAINTAIN

    def test_indoor_equal_comfort_selects_maintain_not_cool_down(self):
        # strict '>' required for cool-down
        decision = decide_economizer_transition(_inputs(indoor=76.0, comfort_cool=76.0))
        assert decision.phase == PHASE_MAINTAIN

    def test_indoor_none_selects_maintain(self):
        decision = decide_economizer_transition(_inputs(indoor=None, outdoor=70.0))
        assert decision.eligible is True
        assert decision.phase == PHASE_MAINTAIN
