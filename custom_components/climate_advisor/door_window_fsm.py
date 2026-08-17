"""Door/window pause/grace lifecycle FSM — the unified transition table (Issue #637,
Block 5 Phase 2, epic #594).

Assembles the 5 new pure pieces this phase built into one explicit
``(state, event) -> Transition`` function, mirroring ``nat_vent_fsm.py``'s shape
exactly:

- ``door_window_lifecycle.derive_door_window_lifecycle_state()`` — the 5-state
  enum this module reuses unchanged.
- ``door_window_pause_entry.decide_pause_entry()`` — the mode-gated
  active-vs-idle pause split (Issue #523).
- ``door_window_open_response.decide_door_open_response()`` — the fresh-open
  guard chain.
- ``door_window_close_response.decide_door_close_response()`` — the
  all-closed pause-clear split.
- ``door_window_grace_expiry.decide_grace_expiry_outcome()`` — the grace-expiry
  first-match-wins chain.
- ``nat_vent_gate.decide_nat_vent_gate()`` (existing, reused unchanged) — the
  shared reactivation gate, called from two sites (fresh-open eligibility and
  the grace-expiry re-pause recheck), same as production calls
  ``_nat_vent_may_reactivate()`` from both ``handle_door_window_open()`` and
  ``_re_pause_for_open_sensor()``.

**Refinement vs. the Phase 2 plan's file-by-file dependency list**: the plan
noted ``door_window_open_response.py`` "reuses ``nat_vent_gate.decide_nat_vent_gate()``
... caller resolves the bool, passes it in." That composition happens HERE, in
the assembly module, not inside ``door_window_open_response.py`` itself — the
leaf module stays a pure, dependency-free function (same shape as
``door_window_pause_entry.py``), and this module owns cross-piece composition,
exactly matching ``nat_vent_fsm.py``'s own convention (its ``_gate_inputs``/
``_exit_inputs`` adapters call into ``nat_vent_gate.py`` from the assembly layer,
not from ``nat_vent_exit.py`` itself). Reuse-not-duplicate is honored either
way; this keeps the dependency direction consistent project-wide.

This module does not re-derive or duplicate any of the 5 pure pieces' own
logic — it is integration/wiring only.

**Markov property, with one documented exception.** Every transition is
computed from (current 5-state enum, event) — but where production's own
choice of live wall-clock inputs (esp. ``hvac_mode``) determines *which*
underlying flag combination a transition lands on (e.g. whether exiting
``PAUSED_DURING_GRACE`` back to a plain paused state resolves to
``PAUSED_ACTIVE`` or ``PAUSED_IDLE``), this module reads that from the event's
own live-inputs snapshot — never from hidden extra memory. This is the same
convention ``nat_vent_fsm.py``'s own docstring already establishes ("an event
carrying a live-inputs snapshot"), not a new exception.

**Explicit scope boundary — new findings surfaced during implementation, not
just the plan's original two.** In addition to the two mutual-exclusion
violation paths already documented in the Phase 2 plan
(``handle_door_window_open()``'s grace-fallthrough,
``_exit_nat_vent()``'s sensor-still-open branch), reading
``_re_pause_for_open_sensor()`` in full during implementation found a THIRD,
narrower case: when grace expires from plain ``GRACE`` with a sensor
still open (``RE_PAUSE``), and the nested nat-vent reactivation recheck inside
``_re_pause_for_open_sensor()`` then succeeds, ``_paused_by_door`` was NOT
being cleared by that nat-vent-activation branch — unlike the structurally
similar branch in ``check_natural_vent_conditions()`` (automation.py:3486/3518),
which DOES clear it. **Fixed in production as of Issue #637 Phase R Step 1
(v0.6.23)** — ``_re_pause_for_open_sensor()`` now clears ``_paused_by_door`` on
that branch too, so this module's mirrored behavior below is no longer a
documented inconsistency, it's simply correct. The origin-state distinction
still matters for a different reason: from ``PAUSED_DURING_GRACE`` specifically
(see ``_transition_from_paused_during_grace()``), the ``RE_PAUSE`` outcome
still always lands on a plain paused state regardless of the nested nat-vent
recheck, because pause was already separately active there and grace clearing
doesn't touch it — unlike from plain ``GRACE``, where the recheck genuinely
decides paused-vs-normal (now correctly reflected in production, per the fix
above).

**#637 Step 1 closed.** ``_exit_nat_vent()``'s unconditional sensor-still-open pause was
investigated and verified **benign, not a bug** with respect to ``_paused_by_door``
itself: the flag is accurate the moment it's set, and ``_on_grace_expired()`` already
re-derives its decision from live sensor state rather than reading ``_paused_by_door``,
so it self-corrects regardless of whether grace is left running.

**#637 Step 3 closed the remaining production gaps.** Three findings from Step 2's
scoping pass are now fixed in production (not just documented as blockers):

- ``handle_door_window_open()``'s grace-fallthrough (**#655**) — the coarse
  outdoor-only proxy check has been removed; the grace-active branch now shares the
  same real ``_nat_vent_may_reactivate()`` gate result used by the nat-vent-vs-pause
  decision further down the function. This module's ``decide_door_open_response()``
  already modeled the gate as a caller-resolved boolean input
  (``nat_vent_gate_entered``), so no change was needed here — production simply now
  passes it the real gate result on the grace path too, closing the prior divergence.
- ``_re_pause_for_open_sensor()``'s incomplete field-clear (**#657**) — the
  nat-vent-reactivation branch now clears ``_paused_with_hvac_already_off``/
  ``_paused_entity``/``_paused_since`` alongside ``_paused_by_door``, matching the
  sibling branches in ``check_natural_vent_conditions()``.
- ``_exit_nat_vent()``'s sensor-still-open branch write-shape divergence — it used to
  write only ``_paused_by_door``/``_pre_pause_mode``, omitting the same 3 fields
  #657 fixed above. Both branches now go through a shared
  ``_set_door_window_pause_fields()`` helper (one definition, not two hand-copies).
  This closes the previously-documented risk to the ``ALL_SENSORS_CLOSED`` transition
  below: ``_paused_with_hvac_already_off`` (which determines ``PAUSED_ACTIVE`` vs.
  ``PAUSED_IDLE``) can no longer go stale from this branch.

**Shadow-feed coverage (Issue #594 Phase R Step 1b, v0.6.24; residual closed in
Step 3).** All 7 ``DoorWindowFsmEventKind`` members have a real feed path — see
``coordinator.py``'s ``_DOOR_WINDOW_FSM_EVENT_KINDS``/
``_DOOR_WINDOW_SYNC_RECONCILE_TRIGGER_METHODS``/``_DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES``.
Step 1b's one documented residual — ``_on_grace_expired()``'s "within planned window"
branch (automation.py, the first branch — windows recommended, sensor open is
expected) previously emitted no event at all — is now closed: that branch emits
``"grace_expired"`` (the same event type its sibling branches already use, with a
``within_planned_window=True`` payload key to distinguish it from a real sensor
re-check), so ``GRACE_TIMER_EXPIRED`` is now fed from all 3 of
``_on_grace_expired()``'s outcome branches.

**Remaining before this FSM can be authoritative for all 7 methods (Step 3b)**:
``_re_pause_for_open_sensor()`` still needs to know whether it was reached from
``GRACE`` or ``PAUSED_DURING_GRACE`` (a signature/plumbing change, not a body swap) —
none of the Step 3 fixes above changed that. See the plan tracked on #637/#594 for the
Step 3b scope.

**Cross-lifecycle inputs.** ``natural_vent_active`` and ``whf_owns_hvac`` are
state *reads* from other lifecycles (Issue #631's "communicating automata"
design) — legacy flag-derived today, same convention as ``nat_vent_fsm.py``'s
own ``paused_by_door`` read.

**Authoritative for production, as of Step 2 (v0.6.25) — PARTIAL scope.** This
module's ``transition()`` is no longer purely a shadow diagnostic: when
``AutomationEngine._doorwindow_fsm_authoritative`` is True,
``handle_manual_override_during_pause()``/``resume_from_pause()`` call it for
real and derive their resulting flags from ``result.to_state`` (via
``AutomationEngine._apply_door_window_fsm_state()``). Both transitions
(``MANUAL_OVERRIDE_DURING_PAUSE``, ``DASHBOARD_RESUME``) land unconditionally on
their target state regardless of origin state — no placeholder-inference risk,
unlike ``_transition_from_paused()``'s ``ALL_SENSORS_CLOSED`` branch below. The
other 5 event kinds/methods stay shadow-only, each blocked on its own documented
gap (see automation.py's ``_doorwindow_fsm_authoritative`` docstring) — this
module's transition table is faithful to production for all 7, but "faithful"
and "safe to read authoritatively" are different bars, and only 2 have cleared
the second one so far.

**Known non-mechanical transition — do not swap without fixing first.**
``_transition_from_paused()``'s ``ALL_SENSORS_CLOSED`` branch infers
``pre_pause_mode`` from ``current_state == PAUSED_ACTIVE`` rather than taking the
real ``AutomationEngine._pre_pause_mode`` value as an input — a placeholder, not
the actual field ``door_window_close_response.DoorCloseResponseInputs`` was
designed to take. Found while attempting to swap
``handle_all_doors_windows_closed()`` in this same increment: since
``PAUSED_ACTIVE`` vs. ``PAUSED_IDLE`` depends on ``_paused_with_hvac_already_off``,
which the still-shadow-only ``_exit_nat_vent()`` branch can leave stale (it never
writes that field), trusting this transition's resulting state for real
flag-derivation risks a genuine divergence in that reachable edge case. Needs
either a real ``pre_pause_mode``-truthiness input added to this branch, or
another safe path, before ``handle_all_doors_windows_closed()`` can join a future
increment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .door_window_close_response import (
    DoorCloseResponseInputs,
    DoorCloseResponseOutcome,
    decide_door_close_response,
)
from .door_window_grace_expiry import (
    GraceExpiryInputs,
    GraceExpiryOutcome,
    decide_grace_expiry_outcome,
)
from .door_window_lifecycle import DoorWindowLifecycleState
from .door_window_open_response import (
    DoorOpenResponse,
    DoorOpenResponseInputs,
    decide_door_open_response,
)
from .door_window_pause_entry import PauseEntryInputs, PauseEntryOutcome, decide_pause_entry
from .nat_vent_gate import NatVentGateInputs, decide_nat_vent_gate


class DoorWindowFsmEventKind(Enum):
    """What prompted this transition evaluation — one per real production
    entry point, unlike nat-vent's single re-evaluated-every-tick model
    (door/window's handlers are each triggered by a distinct event, not a
    periodic re-check)."""

    SENSOR_OPENED = "sensor_opened"
    ALL_SENSORS_CLOSED = "all_sensors_closed"
    GRACE_TIMER_EXPIRED = "grace_timer_expired"
    MANUAL_OVERRIDE_DURING_PAUSE = "manual_override_during_pause"
    DASHBOARD_RESUME = "dashboard_resume"
    NAT_VENT_EXITED_SENSOR_STILL_OPEN = "nat_vent_exited_sensor_still_open"
    SYNC_RECONCILE = "sync_reconcile"  # Issue #620's 4 reconcile call sites
    # Issue #660: check_natural_vent_conditions()'s two reactivation branches
    # (automation.py) clear all 4 door/window pause fields directly when nat-vent
    # reactivates while paused -- this method previously had zero door/window FSM
    # event feed at all (it was only in _NAT_VENT_FSM_TRIGGER_METHODS, feeding the
    # *nat-vent* FSM, not door/window's), making it the real 8th production trigger
    # site the module docstring's "7 event kinds are exhaustive" claim missed.
    PAUSED_NAT_VENT_REACTIVATED = "paused_nat_vent_reactivated"


@dataclass(frozen=True)
class DoorWindowFsmInputs:
    """Every live reading this FSM's transition function may consult —
    explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      hvac_mode                         -> self.hass.states.get(self.climate_entity).state
      outdoor                            -> self._last_outdoor_temp
      indoor                              -> self._get_indoor_temp_f()
      comfort_heat                        -> self._nat_vent_reactivation_floor() —
                                             already sleep-aware; passed to
                                             NatVentGateInputs as both
                                             comfort_heat_raw and sleep_heat
                                             (in_sleep_window is therefore moot)
      comfort_cool                        -> config comfort_cool
      nat_vent_delta                      -> config CONF_NATURAL_VENT_DELTA
      fan_mode                            -> config CONF_FAN_MODE
      aggressive_savings                  -> config aggressive_savings
      within_planned_window               -> self._is_within_planned_window_period()
      any_sensor_open                     -> self._any_monitored_sensor_open()
      sensor_debounce_pending             -> self._sensor_debounce_pending
      override_matches_current_decision   -> self._override_matches_current_decision(
                                              self._current_classification)
      grace_source                        -> self._last_resume_source ("manual" | "automation")
      natural_vent_active                 -> self._natural_vent_active (cross-lifecycle read)
      whf_owns_hvac                       -> self._whf_owns_hvac() (cross-lifecycle read) —
                                             added beyond the Phase 2 plan's literal field
                                             list: required for faithful SYNC_RECONCILE
                                             modeling (_sync_paused_by_door_with_live_sensors()'s
                                             own guard reads it)
      pre_pause_mode_active                -> bool(self._pre_pause_mode) — added under
                                             Issue #660 Step 2: replaces the
                                             ``current_state == PAUSED_ACTIVE`` proxy
                                             ``_transition_from_paused()``'s
                                             ALL_SENSORS_CLOSED branch used to infer
                                             ``pre_pause_mode`` truthiness from, which
                                             could disagree with the real field.
      grace_active                        -> self._grace_active — added under Issue #660:
                                             _sync_reconcile_next_state() previously never
                                             checked live grace_active when already in a
                                             PAUSED_* state, so the FSM's carried state
                                             could silently disagree with production once
                                             grace started running concurrently with an
                                             existing pause (see that function's own
                                             docstring for the fix).
      now                                  -> caller-resolved wall-clock time
    """

    hvac_mode: str | None
    outdoor: float | None
    indoor: float | None
    comfort_heat: float
    comfort_cool: float
    nat_vent_delta: float
    fan_mode: str
    aggressive_savings: bool
    within_planned_window: bool
    any_sensor_open: bool
    sensor_debounce_pending: bool
    override_matches_current_decision: bool
    grace_source: str
    natural_vent_active: bool
    whf_owns_hvac: bool
    grace_active: bool
    pre_pause_mode_active: bool
    now: datetime


@dataclass(frozen=True)
class DoorWindowFsmEvent:
    kind: DoorWindowFsmEventKind
    inputs: DoorWindowFsmInputs


@dataclass(frozen=True)
class DoorWindowTransition:
    """The transition's full record — audit trail by construction."""

    from_state: DoorWindowLifecycleState
    to_state: DoorWindowLifecycleState
    event_kind: DoorWindowFsmEventKind
    outcome: str | None = None
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _nat_vent_gate_inputs(inputs: DoorWindowFsmInputs) -> NatVentGateInputs:
    return NatVentGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_heat_raw=inputs.comfort_heat,
        sleep_heat=inputs.comfort_heat,
        in_sleep_window=False,
        comfort_cool=inputs.comfort_cool,
        nat_vent_delta=inputs.nat_vent_delta,
        hysteresis=0.0,
        fan_mode=inputs.fan_mode,
        aggressive_savings=inputs.aggressive_savings,
    )


