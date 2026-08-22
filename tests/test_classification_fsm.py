"""Tests for Issue #742 (strangler-fig Phase 3): classification decision FSM.

Direct unit coverage of ``classification_fsm.transition()`` — wiring
correctness between ``decide_scheduled_band_gate()`` and
``decide_ode_ceiling_guard()``, not re-proving either's own logic (both
already have exhaustive coverage in ``test_desired_state.py``/
``test_ode_ceiling_guard.py``). Focuses on the ceiling-guard eligibility
branch order mirroring apply_classification()'s own early-return chain.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.classification_fsm import (
    CeilingGuardEligibility,
    ClassificationFsmEvent,
    ClassificationFsmEventKind,
    ClassificationFsmInputs,
    transition,
)
from custom_components.climate_advisor.desired_state import ScheduledBandGate
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome

_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _inputs(
    *,
    occupancy_mode: str = "home",
    manual_override_active: bool = False,
    paused_by_door: bool = False,
    natural_vent_active: bool = False,
    whf_owns_hvac: bool = False,
    aggressive_savings: bool = False,
    fan_mode: str = "disabled",
    predicted_indoor: list[dict] | None = None,
    hvac_mode: str | None = "off",
    k_passive: float | None = -0.5,
    confidence_k_passive: str = "medium",
    k_passive_via_bridge: bool = False,
    k_active_cool: float | None = -2.0,
    comfort_cool: float | None = 76.0,
    outdoor: float | None = 85.0,
    indoor: float | None = 74.0,
    ceiling_threshold: float | None = 76.0,
    now: datetime = _NOW,
) -> ClassificationFsmInputs:
    return ClassificationFsmInputs(
        occupancy_mode=occupancy_mode,
        manual_override_active=manual_override_active,
        paused_by_door=paused_by_door,
        natural_vent_active=natural_vent_active,
        whf_owns_hvac=whf_owns_hvac,
        aggressive_savings=aggressive_savings,
        fan_mode=fan_mode,
        predicted_indoor=predicted_indoor,
        hvac_mode=hvac_mode,
        k_passive=k_passive,
        confidence_k_passive=confidence_k_passive,
        k_passive_via_bridge=k_passive_via_bridge,
        k_active_cool=k_active_cool,
        comfort_cool=comfort_cool,
        outdoor=outdoor,
        indoor=indoor,
        ceiling_threshold=ceiling_threshold,
        now=now,
    )


def _event(**kwargs) -> ClassificationFsmEvent:
    return ClassificationFsmEvent(kind=ClassificationFsmEventKind.CYCLE_EVALUATED, inputs=_inputs(**kwargs))


class TestGateDefersShortCircuitCeilingGuard:
    def test_occupancy_defer_never_reaches_guard(self) -> None:
        decision = transition(_event(occupancy_mode="away"))
        assert decision.gate == ScheduledBandGate.DEFER_OCCUPANCY
        assert decision.ceiling_eligibility == CeilingGuardEligibility.NOT_EVALUATED_OCCUPANCY_DEFER
        assert decision.ceiling_decision is None

    def test_vacation_defer_never_reaches_guard(self) -> None:
        decision = transition(_event(occupancy_mode="vacation"))
        assert decision.gate == ScheduledBandGate.DEFER_OCCUPANCY
        assert decision.ceiling_decision is None

    def test_paused_defer_never_reaches_guard(self) -> None:
        decision = transition(_event(paused_by_door=True))
        assert decision.gate == ScheduledBandGate.DEFER_PAUSED
        assert decision.ceiling_eligibility == CeilingGuardEligibility.NOT_EVALUATED_PAUSED_DEFER
        assert decision.ceiling_decision is None

    def test_defer_override_falls_through_to_guard_like_proceed(self) -> None:
        # decide_scheduled_band_gate() checks override before paused, so this
        # returns DEFER_OVERRIDE — but apply_classification() only ever calls
        # decide_scheduled_band_gate() AFTER its own earlier, separate
        # `self._manual_override_active` early-return (automation.py
        # ~L2456-2497), so DEFER_OVERRIDE is structurally unreachable at this
        # call site in production. Critically, apply_classification() has no
        # `if _gate == DEFER_OVERRIDE:` branch at all — only DEFER_OCCUPANCY/
        # DEFER_PAUSED/DEFER_NAT_VENT are special-cased — so if it were ever
        # returned here, production would fall through to the ceiling guard
        # exactly like PROCEED. This FSM mirrors that exactly (see module
        # docstring: only 3 explicit short-circuit branches).
        decision = transition(_event(manual_override_active=True, paused_by_door=True))
        assert decision.gate == ScheduledBandGate.DEFER_OVERRIDE
        assert decision.ceiling_eligibility == CeilingGuardEligibility.EVALUATED
        assert decision.ceiling_decision is not None


class TestNatVentDeferShortCircuits:
    def test_aggressive_savings_short_circuits(self) -> None:
        decision = transition(_event(natural_vent_active=True, aggressive_savings=True, fan_mode="hvac_fan"))
        assert decision.gate == ScheduledBandGate.DEFER_NAT_VENT
        assert decision.ceiling_eligibility == CeilingGuardEligibility.NOT_EVALUATED_SAVINGS_NAT_VENT
        assert decision.ceiling_decision is None

    def test_whole_house_fan_archetype_short_circuits(self) -> None:
        decision = transition(_event(natural_vent_active=True, aggressive_savings=False, fan_mode="whole_house_fan"))
        assert decision.gate == ScheduledBandGate.DEFER_NAT_VENT
        assert decision.ceiling_eligibility == CeilingGuardEligibility.NOT_EVALUATED_WHF_ARCHETYPE
        assert decision.ceiling_decision is None

    def test_both_archetype_short_circuits(self) -> None:
        decision = transition(_event(whf_owns_hvac=True, aggressive_savings=False, fan_mode="both"))
        assert decision.gate == ScheduledBandGate.DEFER_NAT_VENT
        assert decision.ceiling_eligibility == CeilingGuardEligibility.NOT_EVALUATED_WHF_ARCHETYPE
        assert decision.ceiling_decision is None

    def test_hvac_fan_archetype_falls_through_to_guard(self) -> None:
        decision = transition(
            _event(
                natural_vent_active=True,
                aggressive_savings=False,
                fan_mode="hvac_fan",
                predicted_indoor=[{"ts": "2026-07-15T18:00:00+00:00", "temp": 90.0}],
            )
        )
        assert decision.gate == ScheduledBandGate.DEFER_NAT_VENT
        assert decision.ceiling_eligibility == CeilingGuardEligibility.EVALUATED
        assert decision.ceiling_decision is not None

    def test_disabled_fan_falls_through_to_guard(self) -> None:
        decision = transition(
            _event(
                natural_vent_active=True,
                aggressive_savings=False,
                fan_mode="disabled",
                predicted_indoor=[{"ts": "2026-07-15T18:00:00+00:00", "temp": 90.0}],
            )
        )
        assert decision.gate == ScheduledBandGate.DEFER_NAT_VENT
        assert decision.ceiling_eligibility == CeilingGuardEligibility.EVALUATED


class TestProceedReachesGuard:
    def test_proceed_with_no_predicted_indoor_is_not_applicable(self) -> None:
        decision = transition(_event(predicted_indoor=None))
        assert decision.gate == ScheduledBandGate.PROCEED
        assert decision.ceiling_eligibility == CeilingGuardEligibility.EVALUATED
        assert decision.ceiling_decision is not None
        assert decision.ceiling_decision.outcome == OdeCeilingGuardOutcome.NOT_APPLICABLE

    def test_proceed_with_breach_escalates(self) -> None:
        decision = transition(
            _event(
                predicted_indoor=[{"ts": "2026-07-15T12:20:00+00:00", "temp": 90.0}],
                k_active_cool=-0.5,
            )
        )
        assert decision.gate == ScheduledBandGate.PROCEED
        assert decision.ceiling_decision is not None
        assert decision.ceiling_decision.outcome == OdeCeilingGuardOutcome.ESCALATE


class TestEventKindPassthrough:
    def test_event_kind_carried_into_decision(self) -> None:
        decision = transition(_event())
        assert decision.event_kind == ClassificationFsmEventKind.CYCLE_EVALUATED

    def test_at_timestamp_carried_into_decision(self) -> None:
        decision = transition(_event())
        assert decision.at == _NOW
