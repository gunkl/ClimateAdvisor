"""Tests for Issue #827: the comfort-family FSM (comfort_family_fsm.py).

Mirrors test_economizer_fsm.py's structure: flat inputs dict, an `_event(**overrides)`
helper, class-per-scenario-group, asserting on transition fields. Focuses on wiring
correctness (min-dwell anti-flap, the #823 regression class, cold start, the
comfort_family_switch_locked_out signal) — the underlying breach/deadband/hysteresis
math is already exhaustively tested in test_comfort_family_decision.py; these tests
don't re-prove it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.climate_advisor.comfort_family_decision import ComfortFamilyOutcome
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyDwellState,
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    ComfortFamilyState,
    transition,
)
from custom_components.climate_advisor.const import DAY_TYPE_COLD, DAY_TYPE_HOT
from custom_components.climate_advisor.ode_floor_guard import OdeFloorGuardOutcome

_NOW = datetime(2026, 8, 31, 12, 0, 0)
_SUSTAIN_S = 90.0
_MIN_DWELL_S = 600.0

_BASE = {
    "base_family": "cooling",
    "day_type": DAY_TYPE_HOT,
    "indoor": 74.0,
    "floor": 68.0,
    "ceiling": 76.0,
    "deadband_against_grain_f": 5.0,
    "manual_override_active": False,
    "natural_vent_active": False,
    "whf_owns_hvac": False,
    "ode_floor_outcome": OdeFloorGuardOutcome.MODEL_INELIGIBLE,
    "min_dwell_seconds": _MIN_DWELL_S,
    "sustain_seconds": _SUSTAIN_S,
    # Issue #843: default to "opposite family just ran" so every pre-#843 test
    # below keeps exercising the deadband-enforced path unchanged — see
    # test_comfort_family_decision.py's _inputs() for the same convention.
    "minutes_since_cooling_ended": 0.0,
    "minutes_since_heating_ended": 0.0,
    "recency_window_min": 120.0,
    "now": _NOW,
}


def _event(**overrides) -> ComfortFamilyEvent:
    inputs = ComfortFamilyFsmInputs(**{**_BASE, **overrides})
    return ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=inputs)


class TestColdStart:
    def test_cold_start_no_dwell_state_evaluates_immediately(self):
        # Hot day, native=cooling, current_state=COOLING already matches
        # native -> HOLD, no dwell_state needed at all (defaults applied).
        t = transition(ComfortFamilyState.COOLING, _event())
        assert t.to_state is ComfortFamilyState.COOLING
        assert t.changed is False
        assert t.locked_out is False

    def test_cold_start_far_outside_bounds_escalates_without_waiting_for_prior_state(self):
        # Indoor far below floor on a hot day (against-grain heating want),
        # already sustain-confirmed via a manufactured dwell_state — proves
        # cold start doesn't block on "no assumed starting family": passing
        # current_state=COOLING (a reasonable cold-start seed matching the
        # day's native family) evaluates the real breach immediately.
        seeded = ComfortFamilyDwellState(
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(indoor=60.0, floor=68.0, deadband_against_grain_f=5.0),
            dwell_state=seeded,
        )
        assert t.to_state is ComfortFamilyState.HEATING
        assert t.changed is True
        assert t.decision.outcome is ComfortFamilyOutcome.ESCALATE

    def test_cold_start_first_ever_transition_never_locked_out(self):
        # dwell_since is None (no prior state) -> the dwell lockout must not
        # block the very first transition, matching
        # _family_switch_locked_out()'s preserved "cold start always
        # allowed" precedent.
        seeded = ComfortFamilyDwellState(
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(indoor=60.0, floor=68.0, deadband_against_grain_f=5.0),
            dwell_state=seeded,
        )
        assert t.locked_out is False
        assert t.dwell_state.dwell_since == _NOW


class TestMinDwellAntiFlap:
    def test_reassertion_never_resets_the_dwell_clock(self):
        """The structural #823 fix: a HOLD tick (state reasserting itself,
        e.g. staying COOLING on a hot day with no breach) must NOT touch
        dwell_since — only a genuine transition does. Prove it by running two
        consecutive HOLD ticks and checking dwell_since is still None (never
        set) both times, i.e. the clock never "moves" on reassertion."""
        state = ComfortFamilyDwellState()
        t1 = transition(ComfortFamilyState.COOLING, _event(), dwell_state=state)
        assert t1.changed is False
        assert t1.dwell_state.dwell_since is None

        t2 = transition(ComfortFamilyState.COOLING, _event(now=_NOW + timedelta(minutes=5)), dwell_state=t1.dwell_state)
        assert t2.changed is False
        assert t2.dwell_state.dwell_since is None

    def test_what_would_happen_if_dwell_reset_on_every_call_regression_control(self):
        """Negative control proving this IS the #823 regression class, not
        merely a coincidence: a naive design that re-armed dwell_since on
        every call (even non-transitions) would make the lockout below
        mathematically unreachable-to-clear, exactly as #823's live incident
        (Zone "Simulated 2" locked out every cycle) described. This module's
        actual behavior — dwell_since starts and stays None across HOLD
        ticks — is the opposite of that broken shape; if a future change
        made HOLD ticks set dwell_state.dwell_since to `now`, this assertion
        would start failing and must be treated as a real regression, not a
        drive-by "fix"."""
        state = ComfortFamilyDwellState()
        t1 = transition(ComfortFamilyState.COOLING, _event(), dwell_state=state)
        # The broken shape would look like: dwell_since == _NOW after this
        # merely-reasserting tick. The real implementation does not do this.
        assert t1.dwell_state.dwell_since is None

    def test_transition_then_reassertion_holds_dwell_since_steady(self):
        # First tick: genuine escalation (heat, sustain-confirmed) sets
        # dwell_since. Second tick, still against-grain and still escalate-
        # confirmed (recovery not yet cleared) -> HOLD, must not move
        # dwell_since further.
        seeded = ComfortFamilyDwellState(
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t1 = transition(
            ComfortFamilyState.COOLING,
            _event(indoor=60.0, floor=68.0, deadband_against_grain_f=5.0),
            dwell_state=seeded,
        )
        assert t1.changed is True
        assert t1.dwell_state.dwell_since == _NOW
        assert t1.dwell_state.is_against_grain is True

        # Now heating, still against-grain (native=cooling on this hot day),
        # indoor still below floor -> recovery not cleared -> HOLD.
        t2 = transition(
            ComfortFamilyState.HEATING,
            _event(indoor=60.0, floor=68.0, deadband_against_grain_f=5.0, now=_NOW + timedelta(minutes=1)),
            dwell_state=t1.dwell_state,
        )
        assert t2.changed is False
        assert t2.dwell_state.dwell_since == _NOW  # unchanged from t1

    def test_locked_out_blocks_transition_within_min_dwell_window(self):
        # Establish a prior transition (dwell_since = _NOW), then immediately
        # (within min_dwell_seconds) present a confirmed opposite-direction
        # breach — must be locked out, not transition.
        seeded = ComfortFamilyDwellState(
            dwell_since=_NOW,
            is_against_grain=False,
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(
                indoor=60.0,
                floor=68.0,
                deadband_against_grain_f=5.0,
                now=_NOW + timedelta(seconds=30),
            ),
            dwell_state=seeded,
        )
        assert t.locked_out is True
        assert t.to_state is ComfortFamilyState.COOLING
        assert t.changed is False

    def test_unlocked_once_min_dwell_elapses(self):
        later = _NOW + timedelta(seconds=_MIN_DWELL_S + 1)
        seeded = ComfortFamilyDwellState(
            dwell_since=_NOW,
            is_against_grain=False,
            heat_candidate_since=later - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(
                indoor=60.0,
                floor=68.0,
                deadband_against_grain_f=5.0,
                now=later,
            ),
            dwell_state=seeded,
        )
        assert t.locked_out is False
        assert t.to_state is ComfortFamilyState.HEATING
        assert t.changed is True


class TestLockedOutEventSignal:
    def test_locked_out_transition_carries_decision_for_shell_event_payload(self):
        seeded = ComfortFamilyDwellState(
            dwell_since=_NOW,
            is_against_grain=False,
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(
                indoor=60.0,
                floor=68.0,
                deadband_against_grain_f=5.0,
                now=_NOW + timedelta(seconds=30),
            ),
            dwell_state=seeded,
        )
        assert t.locked_out is True
        # The shell emits "comfort_family_switch_locked_out" using these:
        assert t.decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert t.decision.target_family == "heating"
        assert t.decision.reason


class TestHoldAndEscalateWiring:
    def test_hold_when_no_breach(self):
        t = transition(ComfortFamilyState.COOLING, _event())
        assert t.to_state is ComfortFamilyState.COOLING
        assert t.decision.outcome is ComfortFamilyOutcome.HOLD

    def test_escalate_wires_to_heating_state(self):
        seeded = ComfortFamilyDwellState(
            heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            heat_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.COOLING,
            _event(
                indoor=60.0,
                floor=68.0,
                deadband_against_grain_f=5.0,
            ),
            dwell_state=seeded,
        )
        assert t.to_state is ComfortFamilyState.HEATING
        assert t.from_state is ComfortFamilyState.COOLING
        assert t.changed is True

    def test_revert_wires_to_cooling_state_on_hot_day(self):
        seeded = ComfortFamilyDwellState(
            dwell_since=_NOW - timedelta(seconds=_MIN_DWELL_S + 1),
            is_against_grain=True,
            recovery_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            recovery_candidate_raw=True,
        )
        t = transition(
            ComfortFamilyState.HEATING,
            _event(indoor=74.0, floor=68.0, ceiling=80.0, deadband_against_grain_f=5.0),
            dwell_state=seeded,
        )
        assert t.to_state is ComfortFamilyState.COOLING
        assert t.decision.outcome is ComfortFamilyOutcome.REVERT
        assert t.dwell_state.is_against_grain is False


class TestNoDwellStatePassedDefaultsToFreshColdStart:
    def test_omitting_dwell_state_behaves_like_a_fresh_one(self):
        t_omitted = transition(ComfortFamilyState.COOLING, _event())
        t_explicit = transition(ComfortFamilyState.COOLING, _event(), dwell_state=ComfortFamilyDwellState())
        assert t_omitted.to_state == t_explicit.to_state
        assert t_omitted.decision.outcome == t_explicit.decision.outcome


class TestDayTypeChangeIsNotTreatedAsAgainstGrain:
    """Regression coverage for the "stale family" gap found while designing
    this module: a current_family that merely reflects stale bookkeeping (never
    a real escalation) must snap back to the classifier's family immediately,
    not be gated by recovery-margin hysteresis."""

    def test_stale_family_snaps_back_to_classifier_immediately(self):
        seeded = ComfortFamilyDwellState(is_against_grain=False)
        t = transition(
            ComfortFamilyState.COOLING,
            _event(base_family="heating", day_type=DAY_TYPE_COLD, indoor=72.0, floor=68.0, ceiling=76.0),
            dwell_state=seeded,
        )
        assert t.to_state is ComfortFamilyState.HEATING
        assert t.decision.escalated_against_grain is False

    def test_day_type_alone_never_moves_the_family(self):
        # REGRESSION (Issue #827 Verification): a cold day (day-type native =
        # heating) while the classifier says cool and indoor sits mid-band must
        # NOT move the family. day_type only scales the deadbands; it is never
        # the target. See test_comfort_family_decision.py's
        # test_day_type_alone_never_overrides_the_classifiers_family.
        t = transition(
            ComfortFamilyState.COOLING,
            _event(base_family="cooling", day_type=DAY_TYPE_COLD, indoor=72.0, floor=68.0, ceiling=76.0),
            dwell_state=ComfortFamilyDwellState(is_against_grain=False),
        )
        assert t.to_state is ComfortFamilyState.COOLING
        assert t.changed is False