def _pause_sub_state(outcome: PauseEntryOutcome) -> DoorWindowLifecycleState | None:
    """Maps a ``PauseEntryOutcome`` to its plain (non-grace) paused state, or
    ``None`` for ``NOOP_NOT_APPLICABLE`` (no pause recorded)."""
    if outcome == PauseEntryOutcome.PAUSE_ACTIVE:
        return DoorWindowLifecycleState.PAUSED_ACTIVE
    if outcome == PauseEntryOutcome.PAUSE_IDLE:
        return DoorWindowLifecycleState.PAUSED_IDLE
    return None


def _active_pause_mode(hvac_mode: str | None) -> bool:
    """Mirrors ``_exit_nat_vent()``'s sensor-still-open branch: unlike
    ``_pause_for_door_window()``, this call site sets ``_paused_by_door = True``
    UNCONDITIONALLY (no mode gate at all) and separately derives
    ``_pre_pause_mode`` as ``state.state if state and state.state != "off" else
    None``. So "active" here means "mode present and not off" — this includes
    ``"unavailable"``/``"unknown"`` (production would literally store
    ``pre_pause_mode="unavailable"``, a non-None string), unlike
    ``decide_pause_entry()``'s 3-way split, which treats those as a distinct
    no-op outcome (not applicable here because this call site never has a
    not-applicable branch — it unconditionally pauses). Only a missing state
    object (``None``) or a literal ``"off"`` mode collapses to idle."""
    return bool(hvac_mode) and hvac_mode != "off"


