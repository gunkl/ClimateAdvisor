"""Override/grace joint lifecycle FSM — the unified transition table (Issue #639,
Block 5 Phase 3, epic #594).

Assembles the pure pieces this phase built into one explicit
``(state, event) -> Transition`` function, mirroring ``nat_vent_fsm.py``/
``door_window_fsm.py``'s shape:

- ``override_grace_lifecycle.derive_override_grace_lifecycle_state()`` — the
  ``(OverrideConfirmState, GraceState)`` pair this module reuses unchanged.
- ``override_confirm_split.decide_override_confirm_path()`` — the PATH A/B split
  at confirmation expiry.
- ``override_match.decide_override_matches_decision()`` — feeds
  ``door_window_grace_expiry``'s ``override_matches_current_decision`` input.
- ``override_grace_start.decide_grace_protects_override()`` — whether a newly
  started grace protects a real override.
- ``override_cancel_outcome.decide_override_cancel_outcome()`` — what
  ``cancel_override()`` actually had to clear.
- ``desired_state.decide_override_confirm()`` (existing, reused unchanged) —
  disabled-vs-pending at confirmation start.
- ``desired_state.decide_grace_start()`` (existing, reused unchanged) —
  duration/should_notify resolution (not needed for state transitions, only
  for the ``outcome`` audit field, since a disabled grace never actually sets
  ``grace_active``).
- ``door_window_grace_expiry.decide_grace_expiry_outcome()`` (Phase 2, reused
  UNCHANGED per the Phase 3 plan's explicit instruction — not re-extracted).

This module does not re-derive or duplicate any of these pieces' own logic —
it is integration/wiring only.

**Grace is single-slot and always clears override+grace together at expiry.**
Reading ``_on_grace_expired()`` in full (automation.py:4372-4481) found every
branch — ``CLEAR_PLANNED_WINDOW``, ``RE_PAUSE``, adopt-or-not — unconditionally
calls both ``_cancel_grace_timers()`` and ``clear_manual_override()`` before
branching on notification text. ``GraceExpiryOutcome`` therefore does NOT
determine this FSM's own next state the way it determines door/window's —
from override/grace's perspective, ``GRACE_TIMER_EXPIRED`` always lands on
``(IDLE, NONE)``. ``decide_grace_expiry_outcome()`` is still called and its
result recorded in the transition's ``outcome`` field, for audit-trail parity
with door/window's own FSM (same event, same underlying method, two
lifecycles reading two different consequences of it) — not because it
branches this module's next-state.

**Supersession's transient state — investigated during implementation, confirmed
NOT a live race.** Issue #282's "Fix D" branch (``coordinator._async_thermostat_changed()``,
~4037-4058) calls ``clear_manual_override(reason="new_override_during_grace")`` — which
clears override flags but does NOT call ``_cancel_grace_timers()`` — immediately followed
by ``handle_manual_override()`` re-detecting the new override. An earlier draft of this
docstring hypothesized this window as exploitable by ``_check_orphaned_grace()``'s ~30s
update-cycle check (Issue #508) if it happened to run in between. Verified against the
actual call chain and found FALSE: both ``clear_manual_override()`` and
``handle_manual_override()`` are plain synchronous ``def`` methods (not ``async def``),
called back-to-back with no ``await`` between them inside ``_async_thermostat_changed()``.
Python's single-threaded, cooperative event loop cannot interleave another coroutine
(including ``_check_orphaned_grace()``, itself only ever invoked from inside
``_async_update_data()``'s own tick) into a synchronous call sequence with no yield point —
so this transient flag combination, while real at the attribute level for the duration of
one Python call stack, is never externally observable and never a genuine race. Modeled
here as a same-tick transient (PENDING-or-IDLE, ACTIVE_PROTECTING_OVERRIDE) landing state
purely for completeness of the transition record — not because any caller-side watchdog
could actually intervene on it.

**Cross-lifecycle inputs.** ``paused_by_door`` gates whether
``MANUAL_OVERRIDE_DURING_PAUSE``/``DASHBOARD_RESUME`` are even reachable in
production (both real methods early-return when it's False) — a state read
from door/window's own lifecycle, same "communicating automata" convention
(Issue #631) ``nat_vent_fsm.py``'s ``paused_by_door`` read and
``door_window_fsm.py``'s ``natural_vent_active`` read already establish. This
module does not re-derive that guard; the caller only emits these two event
kinds when production's own guard already passed, same "event implies its own
precondition" convention ``door_window_fsm.py``'s ``SENSOR_OPENED`` uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .desired_state import decide_override_confirm
from .door_window_grace_expiry import GraceExpiryInputs, decide_grace_expiry_outcome
from .override_cancel_outcome import OverrideCancelOutcome, decide_override_cancel_outcome
from .override_confirm_split import OverrideConfirmPathOutcome, decide_override_confirm_path
from .override_grace_lifecycle import GraceState, OverrideConfirmState, OverrideGraceLifecycleState
from .override_grace_start import decide_grace_protects_override
from .override_match import decide_override_matches_decision

_TRIGGER_OVERRIDE_CONFIRMED = "override_confirmed"
_TRIGGER_DASHBOARD_RESUME = "dashboard_resume"


class OverrideGraceFsmEventKind(Enum):
    """What prompted this transition evaluation — one per real production entry
    point, same per-handler convention door/window's FSM uses (not nat-vent's
    single-TICK convention), since override/grace is handler-triggered, not
    periodically re-evaluated."""

    OVERRIDE_DETECTED = "override_detected"
    OVERRIDE_CONFIRM_EXPIRED = "override_confirm_expired"
    OVERRIDE_SUPERSEDED = "override_superseded"
    OVERRIDE_CANCELLED = "override_cancelled"
    GRACE_TIMER_EXPIRED = "grace_timer_expired"
    MANUAL_OVERRIDE_DURING_PAUSE = "manual_override_during_pause"
    DASHBOARD_RESUME = "dashboard_resume"
    # Issue #661: distinct from OVERRIDE_DETECTED. That kind models the
    # thermostat-mode/setpoint override path, which genuinely routes through
    # start_override_confirmation()'s disabled-vs-pending branch. Fan overrides
    # (handle_fan_manual_override()) never touch that machinery at all — they set
    # _fan_override_active and start a protecting grace directly and
    # unconditionally, every time, regardless of origin state. Landing this on
    # the same OVERRIDE_DETECTED kind made _land_after_detection()'s confirm-delay
    # model misfire for the fan path (it would model a PENDING confirmation that
    # production never creates). A dedicated kind lets the fan path land
    # immediately at the dispatcher level without touching the thermostat path's
    # shared confirm-delay logic at all.
    FAN_OVERRIDE_DETECTED = "fan_override_detected"


@dataclass(frozen=True)
class OverrideGraceFsmInputs:
    """Every live reading this FSM's transition function may consult — explicit,
    nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      confirm_seconds        -> config CONF_OVERRIDE_CONFIRM_PERIOD
      setpoint_override       -> (the detected override's source == "setpoint")
      current_mode             -> self.hass.states.get(self.climate_entity).state
      classification_mode      -> self._current_classification.hvac_mode
      manual_override_active   -> self._manual_override_active
      manual_override_mode      -> self._manual_override_mode
      manual_override_source    -> self._manual_override_source
      fan_override_active       -> self._fan_override_active
      current_setpoint_f        -> to_fahrenheit(thermostat's live "temperature" attribute)
      target_setpoint_f         -> band.floor/ceiling from select_comfort_band() for
                                   classification's mode
      tolerance_f               -> OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F
      within_planned_window     -> self._is_within_planned_window_period()
      any_sensor_open            -> self._any_monitored_sensor_open()
      grace_source               -> self._last_resume_source of the EXPIRING grace
                                    ("manual" | "automation")
      now                        -> caller-resolved wall-clock time
    """

    confirm_seconds: float
    setpoint_override: bool
    current_mode: str | None
    classification_mode: str | None
    manual_override_active: bool
    manual_override_mode: str | None
    manual_override_source: str | None
    fan_override_active: bool
    current_setpoint_f: float | None
    target_setpoint_f: float | None
    tolerance_f: float
    within_planned_window: bool
    any_sensor_open: bool
    grace_source: str
    now: datetime


@dataclass(frozen=True)
class OverrideGraceFsmEvent:
    kind: OverrideGraceFsmEventKind
    inputs: OverrideGraceFsmInputs


@dataclass(frozen=True)
class OverrideGraceTransition:
    """The transition's full record — audit trail by construction."""

    from_state: OverrideGraceLifecycleState
    to_state: OverrideGraceLifecycleState
    event_kind: OverrideGraceFsmEventKind
    outcome: str | None = None
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _land_after_detection(inputs: OverrideGraceFsmInputs, current_grace: GraceState) -> OverrideGraceLifecycleState:
    """Shared landing logic for OVERRIDE_DETECTED / OVERRIDE_SUPERSEDED /
    MANUAL_OVERRIDE_DURING_PAUSE — all three route through
    ``start_override_confirmation()``'s own disabled-vs-pending branch."""
    pending = decide_override_confirm(
        confirm_seconds=inputs.confirm_seconds, detected_mode=inputs.current_mode or "", now=inputs.now
    )
    if pending is not None:
        return (OverrideConfirmState.PENDING, current_grace)
    # Confirmation disabled — _confirm_override() fires immediately, which always
    # (re)starts a manual grace with trigger="override_confirmed" (protects).
    return (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)


def _grace_expiry_outcome_str(inputs: OverrideGraceFsmInputs) -> str:
    override_matches = decide_override_matches_decision(
        manual_override_active=inputs.manual_override_active,
        manual_override_mode=inputs.manual_override_mode,
        manual_override_source=inputs.manual_override_source,
        classification_mode=inputs.classification_mode,
        current_setpoint_f=inputs.current_setpoint_f,
        target_setpoint_f=inputs.target_setpoint_f,
        tolerance_f=inputs.tolerance_f,
    )
    outcome = decide_grace_expiry_outcome(
        GraceExpiryInputs(
            within_planned_window=inputs.within_planned_window,
            any_sensor_open=inputs.any_sensor_open,
            source=inputs.grace_source,
            override_matches_current_decision=override_matches,
        )
    )
    return outcome.value


def _transition_from_no_grace(
    current_state: OverrideGraceLifecycleState, event: OverrideGraceFsmEvent
) -> OverrideGraceTransition:
    inputs = event.inputs
    kind = event.kind
    confirm, grace = current_state

    if kind == OverrideGraceFsmEventKind.OVERRIDE_DETECTED:
        next_state = _land_after_detection(inputs, grace)
        return OverrideGraceTransition(current_state, next_state, kind, "detected", inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED:
        if confirm != OverrideConfirmState.PENDING:
            return OverrideGraceTransition(current_state, current_state, kind, "unreachable_not_pending", inputs.now)
        path = decide_override_confirm_path(
            setpoint_override=inputs.setpoint_override,
            current_mode=inputs.current_mode or "unknown",
            classification_mode=inputs.classification_mode,
        )
        if path == OverrideConfirmPathOutcome.CONFIRM:
            next_state = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)
        else:
            next_state = (OverrideConfirmState.IDLE, GraceState.NONE)
        return OverrideGraceTransition(current_state, next_state, kind, path.value, inputs.now)

    if kind == OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE:
        next_state = _land_after_detection(inputs, grace)
        return OverrideGraceTransition(current_state, next_state, kind, "override_confirmation_started", inputs.now)

    if kind == OverrideGraceFsmEventKind.DASHBOARD_RESUME:
        # resume_from_pause() unconditionally (re)starts grace with
        # trigger="dashboard_resume" — not in GRACE_TRIGGERS_PROTECTING_OVERRIDE.
        assert not decide_grace_protects_override(_TRIGGER_DASHBOARD_RESUME)
        next_state = (confirm, GraceState.ACTIVE_UNPROTECTED)
        return OverrideGraceTransition(current_state, next_state, kind, "resumed", inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_CANCELLED:
        outcome = decide_override_cancel_outcome(
            manual_override_active=inputs.manual_override_active,
            fan_override_active=inputs.fan_override_active,
            grace_active=False,
        )
        next_state = (
            current_state if outcome == OverrideCancelOutcome.NOOP else (OverrideConfirmState.IDLE, GraceState.NONE)
        )
        return OverrideGraceTransition(current_state, next_state, kind, outcome.value, inputs.now)

    # OVERRIDE_SUPERSEDED / GRACE_TIMER_EXPIRED: not reachable with no grace
    # active (supersession requires an active override's grace already
    # running; the timer can't expire with nothing running).
    return OverrideGraceTransition(current_state, current_state, kind, "unreachable_no_grace", inputs.now)


def _transition_from_grace_protecting(
    current_state: OverrideGraceLifecycleState, event: OverrideGraceFsmEvent
) -> OverrideGraceTransition:
    inputs = event.inputs
    kind = event.kind

    if kind == OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED:
        outcome_str = _grace_expiry_outcome_str(inputs)
        next_state = (OverrideConfirmState.IDLE, GraceState.NONE)
        return OverrideGraceTransition(current_state, next_state, kind, outcome_str, inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED:
        # Issue #282 "Fix D": clear_manual_override() clears override flags but
        # NOT grace (see module docstring's transient orphaned-grace note),
        # then a fresh detection cycle begins.
        next_state = _land_after_detection(inputs, GraceState.ACTIVE_PROTECTING_OVERRIDE)
        return OverrideGraceTransition(current_state, next_state, kind, "superseded", inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_CANCELLED:
        outcome = decide_override_cancel_outcome(
            manual_override_active=inputs.manual_override_active,
            fan_override_active=inputs.fan_override_active,
            grace_active=True,
        )
        next_state = (OverrideConfirmState.IDLE, GraceState.NONE)
        return OverrideGraceTransition(current_state, next_state, kind, outcome.value, inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_DETECTED:
        # A second, independent override candidate arrives while a DIFFERENT
        # override's grace already protects (e.g. a fan-only override's grace
        # while a thermostat-mode override is separately being detected).
        next_state = _land_after_detection(inputs, GraceState.ACTIVE_PROTECTING_OVERRIDE)
        return OverrideGraceTransition(current_state, next_state, kind, "detected", inputs.now)

    # MANUAL_OVERRIDE_DURING_PAUSE / DASHBOARD_RESUME: both guard on
    # paused_by_door, unreachable while already protecting an active override
    # (production cannot be simultaneously paused-by-door and override-active —
    # pause clears before override confirmation starts). OVERRIDE_CONFIRM_EXPIRED:
    # unreachable — this state's confirm is already IDLE by construction (grace
    # only becomes ACTIVE_PROTECTING_OVERRIDE via a completed confirmation).
    return OverrideGraceTransition(current_state, current_state, kind, "noop_already_protecting", inputs.now)


def _transition_from_grace_unprotected(
    current_state: OverrideGraceLifecycleState, event: OverrideGraceFsmEvent
) -> OverrideGraceTransition:
    inputs = event.inputs
    kind = event.kind
    confirm, _grace = current_state

    if kind == OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED:
        outcome_str = _grace_expiry_outcome_str(inputs)
        next_state = (OverrideConfirmState.IDLE, GraceState.NONE)
        return OverrideGraceTransition(current_state, next_state, kind, outcome_str, inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_DETECTED:
        # A possible override is detected while a non-override grace (fan-off,
        # window-close, dashboard-resume) is running — independent flags, no
        # mutual guard between them.
        next_state = _land_after_detection(inputs, GraceState.ACTIVE_UNPROTECTED)
        return OverrideGraceTransition(current_state, next_state, kind, "detected", inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_CONFIRM_EXPIRED:
        if confirm != OverrideConfirmState.PENDING:
            return OverrideGraceTransition(current_state, current_state, kind, "unreachable_not_pending", inputs.now)
        path = decide_override_confirm_path(
            setpoint_override=inputs.setpoint_override,
            current_mode=inputs.current_mode or "unknown",
            classification_mode=inputs.classification_mode,
        )
        if path == OverrideConfirmPathOutcome.CONFIRM:
            # _confirm_override() cancels the unprotected grace and starts a
            # fresh protecting one.
            next_state = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)
        else:
            next_state = (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)
        return OverrideGraceTransition(current_state, next_state, kind, path.value, inputs.now)

    if kind == OverrideGraceFsmEventKind.OVERRIDE_CANCELLED:
        outcome = decide_override_cancel_outcome(
            manual_override_active=inputs.manual_override_active,
            fan_override_active=inputs.fan_override_active,
            grace_active=True,
        )
        next_state = (
            current_state if outcome == OverrideCancelOutcome.NOOP else (OverrideConfirmState.IDLE, GraceState.NONE)
        )
        return OverrideGraceTransition(current_state, next_state, kind, outcome.value, inputs.now)

    # OVERRIDE_SUPERSEDED: requires an active override to supersede, not
    # reachable while unprotected. MANUAL_OVERRIDE_DURING_PAUSE / DASHBOARD_RESUME:
    # both guard on paused_by_door — orthogonal to this grace running, but
    # production never reaches door/window pause while a non-override grace from
    # a DIFFERENT trigger (fan-off) is simultaneously active in the scenarios
    # traced for this phase; modeled as a defensive no-op rather than assumed.
    return OverrideGraceTransition(current_state, current_state, kind, "noop_unprotected_grace", inputs.now)


def transition(current_state: OverrideGraceLifecycleState, event: OverrideGraceFsmEvent) -> OverrideGraceTransition:
    """The single entry point: given the current (confirm, grace) state pair and
    an event carrying a live-inputs snapshot, return the next state (and why).

    Dispatches on the grace axis first (the dominant axis — grace outlives or
    is independent of confirm state in most transitions), then the confirm axis
    only where it matters, mirroring ``door_window_fsm.py``'s own precedent of
    collapsing sub-states that share almost all their transition logic.

    ``FAN_OVERRIDE_DETECTED`` (Issue #661) is handled here, before the
    state-based dispatch, rather than inside any of the three
    ``_transition_from_*`` functions: ``handle_fan_manual_override()`` is fully
    idempotent and unconditional in production (always lands on
    ``ACTIVE_PROTECTING_OVERRIDE`` regardless of origin state — see
    ``_GRACE_TRIGGERS_PROTECTING_OVERRIDE`` in automation.py), so there is no
    state-dependent branching to do, and short-circuiting here keeps
    ``_land_after_detection()``'s thermostat-only confirm-delay model completely
    untouched by the fan path's different contract.
    """
    if event.kind == OverrideGraceFsmEventKind.FAN_OVERRIDE_DETECTED:
        next_state = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)
        return OverrideGraceTransition(
            current_state, next_state, event.kind, "fan_override_immediate", event.inputs.now
        )
    _confirm, grace = current_state
    if grace == GraceState.NONE:
        return _transition_from_no_grace(current_state, event)
    if grace == GraceState.ACTIVE_PROTECTING_OVERRIDE:
        return _transition_from_grace_protecting(current_state, event)
    return _transition_from_grace_unprotected(current_state, event)
