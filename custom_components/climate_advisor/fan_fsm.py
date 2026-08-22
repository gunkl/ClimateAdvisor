"""Fan/WHF handler-triggered FSM — the unified transition table (Issue #731, Phase 3).

Assembles the pieces Phase 1 (``fan_lifecycle.py``, the composed 5-axis
``FanLifecycleState``) and Phase 2 (``fan_toggle_rate_limit.py``, now delegated
to from ``automation.py``'s ``_fan_toggle_rate_limited()``) built, plus 4 other
pre-existing pure decision modules, into one explicit ``(state, event) ->
Transition`` function, mirroring ``nat_vent_fsm.py``/``override_grace_fsm.py``'s
shape:

- ``fan_lifecycle.derive_fan_lifecycle_state()`` — the 5-axis composed state
  this module reuses unchanged (``FanPhysicalState`` / ``FanOverrideState`` /
  ``FanCyclingState`` / ``WhfHvacOwnership`` / ``FanRateLimitState``).
- ``fan_toggle_rate_limit.decide_fan_toggle_rate_limit()`` — the rapid-cycling
  backstop, called for ``ACTIVATE_REQUESTED``/``DEACTIVATE_REQUESTED``.
- ``fan_drift_reconciliation.decide_fan_drift_reconciliation()`` — the 2-tick
  physical-state self-heal, called for ``DRIFT_TICK``.
- ``fan_thermostat_decision.decide_fan_thermostat_check()`` — the tick-level
  thermostatic stop check, called for ``THERMOSTAT_CHECK_TICK``.
- ``desired_state.decide_fan_cycle_on()`` / ``decide_fan_cycle_off()`` — the
  min-runtime cycling on/off phase decisions, called for
  ``MIN_RUNTIME_CYCLE_ON``/``MIN_RUNTIME_CYCLE_OFF``.
- ``desired_state.decide_fan_thermo_backstop()`` — the 5-minute backstop
  re-arm decision, called for ``THERMO_BACKSTOP_TICK``.

This module does not re-derive or duplicate any of these 6 imported pieces'
own logic — it is integration/wiring only: which pure function (if any) to
call given the event kind, and how to fold its result into the composed
state and the transition's audit-trail shell-directive fields. Each of the 6
pieces already has its own exhaustive unit-test coverage; this module's own
tests focus on wiring correctness, not re-proving decision logic already
proven elsewhere (same scoping discipline ``nat_vent_fsm.py``'s test file
documents for itself).

**NOT wired into any production call site in this phase.** ``automation.py``'s
12 real fan/WHF entry points (``_activate_fan``, ``_deactivate_fan``,
``reconcile_fan_on_startup``, ``handle_fan_manual_override``,
``clear_fan_override``, ``on_fan_turned_off``,
``_clear_fan_flags_and_start_grace``, ``_fan_cycle_on``/``_fan_cycle_off``/
``_stop_fan_min_runtime_cycles``, ``_reconcile_fan_physical_drift``,
``_thermo_backstop_task``/``_start_fan_thermo_backstop``/
``_cancel_fan_thermo_backstop``, ``fan_thermostat_check``, ``_whf_owns_hvac``/
``_resolve_whf_hvac_suppression``/``_release_whf_and_reclassify``) are
unchanged by this phase — cutover to real call sites is a later phase in the
Issue #731 plan.

**Dispatch direction is inverted from ``nat_vent_fsm.py``.** Nat-vent's
``transition()`` dispatches on *current state* (active vs. inactive) because
nat-vent is periodically re-evaluated every tick from a small number of call
sites, all reachable from either state. The fan/WHF subsystem instead has 16
genuinely distinct real entry points (listed above, one per handler), each
with its own precondition set and its own idea of which of the 5 composed
axes it can move — the same shape ``override_grace_fsm.py`` describes for
itself ("handler-triggered, not periodically re-evaluated"). So this module's
``transition()`` dispatches on ``event.kind`` first, exactly like
``override_grace_fsm.py``'s ``transition()`` — the *event* determines almost
all of the transition logic; ``current_state`` is read only for its
``rate_limit``/physical axes where a decision genuinely depends on the prior
tick (e.g. ``DRIFT_TICK``'s tick-count progression).

**Two different transition shapes, by event kind.** Following this module's
own read of the real call sites, the 16 event kinds split into two groups:

1. **Pure caller-already-decided kinds** (``STARTUP_RECONCILE``,
   ``MANUAL_OVERRIDE_DETECTED``, ``OVERRIDE_CLEARED``, ``USER_FAN_OFF``,
   ``TIMER_BOUNDARY_SETTLE``, ``FLAGS_CLEARED_FOR_GRACE``,
   ``MIN_RUNTIME_CYCLE_STOPPED``, ``WHF_SUPPRESSION_REQUESTED``,
   ``WHF_RELEASE_REQUESTED``) — the real production method for these has
   already mutated the underlying ``AutomationEngine`` flags by the time this
   FSM would be consulted (the same "the flags are already true when this
   function is called" convention ``_resolve_override_grace_fsm_state()``
   already establishes for override/grace). ``event.inputs`` is therefore the
   POST-change snapshot, and ``to_state`` is simply
   ``derive_fan_lifecycle_state(event.inputs)`` — no pure-fn call, no
   shell-directive fields populated.

2. **Decision-bearing kinds** (``ACTIVATE_REQUESTED``, ``DEACTIVATE_REQUESTED``,
   ``DRIFT_TICK``, ``MIN_RUNTIME_CYCLE_ON``, ``MIN_RUNTIME_CYCLE_OFF``,
   ``THERMO_BACKSTOP_TICK``, ``THERMOSTAT_CHECK_TICK``) — these correspond to
   the real production call sites where the pure decision function itself
   determines whether/how a flag changes (e.g. whether the rate limiter
   defers an activation, whether drift has been confirmed over 2 ticks,
   whether the cycler should turn the fan on). For these, ``event.inputs`` is
   the PRE-change snapshot: this module calls the relevant pure decision
   function, populates the outcome into the transition's shell-directive
   fields (for a future wired caller to act on), and derives ``to_state`` by
   re-deriving composed state against an EFFECTIVE inputs snapshot that
   applies only the flag changes the pure function's own outcome implies
   (documented per-branch below) — never a flag change the pure function
   didn't decide. This mirrors ``nat_vent_fsm.py``'s ``fan_should_be_active``
   precedent: the FSM surfaces what a pure decision implies, the shell still
   owns applying the real side effects (hardware commands, grace periods,
   HVAC restores, event emission).

   Two of the 7 decision-bearing kinds (``THERMO_BACKSTOP_TICK``,
   ``THERMOSTAT_CHECK_TICK``) deliberately do NOT project their outcome onto
   ``to_state`` at all (``to_state == from_state`` always for these two) —
   see each one's own ``_transition_on_*`` docstring for why: both outcomes
   only inform a downstream decision the real production method makes with
   additional context (HVAC suppression release, nat-vent exit routing,
   event-type selection) this FSM's 5 composed axes cannot represent without
   duplicating logic that belongs to ``_exit_nat_vent()``/``_deactivate_fan()``
   instead.

**Cross-lifecycle input — ``natural_vent_active`` is a plain boolean, not
nat-vent's own ``NatVentLifecycleState`` object (Issue #530).** Several real
fan-subsystem methods (``_release_whf_and_reclassify()``'s guard, most
notably) read ``self._natural_vent_active`` as a simple flag-ownership check
("does the nat-vent session still own this fan"), not as a state-composition
input the way ``paused_by_door``/``natural_vent_active`` feed
``door_window_fsm.py``. Reading a plain bool here — rather than importing
``nat_vent_lifecycle.NatVentLifecycleState`` and pattern-matching on it —
keeps this module decoupled from nat-vent's own state shape (which changed
twice already, Issue #672 and Issue #706, without this module needing to
know); a future ``FanFsmInputs`` field could upgrade to the richer type if a
decision genuinely needs more than "is a session currently claiming this
fan," but no branch in this phase does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .desired_state import (
    FanCycleOutcome,
    decide_fan_cycle_off,
    decide_fan_cycle_on,
    decide_fan_thermo_backstop,
)
from .fan_drift_reconciliation import FanDriftInputs, FanDriftOutcome, decide_fan_drift_reconciliation
from .fan_lifecycle import (
    FanLifecycleInputs,
    FanLifecycleState,
    derive_fan_lifecycle_state,
)
from .fan_thermostat_decision import FanThermostatInputs, FanThermostatOutcome, decide_fan_thermostat_check
from .fan_toggle_rate_limit import FanToggleRateLimitInputs, FanToggleRateLimitOutcome, decide_fan_toggle_rate_limit

# Mirrors const.FAN_MIN_TOGGLE_INTERVAL_S — same import-independence convention
# nat_vent_gate.py/fan_thermostat_decision.py/desired_state.py already use for
# every pure module in this codebase (never import from const.py or automation.py).
_FAN_MIN_TOGGLE_INTERVAL_S_DEFAULT = 300.0

_FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
_FAN_MODE_BOTH = "both"


class FanFsmEventKind(Enum):
    """What prompted this transition evaluation — one per real production entry
    point, same per-handler convention ``override_grace_fsm.py`` uses (fan/WHF
    is handler-triggered, not periodically re-evaluated the way nat-vent is —
    see module docstring's "Dispatch direction" note).

    16 members, one per real call site read in full for this phase:
      ACTIVATE_REQUESTED          -> _activate_fan()
      DEACTIVATE_REQUESTED        -> _deactivate_fan()
      STARTUP_RECONCILE           -> reconcile_fan_on_startup() (3 flag-write sites)
      MANUAL_OVERRIDE_DETECTED    -> handle_fan_manual_override()
      OVERRIDE_CLEARED            -> clear_fan_override()
      USER_FAN_OFF                -> on_fan_turned_off()
      TIMER_BOUNDARY_SETTLE       -> on_fan_turned_off()'s RF-timer-boundary
                                      coalesce branch (Issue #530) — kept distinct
                                      from USER_FAN_OFF because it deliberately
                                      does NOT start a fresh fan-off grace and
                                      may route through _exit_nat_vent() instead
                                      of _clear_fan_flags_and_start_grace()
      FLAGS_CLEARED_FOR_GRACE     -> _clear_fan_flags_and_start_grace()
      MIN_RUNTIME_CYCLE_ON        -> _fan_cycle_on()
      MIN_RUNTIME_CYCLE_OFF       -> _fan_cycle_off()
      MIN_RUNTIME_CYCLE_STOPPED   -> _stop_fan_min_runtime_cycles()
      DRIFT_TICK                  -> _reconcile_fan_physical_drift()
      THERMO_BACKSTOP_TICK        -> _thermo_backstop_task() /
                                      _start_fan_thermo_backstop() /
                                      _cancel_fan_thermo_backstop()
      THERMOSTAT_CHECK_TICK       -> fan_thermostat_check()
      WHF_SUPPRESSION_REQUESTED   -> _suppress_hvac_for_whf() (via
                                      _resolve_whf_hvac_suppression())
      WHF_RELEASE_REQUESTED       -> _release_whf_and_reclassify() (via
                                      _resolve_whf_hvac_suppression()) — also
                                      covers _deactivate_fan()'s two release
                                      branches, which call the same dispatcher
    """

    ACTIVATE_REQUESTED = "activate_requested"
    DEACTIVATE_REQUESTED = "deactivate_requested"
    STARTUP_RECONCILE = "startup_reconcile"
    MANUAL_OVERRIDE_DETECTED = "manual_override_detected"
    OVERRIDE_CLEARED = "override_cleared"
    USER_FAN_OFF = "user_fan_off"
    TIMER_BOUNDARY_SETTLE = "timer_boundary_settle"
    FLAGS_CLEARED_FOR_GRACE = "flags_cleared_for_grace"
    MIN_RUNTIME_CYCLE_ON = "min_runtime_cycle_on"
    MIN_RUNTIME_CYCLE_OFF = "min_runtime_cycle_off"
    MIN_RUNTIME_CYCLE_STOPPED = "min_runtime_cycle_stopped"
    DRIFT_TICK = "drift_tick"
    THERMO_BACKSTOP_TICK = "thermo_backstop_tick"
    THERMOSTAT_CHECK_TICK = "thermostat_check_tick"
    WHF_SUPPRESSION_REQUESTED = "whf_suppression_requested"
    WHF_RELEASE_REQUESTED = "whf_release_requested"


@dataclass(frozen=True)
class FanFsmInputs:
    """Every live reading this FSM's transition function may consult — explicit,
    nothing hidden. Union of ``FanLifecycleInputs``' fields (feeds
    ``derive_fan_lifecycle_state()`` for every event kind) plus the additional
    fields the 6 imported pure decision functions need for the decision-bearing
    event kinds (see module docstring group 2), plus the one cross-lifecycle
    read (``natural_vent_active``).

    Field-by-field correspondence to the real ``AutomationEngine`` attributes
    for the ``FanLifecycleInputs``-shared fields matches ``fan_lifecycle.py``'s
    own docstring exactly (not repeated here). Additional fields below:

      last_toggle_command_time -> self._fan_toggle_command_time (fan_toggle_rate_limit.py)
      toggle_min_interval_s     -> const.py FAN_MIN_TOGGLE_INTERVAL_S (fan_toggle_rate_limit.py)
      recent_fan_command         -> self._is_recent_fan_command_callback(...) (fan_drift_reconciliation.py)
      physical_state_available    -> bool(self._get_fan_physical_state_callback) (fan_drift_reconciliation.py)
      physical_on                  -> self._get_fan_physical_state_callback() (fan_drift_reconciliation.py)
      fan_min_runtime_minutes       -> config CONF_FAN_MIN_RUNTIME_PER_HOUR (desired_state.py cycle fns)
      fan_running                    -> self._fan_running (desired_state.decide_fan_thermo_backstop)
      indoor, outdoor                 -> live sensor reads (fan_thermostat_decision.py)
      comfort_heat_raw                 -> config comfort_heat (fan_thermostat_decision.py)
      sleep_heat                        -> config CONF_SLEEP_HEAT (fan_thermostat_decision.py)
      in_sleep_window                    -> caller-resolved _in_sleep_window() (fan_thermostat_decision.py)
      hysteresis                          -> config CONF_NAT_VENT_HYSTERESIS_F (fan_thermostat_decision.py)
      manual_override_active               -> self._manual_override_active (fan_thermostat_decision.py
                                               Check 0, Issue #714 — distinct from fan_override_active,
                                               which is the fan-specific override flag already on
                                               FanLifecycleInputs)
      manual_override_mode                  -> self._manual_override_mode (fan_thermostat_decision.py Check 0)
    """

    # --- FanLifecycleInputs-shared fields (feed derive_fan_lifecycle_state) ---
    fan_active: bool
    fan_drift_tick_count: int
    fan_override_active: bool
    fan_remote_timer_hours: float | None
    fan_min_runtime_active: bool
    fan_mode: str
    pre_fan_hvac_mode: str | None
    fan_rate_limited_until: datetime | None
    fan_rate_limited_direction: str | None
    now: datetime

    # --- cross-lifecycle input (Issue #530 ownership guard — see module docstring) ---
    natural_vent_active: bool = False

    # --- fan_toggle_rate_limit.py (ACTIVATE_REQUESTED / DEACTIVATE_REQUESTED) ---
    last_toggle_command_time: datetime | None = None
    toggle_min_interval_s: float = _FAN_MIN_TOGGLE_INTERVAL_S_DEFAULT

    # --- fan_drift_reconciliation.py (DRIFT_TICK) ---
    recent_fan_command: bool = False
    physical_state_available: bool = False
    physical_on: bool | None = None

    # --- desired_state.py fan-cycle / thermo-backstop fns ---
    fan_min_runtime_minutes: float = 0.0
    fan_running: bool = False

    # --- fan_thermostat_decision.py (THERMOSTAT_CHECK_TICK) ---
    indoor: float | None = None
    outdoor: float | None = None
    comfort_heat_raw: float = 0.0
    sleep_heat: float = 0.0
    in_sleep_window: bool = False
    hysteresis: float = 0.0
    manual_override_active: bool = False
    manual_override_mode: str | None = None


@dataclass(frozen=True)
class FanFsmEvent:
    kind: FanFsmEventKind
    inputs: FanFsmInputs


@dataclass(frozen=True)
class FanTransition:
    """The transition's full record — audit trail by construction.

    Shell-directive fields are ``None`` unless the event kind's own branch
    populated them — see module docstring's group 1 / group 2 split.
    """

    from_state: FanLifecycleState
    to_state: FanLifecycleState
    event_kind: FanFsmEventKind
    at: datetime | None = None

    drift_outcome: FanDriftOutcome | None = None
    next_drift_tick_count: int | None = None

    cycle_outcome: FanCycleOutcome | None = None
    cycle_delay_seconds: float | None = None
    cycle_should_deactivate: bool | None = None

    thermo_backstop_should_be_armed: bool | None = None

    thermostat_outcome: FanThermostatOutcome | None = None

    rate_limit_outcome: FanToggleRateLimitOutcome | None = None
    rate_limit_applies_at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _lifecycle_inputs(inputs: FanFsmInputs) -> FanLifecycleInputs:
    return FanLifecycleInputs(
        fan_active=inputs.fan_active,
        fan_drift_tick_count=inputs.fan_drift_tick_count,
        fan_override_active=inputs.fan_override_active,
        fan_remote_timer_hours=inputs.fan_remote_timer_hours,
        fan_min_runtime_active=inputs.fan_min_runtime_active,
        fan_mode=inputs.fan_mode,
        pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
        fan_rate_limited_until=inputs.fan_rate_limited_until,
        fan_rate_limited_direction=inputs.fan_rate_limited_direction,
        now=inputs.now,
    )


def _derive(inputs: FanFsmInputs) -> FanLifecycleState:
    """Straight derivation against the given snapshot — no effective-inputs
    substitution. Used by every group-1 (caller-already-decided) event kind."""
    return derive_fan_lifecycle_state(_lifecycle_inputs(inputs))


def _rate_limit_inputs(inputs: FanFsmInputs, *, action: str) -> FanToggleRateLimitInputs:
    return FanToggleRateLimitInputs(
        last_toggle_command_time=inputs.last_toggle_command_time,
        action=action,
        rate_limited_until=inputs.fan_rate_limited_until,
        rate_limited_direction=inputs.fan_rate_limited_direction,
        min_interval_s=inputs.toggle_min_interval_s,
        now=inputs.now,
    )


def _drift_inputs(inputs: FanFsmInputs) -> FanDriftInputs:
    return FanDriftInputs(
        fan_active=inputs.fan_active,
        fan_mode=inputs.fan_mode,
        recent_fan_command=inputs.recent_fan_command,
        physical_state_available=inputs.physical_state_available,
        physical_on=inputs.physical_on,
        tick_count=inputs.fan_drift_tick_count,
    )


def _thermostat_check_inputs(inputs: FanFsmInputs) -> FanThermostatInputs:
    return FanThermostatInputs(
        indoor=inputs.indoor,
        outdoor=inputs.outdoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        hysteresis=inputs.hysteresis,
        natural_vent_active=inputs.natural_vent_active,
        manual_override_active=inputs.manual_override_active,
        manual_override_mode=inputs.manual_override_mode,
    )


def _transition_on_activate_requested(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_activate_fan(): the rate-limit backstop is the only decision this FSM
    can see at this call site (the override/disabled/dry-run/idempotency guards
    ahead of it in the real method are all cheap precondition checks the shell
    keeps — same scoping ``fan_thermostat_decision.py``'s own docstring
    establishes for its own precondition checks).

    ALLOW does not by itself mean the fan turns on (the real method still has
    to command hardware) — so ``to_state`` only changes the ``rate_limit`` axis
    (DEFER_NEW/DEFER_DUPLICATE record the deferral; ALLOW clears any stale
    deferral for this direction). ``fan_active`` itself is left at whatever
    ``event.inputs`` already says — a follow-up event models the actual
    hardware-confirmed activation once wired.
    """
    inputs = event.inputs
    outcome, applies_at = decide_fan_toggle_rate_limit(_rate_limit_inputs(inputs, action="activate"))

    if outcome is FanToggleRateLimitOutcome.ALLOW:
        effective = FanLifecycleInputs(
            fan_active=inputs.fan_active,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=inputs.fan_min_runtime_active,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=None,
            fan_rate_limited_direction=None,
            now=inputs.now,
        )
    else:
        effective = FanLifecycleInputs(
            fan_active=inputs.fan_active,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=inputs.fan_min_runtime_active,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=applies_at,
            fan_rate_limited_direction="activate",
            now=inputs.now,
        )

    return FanTransition(
        from_state=current_state,
        to_state=derive_fan_lifecycle_state(effective),
        event_kind=event.kind,
        at=inputs.now,
        rate_limit_outcome=outcome,
        rate_limit_applies_at=applies_at,
    )


def _transition_on_deactivate_requested(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_deactivate_fan(): mirror of _transition_on_activate_requested() for the
    opposite direction — see that function's docstring for the shared rationale."""
    inputs = event.inputs
    outcome, applies_at = decide_fan_toggle_rate_limit(_rate_limit_inputs(inputs, action="deactivate"))

    if outcome is FanToggleRateLimitOutcome.ALLOW:
        effective = FanLifecycleInputs(
            fan_active=inputs.fan_active,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=inputs.fan_min_runtime_active,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=None,
            fan_rate_limited_direction=None,
            now=inputs.now,
        )
    else:
        effective = FanLifecycleInputs(
            fan_active=inputs.fan_active,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=inputs.fan_min_runtime_active,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=applies_at,
            fan_rate_limited_direction="deactivate",
            now=inputs.now,
        )

    return FanTransition(
        from_state=current_state,
        to_state=derive_fan_lifecycle_state(effective),
        event_kind=event.kind,
        at=inputs.now,
        rate_limit_outcome=outcome,
        rate_limit_applies_at=applies_at,
    )


def _transition_on_drift_tick(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_reconcile_fan_physical_drift(): unlike the two rate-limit kinds above,
    this decision DOES directly determine the physical axis — the pure
    function's own ``next_tick_count`` (RESET/CORRECT -> 0, AWAITING ->
    incremented, NOOP -> unchanged) and, for CORRECT, ``fan_active`` flipping
    False are exactly what ``_clear_fan_flags_and_start_grace()`` applies in
    the real method — so the effective inputs substitute both.
    """
    inputs = event.inputs
    outcome, next_tick_count = decide_fan_drift_reconciliation(_drift_inputs(inputs))

    effective_fan_active = False if outcome is FanDriftOutcome.CORRECT else inputs.fan_active
    effective = FanLifecycleInputs(
        fan_active=effective_fan_active,
        fan_drift_tick_count=next_tick_count,
        fan_override_active=inputs.fan_override_active,
        fan_remote_timer_hours=inputs.fan_remote_timer_hours,
        fan_min_runtime_active=inputs.fan_min_runtime_active,
        fan_mode=inputs.fan_mode,
        pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
        fan_rate_limited_until=inputs.fan_rate_limited_until,
        fan_rate_limited_direction=inputs.fan_rate_limited_direction,
        now=inputs.now,
    )

    return FanTransition(
        from_state=current_state,
        to_state=derive_fan_lifecycle_state(effective),
        event_kind=event.kind,
        at=inputs.now,
        drift_outcome=outcome,
        next_drift_tick_count=next_tick_count,
    )


def _transition_on_min_runtime_cycle_on(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_fan_cycle_on(): ACTIVATE_ALWAYS_ON/ACTIVATE_WITH_OFF_TIMER both set
    fan_active=True and fan_min_runtime_active=True in the real method — the
    only two outcomes this FSM projects onto the composed state. DISABLED/
    OVERRIDE_SUSPENDED/RETRY_LATER change nothing (the real method's own
    early-return/reschedule branches touch no fan-lifecycle flag)."""
    inputs = event.inputs
    outcome, delay = decide_fan_cycle_on(
        min_runtime_minutes=inputs.fan_min_runtime_minutes,
        fan_mode=inputs.fan_mode,
        fan_override_active=inputs.fan_override_active,
        fan_active=inputs.fan_active,
    )

    if outcome in (FanCycleOutcome.ACTIVATE_ALWAYS_ON, FanCycleOutcome.ACTIVATE_WITH_OFF_TIMER):
        effective = FanLifecycleInputs(
            fan_active=True,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=True,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=inputs.fan_rate_limited_until,
            fan_rate_limited_direction=inputs.fan_rate_limited_direction,
            now=inputs.now,
        )
        to_state = derive_fan_lifecycle_state(effective)
    else:
        to_state = _derive(inputs)

    return FanTransition(
        from_state=current_state,
        to_state=to_state,
        event_kind=event.kind,
        at=inputs.now,
        cycle_outcome=outcome,
        cycle_delay_seconds=delay,
    )


def _transition_on_min_runtime_cycle_off(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_fan_cycle_off(): should_deactivate=True sets fan_active=False and
    fan_min_runtime_active=False in the real method (guarded by
    fan_min_runtime_active, matching decide_fan_cycle_off()'s own contract).
    wait_seconds (the next on-phase re-check delay) is always returned by the
    real method regardless — populated unconditionally here too."""
    inputs = event.inputs
    should_deactivate, wait_seconds = decide_fan_cycle_off(
        fan_min_runtime_active=inputs.fan_min_runtime_active,
        min_runtime_minutes=inputs.fan_min_runtime_minutes,
    )

    if should_deactivate:
        effective = FanLifecycleInputs(
            fan_active=False,
            fan_drift_tick_count=inputs.fan_drift_tick_count,
            fan_override_active=inputs.fan_override_active,
            fan_remote_timer_hours=inputs.fan_remote_timer_hours,
            fan_min_runtime_active=False,
            fan_mode=inputs.fan_mode,
            pre_fan_hvac_mode=inputs.pre_fan_hvac_mode,
            fan_rate_limited_until=inputs.fan_rate_limited_until,
            fan_rate_limited_direction=inputs.fan_rate_limited_direction,
            now=inputs.now,
        )
        to_state = derive_fan_lifecycle_state(effective)
    else:
        to_state = _derive(inputs)

    return FanTransition(
        from_state=current_state,
        to_state=to_state,
        event_kind=event.kind,
        at=inputs.now,
        cycle_delay_seconds=wait_seconds,
        cycle_should_deactivate=should_deactivate,
    )


def _transition_on_thermo_backstop_tick(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """_thermo_backstop_task()/_start_fan_thermo_backstop(): the real decision
    (``decide_fan_thermo_backstop()``) only answers "should the 5-minute timer
    re-arm itself" — it reads ``fan_running`` but writes no fan-lifecycle flag
    of its own (timer arm/disarm is not one of the 5 composed axes). ``to_state``
    is therefore always ``from_state``'s own re-derivation against the
    unmodified snapshot; only ``thermo_backstop_should_be_armed`` carries the
    decision."""
    inputs = event.inputs
    result = decide_fan_thermo_backstop(fan_running=inputs.fan_running, delay_seconds=5 * 60, now=inputs.now)
    should_arm = result is not None

    return FanTransition(
        from_state=current_state,
        to_state=_derive(inputs),
        event_kind=event.kind,
        at=inputs.now,
        thermo_backstop_should_be_armed=should_arm,
    )


def _transition_on_thermostat_check_tick(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """fan_thermostat_check(): deliberately does NOT project the outcome onto
    ``to_state`` (always ``from_state`` re-derived unchanged). Every non-KEEP
    outcome in the real method routes through ``_exit_nat_vent()`` or
    ``_deactivate_fan()`` with additional context this FSM's 5 composed axes
    cannot represent without duplicating that routing logic here (event-type
    selection between ``nat_vent_outdoor_rise_exit``/``fan_deactivated``/
    ``nat_vent_comfort_floor_exit``, HVAC-restore-vs-pause-for-open-sensor
    decisions, WHF suppression release) — modeling a partial version of that
    routing would risk silently drifting from ``_exit_nat_vent()``'s own logic,
    exactly the "sibling threshold drift" failure mode this codebase has hit
    repeatedly (Issues #400/#402/#417/#456/#458). ``thermostat_outcome`` alone
    is authoritative for what a future wired caller should do next; the actual
    flag changes are modeled by whatever follow-up event that caller then
    raises (e.g. ``DEACTIVATE_REQUESTED`` or a nat-vent-FSM exit event) once
    cutover happens."""
    inputs = event.inputs
    outcome = decide_fan_thermostat_check(_thermostat_check_inputs(inputs))

    return FanTransition(
        from_state=current_state,
        to_state=_derive(inputs),
        event_kind=event.kind,
        at=inputs.now,
        thermostat_outcome=outcome,
    )


def transition(current_state: FanLifecycleState, event: FanFsmEvent) -> FanTransition:
    """The single entry point: given the current composed state and an event
    carrying a live-inputs snapshot, return the next state (and why).

    Dispatches on ``event.kind`` — the fan/WHF subsystem is handler-triggered,
    not periodically re-evaluated (the inverse of ``nat_vent_fsm.py``'s
    state-first dispatch — see module docstring's "Dispatch direction" note).
    """
    if event.kind is FanFsmEventKind.ACTIVATE_REQUESTED:
        return _transition_on_activate_requested(current_state, event)
    if event.kind is FanFsmEventKind.DEACTIVATE_REQUESTED:
        return _transition_on_deactivate_requested(current_state, event)
    if event.kind is FanFsmEventKind.DRIFT_TICK:
        return _transition_on_drift_tick(current_state, event)
    if event.kind is FanFsmEventKind.MIN_RUNTIME_CYCLE_ON:
        return _transition_on_min_runtime_cycle_on(current_state, event)
    if event.kind is FanFsmEventKind.MIN_RUNTIME_CYCLE_OFF:
        return _transition_on_min_runtime_cycle_off(current_state, event)
    if event.kind is FanFsmEventKind.THERMO_BACKSTOP_TICK:
        return _transition_on_thermo_backstop_tick(current_state, event)
    if event.kind is FanFsmEventKind.THERMOSTAT_CHECK_TICK:
        return _transition_on_thermostat_check_tick(current_state, event)

    # Group 1 — pure caller-already-decided kinds (see module docstring):
    # STARTUP_RECONCILE, MANUAL_OVERRIDE_DETECTED, OVERRIDE_CLEARED, USER_FAN_OFF,
    # TIMER_BOUNDARY_SETTLE, FLAGS_CLEARED_FOR_GRACE, MIN_RUNTIME_CYCLE_STOPPED,
    # WHF_SUPPRESSION_REQUESTED, WHF_RELEASE_REQUESTED. No pure-fn call, no
    # shell-directive fields — just re-derive composed state against the
    # already-post-change snapshot the caller supplied.
    return FanTransition(
        from_state=current_state,
        to_state=_derive(event.inputs),
        event_kind=event.kind,
        at=event.inputs.now,
    )
