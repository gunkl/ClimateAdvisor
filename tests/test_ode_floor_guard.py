"""Tests for Issue #821: pure ODE floor guard decision (ode_floor_guard.py).

Mirrors test_ode_ceiling_guard.py's structure: direct unit coverage of
decide_ode_floor_guard(), in the same priority order the function checks them:
not-applicable, model-ineligible, missing-temps, no-breach-predicted,
standing-by, escalate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor.ode_floor_guard import (
    OdeFloorGuardInputs,
    OdeFloorGuardOutcome,
    decide_ode_floor_guard,
)

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _inputs(
    *,
    hvac_mode: str | None = "cool",
    natural_vent_active: bool = False,
    floor_crossing_time: datetime | None = _NOW + timedelta(hours=1),
    confidence_k_passive: str = "medium",
    k_active_heat: float | None = 6.0,
    comfort_heat: float | None = 68.0,
    indoor: float | None = 70.0,
    now: datetime = _NOW,
) -> OdeFloorGuardInputs:
    return OdeFloorGuardInputs(
        hvac_mode=hvac_mode,
        natural_vent_active=natural_vent_active,
        floor_crossing_time=floor_crossing_time,
        confidence_k_passive=confidence_k_passive,
        k_active_heat=k_active_heat,
        comfort_heat=comfort_heat,
        indoor=indoor,
        now=now,
    )


class TestNotApplicable:
    def test_off_day_not_applicable(self):
        decision = decide_ode_floor_guard(_inputs(hvac_mode="off"))
        assert decision.outcome is OdeFloorGuardOutcome.NOT_APPLICABLE

    def test_nat_vent_active_not_applicable(self):
        decision = decide_ode_floor_guard(_inputs(natural_vent_active=True))
        assert decision.outcome is OdeFloorGuardOutcome.NOT_APPLICABLE

    def test_heat_day_still_evaluates(self):
        # The module itself only gates on hvac_mode == "off" (mirroring the
        # ceiling guard's own complementary gate) — restricting evaluation to
        # "cool"-classified days specifically is the caller's (automation.py's
        # _resolve_comfort_family_mode()) responsibility, not this module's.
        decision = decide_ode_floor_guard(_inputs(hvac_mode="heat"))
        assert decision.outcome is not OdeFloorGuardOutcome.NOT_APPLICABLE


class TestModelIneligible:
    def test_confidence_none_is_ineligible(self):
        decision = decide_ode_floor_guard(_inputs(confidence_k_passive="none"))
        assert decision.outcome is OdeFloorGuardOutcome.MODEL_INELIGIBLE

    def test_confidence_low_is_eligible(self):
        # Only "none" is excluded — any other confidence level proceeds to the
        # breach-scan logic (matches ode_ceiling_guard.py's own precedent of
        # gating only on the "none" boundary).
        decision = decide_ode_floor_guard(_inputs(confidence_k_passive="low"))
        assert decision.outcome is not OdeFloorGuardOutcome.MODEL_INELIGIBLE


class TestMissingTemps:
    def test_missing_indoor(self):
        decision = decide_ode_floor_guard(_inputs(indoor=None))
        assert decision.outcome is OdeFloorGuardOutcome.MISSING_TEMPS

    def test_missing_comfort_heat(self):
        decision = decide_ode_floor_guard(_inputs(comfort_heat=None))
        assert decision.outcome is OdeFloorGuardOutcome.MISSING_TEMPS


class TestNoBreachPredicted:
    def test_no_cached_crossing(self):
        decision = decide_ode_floor_guard(_inputs(floor_crossing_time=None))
        assert decision.outcome is OdeFloorGuardOutcome.NO_BREACH_PREDICTED


class TestStandingBy:
    def test_breach_far_outside_lead_time(self):
        decision = decide_ode_floor_guard(_inputs(floor_crossing_time=_NOW + timedelta(hours=5)))
        assert decision.outcome is OdeFloorGuardOutcome.STANDING_BY
        assert decision.hours_to_breach == pytest.approx(5.0, rel=1e-2)

    def test_recovering_curve_far_out_stands_by(self):
        # A crossing predicted well beyond the lead-time buffer is treated the
        # same as "let it ride out" — matches the ceiling guard's own STANDING_BY
        # semantics for the WHF-overshoot self-correction case.
        decision = decide_ode_floor_guard(_inputs(floor_crossing_time=_NOW + timedelta(hours=3), k_active_heat=6.78))
        assert decision.outcome is OdeFloorGuardOutcome.STANDING_BY


class TestEscalate:
    def test_breach_imminent_escalates(self):
        decision = decide_ode_floor_guard(_inputs(floor_crossing_time=_NOW + timedelta(minutes=10)))
        assert decision.outcome is OdeFloorGuardOutcome.ESCALATE
        assert decision.lead_min is not None

    def test_already_breached_escalates(self):
        decision = decide_ode_floor_guard(
            _inputs(floor_crossing_time=_NOW - timedelta(minutes=5), indoor=66.0, comfort_heat=68.0)
        )
        assert decision.outcome is OdeFloorGuardOutcome.ESCALATE

    def test_boundary_at_exact_lead_time(self):
        # lead_min for delta_t=2 (indoor 70, comfort_heat 68), rate=6.0 °F/hr,
        # safety_multiplier=1.3: (2/6)*60*1.3 = 26 min, clamped to [15, 180] -> 26.
        decision = decide_ode_floor_guard(_inputs(floor_crossing_time=_NOW + timedelta(minutes=26)))
        assert decision.outcome is OdeFloorGuardOutcome.ESCALATE

    def test_no_confident_rate_uses_fallback_lead(self):
        decision = decide_ode_floor_guard(_inputs(k_active_heat=None, floor_crossing_time=_NOW + timedelta(minutes=30)))
        # fallback_minutes=60.0 > 30 -> ESCALATE
        assert decision.outcome is OdeFloorGuardOutcome.ESCALATE
        assert decision.lead_min == 60.0
