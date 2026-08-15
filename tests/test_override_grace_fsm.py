"""Wiring tests for Issue #639 (Block 5 Phase 3): override/grace joint-lifecycle
FSM assembly.

Each of the pure pieces this FSM assembles already has its own exhaustive unit
coverage (``test_override_confirm_split.py``, ``test_override_match.py``,
``test_override_grace_start.py``, ``test_override_cancel_outcome.py``, plus the
reused ``desired_state.decide_override_confirm``/``door_window_grace_expiry.
decide_grace_expiry_outcome``) — these tests focus on wiring correctness only:
does ``transition()`` route to the right pure function given the right adapted
inputs, and land on the state ``override_grace_fsm.py``'s own documented
transition table says it should, for every (state, event kind) cell across all
3 ``_transition_from_*`` functions x all 7 event kinds — including the
documented defensive no-op/"unreachable" branches. Mirrors
``tests/test_door_window_fsm.py``'s structure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.override_grace_fsm import (
    OverrideGraceFsmEvent,
    OverrideGraceFsmEventKind,
    OverrideGraceFsmInputs,
    transition,
)
from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState

_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)

_IDLE_NONE = (OverrideConfirmState.IDLE, GraceState.NONE)
_PENDING_NONE = (OverrideConfirmState.PENDING, GraceState.NONE)
_IDLE_PROTECTING = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)
_PENDING_PROTECTING = (OverrideConfirmState.PENDING, GraceState.ACTIVE_PROTECTING_OVERRIDE)
_IDLE_UNPROTECTED = (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)
_PENDING_UNPROTECTED = (OverrideConfirmState.PENDING, GraceState.ACTIVE_UNPROTECTED)


def _inputs(**overrides) -> OverrideGraceFsmInputs:
    base = dict(
        confirm_seconds=600.0,
        setpoint_override=False,
        current_mode="heat",
        classification_mode="heat",
        manual_override_active=False,
        manual_override_mode=None,
        manual_override_source=None,
        fan_override_active=False,
        current_setpoint_f=None,
        target_setpoint_f=None,
        tolerance_f=1.0,
        within_planned_window=False,
        any_sensor_open=False,
        grace_source="automation",
        now=_NOW,
    )
    base.update(overrides)
    return OverrideGraceFsmInputs(**base)


def _ev(kind: OverrideGraceFsmEventKind, **overrides) -> OverrideGraceFsmEvent:
    return OverrideGraceFsmEvent(kind=kind, inputs=_inputs(**overrides))


class TestFromNoGrace:
    def test_override_detected_confirm_enabled_lands_pending(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_DETECTED, confirm_seconds=600.0))
        assert t.to_state == _PENDING_NONE
        assert t.outcome == "detected"
        assert t.changed

    def test_override_detected_confirm_disabled_lands_protecting(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_DETECTED, confirm_seconds=0.0))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "detected"

    def test_confirm_expired_not_pending_unreachable_noop(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "unreachable_not_pending"
        assert not t.changed

    def test_confirm_expired_pending_confirms_when_still_divergent(self):
        t = transition(
            _PENDING_NONE,
            _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED, current_mode="heat", classification_mode="cool"),
        )
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "confirm"

    def test_confirm_expired_pending_discards_when_self_resolved(self):
        t = transition(
            _PENDING_NONE,
            _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED, current_mode="heat", classification_mode="heat"),
        )
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "discard_self_resolved"

    def test_manual_override_during_pause_confirm_enabled_lands_pending(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE, confirm_seconds=600.0))
        assert t.to_state == _PENDING_NONE
        assert t.outcome == "override_confirmation_started"

    def test_manual_override_during_pause_confirm_disabled_lands_protecting(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE, confirm_seconds=0.0))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "override_confirmation_started"

    def test_dashboard_resume_lands_unprotected(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "resumed"

    def test_dashboard_resume_preserves_confirm_axis(self):
        t = transition(_PENDING_NONE, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == _PENDING_UNPROTECTED

    def test_override_cancelled_noop_when_nothing_active(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "noop"
        assert not t.changed

    def test_override_cancelled_had_manual_clears(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED, manual_override_active=True))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_manual"

    def test_override_cancelled_had_fan_only_clears(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED, fan_override_active=True))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_fan_only"

    def test_override_superseded_unreachable_from_no_grace(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "unreachable_no_grace"
        assert not t.changed

    def test_grace_timer_expired_unreachable_from_no_grace(self):
        t = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "unreachable_no_grace"


class TestFromGraceProtecting:
    def test_grace_timer_expired_lands_idle_none(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "clear_normal"  # grace_source default "automation" never adopts

    def test_grace_timer_expired_planned_window_still_lands_idle_none(self):
        """Confirms GraceExpiryOutcome does NOT branch this FSM's next state —
        every outcome converges on (IDLE, NONE) per the module docstring."""
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED, within_planned_window=True))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "clear_planned_window"

    def test_grace_timer_expired_adopt_override_still_lands_idle_none(self):
        t = transition(
            _IDLE_PROTECTING,
            _ev(
                OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED,
                grace_source="manual",
                manual_override_active=True,
                manual_override_mode="heat",
                manual_override_source="normal",
                classification_mode="heat",
            ),
        )
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "adopt_override"

    def test_override_superseded_confirm_enabled_lands_pending(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, confirm_seconds=600.0))
        assert t.to_state == _PENDING_PROTECTING
        assert t.outcome == "superseded"

    def test_override_superseded_confirm_disabled_stays_protecting(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, confirm_seconds=0.0))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "superseded"

    def test_override_cancelled_had_manual(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED, manual_override_active=True))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_manual"

    def test_override_cancelled_had_grace_only_when_no_override_flags(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_grace_only"

    def test_override_detected_second_candidate_confirm_enabled_lands_pending(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_DETECTED, confirm_seconds=600.0))
        assert t.to_state == _PENDING_PROTECTING
        assert t.outcome == "detected"

    def test_manual_override_during_pause_noop_already_protecting(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "noop_already_protecting"
        assert not t.changed

    def test_dashboard_resume_noop_already_protecting(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "noop_already_protecting"

    def test_confirm_expired_noop_already_protecting(self):
        t = transition(_IDLE_PROTECTING, _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "noop_already_protecting"


class TestFromGraceUnprotected:
    def test_grace_timer_expired_lands_idle_none(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "clear_normal"

    def test_override_detected_confirm_enabled_lands_pending(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_DETECTED, confirm_seconds=600.0))
        assert t.to_state == _PENDING_UNPROTECTED
        assert t.outcome == "detected"

    def test_override_detected_confirm_disabled_upgrades_to_protecting(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_DETECTED, confirm_seconds=0.0))
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "detected"

    def test_confirm_expired_not_pending_unreachable_noop(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED))
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "unreachable_not_pending"

    def test_confirm_expired_pending_confirms_upgrades_to_protecting(self):
        t = transition(
            _PENDING_UNPROTECTED,
            _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED, current_mode="heat", classification_mode="cool"),
        )
        assert t.to_state == _IDLE_PROTECTING
        assert t.outcome == "confirm"

    def test_confirm_expired_pending_discards_stays_unprotected(self):
        t = transition(
            _PENDING_UNPROTECTED,
            _ev(OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED, current_mode="heat", classification_mode="heat"),
        )
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "discard_self_resolved"

    def test_override_cancelled_had_manual(self):
        t = transition(
            _IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED, manual_override_active=True)
        )
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_manual"

    def test_override_cancelled_had_grace_only_when_no_override_flags(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_CANCELLED))
        assert t.to_state == _IDLE_NONE
        assert t.outcome == "had_grace_only"

    def test_override_superseded_noop_unprotected(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED))
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "noop_unprotected_grace"
        assert not t.changed

    def test_manual_override_during_pause_noop_unprotected(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE))
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "noop_unprotected_grace"

    def test_dashboard_resume_noop_unprotected(self):
        t = transition(_IDLE_UNPROTECTED, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == _IDLE_UNPROTECTED
        assert t.outcome == "noop_unprotected_grace"


class TestDispatchOnGraceAxisNotConfirmAxis:
    """Proves transition() dispatches purely on the grace half of the state
    pair (per the module's own docstring) — PENDING/IDLE confirm states within
    the same grace bucket hit the same branch function."""

    def test_pending_none_and_idle_none_both_route_to_no_grace_branch(self):
        t_idle = transition(_IDLE_NONE, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        t_pending = transition(_PENDING_NONE, _ev(OverrideGraceFsmEventKind.DASHBOARD_RESUME))
        assert t_idle.outcome == t_pending.outcome == "resumed"
