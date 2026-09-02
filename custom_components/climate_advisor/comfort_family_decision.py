"""Pure decision core for the comfort-family defense (Issue #827 — consolidates
#821/#823's ad-hoc lockout into a single strangler-fig leaf/FSM pair).

Mirrors ``ode_floor_guard.py``/``ode_ceiling_guard.py``'s contract shape (frozen
``Inputs`` dataclass, an ``Outcome`` enum, a frozen ``Decision`` dataclass, zero
side effects, zero logging, zero HA imports). Answers ONE question per call:
given the day's climate, indoor temperature, and the already-resolved comfort
floor/ceiling, should the comfort family (``"heating"`` vs ``"cooling"``) be
something other than what it currently is, right now?

Consolidates what was split across ``automation.py``'s ``_resolve_comfort_family_mode()``
(confidence-gated ODE floor guard reuse with no fallback between its two paths,
no exit hysteresis, flat non-sleep-aware ``comfort_heat``) plus
``_family_switch_locked_out()``/``_arm_comfort_family()`` (a separately-armed dwell
timer — the direct site of #823's patch, and the class of bug this consolidation
makes structurally impossible: min-dwell now lives exclusively in
``comfort_family_fsm.py``'s ``transition()``, which only advances its clock on a
genuine state change, never on reassertion).

**Two-state model.** Every day type has at most one *native* family
(``DAY_TYPE_HOT``/``DAY_TYPE_WARM`` -> cooling, ``DAY_TYPE_COOL``/``DAY_TYPE_COLD``
-> heating, ``DAY_TYPE_MILD`` -> none). ``current_family`` is either sitting at
that native family, or it has escalated *against the day's grain* because a real
floor/ceiling breach forced it there. Which of those two shapes ``current_family``
is in changes what this function checks:

  - **Native (or no-native/mild) shape** — checks whether a breach in EITHER
    direction is large enough (day-type-scaled deadband, see the table in the
    module docstring for ``comfort_family_fsm.py``) and sustained long enough
    (``confirmed_transition.is_confirmed()``) to force an against-grain
    escalation. The heating direction additionally consults the caller-resolved
    ``ode_floor_outcome`` (from ``ode_floor_guard.decide_ode_floor_guard()``) as
    a proactive, lead-time-based first check — ``ESCALATE`` fires immediately,
    ``STANDING_BY`` is respected (no fallback override this tick), and any
    "guard can't decide" outcome (``MODEL_INELIGIBLE``/``MISSING_TEMPS``/
    ``NO_BREACH_PREDICTED``) falls through to the same sustain-confirm+deadband
    fallback the cooling direction always uses (the "universal fallback" —
    Design §1: this path is the permanent floor under the ODE guard, not gated
    on ``confidence_k_passive == "none"`` literally). There is no equivalent ODE
    ceiling-guard reuse for the cooling direction: ``ode_ceiling_guard.py``'s own
    docstring scopes itself exclusively to ``hvac_mode == "off"`` days (a
    different territory — proactive cooling on an off-classified day, not
    ceiling defense while the heating family is active) — reusing it here would
    silently break that documented boundary.
  - **Against-grain shape** — the only way out is a REVERT, gated by a
    **recovery-margin** (not the entry deadband): indoor must clear the floor/
    ceiling by ``deadband_against_grain_f`` *in the comfort direction* (not just
    stop breaching), then sustain-confirm, before reverting to native. This is
    what a plain "instant de-escalation" design cannot do, and is the specific
    mechanism that defeats the saw-tooth failure mode a same-threshold
    entry/exit pair would produce.

**Manual override.** An active override does not block a confirmed breach once
it has cleared the day-type deadband — at that point it is a floor/ceiling
breach regardless of cause, and the safety backstop must win. What override DOES
do is double the effective deadband for a NEW against-grain escalation
(``_OVERRIDE_DEADBAND_MULTIPLIER``): a breach between 1x and 2x the configured
deadband is held (``OVERRIDE_HELD``) while override is active, but a breach past
2x always escalates regardless of override. This is a deliberate design choice
for this consolidation (Design §1's "override changes what's intentional, not
the safety backstop" — see this module's own investigation trail in Issue #827)
rather than an extraction from prior code, since the prior mechanism blocked
escalation unconditionally whenever ``_manual_override_active`` was set. Revert
(exiting an against-grain state back to native) is never gated by override —
only entry into against-grain is.

**Min-dwell anti-flap is explicitly OUT of scope for this module** — it lives in
``comfort_family_fsm.py``'s ``transition()``, which is the only thing that owns
a clock across calls. This leaf is stateless per call, like every other pure
leaf in this codebase; the ``*_since``/``recovery_since`` timestamp bookkeeping
inputs below are owned and updated by the FSM via
``confirmed_transition.resolve_candidate_since()`` before each call, exactly
matching that primitive's own documented "the caller owns the actual state"
contract.

**GAP-7 (fixed timers vs. climate severity) is explicitly out of scope** for
this pass — no live evidence motivating it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .confirmed_transition import is_confirmed
from .const import DAY_TYPE_COLD, DAY_TYPE_COOL, DAY_TYPE_HOT, DAY_TYPE_WARM
from .ode_floor_guard import OdeFloorGuardOutcome

# Manual override doubles the effective deadband required for a NEW
# against-grain escalation (native -> against-grain). It never gates a revert
# (against-grain -> native) and never gates an ODE floor guard ESCALATE (that
# signal is already lead-time/eligibility-gated and treated as the stronger of
# the two heating-direction checks). See module docstring's "Manual override"
# section for the full rationale.
_OVERRIDE_DEADBAND_MULTIPLIER = 2.0

_FAMILY_HEATING = "heating"
_FAMILY_COOLING = "cooling"


class ComfortFamilyOutcome(Enum):
    """Why the decision came out the way it did — one member per distinct
    terminal branch, in the same priority order the function checks them."""

    NOT_APPLICABLE = "not_applicable"  # nat-vent/WHF owns HVAC, or day_type/indoor/floor/ceiling missing
    HOLD = "hold"  # current_family already matches the resolved target — no change wanted
    WITHIN_DEADBAND = "within_deadband"  # a breach exists but hasn't cleared the required deadband yet
    OVERRIDE_HELD = "override_held"  # manual override active; breach is inside the override grace window
    SUSTAINING = "sustaining"  # breach cleared its deadband but hasn't sustain-confirmed yet
    ESCALATE = "escalate"  # a new against-grain (or native-default) switch is confirmed — see target_family
    RECOVERING = "recovering"  # against-grain state; recovery margin cleared but not yet sustain-confirmed
    REVERT = "revert"  # against-grain state; recovery margin + sustain both confirmed — switch back to native


@dataclass(frozen=True)
class ComfortFamilyDecision:
    """The outcome, plus every computed value the shell's logging/event payload
    needs (mirrors ``OdeFloorGuardDecision``/``OdeCeilingGuardDecision``'s own
    convention of carrying their own audit fields rather than making the caller
    re-derive them)."""

    outcome: ComfortFamilyOutcome
    target_family: str | None = None  # "heating"/"cooling" — always populated except NOT_APPLICABLE
    reason: str = ""
    heat_breach_delta_f: float | None = None  # floor - indoor (positive = below floor)
    cool_breach_delta_f: float | None = None  # indoor - ceiling (positive = above ceiling)
    deadband_applied_f: float | None = None  # the deadband/recovery-margin actually used for this call
    # True only when THIS decision is a genuine breach-driven switch away from
    # the day's native family (i.e. a real ESCALATE, not a plain "the day type
    # itself changed so the native default moved" pass-through). The FSM must
    # persist this into its next call's `is_against_grain` input — see
    # ``ComfortFamilyInputs.is_against_grain``'s own docstring for why this
    # distinction exists and what breaks without it.
    escalated_against_grain: bool = False


@dataclass(frozen=True)
class ComfortFamilyInputs:
    """Every input the decision may read — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine``/coordinator
    reads the shell (``comfort_family_fsm.py`` + its ``automation.py`` call
    sites) is responsible for resolving:
      current_family            -> the FSM's current ComfortFamilyState.value
      day_type                  -> self._current_classification.day_type
      indoor                    -> self._get_indoor_temp_f()
      floor                     -> the already-resolved, sleep-window-aware
                                    comfort_heat the caller has in hand (same
                                    value _apply_comfort_band() already computes
                                    as band.floor — NOT raw config comfort_heat)
      ceiling                   -> the already-resolved band.ceiling (same
                                    convention — NOT raw config comfort_cool;
                                    already includes aggressive_savings' own
                                    CEILING_ESCALATION_SAVINGS_MARGIN_F widening,
                                    so this module composes from the same single
                                    anchor point rather than a second one)
      deadband_against_grain_f  -> config comfort_deadband_{day_type}_f, already
                                    read and clamped by the caller (const.py's
                                    NumberSelectorConfig clamp, per day type)
      manual_override_active    -> self._manual_override_active
      natural_vent_active       -> self._natural_vent_active
      whf_owns_hvac              -> self._whf_owns_hvac()
      ode_floor_outcome          -> decide_ode_floor_guard(...).outcome, called
                                    by the shell once per tick before this
                                    function — see module docstring for how each
                                    outcome is consumed here
      heat_candidate_since       -> FSM-tracked, via
                                    confirmed_transition.resolve_candidate_since()
                                    against a heating-want boolean candidate
      cool_candidate_since       -> same, against a cooling-want boolean candidate
      recovery_since              -> same, against an against-grain-recovered
                                    boolean candidate (only meaningful/updated
                                    while current_family is against-grain)
      sustain_seconds              -> caller-resolved sustain window (mirrors
                                    COMFORT_FALLBACK_CONFIRM_S's existing use),
                                    shared by all three sustain-confirm checks
      now                           -> caller-resolved wall-clock time
                                    (dt_util.now())
    """

    current_family: str
    # The family the CLASSIFIER itself chose for this cycle, derived from the
    # caller's `day_mode` ("heat" -> "heating", "cool" -> "cooling"). This is
    # the base authority for which family should be active absent any breach —
    # NOT `_native_family(day_type)`.
    #
    # Issue #827 Verification correction: an earlier revision of this module
    # used the day-type-derived native family as that base, on the plan's
    # premise that "every family has a native direction for the day (heat on
    # cool/cold days, cool on hot/warm days)". That premise is false against
    # real code: `select_comfort_band()`/the classifier's own `hvac_mode` is an
    # INDEPENDENT authority that routinely picks cooling on a DAY_TYPE_COOL day
    # (golden scenario `override_self_resolve_transient` is exactly that:
    # day_type="cool", hvac_mode="cool"). With native as the base, this module
    # returned ESCALATE->"heating" on that scenario with indoor squarely
    # mid-band and zero breach in either direction — silently rewriting the
    # classifier's cool-mode decision into a furnace command. Design §2's
    # preserved contract ("ComfortBand.active is KEPT unchanged, still computed
    # by select_comfort_band()'s existing day-type logic") requires the base to
    # come from the classifier; this module only ever layers a breach-driven
    # ESCALATE (and its recovery-margin-gated REVERT) on top of it.
    #
    # `day_type` still scales the deadband and still decides which DIRECTION
    # counts as against-grain for deadband purposes — that part of the plan's
    # design is unchanged and correct.
    base_family: str
    # True only when `current_family` was reached via a genuine breach-driven
    # ESCALATE (``ComfortFamilyDecision.escalated_against_grain``), FSM-tracked
    # across calls. Deliberately NOT derived from `current_family != native`
    # inside this module: a day-type change alone (e.g. yesterday hot, today
    # cool) also makes `current_family` differ from the new native, but that
    # is NOT an escalation — it must switch immediately with zero hysteresis,
    # the same way `select_comfort_band()`'s day-type-only edge picker always
    # has. Only a REAL breach-driven escalation should require the recovery-
    # margin-gated revert path (`_decide_revert`). When False, this module
    # always uses `_decide_entry` — even if `current_family != native` — which
    # naturally produces an immediate, zero-friction switch to the new native
    # when there's no active breach in either direction.
    is_against_grain: bool
    day_type: str | None
    indoor: float | None
    floor: float | None
    ceiling: float | None
    deadband_against_grain_f: float
    manual_override_active: bool
    natural_vent_active: bool
    whf_owns_hvac: bool
    ode_floor_outcome: OdeFloorGuardOutcome
    heat_candidate_since: datetime | None
    cool_candidate_since: datetime | None
    recovery_since: datetime | None
    sustain_seconds: float
    now: datetime


def _native_family(day_type: str | None) -> str | None:
    """The day's native comfort family, or ``None`` for a day type with no
    strong preference (``DAY_TYPE_MILD``, or an unrecognized value)."""
    if day_type in (DAY_TYPE_HOT, DAY_TYPE_WARM):
        return _FAMILY_COOLING
    if day_type in (DAY_TYPE_COOL, DAY_TYPE_COLD):
        return _FAMILY_HEATING
    return None


def _effective_deadband(deadband: float, *, override_active: bool) -> float:
    return deadband * _OVERRIDE_DEADBAND_MULTIPLIER if override_active else deadband


def decide_comfort_family(inputs: ComfortFamilyInputs) -> ComfortFamilyDecision:
    """Decide whether the comfort family should switch, right now — see module
    docstring for the full two-state (native-shape vs. against-grain-shape)
    contract."""
    if inputs.natural_vent_active or inputs.whf_owns_hvac:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="nat-vent/WHF owns HVAC — comfort family defense defers",
        )

    if inputs.day_type is None or inputs.indoor is None or inputs.floor is None or inputs.ceiling is None:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="missing day_type/indoor/floor/ceiling",
        )

    native = _native_family(inputs.day_type)
    # "Against-grain" means: current_family was reached by a genuine breach-driven
    # escalation AWAY from what the classifier chose. Measured against
    # `base_family` (the classifier's decision), not `native` (the day-type
    # heuristic) — see ComfortFamilyInputs.base_family's docstring for why.
    against_grain = inputs.is_against_grain and inputs.current_family != inputs.base_family

    if against_grain:
        return _decide_revert(inputs)
    return _decide_entry(inputs, native)


def _decide_revert(inputs: ComfortFamilyInputs) -> ComfortFamilyDecision:
    """``current_family`` is an escalation away from ``base_family`` — the only
    exit is a recovery-margin-gated, sustain-confirmed revert back to what the
    classifier chose. Never gated by manual override (see module docstring)."""
    margin = inputs.deadband_against_grain_f
    if inputs.current_family == _FAMILY_HEATING:
        recovered = inputs.indoor >= inputs.floor + margin
        breach_delta_kwargs = {"heat_breach_delta_f": inputs.floor - inputs.indoor}
    else:
        recovered = inputs.indoor <= inputs.ceiling - margin
        breach_delta_kwargs = {"cool_breach_delta_f": inputs.indoor - inputs.ceiling}

    if not recovered:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.HOLD,
            target_family=inputs.current_family,
            reason="against-grain — recovery margin not yet cleared",
            deadband_applied_f=margin,
            escalated_against_grain=True,
            **breach_delta_kwargs,
        )

    if is_confirmed(
        candidate=True,
        candidate_since=inputs.recovery_since,
        now=inputs.now,
        sustain_seconds=inputs.sustain_seconds,
    ):
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.REVERT,
            target_family=inputs.base_family,
            reason="recovery margin cleared and sustain-confirmed — reverting to the classifier's family",
            deadband_applied_f=margin,
            **breach_delta_kwargs,
        )

    return ComfortFamilyDecision(
        outcome=ComfortFamilyOutcome.RECOVERING,
        target_family=inputs.current_family,
        reason="recovery margin cleared — sustain-confirming before reverting",
        deadband_applied_f=margin,
        escalated_against_grain=True,
        **breach_delta_kwargs,
    )


def _decide_entry(inputs: ComfortFamilyInputs, native: str | None) -> ComfortFamilyDecision:
    """``current_family`` is not a live escalation (it matches ``base_family``,
    or no genuine escalation is on record). Checks both breach directions for a
    NEW escalation away from ``base_family``.

    ``native`` (day-type-derived) is used ONLY to scale the deadbands: the
    direction the day's climate already favors escalates at a near-zero
    deadband, the against-grain direction must clear the configured day-type
    deadband first. It is deliberately NOT the target family — see
    ``ComfortFamilyInputs.base_family``."""
    heat_deadband = 0.0 if native == _FAMILY_HEATING else inputs.deadband_against_grain_f
    cool_deadband = 0.0 if native == _FAMILY_COOLING else inputs.deadband_against_grain_f

    heat_breach_delta = inputs.floor - inputs.indoor
    cool_breach_delta = inputs.indoor - inputs.ceiling

    # Absent a confirmed breach this branch must never move the family off what
    # the classifier chose — the "holding" target while a breach candidate is
    # still sustain-confirming/deadband-held is base_family, not native.
    holding_target = inputs.base_family

    heat_target: str | None = None
    heat_reason = "no floor breach" if heat_breach_delta <= 0 else "within heat deadband"
    heat_held: _DirectionCheck | None = None

    if inputs.ode_floor_outcome is OdeFloorGuardOutcome.ESCALATE:
        heat_target = _FAMILY_HEATING
        heat_reason = "ODE floor guard ESCALATE"
    elif inputs.ode_floor_outcome is OdeFloorGuardOutcome.STANDING_BY:
        heat_reason = "ODE floor guard STANDING_BY — respected, no fallback this tick"
    else:
        # MODEL_INELIGIBLE / MISSING_TEMPS / NO_BREACH_PREDICTED / NOT_APPLICABLE
        # (from the guard's own perspective) — all fall through to the universal
        # sustain-confirm + deadband fallback (Design §1).
        heat_held = _check_direction(
            breach_delta=heat_breach_delta,
            deadband=heat_deadband,
            override_active=inputs.manual_override_active,
            candidate_since=inputs.heat_candidate_since,
            now=inputs.now,
            sustain_seconds=inputs.sustain_seconds,
        )
        if heat_held.outcome in (ComfortFamilyOutcome.OVERRIDE_HELD, ComfortFamilyOutcome.SUSTAINING):
            return ComfortFamilyDecision(
                outcome=heat_held.outcome,
                target_family=holding_target,
                reason=f"floor breach: {heat_held.reason}",
                heat_breach_delta_f=heat_breach_delta,
                deadband_applied_f=heat_held.deadband_applied_f,
            )
        if heat_held.outcome is ComfortFamilyOutcome.ESCALATE:
            heat_target = _FAMILY_HEATING
            heat_reason = "sustain-confirmed floor breach"

    cool_target: str | None = None
    cool_reason = "no ceiling breach" if cool_breach_delta <= 0 else "within cool deadband"
    cool_held: _DirectionCheck | None = None

    if heat_target is None:
        cool_held = _check_direction(
            breach_delta=cool_breach_delta,
            deadband=cool_deadband,
            override_active=inputs.manual_override_active,
            candidate_since=inputs.cool_candidate_since,
            now=inputs.now,
            sustain_seconds=inputs.sustain_seconds,
        )
        if cool_held.outcome in (ComfortFamilyOutcome.OVERRIDE_HELD, ComfortFamilyOutcome.SUSTAINING):
            return ComfortFamilyDecision(
                outcome=cool_held.outcome,
                target_family=holding_target,
                reason=f"ceiling breach: {cool_held.reason}",
                cool_breach_delta_f=cool_breach_delta,
                deadband_applied_f=cool_held.deadband_applied_f,
            )
        if cool_held.outcome is ComfortFamilyOutcome.ESCALATE:
            cool_target = _FAMILY_COOLING
            cool_reason = "sustain-confirmed ceiling breach"

    if heat_target is not None:
        target_family, reason, deadband_applied = heat_target, heat_reason, heat_deadband
    elif cool_target is not None:
        target_family, reason, deadband_applied = cool_target, cool_reason, cool_deadband
    else:
        target_family = inputs.base_family
        reason = "no breach pressure — holding the classifier's family"
        deadband_applied = None

    if target_family == inputs.current_family:
        # No transition wanted. Surface WITHIN_DEADBAND (rather than a plain
        # HOLD) whenever either direction had a real, sub-deadband breach —
        # more useful to the shell's logging than silently collapsing to
        # "nothing happening" (heat direction takes priority, matching the
        # same priority order used for escalation itself).
        heat_within = heat_held is not None and heat_held.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        if heat_breach_delta > 0 and heat_within:
            return ComfortFamilyDecision(
                outcome=ComfortFamilyOutcome.WITHIN_DEADBAND,
                target_family=target_family,
                reason=f"floor breach: {heat_held.reason}",
                heat_breach_delta_f=heat_breach_delta,
                cool_breach_delta_f=cool_breach_delta,
                deadband_applied_f=heat_held.deadband_applied_f,
            )
        cool_within = cool_held is not None and cool_held.outcome is ComfortFamilyOutcome.WITHIN_DEADBAND
        if cool_breach_delta > 0 and cool_within:
            return ComfortFamilyDecision(
                outcome=ComfortFamilyOutcome.WITHIN_DEADBAND,
                target_family=target_family,
                reason=f"ceiling breach: {cool_held.reason}",
                heat_breach_delta_f=heat_breach_delta,
                cool_breach_delta_f=cool_breach_delta,
                deadband_applied_f=cool_held.deadband_applied_f,
            )
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.HOLD,
            target_family=target_family,
            reason=reason,
            heat_breach_delta_f=heat_breach_delta,
            cool_breach_delta_f=cool_breach_delta,
            deadband_applied_f=deadband_applied,
        )

    # A real breach direction fired (heat_target/cool_target) -> genuine
    # against-grain escalation, the FSM must persist is_against_grain=True.
    # Otherwise target_family only moved because the day's native family
    # itself changed (no breach at all) -> a zero-friction native pass-
    # through, not an escalation; the FSM must persist is_against_grain=False.
    escalated_against_grain = heat_target is not None or cool_target is not None
    return ComfortFamilyDecision(
        outcome=ComfortFamilyOutcome.ESCALATE,
        target_family=target_family,
        reason=reason,
        heat_breach_delta_f=heat_breach_delta,
        cool_breach_delta_f=cool_breach_delta,
        deadband_applied_f=deadband_applied,
        escalated_against_grain=escalated_against_grain,
    )


@dataclass(frozen=True)
class _DirectionCheck:
    outcome: ComfortFamilyOutcome  # WITHIN_DEADBAND / OVERRIDE_HELD / SUSTAINING / ESCALATE
    reason: str
    deadband_applied_f: float


def _check_direction(
    *,
    breach_delta: float,
    deadband: float,
    override_active: bool,
    candidate_since: datetime | None,
    now: datetime,
    sustain_seconds: float,
) -> _DirectionCheck | None:
    """Evaluate one breach direction against its entry deadband, the manual
    override grace window, and sustain-confirmation. Returns ``None`` only when
    the caller should treat this direction as fully inert (never happens today
    — kept for symmetry with the other pure modules' branch-per-outcome shape;
    every real call resolves to one of ``_DirectionCheck``'s outcomes)."""
    if breach_delta <= deadband:
        return _DirectionCheck(
            outcome=ComfortFamilyOutcome.WITHIN_DEADBAND,
            reason="within deadband",
            deadband_applied_f=deadband,
        )

    effective_deadband = _effective_deadband(deadband, override_active=override_active)
    if override_active and breach_delta <= effective_deadband:
        return _DirectionCheck(
            outcome=ComfortFamilyOutcome.OVERRIDE_HELD,
            reason="manual override active — breach within override grace window",
            deadband_applied_f=effective_deadband,
        )

    if is_confirmed(candidate=True, candidate_since=candidate_since, now=now, sustain_seconds=sustain_seconds):
        return _DirectionCheck(
            outcome=ComfortFamilyOutcome.ESCALATE,
            reason="deadband cleared and sustain-confirmed",
            deadband_applied_f=effective_deadband,
        )

    return _DirectionCheck(
        outcome=ComfortFamilyOutcome.SUSTAINING,
        reason="deadband cleared — sustain-confirming",
        deadband_applied_f=effective_deadband,
    )
