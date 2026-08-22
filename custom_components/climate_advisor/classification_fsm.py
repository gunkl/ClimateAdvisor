"""Classification decision FSM (Issue #742, strangler-fig completion Phase 3).

Assembles the pieces this phase built/reused into one explicit
transition function, following the shape ``nat_vent_fsm.py`` /
``door_window_fsm.py`` / ``override_grace_fsm.py`` / ``fan_fsm.py`` all
established — but with one deliberate, investigated departure. Read this
whole docstring before touching this module; the departure is load-bearing.

- ``desired_state.decide_scheduled_band_gate()`` (existing, reused unchanged,
  Issue #498) — occupancy/override/paused/nat-vent defer gate.
- ``ode_ceiling_guard.decide_ode_ceiling_guard()`` (this phase, Issue #742) —
  the proactive-cooling escalation decision.

This module does not re-derive or duplicate either piece's own logic — it is
integration/wiring only: given one cycle's live inputs, which gate result
applies, and (only when the gate and mode allow) what the ceiling guard
decides. Each of the 2 composed pieces already has its own exhaustive
unit-test coverage; this module's own tests focus on wiring correctness —
same scoping discipline every other ``*_fsm.py`` module's own docstring
states for itself.

**Five-whys: why is this FSM STATELESS (no persisted lifecycle state, unlike
all 4 precedent FSMs)?** (1) Why no ``ClassificationLifecycleState``? Because
``apply_classification()`` has no genuine multi-tick session state to
compose — ``self._current_classification`` is DATA (a fresh ``DayClassification``
handed in by the caller each cycle), not a state machine that persists and
evolves across calls the way nat-vent's ACTIVE/PAUSED axes or fan's 5 composed
axes do. (2) Why does that matter? Every precedent FSM's ``*LifecycleState``
answers "what is the subsystem doing RIGHT NOW, independent of this one
event" — there is no such question here: ``apply_classification()`` computes
its entire decision fresh from the classification argument, the current
gate-relevant flags, and the current thermal model snapshot, then returns.
Nothing it decides this cycle constrains or is remembered by the next cycle
(the 30-minute scheduler simply calls it again with fresh inputs). (3) Why not
force a state object anyway, for shape parity with the other 4? Because a
state dataclass with no real transitions to model would either sit permanently
at one synthetic "value" (dead weight, actively misleading — a future reader
would reasonably assume it tracks something) or require inventing states that
don't correspond to anything the real system does, which is exactly the
"fake session lifecycle" failure mode this phase's own design brief warned
against. (4) Why does the module still export an ``Event``/``EventKind`` shape
if there's no state to transition between? For the same audit-trail and
call-site-uniformity value every other FSM's ``transition()`` entry point
gives its callers — ``_resolve_classification_fsm_state()`` in automation.py
builds one ``ClassificationFsmEvent`` per cycle and gets one ``ClassificationDecision``
back, matching the calling convention every other ``_resolve_*_fsm_state()``
dispatcher uses, even though this one has no ``current_state`` parameter to
thread through. (5) Why one single event kind (``CYCLE_EVALUATED``) instead of
six, one per real production call site? Because all 6 call sites (``api.py``,
the grace-expiry re-entry, startup coalesce, the 30-min cycle, and two more in
coordinator.py) invoke the exact same ``apply_classification()`` method with no
call-site-specific branching inside it — the method itself never asks "who
called me," only "what are the current inputs." A per-call-site event vocabulary
would model a distinction production doesn't make.

**Ceiling-guard eligibility branches mirror ``apply_classification()``'s own
control flow exactly** (automation.py's early-return chain before the ceiling
guard section is ever reached): ``DEFER_OCCUPANCY`` and ``DEFER_PAUSED`` both
return before the predicted-indoor block; ``DEFER_NAT_VENT`` additionally
short-circuits when ``aggressive_savings`` is on (savings mode never runs the
compressor through open windows) or when the fan archetype is WHOLE_HOUSE/BOTH
(Issue #392 Fix 1b — WHF is mutually exclusive with the compressor by design).
Only ``PROCEED``, or ``DEFER_NAT_VENT`` with a HVAC-fan (or disabled-fan)
archetype under non-aggressive savings, ever reaches the guard. This module
encodes that exact same branch order as ``CeilingGuardEligibility`` — a pure
mirror, not a reinterpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .desired_state import ScheduledBandGate, decide_scheduled_band_gate
from .ode_ceiling_guard import OdeCeilingGuardDecision, OdeCeilingGuardInputs, decide_ode_ceiling_guard

_FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
_FAN_MODE_BOTH = "both"


class ClassificationFsmEventKind(Enum):
    """What prompted this transition evaluation. Deliberately a single member —
    see module docstring's five-whys, point 5, for why all 6 real production
    call sites (``api.py:578``, ``automation.py``'s grace-expiry re-entry,
    ``coordinator.py``'s startup coalesce / 30-min cycle / two other sites)
    share this one kind rather than getting a member each."""

    CYCLE_EVALUATED = "cycle_evaluated"


class CeilingGuardEligibility(Enum):
    """Why the ceiling guard was or wasn't reached this cycle — mirrors
    apply_classification()'s own early-return chain (see module docstring)."""

    NOT_EVALUATED_OCCUPANCY_DEFER = "not_evaluated_occupancy_defer"
    NOT_EVALUATED_PAUSED_DEFER = "not_evaluated_paused_defer"
    NOT_EVALUATED_SAVINGS_NAT_VENT = "not_evaluated_savings_nat_vent"
    NOT_EVALUATED_WHF_ARCHETYPE = "not_evaluated_whf_archetype"
    EVALUATED = "evaluated"


@dataclass(frozen=True)
class ClassificationFsmInputs:
    """Every live reading this FSM's transition function may consult — explicit,
    nothing hidden. Union of ``decide_scheduled_band_gate()``'s 5 keyword
    arguments plus ``OdeCeilingGuardInputs``' fields plus the 2 extra flags
    (``aggressive_savings``, ``fan_mode``) needed to replicate
    apply_classification()'s DEFER_NAT_VENT short-circuit.

    Field-by-field correspondence to the real ``AutomationEngine``/config reads
    matches ``desired_state.decide_scheduled_band_gate()``'s own docstring and
    ``ode_ceiling_guard.OdeCeilingGuardInputs``'s own docstring exactly (not
    repeated here). Additional fields:
      aggressive_savings -> config "aggressive_savings" (apply_classification()'s
                             DEFER_NAT_VENT savings-mode short-circuit)
      fan_mode            -> config CONF_FAN_MODE (apply_classification()'s
                             DEFER_NAT_VENT WHOLE_HOUSE/BOTH short-circuit,
                             Issue #392 Fix 1b)
    """

    # --- decide_scheduled_band_gate() fields ---
    occupancy_mode: str
    manual_override_active: bool
    paused_by_door: bool
    natural_vent_active: bool
    whf_owns_hvac: bool

    # --- DEFER_NAT_VENT short-circuit fields ---
    aggressive_savings: bool
    fan_mode: str

    # --- OdeCeilingGuardInputs fields ---
    predicted_indoor: list[dict] | None
    hvac_mode: str | None
    k_passive: float | None
    confidence_k_passive: str
    k_passive_via_bridge: bool
    k_active_cool: float | None
    comfort_cool: float | None
    outdoor: float | None
    indoor: float | None
    ceiling_threshold: float | None

    now: datetime


@dataclass(frozen=True)
class ClassificationFsmEvent:
    kind: ClassificationFsmEventKind
    inputs: ClassificationFsmInputs


@dataclass(frozen=True)
class ClassificationDecision:
    """The full decision record for one cycle — audit trail by construction.

    ``ceiling_decision`` is ``None`` unless ``ceiling_eligibility ==
    CeilingGuardEligibility.EVALUATED`` — mirrors every other FSM's convention
    of leaving shell-directive fields unpopulated when the branch that would
    populate them never ran.
    """

    event_kind: ClassificationFsmEventKind
    gate: ScheduledBandGate
    ceiling_eligibility: CeilingGuardEligibility
    ceiling_decision: OdeCeilingGuardDecision | None = None
    at: datetime | None = None


def _ceiling_guard_inputs(inputs: ClassificationFsmInputs) -> OdeCeilingGuardInputs:
    return OdeCeilingGuardInputs(
        predicted_indoor=inputs.predicted_indoor,
        hvac_mode=inputs.hvac_mode,
        k_passive=inputs.k_passive,
        confidence_k_passive=inputs.confidence_k_passive,
        k_passive_via_bridge=inputs.k_passive_via_bridge,
        k_active_cool=inputs.k_active_cool,
        comfort_cool=inputs.comfort_cool,
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        natural_vent_active=inputs.natural_vent_active,
        ceiling_threshold=inputs.ceiling_threshold,
        now=inputs.now,
    )


def transition(event: ClassificationFsmEvent) -> ClassificationDecision:
    """The single entry point: given one cycle's live-inputs snapshot, return
    the gate result and (when reached) the ceiling-guard decision.

    No ``current_state`` parameter, unlike the other 4 ``*_fsm.py`` modules —
    see module docstring's five-whys for why this FSM is genuinely stateless.
    """
    inputs = event.inputs
    gate = decide_scheduled_band_gate(
        occupancy_mode=inputs.occupancy_mode,
        manual_override_active=inputs.manual_override_active,
        paused_by_door=inputs.paused_by_door,
        natural_vent_active=inputs.natural_vent_active,
        whf_owns_hvac=inputs.whf_owns_hvac,
    )

    if gate == ScheduledBandGate.DEFER_OCCUPANCY:
        return ClassificationDecision(
            event.kind, gate, CeilingGuardEligibility.NOT_EVALUATED_OCCUPANCY_DEFER, at=inputs.now
        )
    if gate == ScheduledBandGate.DEFER_PAUSED:
        return ClassificationDecision(
            event.kind, gate, CeilingGuardEligibility.NOT_EVALUATED_PAUSED_DEFER, at=inputs.now
        )
    if gate == ScheduledBandGate.DEFER_NAT_VENT:
        if inputs.aggressive_savings:
            return ClassificationDecision(
                event.kind, gate, CeilingGuardEligibility.NOT_EVALUATED_SAVINGS_NAT_VENT, at=inputs.now
            )
        if inputs.fan_mode in (_FAN_MODE_WHOLE_HOUSE, _FAN_MODE_BOTH):
            return ClassificationDecision(
                event.kind, gate, CeilingGuardEligibility.NOT_EVALUATED_WHF_ARCHETYPE, at=inputs.now
            )
        # Falls through — DEFER_NAT_VENT with a HVAC-fan (or disabled-fan)
        # archetype under non-aggressive savings still reaches the guard,
        # matching apply_classification()'s own fallthrough.

    ceiling_decision = decide_ode_ceiling_guard(_ceiling_guard_inputs(inputs))
    return ClassificationDecision(event.kind, gate, CeilingGuardEligibility.EVALUATED, ceiling_decision, at=inputs.now)