def _transition_from_normal(current_state: DoorWindowLifecycleState, event: DoorWindowFsmEvent) -> DoorWindowTransition:
    inputs = event.inputs
    kind = event.kind

    if kind == DoorWindowFsmEventKind.SENSOR_OPENED:
        response = decide_door_open_response(
            DoorOpenResponseInputs(
                paused_by_door=False,
                grace_active=False,
                outdoor=inputs.outdoor,
                comfort_cool=inputs.comfort_cool,
                nat_vent_delta=inputs.nat_vent_delta,
                within_planned_window=inputs.within_planned_window,
                nat_vent_gate_entered=decide_nat_vent_gate(_nat_vent_gate_inputs(inputs)),
            )
        )
        if response == DoorOpenResponse.PAUSE:
            next_state = _pause_sub_state(decide_pause_entry(PauseEntryInputs(hvac_mode=inputs.hvac_mode)))
            next_state = next_state or current_state
        else:
            next_state = current_state
        return DoorWindowTransition(current_state, next_state, kind, response.value, inputs.now)

    if kind == DoorWindowFsmEventKind.SYNC_RECONCILE:
        next_state = _sync_reconcile_next_state(current_state, inputs)
        return DoorWindowTransition(current_state, next_state, kind, "sync_reconcile", inputs.now)

    if kind == DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN:
        next_state = (
            DoorWindowLifecycleState.PAUSED_ACTIVE
            if _active_pause_mode(inputs.hvac_mode)
            else DoorWindowLifecycleState.PAUSED_IDLE
        )
        return DoorWindowTransition(current_state, next_state, kind, "nat_vent_exit_pause", inputs.now)

    if kind == DoorWindowFsmEventKind.ALL_SENSORS_CLOSED:
        outcome = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=False, pre_pause_mode=None))
        return DoorWindowTransition(current_state, current_state, kind, outcome.value, inputs.now)

    # GRACE_TIMER_EXPIRED / MANUAL_OVERRIDE_DURING_PAUSE / DASHBOARD_RESUME: not
    # reachable from NORMAL in production (each has its own early-return guard
    # keyed on paused_by_door/an active grace timer, neither of which exists
    # here) — modeled as a defensive no-op rather than silently ignored.
    return DoorWindowTransition(current_state, current_state, kind, "unreachable_from_normal", inputs.now)


