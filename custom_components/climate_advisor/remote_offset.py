"""Remote-sensor outer loop: the pure offset decision core (Issue #1194, increment a).

Occupant outcome: the thermostat only sees its own sensor, so when the bedroom (the room the person
sleeps in) is colder than the hallway thermostat the furnace never starts and the bedroom stays below
the sleep temperature. While Climate Advisor is using a REMOTE sensor reading for a context it therefore
acts as an outer loop on that sensor: it nudges the thermostat's setpoint in small steps so the
thermostat, running on its own sensor and its own hysteresis, produces the target at the remote sensor,
and steps the nudge back down as soon as the remote room is warm enough.

This module is pure and stateless (no hass, no clock): the engine
(``AutomationEngine.remote_offset_check`` / ``_remote_offset_for_write``) owns the state and the
writes; every number here is an internal-Fahrenheit DELTA unless it says otherwise.

The rule (heat; cool mirrors, with "beyond the target in the gap direction" meaning lower):

* ``gap`` = how far the remote room is from needing LESS conditioning than the target. For heat
  ``target - remote``; for cool ``remote - target``. Positive = too cold (heat) / too warm (cool).
* ``gap > swing``  -> RAISE one step (after the startup wait / actuation feedback, never while the
  equipment is already running).
* ``gap < -swing`` -> STEP DOWN one step (never below zero; not delayed by the wait).
* otherwise HOLD.  At most one step per call.

``swing`` is the learned thermostat swing (``swing_heat_f`` / ``swing_cool_f``), read-only here.
Caps -- the tighter wins: ``cap`` beyond the target, and never past the OPPOSITE comfort-band edge.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, NamedTuple

from .const import (
    REMOTE_CONTEXT_SLEEP,
    REMOTE_OFFSET_CAP_C,
    REMOTE_OFFSET_CAP_F,
    REMOTE_OFFSET_DEFAULT_WAIT_S,
    REMOTE_OFFSET_MIN_WAIT_S,
    REMOTE_OFFSET_STEP_C,
    REMOTE_OFFSET_STEP_F,
    REMOTE_OFFSET_WAIT_MARGIN_S,
)
from .temperature import UNIT_SYMBOL, convert_delta, display_delta, display_temp, from_fahrenheit

_EPS = 1e-6
_F_PER_C = 9.0 / 5.0


class RemoteOffsetAction(StrEnum):
    RAISE = "raise"
    STEP_DOWN = "step_down"
    HOLD = "hold"


class RemoteOffsetDecision(NamedTuple):
    action: RemoteOffsetAction
    magnitude_f: float  # new offset magnitude beyond the target in the gap direction (>= 0, internal F delta)
    reason: str  # in_band | raised | stepped_down | nothing_to_lower | at_cap | awaiting_startup | equipment_running


class RemoteContext(NamedTuple):
    """The remote sensor currently supplying the comfort-facing indoor reading (from ``IndoorTempReading``)."""

    key: str  # "<remote_context>:<entity_id>" -- the offset belongs to exactly this context
    value_f: float  # the remote reading, internal F
    fresh: bool  # reported within REMOTE_OFFSET_FRESHNESS_MINUTES (last_reported)
    reported_at: datetime | None = None  # last_reported (else last_updated): "a new remote report" for the back-off


def step_f(unit: str) -> float:
    """One offset step as an internal-Fahrenheit delta: 2 F, or 1 C (= 1.8 F). Unknown unit -> Fahrenheit."""
    return REMOTE_OFFSET_STEP_C * _F_PER_C if unit == "celsius" else REMOTE_OFFSET_STEP_F


def cap_f(unit: str) -> float:
    """The +cap beyond the target as an internal-Fahrenheit delta: 4 F, or 2 C (= 3.6 F)."""
    return REMOTE_OFFSET_CAP_C * _F_PER_C if unit == "celsius" else REMOTE_OFFSET_CAP_F


def startup_wait_s(latency_s: float | None) -> float:
    """How long to wait after a raise for the thermostat to start (actuation feedback window).

    The thermostat's MEASURED startup latency plus a margin, never below the minimum and never above the
    bounded default; the default itself where nothing has been measured yet.
    """
    if latency_s is None:
        return REMOTE_OFFSET_DEFAULT_WAIT_S
    return min(REMOTE_OFFSET_DEFAULT_WAIT_S, max(REMOTE_OFFSET_MIN_WAIT_S, latency_s + REMOTE_OFFSET_WAIT_MARGIN_S))


def _room_f(mode: str, target_f: float, *, floor: float, ceiling: float) -> float:
    """How far the OPPOSITE comfort-band edge lies beyond ``target_f`` in the gap direction (may be negative)."""
    return (ceiling - target_f) if mode == "heat" else (target_f - floor)


def max_magnitude_f(mode: str, target_f: float, *, floor: float, ceiling: float, cap_f: float) -> float:
    """The largest offset allowed beyond ``target_f``: the tighter of ``cap_f`` and the OPPOSITE band edge.

    Heating never goes above the band's ceiling, cooling never below the band's floor. Never negative
    (a target already past the edge, e.g. TOU banking, gets no offset).
    """
    return max(0.0, min(cap_f, _room_f(mode, target_f, floor=floor, ceiling=ceiling)))


class ClampLimit(StrEnum):
    """Which of the two limits ``max_magnitude_f`` is applying (Issue #1194 d, Status "clamped")."""

    CAP = "cap"  # the +4 F / +2 C cap beyond the target
    BAND_EDGE = "band_edge"  # the opposite comfort-band edge (heat: ceiling, cool: floor)


def binding_limit(mode: str, target_f: float, *, floor: float, ceiling: float, cap_f: float) -> ClampLimit:
    """The limit that is the tighter one in ``max_magnitude_f`` (the same comparison, never a second rule)."""
    return (
        ClampLimit.BAND_EDGE if _room_f(mode, target_f, floor=floor, ceiling=ceiling) < cap_f - _EPS else ClampLimit.CAP
    )


def decide_remote_offset(
    *,
    mode: str,
    target_f: float,
    remote_f: float,
    swing_f: float,
    magnitude_f: float,
    step_f: float,
    max_magnitude_f: float,
    seconds_since_raise: float | None,
    wait_s: float,
    equipment_active: bool,
) -> RemoteOffsetDecision:
    """One check of the two-sided hysteresis at the remote sensor. See the module docstring."""
    m = min(max(magnitude_f, 0.0), max_magnitude_f)
    gap = (target_f - remote_f) if mode == "heat" else (remote_f - target_f)
    if gap < -swing_f:
        if m <= _EPS:
            return RemoteOffsetDecision(RemoteOffsetAction.HOLD, 0.0, "nothing_to_lower")
        lowered = max(m - step_f, 0.0)
        return RemoteOffsetDecision(RemoteOffsetAction.STEP_DOWN, 0.0 if lowered <= _EPS else lowered, "stepped_down")
    if gap > swing_f:
        if m >= max_magnitude_f - _EPS:
            return RemoteOffsetDecision(RemoteOffsetAction.HOLD, m, "at_cap")
        if equipment_active:
            return RemoteOffsetDecision(RemoteOffsetAction.HOLD, m, "equipment_running")
        if seconds_since_raise is not None and seconds_since_raise < wait_s:
            return RemoteOffsetDecision(RemoteOffsetAction.HOLD, m, "awaiting_startup")
        return RemoteOffsetDecision(RemoteOffsetAction.RAISE, min(m + step_f, max_magnitude_f), "raised")
    return RemoteOffsetDecision(RemoteOffsetAction.HOLD, m, "in_band")


class RemoteOffsetGate(StrEnum):
    """What the outer loop may do right now (Issue #1194 b); asked by the write path AND the check."""

    PROCEED = "proceed"  # nobody owns the thermostat: the loop steps, every writer applies the offset
    FREEZE = "freeze"  # a monitored door/window is open but still inside its debounce: hold the offset as is
    CLOSED = "closed"  # an owner (occupant, pause, fan, away setback, a window open past its debounce): offset 0


def decide_remote_offset_gate(*, owner_holds: bool, sensor_open: bool, debounce_pending: bool) -> RemoteOffsetGate:
    """The ONE gate rule of the outer loop (Issue #1194 b, owner decision D1 = FREEZE).

    ``owner_holds``: the occupant's protected change, or the shared scheduled-band gate (away / vacation, override,
    door/window pause, nat-vent or WHF ownership) says defer. ``sensor_open``: a monitored door/window reads open now
    (or went dark after reading open). ``debounce_pending``: a monitored sensor is still inside its open debounce.

    * An owner -> CLOSED (forget the offset; the owner commands the thermostat).
    * Open inside the debounce -> FREEZE: no raise, no step down, no reset and no loop write; any other writer
      re-applies the HELD offset. A blip changes nothing; an open that outlasts the debounce becomes the pause (rule 3)
      and the pause is an owner.
    * Open past the debounce with no pause (e.g. the occupant resumed into an open window) -> CLOSED: no extra degrees
      into a settled-open window.
    """
    if owner_holds:
        return RemoteOffsetGate.CLOSED
    if sensor_open:
        return RemoteOffsetGate.FREEZE if debounce_pending else RemoteOffsetGate.CLOSED
    return RemoteOffsetGate.PROCEED


# ---------------------------------------------------------------------------
# Status / chart / diagnostics: ONE description of the loop (Issue #1194 d)
# ---------------------------------------------------------------------------
# The engine owns the loop state and stores the last decision reason; the functions below only DESCRIBE it
# (state classification, display-unit conversion, the occupant-facing sentence). Nothing here re-decides: the
# Status card, the chart log, the AI context and the diagnostic snapshot all read the same ``RemoteOffsetState``
# the engine builds (``AutomationEngine.remote_offset_state``).


class RemoteLoopState(StrEnum):
    INACTIVE = "inactive"  # the primary sensor supplies the indoor reading: there is no remote loop to describe
    ACTIVE = "active"  # an offset is in force and the loop is working (raising, waiting for the start, easing off)
    HOLDING = "holding"  # remote room inside its band: the offset (if any) is held as it is
    FROZEN = "frozen"  # a door/window is open inside its debounce: the offset is held, nothing is written
    GATED = "gated"  # an owner (occupant, pause, fan, away ...) holds the thermostat: no offset
    STALE = "stale"  # the remote reading is too old: the plain setpoint is in force
    CLAMPED = "clamped"  # the loop wants more offset but a limit (``ClampLimit``) stops it
    # Issue #1194 (c)+(d): the two edges of a context, so Status never contradicts what the thermostat holds.
    WAITING = "waiting"  # the context began; the loop waits for the context's own band before its first decision
    CLEARING = "clearing"  # the context ended but the thermostat still holds target + offset (exit grace / back-off)


_ACTIVE_REASONS = frozenset({"raised", "stepped_down", "awaiting_startup", "equipment_running"})

# The states in which CA deliberately holds the offset on the thermostat (the commanded value is what it should read).
_OFFSET_INTENDED = frozenset(
    {RemoteLoopState.ACTIVE, RemoteLoopState.HOLDING, RemoteLoopState.CLAMPED, RemoteLoopState.FROZEN}
)


def classify_loop_state(
    *,
    in_context: bool,
    fresh: bool,
    gate: RemoteOffsetGate,
    last_reason: str | None,
    offset_f: float,
    awaiting_band: bool = False,
    outstanding: bool = False,
) -> RemoteLoopState:
    """Name the loop's state from facts the engine already holds (the gate, freshness, the last stored decision).

    ``awaiting_band``: the engine's ``_remote_awaiting_context_band`` (entry hold, #1194 c). ``outstanding``: out of
    any context, the thermostat still holds an offset CA wrote (the exit grace or a back-off has not cleared it yet).
    """
    if not in_context:
        if outstanding and gate is not RemoteOffsetGate.CLOSED:  # an owner's value is not "our offset clearing"
            return RemoteLoopState.CLEARING
        return RemoteLoopState.INACTIVE
    if not fresh:
        return RemoteLoopState.STALE
    if gate is RemoteOffsetGate.CLOSED:
        return RemoteLoopState.GATED
    if gate is RemoteOffsetGate.FREEZE:
        return RemoteLoopState.FROZEN
    if awaiting_band:
        return RemoteLoopState.WAITING
    if last_reason == "at_cap":
        return RemoteLoopState.CLAMPED
    if abs(offset_f) > _EPS and last_reason in _ACTIVE_REASONS:
        return RemoteLoopState.ACTIVE
    return RemoteLoopState.HOLDING


class RemoteOffsetState(NamedTuple):
    """The loop as the engine holds it right now (internal Fahrenheit; deltas are signed: heat +, cool -)."""

    state: RemoteLoopState
    context: str | None  # REMOTE_CONTEXT_* of the reading in use, None when the primary sensor is in use
    mode: str | None  # "heat" | "cool" of the last setpoint write
    comfort_target_f: float | None  # what the room is supposed to be (the comfort band's target)
    commanded_f: float | None  # what the thermostat was last told (target + offset)
    offset_f: float | None  # signed offset the thermostat holds; None = unknown (a gate cleared it, not re-sent yet)
    gate_reason: str | None  # why GATED/FROZEN (see ``GATE_REASON_TEXT``)
    clamp_limit: ClampLimit | None  # which limit, when CLAMPED
    last_reason: str | None  # the engine's last stored decision reason (``RemoteOffsetDecision.reason``)
    remote_value_f: float | None  # the remote sensor's reading
    remote_age_s: float | None  # seconds since that sensor last reported
    cap_limit_f: float  # the +cap beyond the target (so the text can name it)
    note: str | None = None  # WAITING: "context_band"; CLEARING: "exit_grace" | "backing_off" (Issue #1194 c)


# Why the loop is gated (keys ``AutomationEngine._remote_offset_gate_and_reason`` returns) -> short occupant text.
GATE_REASON_TEXT: dict[str, str] = {
    "occupant_change": "you changed the thermostat",
    "override": "manual override",
    "away": "away mode",
    "door_window_pause": "paused for an open window or door",
    "fan": "fan running",
    "window_open": "window or door open",
    "debounce": "window or door just opened",
}

_CONTEXT_LABELS: dict[str, str] = {REMOTE_CONTEXT_SLEEP: "Bedroom"}
# The band the context starts on (the WAITING text): the sleep window's is written by the bedtime routine.
_CONTEXT_BAND_TEXT: dict[str, str] = {REMOTE_CONTEXT_SLEEP: "the bedtime setting"}

_ACTIVE_NOTE: dict[str, str] = {
    "raised": "raising",
    "awaiting_startup": "waiting for the thermostat to start",
    "equipment_running": "heating/cooling running",
    "stepped_down": "easing off",
}


def context_label(context: str | None) -> str:
    return _CONTEXT_LABELS.get(context or "", "Remote sensor")


def _temp_text(value_f: float, unit: str) -> str:
    return f"{round(from_fahrenheit(value_f, unit))}{UNIT_SYMBOL.get(unit, '°F')}"


def _delta_text(delta_f: float, unit: str) -> str:
    shown = round(convert_delta(abs(delta_f), unit), 1)
    number = f"{shown:.0f}" if shown == int(shown) else f"{shown:.1f}"
    return f"{'+' if delta_f >= 0 else '-'}{number}{UNIT_SYMBOL.get(unit, '°F')}"


def remote_offset_text(st: RemoteOffsetState, unit: str) -> str | None:
    """The one occupant-facing sentence for the loop, or None when there is nothing worth saying.

    Example: "Bedroom 66°F target - thermostat set 70°F (+4°F, clamped at the +4°F cap)". The comfort target is the
    room's target; the thermostat's number is only what the loop commands to get there. It is never a comfort-band
    change and never the #402 "CA:" disagreement wording.
    """
    label = context_label(st.context)
    if st.state is RemoteLoopState.INACTIVE:
        return None
    if st.state is RemoteLoopState.STALE:
        # Not "thermostat at the plain target": an owner may hold it, or the plain write may not have gone through.
        return f"{label} sensor not reporting - offset not in use"
    if st.state is RemoteLoopState.WAITING:
        band = _CONTEXT_BAND_TEXT.get(st.context or "", "the room's own setting")
        return f"{label} offset starting: waiting for {band}"
    if st.state is RemoteLoopState.CLEARING:
        why = "clearing, retrying after a refused write" if st.note == "backing_off" else "clearing"
        if st.commanded_f is None or st.offset_f is None or abs(st.offset_f) <= _EPS:
            return f"{label} offset ending: {why}"
        return (
            f"{label} offset ending - thermostat still set {_temp_text(st.commanded_f, unit)}"
            f" ({_delta_text(st.offset_f, unit)}, {why})"
        )
    reason = GATE_REASON_TEXT.get(st.gate_reason or "", st.gate_reason or "")
    if st.state is RemoteLoopState.GATED:
        return f"{label} offset off: {reason}" if reason else f"{label} offset off"
    if st.comfort_target_f is None or st.commanded_f is None or st.offset_f is None:
        return f"{label} offset held: {reason}" if st.state is RemoteLoopState.FROZEN and reason else None
    if abs(st.offset_f) <= _EPS and st.state is not RemoteLoopState.CLAMPED:
        return None  # no offset in force and nothing blocked: the thermostat simply sits at the target
    head = f"{label} {_temp_text(st.comfort_target_f, unit)} target - thermostat set {_temp_text(st.commanded_f, unit)}"
    amount = _delta_text(st.offset_f, unit) if abs(st.offset_f) > _EPS else "no offset"
    if st.state is RemoteLoopState.FROZEN:
        note = f"held, {reason}" if reason else "held"
    elif st.state is RemoteLoopState.CLAMPED:
        if st.clamp_limit is ClampLimit.BAND_EDGE:
            edge = "ceiling" if st.mode == "heat" else "floor"
            note = f"clamped at the comfort band {edge}"
        else:
            note = f"clamped at the {_delta_text(st.cap_limit_f, unit)} cap"
    elif st.state is RemoteLoopState.ACTIVE:
        note = _ACTIVE_NOTE.get(st.last_reason or "", "adjusting")
    else:
        note = "holding"
    return f"{head} ({amount}, {note})"


def remote_offset_view(st: RemoteOffsetState, unit: str) -> dict[str, Any]:
    """``RemoteOffsetState`` in the user's display unit, plus the sentence: the payload Status/AI/diagnostics share."""
    offset = st.offset_f
    return {
        "state": st.state.value,
        "detail": (st.clamp_limit.value if st.clamp_limit is not None else st.gate_reason or st.note),
        "context": st.context,
        "context_label": context_label(st.context),
        "mode": st.mode,
        "comfort_target": display_temp(st.comfort_target_f, unit),
        "commanded": display_temp(st.commanded_f, unit),
        "offset_applied_f": None if offset is None else round(offset, 2),  # signed, internal degF
        "offset_applied": None if offset is None else display_delta(offset, unit, 2),  # signed, display unit
        "offset_active": offset is not None and abs(offset) > _EPS,
        # What the thermostat SHOULD read while an offset is held (the commanded value). The Status card compares the
        # thermostat with this instead of the band edge (#402), so a deliberate offset is never shown as a disagreement
        # but a thermostat that reports something else still is. None when no offset is held, and when CA no longer
        # intends one (gated / stale / inactive) even if the thermostat may still hold it: the band edge applies, so a
        # stranded offset shows as "(CA: X)" instead of being hidden.
        "expected_setpoint": (
            display_temp(st.commanded_f, unit)
            if st.state in _OFFSET_INTENDED and offset is not None and abs(offset) > _EPS
            else None
        ),
        "clamp_limit": st.clamp_limit.value if st.clamp_limit is not None else None,
        "gate_reason": st.gate_reason,
        "last_decision_reason": st.last_reason,
        "remote_reading": display_temp(st.remote_value_f, unit),
        "remote_reading_age_s": None if st.remote_age_s is None else round(st.remote_age_s),
        "unit": unit,
        "text": remote_offset_text(st, unit),
    }


def remote_offset_view_for(engine: Any, unit: str) -> dict[str, Any] | None:
    """The view for an engine, or None when it has no remote sensor / is not an engine (tolerant of stubs)."""
    getter = getattr(engine, "remote_offset_state", None)
    if not callable(getter):
        return None
    try:
        st = getter()
    except Exception:  # noqa: BLE001 - a status read must never break the endpoint
        return None
    return remote_offset_view(st, unit) if isinstance(st, RemoteOffsetState) else None


def remote_offset_lines(view: dict[str, Any]) -> list[str]:
    """The view as indented ``key: value`` lines (AI context and the diagnostic footer share this)."""
    keys = (
        "state",
        "detail",
        "context",
        "mode",
        "comfort_target",
        "commanded",
        "offset_applied",
        "offset_applied_f",
        "expected_setpoint",
        "clamp_limit",
        "gate_reason",
        "last_decision_reason",
        "remote_reading",
        "remote_reading_age_s",
        "unit",
        "text",
    )
    return [f"    {k}: {view.get(k)!r}" for k in keys]
