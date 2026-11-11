"""Tests for Issue #742 (strangler-fig Phase 3): pure ODE ceiling guard decision.

Direct unit coverage of ``decide_ode_ceiling_guard()`` — every branch in
apply_classification()'s ODE ceiling guard block (automation.py ~L2664-2851),
in the same priority order production checks them: not-applicable, model
ineligibility, missing temps, no-ceiling-threshold archetype, dormancy
(Issue #247's 3 conditions), no-breach, standing-by, escalate.

The swap-in itself was verified separately (pre-graduation) by
``tests/test_classification_fsm_authoritative_compare.py`` against the full
golden/pending scenario corpus — that comparator was deleted at graduation
(Issue #757 Phase 7 Step 7) once the legacy branch it compared against was
removed from ``apply_classification()``; this file's direct unit coverage of
``decide_ode_ceiling_guard()`` is unaffected and remains the primary test
coverage for the guard's decision logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.ode_ceiling_guard import (
    OdeCeilingGuardInputs,
    OdeCeilingGuardOutcome,
    decide_ode_ceiling_guard,
)

_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _curve(*points: tuple[str, float]) -> list[dict]:
    return [{"ts": ts, "temp": temp} for ts, temp in points]


def _inputs(
    *,
    predicted_indoor: list[dict] | None = None,
    hvac_mode: str | None = "off",
    k_passive: float | None = -0.5,
    confidence_k_passive: str = "medium",
    k_active_cool: float | None = -2.0,
    comfort_cool: float | None = 76.0,
    outdoor: float | None = 85.0,
    indoor: float | None = 74.0,
    natural_vent_active: bool = False,
    ceiling_threshold: float | None = 76.0,
    now: datetime = _NOW,
) -> OdeCeilingGuardInputs:
    return OdeCeilingGuardInputs(
        predicted_indoor=predicted_indoor,
        hvac_mode=hvac_mode,
        k_passive=k_passive,
        confidence_k_passive=confidence_k_passive,
        k_active_cool=k_active_cool,
        comfort_cool=comfort_cool,
        outdoor=outdoor,
        indoor=indoor,
        natural_vent_active=natural_vent_active,
        ceiling_threshold=ceiling_threshold,
        now=now,
    )


class TestNotApplicable:
    def test_no_predicted_indoor(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=None))
        assert decision.outcome == OdeCeilingGuardOutcome.NOT_APPLICABLE

    def test_empty_predicted_indoor(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=[]))
        assert decision.outcome == OdeCeilingGuardOutcome.NOT_APPLICABLE

    def test_hvac_mode_not_off(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(predicted_indoor=_curve(("2026-07-15T18:00:00+00:00", 80.0)), hvac_mode="cool")
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NOT_APPLICABLE

    def test_hvac_mode_none(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(predicted_indoor=_curve(("2026-07-15T18:00:00+00:00", 80.0)), hvac_mode=None)
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NOT_APPLICABLE


_CURVE = _curve(("2026-07-15T18:00:00+00:00", 80.0))


class TestModelIneligible:
    def test_k_passive_none(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, k_passive=None))
        assert decision.outcome == OdeCeilingGuardOutcome.MODEL_INELIGIBLE

    def test_k_passive_not_negative(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, k_passive=0.1))
        assert decision.outcome == OdeCeilingGuardOutcome.MODEL_INELIGIBLE

    def test_confidence_none_and_no_bridge(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, confidence_k_passive="none"))
        assert decision.outcome == OdeCeilingGuardOutcome.MODEL_INELIGIBLE

    def test_confidence_none_is_ineligible_without_a_bridge(self) -> None:
        # The gate bridge was removed in #1512 because k_vent_window is a windows-open rate, not a
        # closed-house rate: confidence "none" is now always MODEL_INELIGIBLE (was eligible via bridge).
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_CURVE,
                confidence_k_passive="none",
                outdoor=60.0,
                indoor=74.0,
                natural_vent_active=False,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.MODEL_INELIGIBLE

    def test_comfort_cool_none(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, comfort_cool=None))
        assert decision.outcome == OdeCeilingGuardOutcome.MODEL_INELIGIBLE


class TestMissingTemps:
    def test_outdoor_none(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, outdoor=None))
        assert decision.outcome == OdeCeilingGuardOutcome.MISSING_TEMPS
        assert (decision.indoor, decision.outdoor) == (74.0, None)

    def test_indoor_none(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, indoor=None))
        assert decision.outcome == OdeCeilingGuardOutcome.MISSING_TEMPS
        assert (decision.indoor, decision.outdoor) == (None, 85.0)


class TestNoCeilingThreshold:
    def test_whf_archetype_never_escalates(self) -> None:
        decision = decide_ode_ceiling_guard(_inputs(predicted_indoor=_CURVE, ceiling_threshold=None))
        assert decision.outcome == OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD


class TestDormancy:
    """Issue #247: dormancy is THREE conditions — outdoor <= indoor AND
    nat-vent actually running AND indoor within the ceiling threshold."""

    def test_all_three_conditions_true_is_dormant(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_CURVE,
                outdoor=70.0,
                indoor=74.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.DORMANT

    def test_outdoor_above_indoor_lifts_dormancy(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:05:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome != OdeCeilingGuardOutcome.DORMANT

    def test_nat_vent_not_running_lifts_dormancy(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:05:00+00:00", 90.0)),
                outdoor=70.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome != OdeCeilingGuardOutcome.DORMANT

    def test_indoor_breached_ceiling_lifts_dormancy(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:05:00+00:00", 90.0)),
                outdoor=70.0,
                indoor=78.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome != OdeCeilingGuardOutcome.DORMANT

    def test_boundary_outdoor_equals_indoor_still_dormant(self) -> None:
        # Non-strict <=, matching production.
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_CURVE,
                outdoor=74.0,
                indoor=74.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.DORMANT

    def test_boundary_indoor_equals_ceiling_still_dormant(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_CURVE,
                outdoor=70.0,
                indoor=76.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.DORMANT


class TestNoBreachPredicted:
    def test_curve_never_crosses_threshold(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T18:00:00+00:00", 75.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NO_BREACH_PREDICTED

    def test_boundary_exactly_at_threshold_no_breach(self) -> None:
        # Strict >, matching production (`temp > threshold`).
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T18:00:00+00:00", 76.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NO_BREACH_PREDICTED

    def test_no_bridge_tolerance_any_overshoot_breaches(self) -> None:
        # The bridge tolerance was removed in #1512 (k_vent_window is a windows-open rate, not a
        # closed-house rate): threshold is the plain ceiling 76, so 76.5 now counts as a breach.
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T18:00:00+00:00", 76.5)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome != OdeCeilingGuardOutcome.NO_BREACH_PREDICTED

    def test_malformed_timestamp_treated_as_no_breach(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=[{"ts": "not-a-timestamp", "temp": 90.0}],
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NO_BREACH_PREDICTED

    def test_missing_ts_key_treated_as_no_breach(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=[{"temp": 90.0}],
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NO_BREACH_PREDICTED


class TestStandingByVsEscalate:
    def test_breach_far_away_stands_by(self) -> None:
        # Breach 5 hours out, lead time computed from k_active_cool is small.
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T17:00:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_passive=-0.05,  # Issue #1517: leak-aware lead is ~25 min here -> clamped to the 30 min floor
                k_active_cool=-6.0,
                now=_NOW,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.STANDING_BY
        assert decision.hours_to_breach is not None and decision.hours_to_breach > 1.0
        assert decision.lead_min == 30.0

    def test_breach_within_lead_time_escalates(self) -> None:
        # Breach in 20 minutes, k_active_cool small -> lead_min large enough to cover it.
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:20:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=True,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_active_cool=-0.5,  # (76-74)/0.5*60*1.3 = 312min -> clamped to 240min ceil
                now=_NOW,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        assert decision.lead_min == 240.0
        assert decision.should_deactivate_fan is True

    def test_escalate_should_deactivate_fan_false_when_nat_vent_not_active(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:20:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_active_cool=-0.5,
                now=_NOW,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        assert decision.should_deactivate_fan is False

    def test_missing_k_active_cool_uses_fallback_lead(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:05:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_active_cool=None,
                now=_NOW,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        assert decision.lead_min == 120.0

    def test_zero_k_active_cool_uses_fallback_lead(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:05:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_active_cool=0.0,
                now=_NOW,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        assert decision.lead_min == 120.0

    def test_boundary_exactly_at_lead_time_escalates(self) -> None:
        # hours_to_breach <= lead_min/60 is non-strict — exact equality escalates.
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=_curve(("2026-07-15T12:30:00+00:00", 90.0)),
                outdoor=85.0,
                indoor=74.0,
                natural_vent_active=False,
                ceiling_threshold=76.0,
                comfort_cool=76.0,
                k_active_cool=None,  # fallback -> 120min lead? no: breach in 30min exactly
                now=_NOW,
            )
        )
        # fallback lead_min = 120 (clamped [30,240]) > 30min breach -> escalate
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE


class TestBreachAndLeadMeasureAgainstResolvedCeiling:
    """Issue #1093: the breach scan, lead time and decision.ceiling all read ceiling_threshold, not comfort_cool."""

    _SAVINGS_CURVE = _curve(("2026-07-15T12:30:00+00:00", 76.0), ("2026-07-15T13:30:00+00:00", 78.0))

    def test_breach_of_comfort_cool_only_is_not_a_breach_of_the_resolved_ceiling(self) -> None:
        curve = _curve(("2026-07-15T12:30:00+00:00", 76.0))
        decision = decide_ode_ceiling_guard(
            _inputs(predicted_indoor=curve, comfort_cool=75.0, ceiling_threshold=77.0, indoor=74.0)
        )
        assert decision.outcome == OdeCeilingGuardOutcome.NO_BREACH_PREDICTED
        assert decision.ceiling == 77.0

    def test_breach_of_the_resolved_ceiling_escalates_with_lead_from_that_ceiling(self) -> None:
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=self._SAVINGS_CURVE,
                comfort_cool=75.0,
                ceiling_threshold=77.0,
                indoor=74.0,
                k_active_cool=-2.0,
                k_passive=-0.05,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        assert decision.breach_ts == datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
        # Issue #1517: leak-aware (outdoor 85, k -0.05); the old (3 / 2) * 60 * 1.3 = 117 ignored the leak.
        assert decision.lead_min == pytest.approx(135.85, abs=0.01)
        assert decision.ceiling == 77.0

    def test_plain_ceiling_keeps_the_old_behaviour(self) -> None:
        curve = _curve(("2026-07-15T12:30:00+00:00", 76.0))
        decision = decide_ode_ceiling_guard(
            _inputs(
                predicted_indoor=curve,
                comfort_cool=75.0,
                ceiling_threshold=75.0,
                indoor=74.0,
                k_active_cool=-2.0,
                k_passive=-0.05,
            )
        )
        assert decision.outcome == OdeCeilingGuardOutcome.ESCALATE
        # Issue #1517: leak-aware; the old (1 / 2) * 60 * 1.3 = 39 ignored the leak toward the 85 F outdoor.
        assert decision.lead_min == pytest.approx(46.78, abs=0.01)
        assert decision.ceiling == 75.0
