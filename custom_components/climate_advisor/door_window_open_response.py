"""Pure decision core for a fresh door/window open event (Issue #637, Block 5 Phase 2).

Reimplements ``handle_door_window_open()``'s guard chain (already-paused check,
grace suppression, planned-window exemption, nat-vent eligibility) up to the
point where production either hands off to nat-vent or falls through to a pause.

**Deviation from the Phase 2 plan's initial outcome list**: the plan's draft
listed a standalone ``GRACE_FALL_THROUGH`` outcome alongside the terminal ones.
On implementation, "grace suppressed but outdoor is cool enough to continue
evaluating" is not itself a distinguishable decision a caller needs to act on —
production's own code re-enters the SAME planned-window/nat-vent/pause chain
below it, not a different one. Modeling it as a separate terminal outcome would
require the assembly FSM to call this function twice for one event (once to
learn "fell through", again for what happens next) — extra indirection with no
behavioral payoff, and a DRY violation against this project's own
``desired_state.decide_scheduled_band_gate()`` precedent (one gate, evaluated
once, not staged). The fall-through is handled as plain internal control flow
instead; only outcomes that require the assembly FSM to route to a genuinely
different next state are modeled as enum members.

**Nat-vent gate reuse**: ``nat_vent_gate_entered`` is caller-resolved (via
``nat_vent_gate.decide_nat_vent_gate()``, hysteresis=0.0 — matching this call
site's real behavior) rather than re-derived here, per the "no logic duplicated
across modules" rule this whole FSM effort is built around.

**Explicit scope boundary — Phase 2 forecast/thermal-floor nat-vent guards.**
NOT modeled here, same discipline as ``nat_vent_fsm.py``'s own exclusions:
production's ``handle_door_window_open()`` applies two additional guards
(rising-forecast lookahead, thermal-floor imminence) AFTER the nat-vent gate
passes, which can still skip nat-vent and fall through to pause even when this
module returns ``NAT_VENT_ELIGIBLE``. Neither guard has its own pure extraction
yet — folding them in here would silently introduce unvalidated behavior.
Candidate for a future revision once evidenced by their own pure extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DoorOpenResponse(Enum):
    ALREADY_PAUSED_NOOP = "already_paused_noop"
    GRACE_SUPPRESSED = "grace_suppressed"
    PLANNED_WINDOW_NOOP = "planned_window_noop"
    NAT_VENT_ELIGIBLE = "nat_vent_eligible"
    PAUSE = "pause"


@dataclass(frozen=True)
class DoorOpenResponseInputs:
    """Field-by-field correspondence to ``handle_door_window_open()``:

    paused_by_door        -> self._paused_by_door
    grace_active           -> self._grace_active
    outdoor                 -> self._last_outdoor_temp
    comfort_cool            -> config comfort_cool
    nat_vent_delta          -> config CONF_NATURAL_VENT_DELTA
    within_planned_window   -> self._is_within_planned_window_period()
    nat_vent_gate_entered   -> self._nat_vent_may_reactivate(..., hysteresis=0.0) —
                                caller-resolved via nat_vent_gate.decide_nat_vent_gate()
    """

    paused_by_door: bool
    grace_active: bool
    outdoor: float | None
    comfort_cool: float
    nat_vent_delta: float
    within_planned_window: bool
    nat_vent_gate_entered: bool


def decide_door_open_response(inputs: DoorOpenResponseInputs) -> DoorOpenResponse:
    """Pure reimplementation of ``handle_door_window_open()``'s guard chain."""
    if inputs.paused_by_door:
        return DoorOpenResponse.ALREADY_PAUSED_NOOP

    if inputs.grace_active and not inputs.nat_vent_gate_entered:
        return DoorOpenResponse.GRACE_SUPPRESSED
    # else (grace_active and gate entered): real reactivation gate says viable —
    # fall through to the same chain below, matching production's fallthrough.

    if inputs.within_planned_window:
        return DoorOpenResponse.PLANNED_WINDOW_NOOP

    if inputs.nat_vent_gate_entered:
        return DoorOpenResponse.NAT_VENT_ELIGIBLE

    return DoorOpenResponse.PAUSE