def _sync_reconcile_next_state(
    current_state: DoorWindowLifecycleState, inputs: DoorWindowFsmInputs
) -> DoorWindowLifecycleState:
    """Pure reimplementation of ``_sync_paused_by_door_with_live_sensors()`` (Issue #620),
    for the origin states where its own guard doesn't immediately no-op — NORMAL/GRACE
    (both have ``paused_by_door=False``), plus PAUSED_ACTIVE/PAUSED_IDLE's own narrower
    ``grace_active``-while-paused check (Issue #660, added below)."""
    # Issue #660: when already paused and a grace period is independently running
    # (live grace_active True, e.g. started by a different code path than the pause
    # itself), the FSM's carried state must upgrade to PAUSED_DURING_GRACE — the
    # composite state — rather than silently staying a plain paused state that no
    # longer reflects live grace. This check must run before the early-return guards
    # below, which are specific to the NORMAL/GRACE reconcile-into-pause direction and
    # would otherwise leave a plain-paused + grace-active disagreement unresolved.
    if current_state in (DoorWindowLifecycleState.PAUSED_ACTIVE, DoorWindowLifecycleState.PAUSED_IDLE):
        if inputs.grace_active:
            return DoorWindowLifecycleState.PAUSED_DURING_GRACE
        return current_state

    if inputs.natural_vent_active or inputs.whf_owns_hvac:
        return current_state
    if inputs.within_planned_window:
        return current_state
    if inputs.sensor_debounce_pending or not inputs.any_sensor_open:
        return current_state

    sub_state = _pause_sub_state(decide_pause_entry(PauseEntryInputs(hvac_mode=inputs.hvac_mode)))
    if sub_state is None:
        return current_state
    if current_state == DoorWindowLifecycleState.GRACE:
        return DoorWindowLifecycleState.PAUSED_DURING_GRACE
    return sub_state


