"""Tests for Issue #827: pure comfort-family decision core (comfort_family_decision.py).

Mirrors test_ode_floor_guard.py's structure: direct unit coverage of
decide_comfort_family(), grouped by scenario class in roughly the same
priority order the function checks them: not-applicable, native-direction
tight escalation, against-grain deadband held/cleared, manual override,
sustain confirmation, missing-forecast-equivalent (ODE guard "can't decide")
fallback reachability, recovery-margin hysteresis (including the #823
regression-class revert-test), and cold start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.comfort_family_decision import (
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)
from custom_components.climate_advisor.const import (
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
)
from custom_components.climate_advisor.ode_floor_guard import OdeFloorGuardOutcome

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
_SUSTAIN_S = 90.0
_DEADBAND_F = 2.0  # e.g. cool/warm/mild default


def _inputs(
    *,
    current_family: str = "cooling",
    # The classifier's own family. Defaults to match `current_family` — the
    # steady state in which no escalation is in force. Tests exercising a live
    # escalation (current_family away from what the classifier chose) set this
    # explicitly. See ComfortFamilyInputs.base_family.
    base_family: str | None = None,
    # Defaults True: most scenarios below construct current_family as the
    # product of a genuine prior escalation (the thing this module actually
    # defends against). Tests exercising the "day type itself just changed,
    # no real escalation happened" pass-through set this False explicitly —
    # see TestNativeDirectionTightEscalation's dedicated test.
    is_against_grain: bool = True,
    day_type: str | None = DAY_TYPE_HOT,
    indoor: float | None = 74.0,
    floor: float | None = 68.0,
    ceiling: float | None = 76.0,
    deadband_against_grain_f: float = _DEADBAND_F,
    manual_override_active: bool = False,
    natural_vent_active: bool = False,
    whf_owns_hvac: bool = False,
    ode_floor_outcome: OdeFloorGuardOutcome = OdeFloorGuardOutcome.MODEL_INELIGIBLE,
    heat_candidate_since: datetime | None = None,
    cool_candidate_since: datetime | None = None,
    recovery_since: datetime | None = None,
    sustain_seconds: float = _SUSTAIN_S,
    # Issue #843: default to "opposite family just ran" (0 minutes) so every
    # pre-#843 test below keeps exercising the deadband-enforced path unchanged
    # — the recency gate only starts mattering once minutes_since_*_ended grows
    # past recency_window_min. Dedicated recency tests override these.
    minutes_since_cooling_ended: float | None = 0.0,
    minutes_since_heating_ended: float | None = 0.0,
    recency_window_min: float = 120.0,
    now: datetime = _NOW,
) -> ComfortFamilyInputs:
    return ComfortFamilyInputs(
        current_family=current_family,
        base_family=current_family if base_family is None else base_family,
        is_against_grain=is_against_grain,
        day_type=day_type,
        indoor=indoor,
        floor=floor,
        ceiling=ceiling,
        deadband_against_grain_f=deadband_against_grain_f,
        manual_override_active=manual_override_active,
        natural_vent_active=natural_vent_active,
        whf_owns_hvac=whf_owns_hvac,
        ode_floor_outcome=ode_floor_outcome,
        heat_candidate_since=heat_candidate_since,
        cool_candidate_since=cool_candidate_since,
        recovery_since=recovery_since,
        sustain_seconds=sustain_seconds,
        minutes_since_cooling_ended=minutes_since_cooling_ended,
        minutes_since_heating_ended=minutes_since_heating_ended,
        recency_window_min=recency_window_min,
        now=now,
    )


class TestNotApplicable:
    def test_natural_vent_active_defers(self):
        decision = decide_comfort_family(_inputs(natural_vent_active=True))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE

    def test_whf_owns_hvac_defers(self):
        decision = decide_comfort_family(_inputs(whf_owns_hvac=True))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE

    def test_missing_day_type(self):
        decision = decide_comfort_family(_inputs(day_type=None))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE

    def test_missing_indoor(self):
        decision = decide_comfort_family(_inputs(indoor=None))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE

    def test_missing_floor(self):
        decision = decide_comfort_family(_inputs(floor=None))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE

    def test_missing_ceiling(self):
        decision = decide_comfort_family(_inputs(ceiling=None))
        assert decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE


class TestNativeDirectionTightEscalation:
    """On a cool/cold day (native=heating), the family is already heating in
    steady state, so there's nothing to escalate. The meaningful "native
    direction" check is the day-to-day baseline: with no breach pressure at
    all, the resolved target always follows the day's native family with zero
    deadband involved (HOLD once current already matches it)."""

    def test_cold_day_holds_heating_with_no_breach(self):
        decision = decide_comfort_family(
            _inputs(current_family="heating", day_type=DAY_TYPE_COLD, indoor=70.0, floor=68.0, ceiling=76.0)
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "heating"

    def test_hot_day_holds_cooling_with_no_breach(self):
        decision = decide_comfort_family(
            _inputs(current_family="cooling", day_type=DAY_TYPE_HOT, indoor=74.0, floor=68.0, ceiling=76.0)
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"

    def test_day_type_alone_never_overrides_the_classifiers_family(self):
        # REGRESSION (Issue #827 Verification): day_type=COOL means the
        # day-type "native" family is heating, but the CLASSIFIER chose cooling
        # (base_family="cooling") — a routine, real combination, and exactly
        # what golden scenario `override_self_resolve_transient` encodes
        # (day_type="cool", hvac_mode="cool"). Indoor is squarely mid-band
        # (72 is between floor 68 and ceiling 76): zero breach in either
        # direction. This module must HOLD the classifier's cooling family.
        #
        # An earlier revision derived the target from day_type instead, and
        # returned ESCALATE->"heating" here — silently rewriting the
        # classifier's cool-mode decision into a furnace command with the home
        # perfectly comfortable. Occupant impact: heat they never asked for on
        # a mild autumn day, burning gas to fight nothing. day_type's only job
        # is scaling the deadbands.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                base_family="cooling",
                is_against_grain=False,
                day_type=DAY_TYPE_COOL,
                indoor=72.0,
                floor=68.0,
                ceiling=76.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"
        assert decision.escalated_against_grain is False

    def test_stale_family_snaps_back_to_the_classifier_with_no_hysteresis(self):
        # current_family="cooling" is stale bookkeeping (e.g. one of the 7
        # out-of-scope _arm_comfort_family("cooling") writers), while the
        # classifier now says heat and no genuine escalation is on record
        # (is_against_grain=False). Indoor is mid-band. The correction back to
        # the classifier's family must be immediate — the recovery-margin gate
        # is reserved for a REAL breach-driven escalation
        # (see TestRecoveryMarginHysteresis).
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                base_family="heating",
                is_against_grain=False,
                day_type=DAY_TYPE_COOL,
                indoor=72.0,
                floor=68.0,
                ceiling=76.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"
        assert decision.escalated_against_grain is False

    def test_stale_family_with_is_against_grain_true_uses_hysteresis_instead(self):
        # Same current_family/base_family as the test above, EXCEPT
        # is_against_grain=True — proves the flag, not the current_family/
        # base_family mismatch alone, is what selects the revert-with-hysteresis
        # path. current_family="cooling" while recovering means indoor must
        # drop to/below ceiling - margin (76 - 2 = 74) before reverting to the
        # classifier's heating family; indoor=75 has not cleared that yet ->
        # HOLD, not an immediate switch.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                base_family="heating",
                is_against_grain=True,
                day_type=DAY_TYPE_COOL,
                indoor=75.0,
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"


class TestAgainstGrainDeadbandHeld:
    def test_hot_day_dawn_dip_within_deadband_does_not_escalate(self):
        # Hot day, native=cooling. comfort_deadband_hot_f=5.0. Indoor dips to
        # 65 (floor=68) -> breach_delta=3.0 < 5.0 deadband -> held.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=65.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        assert decision.target_family == "cooling"

    def test_warm_day_small_ceiling_overshoot_within_deadband_does_not_escalate(self):
        # Warm day, native=cooling, so ceiling direction is the NATIVE one
        # (deadband 0) and floor direction is against-grain (deadband 2.0).
        # Use a floor breach smaller than 2.0 to exercise "held".
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_WARM,
                indoor=67.0,
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        assert decision.target_family == "cooling"


class TestAgainstGrainDeadbandCleared:
    def test_hot_day_dawn_dip_past_deadband_escalates_once_sustained(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=62.0,  # floor=68, breach_delta=6.0 > 5.0 deadband
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_hot_day_dawn_dip_past_deadband_but_not_yet_sustained(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=62.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW,  # just started
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.SUSTAINING
        assert decision.target_family == "cooling"  # holds current family while sustaining


class TestRecencyGatedDeadband:
    """Issue #843: the against-grain deadband applies only when the opposite
    family actually ran within comfort_family_recency_window_min. Nothing
    recorded, or nothing within the window, means the against-grain direction
    behaves like native (near-zero deadband, still sustain-confirmed) — fixes
    the overnight-drift bug where a static deadband made no distinction between
    "just finished cooling" and "nothing has run in 3 hours"."""

    def test_no_recent_cooling_escalates_on_small_breach_once_sustained(self):
        # Hot day, native=cooling. Floor breach of only 3.0 (would normally be
        # held by the 5.0 deadband — see TestAgainstGrainDeadbandHeld) escalates
        # once sustained, because nothing cooling-side ran within the window.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=65.0,  # floor=68, breach_delta=3.0 — within the 5.0 deadband
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
                minutes_since_cooling_ended=None,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_recent_cooling_still_holds_the_same_small_breach(self):
        # Same breach, but cooling ended 10 minutes ago (within the 120-minute
        # window) — deadband is enforced exactly as before Issue #843.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=65.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                minutes_since_cooling_ended=10.0,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        assert decision.target_family == "cooling"

    def test_recency_window_boundary_exactly_at_window_is_not_recent(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=65.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
                minutes_since_cooling_ended=120.0,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_recency_window_just_inside_still_gates(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=65.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                minutes_since_cooling_ended=119.9,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        assert decision.target_family == "cooling"

    def test_symmetric_no_recent_heating_escalates_to_cooling(self):
        # Cold day, native=heating. Ceiling breach of 3.0 (within the 5.0
        # deadband) escalates once sustained, because nothing heating-side ran
        # within the window.
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                day_type=DAY_TYPE_COLD,
                indoor=75.0,  # ceiling=72, breach_delta=3.0 — within the 5.0 deadband
                floor=65.0,
                ceiling=72.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                cool_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
                minutes_since_heating_ended=None,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "cooling"

    def test_symmetric_recent_heating_still_holds(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                day_type=DAY_TYPE_COLD,
                indoor=75.0,
                floor=65.0,
                ceiling=72.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                minutes_since_heating_ended=10.0,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        assert decision.target_family == "heating"

    def test_asymmetry_recent_cooling_does_not_gate_the_cool_direction(self):
        # Recent WHF/nat-vent/HVAC-cool activity gates a switch TO heat, never a
        # switch to cool — only actual heating counts toward "recent heating",
        # per the project's explicit asymmetric-by-design decision. Cold day
        # ceiling breach: minutes_since_cooling_ended is "recent" (irrelevant to
        # this direction), minutes_since_heating_ended is None (nothing to
        # protect against) — escalates exactly as the "no recent" case above,
        # regardless of how recently cooling-side activity happened.
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                day_type=DAY_TYPE_COLD,
                indoor=75.0,
                floor=65.0,
                ceiling=72.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                cool_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
                minutes_since_cooling_ended=1.0,
                minutes_since_heating_ended=None,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "cooling"


class TestManualOverride:
    def test_override_within_grace_window_is_held(self):
        # deadband=2.0, override doubles to 4.0. breach_delta=3.0 is past the
        # base deadband but inside the override grace window -> held.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_WARM,
                indoor=65.0,  # floor=68, breach_delta=3.0
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                manual_override_active=True,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.OVERRIDE_HELD
        assert decision.target_family == "cooling"

    def test_override_past_grace_window_escalates_anyway(self):
        # breach_delta=5.0 > effective deadband (2.0*2=4.0) -> escalates
        # regardless of override, once sustained.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_WARM,
                indoor=63.0,  # floor=68, breach_delta=5.0
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                manual_override_active=True,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_override_never_blocks_a_revert(self):
        # Currently against-grain (heating on a hot day). Recovery margin
        # cleared and sustained -> reverts even though override is active.
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                base_family="cooling",  # escalated away from the classifier's cool-mode choice
                day_type=DAY_TYPE_HOT,
                indoor=76.0,  # floor=68, margin(5.0) cleared: 76 >= 68+5
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                manual_override_active=True,
                recovery_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.REVERT
        assert decision.target_family == "cooling"


class TestOdeFloorGuardFallbackReachability:
    """The universal fallback (Design §1): the sustain-confirm+deadband path
    must always be reachable when the ODE guard "can't decide" — not only
    when confidence_k_passive == "none" literally."""

    def test_ode_escalate_bypasses_deadband_and_sustain(self):
        # Issue #843: ESCALATE only bypasses the deadband/sustain checks when
        # there's nothing recent to protect against (minutes_since_cooling_ended
        # outside the recency window, or never recorded) — see
        # test_ode_escalate_deferred_when_recent_cooling_within_window below for
        # the new gated case this test used to not distinguish from.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=67.0,  # floor=68, breach_delta=1.0, well within the 5.0 deadband
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.ESCALATE,
                minutes_since_cooling_ended=None,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_ode_escalate_deferred_when_recent_cooling_within_window(self):
        """Issue #843: a predicted breach minutes after cooling actually ran is
        exactly the flip-flop the recency-gated deadband exists to prevent —
        predictive (ODE) vs. reactive shouldn't matter. ESCALATE is demoted to
        the same "respected, no fallback this tick" treatment STANDING_BY gets."""
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=67.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.ESCALATE,
                minutes_since_cooling_ended=10.0,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"

    def test_ode_escalate_not_deferred_once_recency_window_elapsed(self):
        """Same as above, but cooling ended 121 minutes ago (past the 120-minute
        window) — ESCALATE fires immediately again, same as the "never recorded"
        case."""
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=67.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.ESCALATE,
                minutes_since_cooling_ended=121.0,
                recency_window_min=120.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_ode_standing_by_suppresses_fallback_this_tick(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=60.0,  # would clear a 5.0 deadband on its own
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.STANDING_BY,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"

    def test_ode_model_ineligible_falls_through_to_fallback(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=60.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_ode_missing_temps_falls_through_to_fallback(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=60.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.MISSING_TEMPS,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_ode_no_breach_predicted_falls_through_to_fallback(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=60.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NO_BREACH_PREDICTED,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"


class TestRecoveryMarginHysteresis:
    def test_instant_de_escalation_does_not_happen_at_bare_threshold(self):
        # Against-grain in heating on a hot day. Indoor just barely clears
        # the raw floor (68.0) but NOT floor + margin (68+5=73) -> must NOT
        # revert yet (this is the saw-tooth negative control).
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                base_family="cooling",  # escalated away from the classifier's cool-mode choice
                day_type=DAY_TYPE_HOT,
                indoor=68.5,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "heating"

    def test_reverts_once_margin_cleared_and_sustained(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                base_family="cooling",  # escalated away from the classifier's cool-mode choice
                day_type=DAY_TYPE_HOT,
                indoor=74.0,  # 68 + 5 margin cleared
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                recovery_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.REVERT
        assert decision.target_family == "cooling"

    def test_margin_cleared_but_not_yet_sustained_is_recovering(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="heating",
                base_family="cooling",  # escalated away from the classifier's cool-mode choice
                day_type=DAY_TYPE_HOT,
                indoor=74.0,
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                recovery_since=_NOW,  # just crossed
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.RECOVERING
        assert decision.target_family == "heating"

    def test_cooling_direction_hysteresis_symmetric(self):
        # Against-grain in cooling on a cold day (native=heating). Indoor
        # must clear ceiling - margin before reverting.
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                base_family="heating",  # escalated away from the classifier's heat-mode choice
                day_type=DAY_TYPE_COLD,
                indoor=71.0,  # ceiling=76, margin=5.0 -> need <=71 to clear
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=5.0,
                recovery_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.REVERT
        assert decision.target_family == "heating"

    def test_cooling_direction_hysteresis_holds_before_margin_cleared(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                base_family="heating",  # escalated away from the classifier's heat-mode choice
                day_type=DAY_TYPE_COLD,
                indoor=73.0,  # ceiling=76, only 3 below — margin needs 5
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=5.0,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"


class TestMildDayBothDirectionsAgainstGrain:
    """A mild/off day has no native family — Design §1: "an off-classified
    day now gets real defense (was zero before) at a conservative tier rather
    than none." Both directions use the mild deadband; neither is free."""

    def test_mild_day_floor_breach_within_deadband_holds(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_MILD,
                indoor=67.0,  # floor=68, breach_delta=1.0 < 2.0
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND

    def test_mild_day_floor_breach_past_deadband_escalates(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_MILD,
                indoor=65.0,  # breach_delta=3.0 > 2.0
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"

    def test_mild_day_no_native_family_holds_current_absent_breach(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_MILD,
                indoor=72.0,
                floor=68.0,
                ceiling=76.0,
                deadband_against_grain_f=2.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.HOLD
        assert decision.target_family == "cooling"


class TestColdStartEvaluatesImmediately:
    """No separate cold-start branch — a fresh call with indoor already far
    outside bounds evaluates real thresholds immediately (given sustain
    already satisfied), not blocked by an assumed prior state."""

    def test_cold_start_with_indoor_far_below_floor_escalates_immediately_once_sustained(self):
        decision = decide_comfort_family(
            _inputs(
                current_family="cooling",
                day_type=DAY_TYPE_HOT,
                indoor=55.0,  # far below floor=68
                floor=68.0,
                ceiling=80.0,
                deadband_against_grain_f=5.0,
                ode_floor_outcome=OdeFloorGuardOutcome.NOT_APPLICABLE,
                heat_candidate_since=_NOW - timedelta(seconds=_SUSTAIN_S + 1),
            )
        )
        assert decision.outcome is ComfortFamilyOutcome.ESCALATE
        assert decision.target_family == "heating"
