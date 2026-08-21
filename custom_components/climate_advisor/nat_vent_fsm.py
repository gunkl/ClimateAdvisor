"""Nat-vent lifecycle FSM — the unified transition table (Issue #633, Block 5
Phase P completion, epic #594).

Assembles the 3 existing, already-validated pure pieces into one explicit
``(state, event) -> Transition`` function — the piece Phase P's earlier
sub-issues (#606/#607, #608/#609) deliberately left unbuilt:

- ``nat_vent_lifecycle.derive_nat_vent_lifecycle_state()`` — the 4-state enum
  this module reuses unchanged.
- ``nat_vent_gate.decide_nat_vent_gate()`` / ``decide_nat_vent_soft_start_gate()``
  — entry/reactivation decisions, called only while inactive/locked-out.
- ``nat_vent_exit.decide_nat_vent_exit()`` — the active-session exit chain,
  called only while active.

This module does not re-derive or duplicate any of that logic — it is
integration/wiring only: which pure function to call given the current state,
and how to map its result to the next state. Each of the 3 pieces already has
its own exhaustive unit-test coverage; this module's own tests focus on the
wiring correctness, not re-proving gate/exit logic already proven elsewhere.

**Markov property**: every transition is a pure function of (current state,
event) alone — no hidden extra memory. This is why ``ACTIVE_SOFT_START`` and
``ACTIVE_FULL_GATE`` are distinct states rather than one ``ACTIVE`` state plus
an out-of-band "how did we get here" flag (this precedent already exists in
``nat_vent_lifecycle.py``, reused here).

**v2 (Phase R prep, Issue #594 follow-up): soft-start escalation now modeled.**
``_transition_from_active()`` re-checks ``decide_nat_vent_gate()`` — the same
pure function ``_transition_from_inactive()`` already calls — whenever the
current state is ``ACTIVE_SOFT_START``, before running the exit chain. This
mirrors production's own upgrade check at ``automation.py``'s
"soft-start → full nat-vent upgrade" block (Issue #540), which independently
re-evaluates the identical full-gate condition each active tick. No new
decision logic was written — this reuses the one pure function already in
scope, the same discipline every other piece of this module already follows.

**Still-explicit scope boundary — the ``_idle_open`` widening.** Not modeled,
but re-classified: this is not omitted decision logic, it's a **caller-side
triggering precondition**. ``check_natural_vent_conditions()``
(Issue #244/#402/#504/#620) only re-evaluates reactivation on a given tick if
a contact sensor is open, HVAC is idle, debounce has settled, and grace isn't
blocking it (or grace + over-ceiling). That gate decides *whether this FSM's
entry logic runs at all this tick*, not what it should decide once run — the
same relationship ``paused_by_door`` already has to this module (an external
fact fed in, not re-derived here). Once Step 2's cutover work makes this FSM's
``transition()`` the thing production's own call site invokes, it will
naturally only fire when that same precondition already holds, since it's the
same call site — no separate modeling needed inside the transition table
itself.

**Cross-lifecycle inputs.** ``paused_by_door`` is a state *read* from the
door/window lifecycle (Issue #631's "communicating automata" design) — legacy
flag-derived today, unchanged in shape once door/window gets its own FSM.

**Override/grace awareness (Issue #687, Phase 2a).** ``override_active``
(``AutomationEngine._fan_override_active`` or ``_manual_override_active``) and
``grace_active`` (``AutomationEngine._grace_active``) are now read inputs too.
While either is true, both ``_transition_from_inactive()`` and
``_transition_from_active()`` short-circuit straight to ``INACTIVE`` before any
gate/exit math runs. This mirrors production's real guarding, but split across
two different call sites, not one: ``_activate_fan()``'s own early return
("Fan override active — skipping fan activation") checks only
``_fan_override_active``; the ``_grace_active`` guard is separately enforced in
``check_natural_vent_conditions()``. Previously the FSM had no way to see
either flag and would report ``ACTIVE_FULL_GATE`` for the full duration of a
manual override/grace window while production correctly stayed inactive —
confirmed live as the single largest disagreement bucket in a night of logs
(38 of 114 disagreement lines). This closes that specific gap; it does not
implement any other new decision authority (see epic #594 Phase R for the
larger authority question).

**Issue #134 overheat-during-grace exception (Issue #706, closes #688).**
Production's grace guard has an exception that *allows* nat-vent to engage
during grace when indoor genuinely exceeds ``comfort_cool`` (overheat
protection — ``automation.py``'s real condition:
``self._grace_active and indoor is not None and indoor > comfort_cool``).
This module's grace short-circuit now models that same exception via
``_grace_blocks_natvent()``, shared by both ``_transition_from_active()`` and
``_transition_from_inactive()``: grace blocks nat-vent UNLESS indoor is known
and exceeds ``comfort_cool``, in which case the short-circuit does not fire.
``override_active`` is unaffected — a manual fan override always wins
regardless of temperature.

This was originally shipped (Issue #687) as a known diagnostic-only gap,
because at the time this FSM was shadow/diagnostic-only and the field
defaulted to ``False`` in every real production construction (Issue #706
Bug D). Once Bug D wires ``grace_active`` to a real live value in
``automation.py``'s own ``_build_nat_vent_fsm_inputs()``, this exception
becomes load-bearing — without it, nat-vent would wrongly shut off during a
genuine overheat-during-grace window once ``_natvent_fsm_authoritative`` is
enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .nat_vent_cycling import NatVentCyclingInputs, decide_nat_vent_cycling
from .nat_vent_exit import NatVentExitInputs, NatVentExitReason, decide_nat_vent_exit
from .nat_vent_gate import (
    NatVentGateInputs,
    NatVentSoftStartGateInputs,
    decide_nat_vent_gate,
    decide_nat_vent_soft_start_gate,
)
from .nat_vent_lifecycle import NatVentLifecycleState
from .nat_vent_reactivation_lockout import is_reactivation_locked_out


class NatVentFsmEventKind(Enum):
    """What prompted this transition evaluation. Every kind runs the same
    re-evaluation logic in v1 (see module docstring) — captured for the audit
    trail (epic success criterion #2), not yet branched on internally."""

    TICK = "tick"
    DOOR_PAUSE_STARTED = "door_pause_started"
    DOOR_PAUSE_ENDED = "door_pause_ended"
    GRACE_STARTED = "grace_started"
    GRACE_ENDED = "grace_ended"
    OVERRIDE_CONFIRMED = "override_confirmed"
    OVERRIDE_CLEARED = "override_cleared"
    # Issue #673 Phase 3 audit: confirmed only TICK is fed from any real call site
    # (automation.py, coordinator.py) — the other 6 members are unused-in-v1, exactly
    # as this class's own docstring above says. Confirmed safe by grep: transition()
    # only ever assigns `event_kind=event.kind` into the result record, never branches
    # on it (`if`/`==` against .kind does not appear anywhere in this module), so
    # feeding TICK for a door-pause/grace/override-triggered re-evaluation produces the
    # identical transition a "correctly" kinded event would. No gap; not wired further.


@dataclass(frozen=True)
class NatVentFsmInputs:
    """Every live reading this FSM's transition function may consult —
    explicit, nothing hidden. Union of ``NatVentGateInputs``,
    ``NatVentSoftStartGateInputs``, and ``NatVentExitInputs``' fields, plus the
    cross-lifecycle state reads (``paused_by_door``) and the lockout/outdoor-
    exit-time bookkeeping ``derive_nat_vent_lifecycle_state()`` already reads.
    """

    indoor: float | None
    outdoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    comfort_cool: float
    nat_vent_delta: float
    hysteresis: float
    fan_mode: str
    aggressive_savings: bool
    occupancy_mode: str
    thermal_confidence: str
    k_passive: float | None
    outdoor_today_peak: float | None
    outdoor_sample_count: int
    peak_decline_margin: float
    paused_by_door: bool
    outdoor_exit_time: datetime | None
    lockout_seconds: float
    now: datetime
    # Default False (rather than requiring every existing construction site to be
    # updated) is a deliberate choice: automation.py's own
    # ``_build_nat_vent_fsm_inputs()`` construction site is out of scope for this
    # fix (Issue #687, Phase 2a is confined to nat_vent_fsm.py + how
    # coordinator.py feeds it — see that issue's scope note). A default lets this
    # dataclass change land without touching production automation.py logic;
    # coordinator.py's own construction site is updated explicitly below to pass
    # real values rather than relying on the default.
    override_active: bool = False
    grace_active: bool = False
    # Issue #698 (Phase 2d): the live fan-hardware on/off flag (AutomationEngine's
    # ``_fan_active``) -- distinct from the session flag ``_natural_vent_active``
    # this FSM's states already model. Default False mirrors ``override_active``/
    # ``grace_active``'s own non-breaking-default precedent: only
    # ``nat_vent_temperature_check()``'s FSM-authoritative branch (the one caller
    # that needs mid-session cycling decisions) passes a real value; every other
    # existing call site's inputs are unaffected by this field, since
    # ``decide_nat_vent_cycling()`` is only reached when the exit chain returns
    # NONE and the resulting state is one of the two ACTIVE_* states (see
    # ``_transition_from_active()`` below) -- those other call sites don't read
    # ``NatVentTransition.fan_should_be_active`` at all.
    fan_hardware_active: bool = False
    # Issue #714: distinct from override_active above (which OR's in
    # _fan_override_active and carries no mode information) -- this pair is
    # specifically the HVAC-mode override the manual-override-vs-nat-vent mutex
    # needs (NatVentExitInputs.manual_override_active/manual_override_mode). A
    # bare fan-on override doesn't set an HVAC mode and must not trip this check.
    # Default False/None mirrors this dataclass's own established non-breaking-
    # default precedent for every field added after the original construction.
    manual_override_active: bool = False
    manual_override_mode: str | None = None


@dataclass(frozen=True)
class NatVentFsmEvent:
    kind: NatVentFsmEventKind
    inputs: NatVentFsmInputs


@dataclass(frozen=True)
class NatVentTransition:
    """The transition's full record — audit trail by construction."""

    from_state: NatVentLifecycleState
    to_state: NatVentLifecycleState
    event_kind: NatVentFsmEventKind
    exit_reason: NatVentExitReason | None = None
    at: datetime | None = None
    # Issue #698 (Phase 2d): populated ONLY when the exit chain returned NONE and
    # to_state is one of the two ACTIVE_* states (see _transition_from_active()) --
    # None in every other case (inactive states, any exit reason firing). Callers
    # that don't care about mid-session cycling (every call site wired before
    # Phase 2d) simply never read this field.
    fan_should_be_active: bool | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _exit_inputs(inputs: NatVentFsmInputs) -> NatVentExitInputs:
    return NatVentExitInputs(
        indoor=inputs.indoor,
        outdoor=inputs.outdoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        hysteresis=inputs.hysteresis,
        comfort_cool=inputs.comfort_cool,
        nat_vent_delta=inputs.nat_vent_delta,
        occupancy_mode=inputs.occupancy_mode,
        thermal_confidence=inputs.thermal_confidence,
        k_passive=inputs.k_passive,
        manual_override_active=inputs.manual_override_active,
        manual_override_mode=inputs.manual_override_mode,
    )


def _gate_inputs(inputs: NatVentFsmInputs) -> NatVentGateInputs:
    return NatVentGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        comfort_cool=inputs.comfort_cool,
        nat_vent_delta=inputs.nat_vent_delta,
        hysteresis=inputs.hysteresis,
        fan_mode=inputs.fan_mode,
        aggressive_savings=inputs.aggressive_savings,
    )


