"""Pure decision core for the economizer (two-phase window-cooling) eligibility/
phase gate — strangler-fig completion program, Phase 5 (Issue #746).

Occupant framing: the economizer decides whether it is worth running the fan
(alone, or assisting the AC) to pull in already-open-window outdoor air instead
of relying purely on the compressor. Getting this gate wrong either wastes a
free-cooling opportunity (AC runs when a fan would have sufficed) or pulls in
outdoor air that is not actually helping (fan runs pointlessly, or worse, warm
outdoor air is drawn in against the occupant's comfort).

Scope: this module answers exactly one question — "given current conditions
(and assuming the economizer is even in play this tick), is it eligible to run,
and if so which phase (cool-down vs maintain)?" — as a pure function of an
explicit ``EconomizerGateInputs`` value. It does NOT decide whether the
economizer subsystem should even be consulted this tick (that's the day_type
and natural-vent-active short-circuits, which stay in ``economizer_fsm.py`` —
see that module's docstring for why, mirroring ``nat_vent_gate.py``'s own scope
boundary relative to ``nat_vent_fsm.py``).

Mirrors the exact eligibility/phase-selection logic in ``automation.py``'s
``check_window_cooling_opportunity()`` (Issue #27, with the Issue #327/#429
direction-guard consolidation) — this function exists to be differentially
validated against that method, not to introduce new behavior. Reuses
``temperature.free_cooling_direction_ok()`` unchanged rather than re-deriving
the direction check a third time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .temperature import free_cooling_direction_ok

PHASE_INACTIVE = "inactive"
PHASE_COOL_DOWN = "cool-down"
PHASE_MAINTAIN = "maintain"


@dataclass(frozen=True)
class EconomizerGateInputs:
    """Every input the eligibility/phase gate may read — explicit, nothing hidden.

    Field-by-field correspondence to the real
    ``check_window_cooling_opportunity()`` code:
      outdoor                 -> outdoor_temp parameter
      indoor                  -> indoor_temp parameter
      comfort_cool             -> config "comfort_cool"
      delta                    -> config "economizer_temp_delta"
      windows_physically_open  -> windows_physically_open parameter
      in_window                -> the caller-resolved time-of-day gate (morning
                                   6-9 / evening 17-24), same convention as
                                   nat_vent_gate.py's in_sleep_window: resolved
                                   once by the caller from current_hour, never
                                   re-derived from a live wall clock in here
      aggressive_savings       -> config "aggressive_savings"
    """

    outdoor: float
    indoor: float | None
    comfort_cool: float
    delta: float
    windows_physically_open: bool
    in_window: bool
    aggressive_savings: bool


@dataclass(frozen=True)
class EconomizerDecision:
    """The gate's full decision — eligibility, selected phase, and the
    direction-check result (exposed so the caller can reproduce production's
    own direction-rejected debug log without recomputing the check)."""

    eligible: bool
    phase: str  # PHASE_INACTIVE / PHASE_COOL_DOWN / PHASE_MAINTAIN
    direction_ok: bool


def decide_economizer_transition(inputs: EconomizerGateInputs) -> EconomizerDecision:
    """Pure reimplementation of the eligibility + phase-selection logic inside
    ``check_window_cooling_opportunity()`` (the part reached once the caller has
    already confirmed it's a hot day and natural ventilation is not active —
    those two short-circuits are session-level concerns handled by
    ``economizer_fsm.py``, not this gate).

    Same eligibility formula, same two-phase selection (aggressive_savings ->
    maintain immediately; otherwise indoor > comfort_cool -> cool-down, else
    maintain) as production.
    """
    direction_ok = free_cooling_direction_ok(inputs.outdoor, inputs.indoor)
    eligible = (
        inputs.windows_physically_open
        and inputs.outdoor <= inputs.comfort_cool + inputs.delta
        and inputs.in_window
        and direction_ok
    )

    if not eligible:
        return EconomizerDecision(eligible=False, phase=PHASE_INACTIVE, direction_ok=direction_ok)

    if inputs.aggressive_savings:
        phase = PHASE_MAINTAIN
    elif inputs.indoor is not None and inputs.indoor > inputs.comfort_cool:
        phase = PHASE_COOL_DOWN
    else:
        phase = PHASE_MAINTAIN

    return EconomizerDecision(eligible=True, phase=phase, direction_ok=direction_ok)
