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
from typing import NamedTuple

from .const import (
    REMOTE_OFFSET_CAP_C,
    REMOTE_OFFSET_CAP_F,
    REMOTE_OFFSET_DEFAULT_WAIT_S,
    REMOTE_OFFSET_MIN_WAIT_S,
    REMOTE_OFFSET_STEP_C,
    REMOTE_OFFSET_STEP_F,
    REMOTE_OFFSET_WAIT_MARGIN_S,
)

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


def max_magnitude_f(mode: str, target_f: float, *, floor: float, ceiling: float, cap_f: float) -> float:
    """The largest offset allowed beyond ``target_f``: the tighter of ``cap_f`` and the OPPOSITE band edge.

    Heating never goes above the band's ceiling, cooling never below the band's floor. Never negative
    (a target already past the edge, e.g. TOU banking, gets no offset).
    """
    room = (ceiling - target_f) if mode == "heat" else (target_f - floor)
    return max(0.0, min(cap_f, room))


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
