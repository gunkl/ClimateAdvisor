"""Pure decision core for the ODE floor guard (Issue #821).

Mirrors ``ode_ceiling_guard.py``'s contract shape (frozen ``Inputs`` dataclass,
``Outcome`` enum, ``Decision`` dataclass, zero side effects, zero logging) for the
opposite edge: a day classified for cooling (``classification.hvac_mode == "cool"``)
whose comfort band therefore never arms a defended floor, because
``select_comfort_band()``/``_apply_comfort_band()`` only issue a ``set_temperature``
call for the day's active edge (the ceiling on a cool day) — see Issue #821's root
cause. This module answers: given the day says cool, does the comfort floor need
active defense (a switch to the heating family) right now, or can it ride out the dip
per the forecast (the WHF-overshoot concern the project owner raised)?

**Does NOT independently scan ``predicted_indoor`` for the floor crossing.** Issue
#817 made ``nat_vent_plan.compute_nat_vent_plan()`` the single source of truth for
"when does a comfort-floor crossing happen" (``comfort_floor_crossing_time``, added by
this same issue) — a second, separate scanner here would reintroduce exactly the class
of duplicate-computation bug #817 fixed (#528/#814/#818) and would fail
``tests/test_nat_vent_plan_single_source.py``'s AST enforcement if it called
``compute_nat_vent_plan()`` from a new, undeclared site. Instead, this module's
``Inputs.floor_crossing_time`` is the caller-resolved value read off the coordinator's
already-cached ``self._nat_vent_plan["comfort_floor_crossing_time"]`` — the shell's
job, not this module's.

This module's genuine value-add is the ESCALATE/STANDING_BY lead-time judgment on top
of that shared crossing time, using ``compute_lead_minutes_from_rate()`` with
``k_active_heat`` — a new reuse (the existing plan module uses ``k_active_cool`` for
its own ceiling-precool margin; the ceiling guard also uses ``k_active_cool`` for its
own escalation lead time). Requires ``confidence_k_passive != "none"`` before trusting
the cached crossing time at all — ``MODEL_INELIGIBLE`` otherwise, matching
``ode_ceiling_guard.py``'s own precedent exactly. This is deliberately NOT what fixes
the reported live incident (Zone "Simulated 2", confidence_k_passive == "none" during
the entire incident window) — see the fallback path in ``automation.py``'s
``_resolve_comfort_family_via_fsm()``/``comfort_family_decision.py``'s
``_check_direction()`` sustain-confirm branch for the conservative, non-ODE guard
that actually covers that case. (Issue #858: this incident recurred even with that
fallback in place — root cause was the ~30-minute ``apply_classification()`` cadence,
not this guard or the fallback's own decision logic, which a deterministic
reproduction confirmed defect-free; see ``comfort_family_temperature_check()``.)

Scoped to ``hvac_mode != "off"`` (the logical complement of
``ode_ceiling_guard.py``'s own ``hvac_mode == "off"`` gate) — a shoulder day's diurnal
swing (cold pre-dawn dip, warm afternoon rise, both within each guard's own lookahead)
could otherwise make both guards independently escalate in the same cycle with no
cross-check. Restricting each guard to its own complementary day-type keeps escalation
to `cool`/`heat`-classified days as this module's exclusive territory and `off`-day
proactive-cooling escalation as the ceiling guard's, structurally preventing a
contradictory double command from a single indoor reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from .thermal_lead_time import compute_lead_minutes_from_rate

_LEAD_MIN_FLOOR = 15.0
_LEAD_MIN_CEIL = 180.0
_LEAD_MIN_LOOKAHEAD_MULTIPLIER = 1.3
_FLOOR_DEFENSE_FALLBACK_MIN = 60.0

_HVAC_MODE_OFF = "off"


class OdeFloorGuardOutcome(Enum):
    """Why the guard did (or did not) escalate to the heating family."""

    NOT_APPLICABLE = "not_applicable"  # hvac_mode == "off" (ceiling guard's territory), or nat-vent owns HVAC
    MODEL_INELIGIBLE = "model_ineligible"  # confidence_k_passive == "none" — see the fallback path instead
    MISSING_TEMPS = "missing_temps"  # indoor or comfort_heat unavailable
    NO_BREACH_PREDICTED = "no_breach_predicted"  # cached plan has no comfort-floor crossing
    STANDING_BY = "standing_by"  # breach predicted, but outside the lead-time window — let it ride out
    ESCALATE = "escalate"  # breach within lead time (or already breached) — shell should switch to heat


@dataclass(frozen=True)
class OdeFloorGuardDecision:
    """The outcome, plus the computed values the shell's logging/event payload needs
    (mirrors ``OdeCeilingGuardDecision``'s convention of carrying its own audit fields)."""

    outcome: OdeFloorGuardOutcome
    breach_ts: datetime | None = None
    hours_to_breach: float | None = None
    lead_min: float | None = None


@dataclass(frozen=True)
class OdeFloorGuardInputs:
    """Every input the guard may read — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine``/coordinator reads:
      hvac_mode              -> classification.hvac_mode
      natural_vent_active    -> self._natural_vent_active — the guard defers while
                                 nat-vent/WHF owns HVAC; it only fills the gap where no
                                 active session is riding the floor down
      floor_crossing_time    -> self._nat_vent_plan.get("comfort_floor_crossing_time")
                                 (Issue #817/#821 single source of truth — see module
                                 docstring)
      confidence_k_passive   -> (self._thermal_model or {}).get("confidence_k_passive")
                                 or .get("confidence", "none")
      k_active_heat          -> (self._thermal_model or {}).get("k_active_heat")
      comfort_heat            -> config comfort_heat (sleep-window-resolved by the
                                  caller, same convention as nat_vent_exit.py's
                                  comfort_heat_raw/sleep_heat inputs — this module takes
                                  the single already-resolved value, it does not
                                  re-derive sleep-window awareness itself)
      indoor                   -> self._get_indoor_temp_f()
      now                       -> caller-resolved wall-clock time (dt_util.now())
    """

    hvac_mode: str | None
    natural_vent_active: bool
    floor_crossing_time: datetime | None
    confidence_k_passive: str
    k_active_heat: float | None
    comfort_heat: float | None
    indoor: float | None
    now: datetime


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def decide_ode_floor_guard(inputs: OdeFloorGuardInputs) -> OdeFloorGuardDecision:
    """Decide whether the comfort floor needs active defense (a switch to heat) right
    now, using the already-cached comfort-floor crossing time — see module docstring."""
    if inputs.hvac_mode == _HVAC_MODE_OFF or inputs.natural_vent_active:
        return OdeFloorGuardDecision(outcome=OdeFloorGuardOutcome.NOT_APPLICABLE)

    if inputs.confidence_k_passive == "none":
        return OdeFloorGuardDecision(outcome=OdeFloorGuardOutcome.MODEL_INELIGIBLE)

    if inputs.indoor is None or inputs.comfort_heat is None:
        return OdeFloorGuardDecision(outcome=OdeFloorGuardOutcome.MISSING_TEMPS)

    if inputs.floor_crossing_time is None:
        return OdeFloorGuardDecision(outcome=OdeFloorGuardOutcome.NO_BREACH_PREDICTED)

    hours_to_breach = (_to_utc(inputs.floor_crossing_time) - _to_utc(inputs.now)).total_seconds() / 3600

    lead_min = compute_lead_minutes_from_rate(
        delta_t=inputs.indoor - inputs.comfort_heat,
        rate=inputs.k_active_heat,
        min_minutes=_LEAD_MIN_FLOOR,
        max_minutes=_LEAD_MIN_CEIL,
        safety_multiplier=_LEAD_MIN_LOOKAHEAD_MULTIPLIER,
        fallback_minutes=_FLOOR_DEFENSE_FALLBACK_MIN,
    )

    if hours_to_breach <= lead_min / 60:
        return OdeFloorGuardDecision(
            outcome=OdeFloorGuardOutcome.ESCALATE,
            breach_ts=inputs.floor_crossing_time,
            hours_to_breach=hours_to_breach,
            lead_min=lead_min,
        )

    return OdeFloorGuardDecision(
        outcome=OdeFloorGuardOutcome.STANDING_BY,
        breach_ts=inputs.floor_crossing_time,
        hours_to_breach=hours_to_breach,
        lead_min=lead_min,
    )
