"""Comfort-family FSM — the unified transition table for the comfort-family
defense (Issue #827, consolidating #821/#823's ad-hoc lockout).

Mirrors ``economizer_lifecycle.py``/``economizer_fsm.py``'s exact split:

- ``ComfortFamilyState`` — the 2-state enum (``HEATING``/``COOLING``) this
  module owns directly (unlike the economizer's separate lifecycle module,
  two states with no derivation-from-flags step is small enough to keep in one
  file — matching the plan's "split only if the combined file would be
  unreasonably large; otherwise one module is fine" guidance).
- ``transition(current_state, event) -> ComfortFamilyTransition`` — the single
  entry point, called once per relevant cycle (today, from both
  ``_apply_comfort_band()`` and ``_set_temperature_for_mode()``, same as
  ``_resolve_comfort_family_mode()``'s two callers before this consolidation).

**What this module owns that the pure leaf (``comfort_family_decision.py``)
deliberately does not:**

1. **Min-dwell anti-flap.** ``comfort_mode_switch_min_interval_s`` (600s
   default, ``CONF_COMFORT_MODE_SWITCH_MIN_INTERVAL_S``) blocks a state change
   the leaf wants to make until that much time has passed since the FSM's
   ``dwell_since`` clock last moved. Critically, ``dwell_since`` **only
   advances on a genuine transition** (``ComfortFamilyTransition.changed is
   True``) — a HOLD/WITHIN_DEADBAND/SUSTAINING/OVERRIDE_HELD/RECOVERING tick
   never touches it. This is the structural fix for #823: the old
   ``_arm_comfort_family(..., only_if_changed=True)`` patch had to be bolted on
   after the fact because every call site re-armed the clock on mere
   reassertion; here there is only one place the clock can move, and it moves
   only when the state genuinely changes, so the reassertion bug is
   unrepresentable rather than patched around.
2. **The ``comfort_family_switch_locked_out`` event contract.** When the leaf
   wants a transition (``ESCALATE`` or ``REVERT``) but the dwell timer blocks
   it, ``ComfortFamilyTransition.locked_out`` is set (mirroring
   ``ai_skills_context.py``'s ``_render_comfort_family_switch_locked_out()``
   payload shape: ``candidate_family``, ``reason``) — the shell is responsible
   for actually emitting the event, this module only signals that it should.
3. **Cold start.** ``transition()`` takes whatever ``current_state`` the shell
   passes in (the shell may seed it from the day's native family — see
   ``comfort_family_decision.py``'s ``_native_family()``) and evaluates real
   thresholds immediately through the same leaf every real cycle uses — there
   is no separate cold-start branch that assumes a starting family or skips a
   cycle of evaluation.
4. **Restart persistence — deliberately NOT persisted.** Matches every other
   FSM's documented convention in this codebase (``economizer_fsm.py``,
   ``nat_vent_fsm.py``): a fresh process has no flapping history worth
   guarding against, so ``dwell_since``/the sustain-confirm ``*_since`` fields
   all start ``None`` on restart, exactly like
   ``confirmed_transition.py``'s own documented precedent.

**Day-type deadband table** (``deadband_against_grain_f``, already resolved
and clamped by the caller from ``CONFIG_METADATA``'s 5
``comfort_deadband_{hot,warm,mild,cool,cold}_f`` keys — this module and the
leaf never read config directly, matching every other pure module's
convention):

| Day type | Config key | Default | Clamp |
|---|---|---|---|
| Hot | ``comfort_deadband_hot_f`` | 5.0°F | [2.0, 8.0] |
| Warm | ``comfort_deadband_warm_f`` | 2.0°F | [1.0, 5.0] |
| Mild / off | ``comfort_deadband_mild_f`` | 2.0°F | [1.0, 5.0] |
| Cool | ``comfort_deadband_cool_f`` | 2.0°F | [1.0, 5.0] |
| Cold | ``comfort_deadband_cold_f`` | 5.0°F | [2.0, 8.0] |

Named ``deadband``, not ``hysteresis`` — deliberately distinct vocabulary from
``NAT_VENT_HYSTERESIS_F`` (a symmetric, day-type-independent, override-blind
noise filter). This concept is asymmetric (native vs. against-grain), day-type-
scaled, and override-aware in a way that term already doesn't describe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .comfort_family_decision import (
    ComfortFamilyDecision,
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)
from .confirmed_transition import resolve_candidate_since
from .ode_floor_guard import OdeFloorGuardOutcome


class ComfortFamilyState(Enum):
    """The two families the comfort-defense FSM tracks. Distinct from
    ``ComfortBand.active``/``classification.hvac_mode`` (``"heat"``/``"cool"``/
    ``"off"``) — those stay exactly as ``select_comfort_band()`` already
    computes them (Design §2, preserved unchanged); this enum is the
    escalation-aware state layered on top."""

    HEATING = "heating"
    COOLING = "cooling"


class ComfortFamilyEventKind(Enum):
    """What prompted this transition evaluation. Only ``TICK`` is fed from the
    real call sites today (``_apply_comfort_band()``/``_set_temperature_for_mode()``,
    same as the economizer's own single-``TICK``-kind precedent)."""

    TICK = "tick"


@dataclass(frozen=True)
class ComfortFamilyDwellState:
    """The FSM's own cross-call bookkeeping — owned and persisted call-to-call
    by the shell (NOT this module; NOT restart-persisted, see module
    docstring), passed in on every ``transition()`` call and returned updated
    via ``ComfortFamilyTransition.dwell_state``. Frozen/immutable, like every
    other value in this module's Markov-property call shape — the shell
    replaces its stored copy with the returned one rather than mutating in
    place.
    """

    dwell_since: datetime | None = None
    # True only when the CURRENT state (before this tick) was reached via a
    # genuine breach-driven escalation, as opposed to simply being the day's
    # native family (whether because it always was, or because a prior tick's
    # zero-friction native pass-through moved it there when the day type
    # itself changed). See ``ComfortFamilyInputs.is_against_grain``'s own
    # docstring for why this can't be derived from current_family alone.
    # Starts False at cold start — correct, since a fresh process has no
    # escalation history (mirrors every other FSM's restart convention).
    is_against_grain: bool = False
    heat_candidate_since: datetime | None = None
    cool_candidate_since: datetime | None = None
    recovery_since: datetime | None = None
    # The last raw candidate values, needed so resolve_candidate_since() can
    # tell "same candidate, keep timing it" from "fresh candidate, restart the
    # clock" — mirrors nat_vent_exit.py's own bookkeeping shape for the same
    # primitive.
    heat_candidate_raw: bool | None = None
    cool_candidate_raw: bool | None = None
    recovery_candidate_raw: bool | None = None


@dataclass(frozen=True)
class ComfortFamilyFsmInputs:
    """Every live reading this FSM's transition function may consult —
    explicit, nothing hidden. Superset of ``ComfortFamilyInputs`` (minus the
    ``*_since`` bookkeeping fields, which this module derives from
    ``dwell_state`` before calling the leaf) plus the dwell-timer's own config
    input.

    Field-by-field correspondence: see ``comfort_family_decision.ComfortFamilyInputs``'s
    own docstring for every field this dataclass forwards unchanged; the two
    additions are:
      min_dwell_seconds -> config comfort_mode_switch_min_interval_s
                            (CONF_COMFORT_MODE_SWITCH_MIN_INTERVAL_S), the
                            existing dwell config key, unchanged by this issue
      sustain_seconds    -> config comfort_fallback_confirm_s equivalent
                            (COMFORT_FALLBACK_CONFIRM_S today), shared by the
                            leaf's three sustain-confirm checks
      minutes_since_cooling_ended -> Issue #843, forwarded unchanged — see
                            comfort_family_decision.ComfortFamilyInputs
      minutes_since_heating_ended -> same
      recency_window_min          -> config comfort_family_recency_window_min
    """

    # The classifier's own family for this cycle ("heating"/"cooling", derived
    # from the caller's day_mode). The base authority for which family should be
    # active absent a breach — see comfort_family_decision.ComfortFamilyInputs.base_family.
    base_family: str
    day_type: str | None
    indoor: float | None
    floor: float | None
    ceiling: float | None
    deadband_against_grain_f: float
    manual_override_active: bool
    natural_vent_active: bool
    whf_owns_hvac: bool
    ode_floor_outcome: OdeFloorGuardOutcome
    min_dwell_seconds: float
    sustain_seconds: float
    minutes_since_cooling_ended: float | None
    minutes_since_heating_ended: float | None
    recency_window_min: float
    now: datetime


@dataclass(frozen=True)
class ComfortFamilyEvent:
    kind: ComfortFamilyEventKind
    inputs: ComfortFamilyFsmInputs


@dataclass(frozen=True)
class ComfortFamilyTransition:
    """The transition's full record — audit trail by construction (mirrors
    ``EconomizerTransition``'s own convention)."""

    from_state: ComfortFamilyState
    to_state: ComfortFamilyState
    event_kind: ComfortFamilyEventKind
    decision: ComfortFamilyDecision
    dwell_state: ComfortFamilyDwellState
    # True when the leaf wanted a transition (ESCALATE/REVERT) but the min-
    # dwell lockout blocked it this tick — the shell must emit
    # "comfort_family_switch_locked_out" with (candidate_family=to-be-decided
    # target, reason=decision.reason) when this is True. `to_state` stays
    # equal to `from_state` in this case (the block held the prior state).
    locked_out: bool = False
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _leaf_inputs(
    current_state: ComfortFamilyState,
    inputs: ComfortFamilyFsmInputs,
    dwell_state: ComfortFamilyDwellState,
) -> ComfortFamilyInputs:
    return ComfortFamilyInputs(
        current_family=current_state.value,
        base_family=inputs.base_family,
        is_against_grain=dwell_state.is_against_grain,
        day_type=inputs.day_type,
        indoor=inputs.indoor,
        floor=inputs.floor,
        ceiling=inputs.ceiling,
        deadband_against_grain_f=inputs.deadband_against_grain_f,
        manual_override_active=inputs.manual_override_active,
        natural_vent_active=inputs.natural_vent_active,
        whf_owns_hvac=inputs.whf_owns_hvac,
        ode_floor_outcome=inputs.ode_floor_outcome,
        heat_candidate_since=dwell_state.heat_candidate_since,
        cool_candidate_since=dwell_state.cool_candidate_since,
        recovery_since=dwell_state.recovery_since,
        sustain_seconds=inputs.sustain_seconds,
        minutes_since_cooling_ended=inputs.minutes_since_cooling_ended,
        minutes_since_heating_ended=inputs.minutes_since_heating_ended,
        recency_window_min=inputs.recency_window_min,
        now=inputs.now,
    )


def _advance_dwell_bookkeeping(
    decision: ComfortFamilyDecision,
    dwell_state: ComfortFamilyDwellState,
    now: datetime,
) -> ComfortFamilyDwellState:
    """Update the sustain-confirm ``*_since`` bookkeeping for the NEXT call,
    using ``confirmed_transition.resolve_candidate_since()`` — the shared
    reset-on-change/hold-while-unchanged primitive, not reimplemented here.

    A direction counts as "still a candidate" whenever the decision reports a
    non-zero breach in that direction (WITHIN_DEADBAND/OVERRIDE_HELD/SUSTAINING/
    ESCALATE all count — only a fully-cleared breach, where the leaf didn't
    even populate the corresponding ``*_breach_delta_f`` field with a positive
    value, resets the clock). Recovery uses the same shape against RECOVERING/
    REVERT.
    """
    heat_candidate = decision.heat_breach_delta_f is not None and decision.heat_breach_delta_f > 0
    cool_candidate = decision.cool_breach_delta_f is not None and decision.cool_breach_delta_f > 0
    recovery_candidate = decision.outcome in (ComfortFamilyOutcome.RECOVERING, ComfortFamilyOutcome.REVERT)

    heat_since = resolve_candidate_since(
        candidate=heat_candidate or None,
        previous_candidate=dwell_state.heat_candidate_raw,
        previous_since=dwell_state.heat_candidate_since,
        now=now,
    )
    cool_since = resolve_candidate_since(
        candidate=cool_candidate or None,
        previous_candidate=dwell_state.cool_candidate_raw,
        previous_since=dwell_state.cool_candidate_since,
        now=now,
    )
    recovery_since = resolve_candidate_since(
        candidate=recovery_candidate or None,
        previous_candidate=dwell_state.recovery_candidate_raw,
        previous_since=dwell_state.recovery_since,
        now=now,
    )

    return ComfortFamilyDwellState(
        dwell_since=dwell_state.dwell_since,
        is_against_grain=dwell_state.is_against_grain,  # unchanged here — see transition()'s own overrides
        heat_candidate_since=heat_since,
        cool_candidate_since=cool_since,
        recovery_since=recovery_since,
        heat_candidate_raw=heat_candidate,
        cool_candidate_raw=cool_candidate,
        recovery_candidate_raw=recovery_candidate,
    )


def transition(
    current_state: ComfortFamilyState,
    event: ComfortFamilyEvent,
    dwell_state: ComfortFamilyDwellState | None = None,
) -> ComfortFamilyTransition:
    """The single entry point: given the current state, a live-inputs
    snapshot, and the FSM's own cross-call dwell bookkeeping, return the next
    state (and why). ``dwell_state`` defaults to a fresh
    ``ComfortFamilyDwellState()`` — the correct cold-start value (see module
    docstring's "Cold start" section): no prior dwell clock, no prior
    sustain-confirm candidates, evaluated immediately against real thresholds.
    """
    inputs = event.inputs
    state = dwell_state if dwell_state is not None else ComfortFamilyDwellState()

    leaf_decision = decide_comfort_family(_leaf_inputs(current_state, inputs, state))
    next_dwell_state = _advance_dwell_bookkeeping(leaf_decision, state, inputs.now)

    wants_transition = leaf_decision.outcome in (ComfortFamilyOutcome.ESCALATE, ComfortFamilyOutcome.REVERT)

    if not wants_transition:
        # HOLD / WITHIN_DEADBAND / OVERRIDE_HELD / SUSTAINING / RECOVERING /
        # NOT_APPLICABLE — the dwell clock never moves on any of these; only a
        # genuine transition advances it (this is the structural #823 fix).
        return ComfortFamilyTransition(
            from_state=current_state,
            to_state=current_state,
            event_kind=event.kind,
            decision=leaf_decision,
            dwell_state=next_dwell_state,
            at=inputs.now,
        )

    target_state = (
        ComfortFamilyState.HEATING if leaf_decision.target_family == "heating" else ComfortFamilyState.COOLING
    )

    if target_state == current_state:
        # Should not occur (the leaf only reports ESCALATE/REVERT when
        # target_family != current_family), but stay defensive rather than
        # advance the dwell clock on a no-op.
        return ComfortFamilyTransition(
            from_state=current_state,
            to_state=current_state,
            event_kind=event.kind,
            decision=leaf_decision,
            dwell_state=next_dwell_state,
            at=inputs.now,
        )

    if state.dwell_since is not None:
        elapsed = (inputs.now - state.dwell_since).total_seconds()
        if elapsed < inputs.min_dwell_seconds:
            # Locked out — the leaf wants to move, the dwell timer says not
            # yet. Hold the prior state; the shell emits
            # "comfort_family_switch_locked_out" using decision.target_family/
            # decision.reason.
            return ComfortFamilyTransition(
                from_state=current_state,
                to_state=current_state,
                event_kind=event.kind,
                decision=leaf_decision,
                dwell_state=next_dwell_state,
                locked_out=True,
                at=inputs.now,
            )

    # Genuine transition, dwell timer clear (or this is the very first
    # transition ever — dwell_since is None, cold start allows it
    # unconditionally, matching _family_switch_locked_out()'s own preserved
    # "cold start always allowed" precedent). Advance the dwell clock.
    return ComfortFamilyTransition(
        from_state=current_state,
        to_state=target_state,
        event_kind=event.kind,
        decision=leaf_decision,
        dwell_state=ComfortFamilyDwellState(
            dwell_since=inputs.now,
            # A REAL state change just happened — persist whether it was a
            # genuine breach-driven escalation (ESCALATE with a real breach
            # direction) or landed on native (REVERT, or a zero-friction
            # native pass-through) for the NEXT call's against-grain check.
            is_against_grain=leaf_decision.escalated_against_grain,
            heat_candidate_since=next_dwell_state.heat_candidate_since,
            cool_candidate_since=next_dwell_state.cool_candidate_since,
            recovery_since=next_dwell_state.recovery_since,
            heat_candidate_raw=next_dwell_state.heat_candidate_raw,
            cool_candidate_raw=next_dwell_state.cool_candidate_raw,
            recovery_candidate_raw=next_dwell_state.recovery_candidate_raw,
        ),
        at=inputs.now,
    )
