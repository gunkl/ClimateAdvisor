"""Inline unit tests for the economizer lifecycle FSM (strangler-fig completion
program, Phase 5, Issue #746).

Focuses on wiring correctness (session short-circuits, state transitions) —
the underlying gate math is already exhaustively tested in
test_economizer_gate.py; these tests don't re-prove it.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.climate_advisor.economizer_fsm import (
    EconomizerFsmEvent,
    EconomizerFsmEventKind,
    EconomizerFsmInputs,
    transition,
)
from custom_components.climate_advisor.economizer_lifecycle import EconomizerLifecycleState

_NOW = datetime(2026, 8, 1, 18, 0, 0)

_BASE = {
    "day_type": "hot",
    "natural_vent_active": False,
    "outdoor": 70.0,
    "indoor": 78.0,
    "comfort_cool": 76.0,
    "delta": 3.0,
    "windows_physically_open": True,
    "in_window": True,
    "aggressive_savings": False,
    "now": _NOW,
}


def _event(**overrides) -> EconomizerFsmEvent:
    inputs = EconomizerFsmInputs(**{**_BASE, **overrides})
    return EconomizerFsmEvent(kind=EconomizerFsmEventKind.TICK, inputs=inputs)


class TestNotHotDayShortCircuit:
    def test_not_hot_day_transitions_to_inactive_from_active(self):
        t = transition(EconomizerLifecycleState.COOL_DOWN, _event(day_type="mild"))
        assert t.to_state is EconomizerLifecycleState.INACTIVE
        assert t.changed is True
        assert t.deferred is False

    def test_no_classification_yet_treated_as_not_hot(self):
        t = transition(EconomizerLifecycleState.MAINTAIN, _event(day_type=None))
        assert t.to_state is EconomizerLifecycleState.INACTIVE

    def test_not_hot_day_already_inactive_no_change(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(day_type="cold"))
        assert t.to_state is EconomizerLifecycleState.INACTIVE
        assert t.changed is False

    def test_not_hot_day_does_not_reach_gate(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(day_type="mild"))
        assert t.direction_ok is None


class TestNatVentDeferShortCircuit:
    def test_nat_vent_active_defers_without_touching_state(self):
        t = transition(EconomizerLifecycleState.COOL_DOWN, _event(natural_vent_active=True))
        assert t.to_state is EconomizerLifecycleState.COOL_DOWN
        assert t.from_state is EconomizerLifecycleState.COOL_DOWN
        assert t.changed is False
        assert t.deferred is True

    def test_nat_vent_active_from_inactive_stays_inactive_and_deferred(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(natural_vent_active=True))
        assert t.to_state is EconomizerLifecycleState.INACTIVE
        assert t.deferred is True

    def test_nat_vent_checked_after_day_type(self):
        # Not-hot-day wins even if natural_vent_active is also True — matches
        # production's if/elif order (day_type check first).
        t = transition(
            EconomizerLifecycleState.COOL_DOWN,
            _event(day_type="mild", natural_vent_active=True),
        )
        assert t.deferred is False
        assert t.to_state is EconomizerLifecycleState.INACTIVE


class TestGateReachedTransitions:
    def test_eligible_cool_down(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(indoor=78.0, comfort_cool=76.0))
        assert t.to_state is EconomizerLifecycleState.COOL_DOWN
        assert t.changed is True
        assert t.direction_ok is True

    def test_eligible_maintain(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(indoor=74.0, comfort_cool=76.0))
        assert t.to_state is EconomizerLifecycleState.MAINTAIN

    def test_ineligible_transitions_to_inactive(self):
        t = transition(EconomizerLifecycleState.COOL_DOWN, _event(windows_physically_open=False))
        assert t.to_state is EconomizerLifecycleState.INACTIVE
        assert t.changed is True

    def test_unchanged_when_already_in_target_phase(self):
        t = transition(EconomizerLifecycleState.COOL_DOWN, _event(indoor=78.0, comfort_cool=76.0))
        assert t.to_state is EconomizerLifecycleState.COOL_DOWN
        assert t.changed is False

    def test_direction_rejected_ineligible_exposes_direction_ok_false(self):
        t = transition(EconomizerLifecycleState.INACTIVE, _event(outdoor=79.0, indoor=70.0))
        assert t.direction_ok is False
        assert t.to_state is EconomizerLifecycleState.INACTIVE