def _cycling_inputs(inputs: NatVentFsmInputs) -> NatVentCyclingInputs:
    return NatVentCyclingInputs(
        indoor=inputs.indoor,
        outdoor=inputs.outdoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        comfort_cool=inputs.comfort_cool,
        hysteresis=inputs.hysteresis,
        fan_hardware_active=inputs.fan_hardware_active,
    )


def _soft_start_inputs(inputs: NatVentFsmInputs, *, full_gate_active: bool) -> NatVentSoftStartGateInputs:
    return NatVentSoftStartGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_heat=inputs.comfort_heat_raw,
        comfort_cool=inputs.comfort_cool,
        fan_mode=inputs.fan_mode,
        outdoor_today_peak=inputs.outdoor_today_peak,
        outdoor_sample_count=inputs.outdoor_sample_count,
        peak_decline_margin=inputs.peak_decline_margin,
        full_gate_active=full_gate_active,
    )


def _grace_blocks_natvent(inputs: NatVentFsmInputs) -> bool:
    """Whether an active grace period should block nat-vent this tick (Issue #706,
    closes #688).

    Grace blocks nat-vent UNLESS indoor is known and genuinely exceeds
    ``comfort_cool`` — the Issue #134 overheat-during-grace exception production
    applies (``automation.py``: ``self._grace_active and indoor is not None and
    indoor > comfort_cool``). Shared by both ``_transition_from_active()`` and
    ``_transition_from_inactive()`` so the exception can't drift between the two
    call paths.
    """
    if not inputs.grace_active:
        return False
    return not (inputs.indoor is not None and inputs.comfort_cool is not None and inputs.indoor > inputs.comfort_cool)


