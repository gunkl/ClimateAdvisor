"""Occupancy dispatch decision FSM (Issue #744, strangler-fig completion Phase 4).

Assembles this phase's pure decisions for the three real occupancy-transition
handlers in ``automation.py`` — ``handle_occupancy_away()``, ``handle_occupancy_home()``,
``handle_occupancy_vacation()``. Follows the shape ``nat_vent_fsm.py`` /
``door_window_fsm.py`` / ``override_grace_fsm.py`` / ``fan_fsm.py`` /
``classification_fsm.py`` all established — with the same deliberate departure
``classification_fsm.py`` took (statelessness), plus one further departure of its
own (two decision functions instead of one ``transition()`` entry point). Both
departures are load-bearing; read this whole docstring before touching this module.

**Five-whys: why is this FSM STATELESS (no persisted lifecycle state, like
``classification_fsm.py`` and unlike the other 4 precedent FSMs)?** (1) Why no
``OccupancyLifecycleState``? Because ``self._occupancy_mode`` is DATA — a fresh
string handed in by the caller (``set_occupancy_mode()``, called by the coordinator
right after it resolves the toggle priority) — not a state machine that persists and
evolves across calls the way nat-vent's ACTIVE/PAUSED axes or fan's 5 composed axes
do. (2) Why does that matter? Every precedent FSM's ``*LifecycleState`` answers "what
is the subsystem doing RIGHT NOW, independent of this one event" — there is no such
question here: each of the 3 handlers computes its entire decision fresh from
``self._occupancy_mode`` (already resolved) plus a handful of independent guard flags
(``_paused_by_door``, ``_manual_override_active``, classification presence, welcome-
home debounce timing), then returns. Nothing decided this call constrains or is
remembered by the next call. (3) What about the AWAY-pending-vs-AWAY-active split —
isn't THAT a real lifecycle the FSM should track? No: the 15-minute delay between
"occupancy toggle flipped to away" and "setback actually applied" is a plain
``async_call_later`` timer owned by the COORDINATOR (``_async_occupancy_toggle_changed()``
schedules it, ``_cancel_occupancy_away_timer()`` cancels it on any mode change before
the delay elapses) — the ENGINE never observes "pending" as a state; it only ever
sees the timer's eventual callback invoke ``handle_occupancy_away()`` once, the same
single call shape as every other trigger site. There is no "pending" input this FSM's
decisions read or branch on, because the pending window never reaches the engine at
all — modeling it here would invent a state that doesn't correspond to anything this
layer does (the same "fake session lifecycle" failure mode ``classification_fsm.py``'s
own five-whys warned against). (4) Why not force a state object anyway, for shape
parity with the other 4? Same answer as ``classification_fsm.py``'s point 3: a state
dataclass with no real transitions to model is either dead weight or forces inventing
states production doesn't have.

**Five-whys (continued): why TWO decision functions instead of ONE unifying
``transition()``, unlike every precedent including ``classification_fsm.py``?**
(1) ``classification_fsm.py`` has ONE real call site (``apply_classification()``)
with internal branching — one event kind, one entry point, matching production's
own single call shape. (2) Occupancy dispatch has THREE distinct real call sites
(``handle_occupancy_away()``, ``handle_occupancy_home()``, ``handle_occupancy_vacation()``)
that are genuinely different methods with different bodies, called from different
places (the coordinator's toggle-change dispatcher picks exactly one per transition;
``_set_temperature_for_mode()``'s safety net redirects into away/vacation only, never
home). (3) Away and vacation share IDENTICAL structure (paused-suppress → optional
override-clear → classification-gate → band-apply, differing only in which setback
band and which mode label), so they share ONE decision function,
``decide_away_vacation_dispatch()``, parameterized by nothing mode-specific at the
pure-decision layer (the mode string is threaded through by the caller for logging/
event-payload purposes only — see ``occupancy_fsm.py``'s two dataclasses below).
Home has a fundamentally different shape (restore-gate, then an independent 2-stage
notification suppression ladder) and gets its own ``decide_home_dispatch()``. (4) Why
not force these into one ``transition(event)`` behind a shared ``EventKind`` enum
anyway, purely for calling-convention uniformity with the other 5 modules? Because
the two decision shapes (``AwayVacationDecision`` vs ``HomeDecision``) don't unify
into one return type without a synthetic union nothing in production ever needs —
every real caller already knows statically which of the 3 handlers it is in, so an
event-kind dispatch would just re-derive information the call site already has. This
mirrors the same "don't model a distinction production doesn't make" principle
``classification_fsm.py``'s point 5 used to justify going the OTHER way (collapsing
6 call sites to 1 kind) — applied here to justify NOT collapsing 2 genuinely
different shapes into 1.

**Side-effect boundary (unchanged from every precedent): this module decides
branch/outcome only.** Real side effects — logging, ``_emit_event_callback()``
firing (itself gated by the stateful, time-windowed ``_recent_duplicate()``
dedup — Issue #591 — which is NOT something a pure function can decide once and
cache, since its answer depends on wall-clock-adjacent state that keeps changing
between calls), ``clear_manual_override()``, ``_apply_comfort_band()``,
``_set_temperature_for_mode()``, ``_notify()`` — all remain in automation.py's
apply-shell methods (``_apply_occupancy_away_vacation_decision()``,
``_apply_occupancy_home_decision()``), driven by the returned decision instead of
re-deriving the same branch inline. Same scoping discipline every other ``*_fsm.py``
module's own docstring states for itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_RESTORE_MODES = ("heat", "cool")


class AwayVacationOutcome(Enum):
    """Branch selected by ``decide_away_vacation_dispatch()`` — mirrors
    ``handle_occupancy_away()``/``handle_occupancy_vacation()``'s own early-return
    chain exactly (see module docstring)."""

    SUPPRESSED_PAUSED = "suppressed_paused"
    NO_CLASSIFICATION = "no_classification"
    APPLY = "apply"


@dataclass(frozen=True)
class AwayVacationInputs:
    """Every live reading ``decide_away_vacation_dispatch()`` may consult.

    Field-by-field correspondence to the real ``AutomationEngine`` state:
      paused_by_door       -> self._paused_by_door
      manual_override_active -> self._manual_override_active
      has_classification   -> self._current_classification is not None
    """

    paused_by_door: bool
    manual_override_active: bool
    has_classification: bool


@dataclass(frozen=True)
class AwayVacationDecision:
    """The full decision for one away/vacation transition call — audit trail by
    construction.

    ``clear_override`` is independent of ``outcome`` being ``APPLY`` — legacy clears
    the override flag whenever not paused AND override-active, REGARDLESS of whether
    a classification later turns out to be missing (the clear happens before the
    classification-presence check in both ``handle_occupancy_away()`` and
    ``handle_occupancy_vacation()``). Always ``False`` when ``outcome`` is
    ``SUPPRESSED_PAUSED`` (the paused branch returns before the override check).
    """

    outcome: AwayVacationOutcome
    clear_override: bool


def decide_away_vacation_dispatch(inputs: AwayVacationInputs) -> AwayVacationDecision:
    """Pure mirror of ``handle_occupancy_away()``/``handle_occupancy_vacation()``'s
    shared early-return structure — see module docstring for why one function serves
    both handlers.
    """
    if inputs.paused_by_door:
        return AwayVacationDecision(AwayVacationOutcome.SUPPRESSED_PAUSED, clear_override=False)
    clear_override = inputs.manual_override_active
    if not inputs.has_classification:
        return AwayVacationDecision(AwayVacationOutcome.NO_CLASSIFICATION, clear_override=clear_override)
    return AwayVacationDecision(AwayVacationOutcome.APPLY, clear_override=clear_override)


class HomeNotifyOutcome(Enum):
    """Branch selected by ``decide_home_dispatch()`` for the welcome-home
    notification half of ``handle_occupancy_home()``."""

    NONE = "none"  # no classification at all — handler no-ops entirely
    SUPPRESSED_NEAR_COMFORT = "suppressed_near_comfort"
    SUPPRESSED_DEBOUNCE = "suppressed_debounce"
    SEND = "send"


@dataclass(frozen=True)
class HomeInputs:
    """Every live reading ``decide_home_dispatch()`` may consult.

    Field-by-field correspondence to the real ``AutomationEngine``/config reads:
      has_classification -> self._current_classification is not None
      hvac_mode           -> self._current_classification.hvac_mode (None if absent)
      indoor_temp_f        -> self._get_indoor_temp_f()
      comfort_f            -> comfort_heat/comfort_cool matching hvac_mode (None
                               unless hvac_mode in ("heat", "cool"))
      setback_f            -> setback_heat/setback_cool matching hvac_mode (same
                               gating as comfort_f)
      debounce_seconds      -> config CONF_WELCOME_HOME_DEBOUNCE (or its default)
      seconds_since_last_notified -> elapsed seconds since self._last_welcome_home_notified,
                               or None if never notified
    """

    has_classification: bool
    hvac_mode: str | None
    indoor_temp_f: float | None
    comfort_f: float | None
    setback_f: float | None
    debounce_seconds: float
    seconds_since_last_notified: float | None


@dataclass(frozen=True)
class HomeDecision:
    """The full decision for one home-transition call — audit trail by construction.

    ``restore`` is independent of ``notify`` — legacy restores comfort temperature
    (when ``hvac_mode`` is heat/cool) unconditionally before evaluating either
    notification-suppression check, so a restore can happen even when the
    notification itself is suppressed (near-comfort or debounce), and a notification
    can be sent even on an "off" day when no restore occurs.
    """

    restore: bool
    notify: HomeNotifyOutcome


def decide_home_dispatch(inputs: HomeInputs) -> HomeDecision:
    """Pure mirror of ``handle_occupancy_home()``'s restore-gate + 2-stage
    notification-suppression ladder (near-comfort check, then debounce check, else
    send).
    """
    if not inputs.has_classification:
        return HomeDecision(restore=False, notify=HomeNotifyOutcome.NONE)

    restore = inputs.hvac_mode in _RESTORE_MODES

    # Check 1: temperature proximity — skip notification if house already near comfort.
    if (
        inputs.indoor_temp_f is not None
        and inputs.hvac_mode in _RESTORE_MODES
        and inputs.comfort_f is not None
        and inputs.setback_f is not None
        and abs(inputs.indoor_temp_f - inputs.comfort_f) < abs(inputs.indoor_temp_f - inputs.setback_f)
    ):
        return HomeDecision(restore=restore, notify=HomeNotifyOutcome.SUPPRESSED_NEAR_COMFORT)

    # Check 2: debounce — skip notification if one was sent recently.
    if (
        inputs.debounce_seconds > 0
        and inputs.seconds_since_last_notified is not None
        and inputs.seconds_since_last_notified < inputs.debounce_seconds
    ):
        return HomeDecision(restore=restore, notify=HomeNotifyOutcome.SUPPRESSED_DEBOUNCE)

    return HomeDecision(restore=restore, notify=HomeNotifyOutcome.SEND)