def _transition_from_paused(current_state: DoorWindowLifecycleState, event: DoorWindowFsmEvent) -> DoorWindowTransition:
    inputs = event.inputs
    kind = event.kind

    if kind == DoorWindowFsmEventKind.ALL_SENSORS_CLOSED:
        # Issue #660 Step 2: reads the real pre_pause_mode truthiness from the event's
        # inputs rather than inferring it from current_state == PAUSED_ACTIVE — that
        # proxy could disagree with the real AutomationEngine._pre_pause_mode value in
        # a reachable edge case (see this file's module docstring, formerly "Known
        # non-mechanical transition").
        outcome = decide_door_close_response(
            DoorCloseResponseInputs(
                paused_by_door=True,
                pre_pause_mode="restored" if inputs.pre_pause_mode_active else None,
            )
        )
        next_state = (
            DoorWindowLifecycleState.GRACE
            if outcome == DoorCloseResponseOutcome.RESTORE_AND_GRACE
            else DoorWindowLifecycleState.NORMAL
        )
        return DoorWindowTransition(current_state, next_state, kind, outcome.value, inputs.now)

    if kind == DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE:
        return DoorWindowTransition(
            current_state, DoorWindowLifecycleState.NORMAL, kind, "override_confirmation_started", inputs.now
        )

    if kind == DoorWindowFsmEventKind.DASHBOARD_RESUME:
        return DoorWindowTransition(current_state, DoorWindowLifecycleState.GRACE, kind, "resumed", inputs.now)

    if kind == DoorWindowFsmEventKind.SYNC_RECONCILE:
        # Issue #660: dispatches to the same single reconciliation function every
        # other origin state uses, instead of silently no-op'ing here — see
        # _sync_reconcile_next_state()'s own grace_active-while-paused branch.
        next_state = _sync_reconcile_next_state(current_state, inputs)
        return DoorWindowTransition(current_state, next_state, kind, "sync_reconcile", inputs.now)

    if kind == DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED:
        # Issue #660 Step 3: check_natural_vent_conditions()'s reactivation branches
        # clear all 4 pause fields unconditionally when nat-vent may reactivate while
        # paused. From a plain paused state there is no grace running by definition,
        # so this always lands on NORMAL.
        return DoorWindowTransition(
            current_state, DoorWindowLifecycleState.NORMAL, kind, "nat_vent_reactivated", inputs.now
        )

    # SENSOR_OPENED: guards on paused_by_door already being True and no-ops.
    # GRACE_TIMER_EXPIRED / NAT_VENT_EXITED_SENSOR_STILL_OPEN: not reachable from a
    # plain paused state (no grace timer running; nat-vent cannot be active while
    # already paused per the two evidenced entry paths).
    return DoorWindowTransition(current_state, current_state, kind, "noop_already_paused", inputs.now)