def _transition_from_active(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    # Issue #687 (Phase 2a): a manual override or grace period wins over everything
    # else in this function, including the lockout-recognition check right below it.
    # Mirrors production's real guarding, split across two call sites: override
    # is _activate_fan()'s own early return ("Fan override active — skipping fan
    # activation"); grace is enforced separately in check_natural_vent_conditions()
    # WITH its Issue #134 overheat-during-grace exception, modeled here via
    # _grace_blocks_natvent() (Issue #706, closes #688). Placed first, before any
    # other branch.
    if event.inputs.override_active or _grace_blocks_natvent(event.inputs):
        return NatVentTransition(
            from_state=current_state,
            to_state=NatVentLifecycleState.INACTIVE,
            event_kind=event.kind,
            at=event.inputs.now,
        )

    # Issue #672: before anything else, recognize a door-pause/reactivation-lockout
    # condition — the one thing this branch previously had NO way to ever detect once
    # wrongly active. _transition_from_inactive() already checks this same condition on
    # every tick from an inactive-like origin, but transition() never routes back there
    # once current_state is ACTIVE_*, so a stale/incorrect ACTIVE_SOFT_START/ACTIVE_FULL_GATE
    # state was permanently stuck — decide_nat_vent_exit()'s exit chain below only
    # recognizes thermal/comfort exit reasons, never a pause/lockout condition. Confirmed
    # live: 34+ minutes and 2 full startup-coalesce cycles of real ticks landing here with
    # no way to reach PAUSED_REACTIVATION_LOCKOUT/INACTIVE (2026-08-17). Provably inert on
    # the live authoritative escalation path (automation.py's soft-start upgrade check) —
    # production's real flags never present natural_vent_active=True simultaneously with an
    # active lockout (_exit_nat_vent() always clears the former before/as part of setting
    # the field that drives the latter) — this only ever fires for the separately-tracked,
    # staleness-prone coordinator diagnostic state.
    inputs = event.inputs
    if inputs.paused_by_door and is_reactivation_locked_out(
        outdoor_exit_time=inputs.outdoor_exit_time, now=inputs.now, lockout_seconds=inputs.lockout_seconds
    ):
        return NatVentTransition(
            from_state=current_state,
            to_state=NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT,
            event_kind=event.kind,
            at=inputs.now,
        )

    # Issue #540 (mirrored, Phase R prep): while soft-started, re-check the full
    # gate each tick — same pure function, same condition production's own
    # upgrade block re-evaluates. Escalating doesn't change the exit-chain
    # outcome below (decide_nat_vent_exit() treats soft-start and full-gate
    # identically), it only changes what state a NONE-exit tick lands on.
    escalated_state = current_state
    if current_state == NatVentLifecycleState.ACTIVE_SOFT_START and decide_nat_vent_gate(_gate_inputs(event.inputs)):
        escalated_state = NatVentLifecycleState.ACTIVE_FULL_GATE

    decision = decide_nat_vent_exit(_exit_inputs(event.inputs))
    if decision.reason == NatVentExitReason.NONE:
        # Issue #698 (Phase 2d): the session continues -- now also decide whether the
        # fan HARDWARE should be on or off this tick (thermostat-style cycling around
        # the comfort midpoint). Only reached once the exit chain has cleared, mirroring
        # nat_vent_temperature_check()'s own priority order (hard-floor/exit checks
        # first, cycling second) -- decide_nat_vent_cycling() does not re-check any exit
        # condition itself (see its own docstring).
        cycling_decision = decide_nat_vent_cycling(_cycling_inputs(event.inputs))
        return NatVentTransition(
            from_state=current_state,
            to_state=escalated_state,
            event_kind=event.kind,
            at=event.inputs.now,
            fan_should_be_active=cycling_decision.fan_should_be_active,
        )

    # Only the outdoor-rise exit records an outdoor_exit_time in production
    # (nat_vent_lifecycle.py's own documented field correspondence) — and only
    # hands off to the locked-out pause state while the monitored sensor is
    # still open; otherwise it's a clean inactive exit (grace, if any, is a
    # separate not-yet-migrated lifecycle's concern, not this FSM's).
    if decision.reason == NatVentExitReason.OUTDOOR_RISE and event.inputs.paused_by_door:
        next_state = NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT
    else:
        next_state = NatVentLifecycleState.INACTIVE

    return NatVentTransition(
        from_state=current_state,
        to_state=next_state,
        event_kind=event.kind,
        exit_reason=decision.reason,
        at=event.inputs.now,
    )


def _transition_from_inactive(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    inputs = event.inputs

    # Issue #687 (Phase 2a): same override/grace short-circuit as
    # _transition_from_active() — see that function's comment for the production
    # mirror this reflects, including the Issue #134/#688 overheat-during-grace
    # exception via _grace_blocks_natvent() (Issue #706). Placed first, before the
    # lockout check, so an override/grace window can never be masked by (or race
    # with) lockout state.
    if inputs.override_active or _grace_blocks_natvent(inputs):
        return NatVentTransition(
            from_state=current_state,
            to_state=NatVentLifecycleState.INACTIVE,
            event_kind=event.kind,
            at=inputs.now,
        )

    if inputs.paused_by_door and is_reactivation_locked_out(
        outdoor_exit_time=inputs.outdoor_exit_time, now=inputs.now, lockout_seconds=inputs.lockout_seconds
    ):
        return NatVentTransition(
            from_state=current_state,
            to_state=NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT,
            event_kind=event.kind,
            at=inputs.now,
        )

    full_gate = decide_nat_vent_gate(_gate_inputs(inputs))
    if full_gate:
        next_state = NatVentLifecycleState.ACTIVE_FULL_GATE
    elif decide_nat_vent_soft_start_gate(_soft_start_inputs(inputs, full_gate_active=full_gate)):
        next_state = NatVentLifecycleState.ACTIVE_SOFT_START
    else:
        next_state = NatVentLifecycleState.INACTIVE

    return NatVentTransition(from_state=current_state, to_state=next_state, event_kind=event.kind, at=inputs.now)


def transition(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    """The single entry point: given the current state and an event carrying a
    live-inputs snapshot, return the next state (and why).

    Dispatches to the exit chain while active, the entry/soft-start gates
    while inactive or locked-out — mirrors ``check_natural_vent_conditions()``'s
    own top-level ``if self._natural_vent_active:`` branch, the one structural
    decision every real call site already makes.
    """
    if current_state in (NatVentLifecycleState.ACTIVE_FULL_GATE, NatVentLifecycleState.ACTIVE_SOFT_START):
        return _transition_from_active(current_state, event)
    return _transition_from_inactive(current_state, event)