def _transition_from_grace(current_state: DoorWindowLifecycleState, event: DoorWindowFsmEvent) -> DoorWindowTransition:
    inputs = event.inputs
    kind = event.kind

    if kind == DoorWindowFsmEventKind.SENSOR_OPENED:
        response = decide_door_open_response(
            DoorOpenResponseInputs(
                paused_by_door=False,
                grace_active=True,
                outdoor=inputs.outdoor,
                comfort_cool=inputs.comfort_cool,
                nat_vent_delta=inputs.nat_vent_delta,
                within_planned_window=inputs.within_planned_window,
                nat_vent_gate_entered=decide_nat_vent_gate(_nat_vent_gate_inputs(inputs)),
            )
        )
        if response == DoorOpenResponse.PAUSE:
            # _pause_for_door_window() runs with grace untouched — the
            # confirmed-reachable violation path #1 (see module docstring).
            sub = decide_pause_entry(PauseEntryInputs(hvac_mode=inputs.hvac_mode))
            next_state = (
                DoorWindowLifecycleState.PAUSED_DURING_GRACE
                if sub != PauseEntryOutcome.NOOP_NOT_APPLICABLE
                else current_state
            )
        else:
            next_state = current_state
        return DoorWindowTransition(current_state, next_state, kind, response.value, inputs.now)

    if kind == DoorWindowFsmEventKind.SYNC_RECONCILE:
        next_state = _sync_reconcile_next_state(current_state, inputs)
        return DoorWindowTransition(current_state, next_state, kind, "sync_reconcile", inputs.now)

    if kind == DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN:
        # Unconditional pause set, grace untouched — violation path #2.
        return DoorWindowTransition(
            current_state, DoorWindowLifecycleState.PAUSED_DURING_GRACE, kind, "nat_vent_exit_pause", inputs.now
        )

    if kind == DoorWindowFsmEventKind.ALL_SENSORS_CLOSED:
        outcome = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=False, pre_pause_mode=None))
        return DoorWindowTransition(current_state, current_state, kind, outcome.value, inputs.now)

    if kind == DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED:
        outcome = decide_grace_expiry_outcome(
            GraceExpiryInputs(
                within_planned_window=inputs.within_planned_window,
                any_sensor_open=inputs.any_sensor_open,
                source=inputs.grace_source,
                override_matches_current_decision=inputs.override_matches_current_decision,
            )
        )
        if outcome == GraceExpiryOutcome.RE_PAUSE:
            # From plain GRACE, the nested nat-vent recheck inside
            # _re_pause_for_open_sensor() genuinely decides NORMAL (nat-vent
            # handoff) vs a plain paused state — unlike from
            # PAUSED_DURING_GRACE, see _transition_from_paused_during_grace().
            if decide_nat_vent_gate(_nat_vent_gate_inputs(inputs)):
                next_state = DoorWindowLifecycleState.NORMAL
            else:
                sub = _pause_sub_state(decide_pause_entry(PauseEntryInputs(hvac_mode=inputs.hvac_mode)))
                next_state = sub or DoorWindowLifecycleState.NORMAL
        else:
            next_state = DoorWindowLifecycleState.NORMAL
        return DoorWindowTransition(current_state, next_state, kind, outcome.value, inputs.now)

    # MANUAL_OVERRIDE_DURING_PAUSE / DASHBOARD_RESUME: both guard on
    # paused_by_door being True and no-op from plain GRACE.
    return DoorWindowTransition(current_state, current_state, kind, "noop_not_paused", inputs.now)


def _transition_from_paused_during_grace(
    current_state: DoorWindowLifecycleState, event: DoorWindowFsmEvent
) -> DoorWindowTransition:
    inputs = event.inputs
    kind = event.kind

    if kind == DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED:
        # All 4 real outcomes clear grace (_cancel_grace_timers()) without
        # touching _paused_by_door — including RE_PAUSE, whose nested nat-vent
        # recheck (unlike the plain-GRACE case above) does NOT clear
        # _paused_by_door either way (see module docstring's third finding).
        # So every outcome converges on the same next-state logic: grace
        # clears, pause remains, sub-state re-derived from the live hvac_mode
        # on THIS event (the composite state doesn't itself retain which
        # plain sub-state it came from — same live-inputs-on-the-event
        # convention nat_vent_fsm.py already establishes).
        outcome = decide_grace_expiry_outcome(
            GraceExpiryInputs(
                within_planned_window=inputs.within_planned_window,
                any_sensor_open=inputs.any_sensor_open,
                source=inputs.grace_source,
                override_matches_current_decision=inputs.override_matches_current_decision,
            )
        )
        sub = _pause_sub_state(decide_pause_entry(PauseEntryInputs(hvac_mode=inputs.hvac_mode)))
        next_state = sub or DoorWindowLifecycleState.PAUSED_ACTIVE
        return DoorWindowTransition(current_state, next_state, kind, outcome.value, inputs.now)

    if kind == DoorWindowFsmEventKind.ALL_SENSORS_CLOSED:
        # Grace was already running; closing sensors clears the pause and
        # either refreshes grace (RESTORE_AND_GRACE) or leaves the existing
        # grace timer untouched (CLEAR_NO_GRACE) — both land on plain GRACE,
        # never NORMAL, since neither branch cancels grace outright.
        outcome = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=True, pre_pause_mode="restored"))
        return DoorWindowTransition(current_state, DoorWindowLifecycleState.GRACE, kind, outcome.value, inputs.now)

    if kind == DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE:
        # Clears pause, leaves grace running (untouched) — lands on GRACE.
        return DoorWindowTransition(
            current_state, DoorWindowLifecycleState.GRACE, kind, "override_confirmation_started", inputs.now
        )

    if kind == DoorWindowFsmEventKind.DASHBOARD_RESUME:
        # Clears pause, unconditionally re-arms grace — lands on GRACE.
        return DoorWindowTransition(current_state, DoorWindowLifecycleState.GRACE, kind, "resumed", inputs.now)

    if kind == DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED:
        # Issue #660 Step 3: clears the pause but leaves the already-running grace
        # timer untouched — production's reactivation branches never touch
        # _grace_active — so this lands on plain GRACE, not NORMAL (unlike the
        # plain-paused-origin case above, where there is no grace to preserve).
        return DoorWindowTransition(
            current_state, DoorWindowLifecycleState.GRACE, kind, "nat_vent_reactivated", inputs.now
        )

    # SENSOR_OPENED / SYNC_RECONCILE: both guard on paused_by_door already
    # being True and no-op. NAT_VENT_EXITED_SENSOR_STILL_OPEN: not reachable
    # (nat-vent cannot be active while already paused, per the evidenced entry
    # paths — see module docstring).
    return DoorWindowTransition(current_state, current_state, kind, "noop_already_paused_during_grace", inputs.now)


def transition(current_state: DoorWindowLifecycleState, event: DoorWindowFsmEvent) -> DoorWindowTransition:
    """The single entry point: given the current state and an event carrying a
    live-inputs snapshot, return the next state (and why)."""
    if current_state == DoorWindowLifecycleState.NORMAL:
        return _transition_from_normal(current_state, event)
    if current_state in (DoorWindowLifecycleState.PAUSED_ACTIVE, DoorWindowLifecycleState.PAUSED_IDLE):
        return _transition_from_paused(current_state, event)
    if current_state == DoorWindowLifecycleState.GRACE:
        return _transition_from_grace(current_state, event)
    return _transition_from_paused_during_grace(current_state, event)
