"""Pure recognition of Climate Advisor's own late thermostat setpoint echoes (Issue #1232).

Occupant story: Climate Advisor writes a setpoint; a cloud thermostat reports it back 30-120 s later.
Read as the occupant touching the dial, that echo would confirm a false manual override and pause
automation for the manual grace period (1 h by default). The planned remote-sensor outer loop (#1194)
writes stepped setpoint nudges overnight, so the person asleep would wake to a house that stopped
following its plan. A REAL occupant change must still be caught.

This module is the ONE recognition rule (no per-site copies). The engine owns the bounded memory of
what it wrote (``AutomationEngine._own_setpoint_writes``) and the coordinator asks it, through
``decide_setpoint_echo``, whether a thermostat setpoint change is one of those writes coming back.

Rules (owner decisions 2026-11-02):

* A change is CA's own echo when, per changed attribute, a remembered write inside the window either
  carries the event's HA Context (``context_id_matches`` -- the same predicate the #482 fan-command
  check uses) or equals the observed value after rounding to the thermostat's step.
* Once an echo is recognised, older remembered writes of the same entity and recorded attribute (sent before
  the newest write it answers) are superseded and forgotten (owner decision, D3): an occupant dialling back to
  one of those values is the occupant; an older echo arriving out of order is then judged as the occupant too.
* A different value, or one outside the window, is NOT an echo: it is evaluated as a possible occupant
  change exactly as before.
* Every changed attribute must be accounted for. A heat_cool event that moves ``target_temp_low`` as
  echo and ``target_temp_high`` as a real change is an occupant change.
* CA writes a single ``temperature`` (with ``hvac_mode``). A thermostat in a band mode may report that
  write on ``target_temp_low`` / ``target_temp_high``, so a ``temperature`` write can account for a band
  attribute. A write recorded on a band attribute (a future dual-setpoint writer) only accounts for that
  same attribute.
* An occupant who sets the SAME value Climate Advisor wrote, inside the window and without a Context
  that tells them apart, is indistinguishable by value and is treated as the echo. Accepted: the
  occupant asked for exactly the temperature the plan already holds, so there is nothing to override.
  When HA does provide a Context that separates them -- a change made by a signed-in person carries a
  ``user_id``, which Climate Advisor's and a cloud poll's never do -- the value fallback is not used and
  the change is the occupant's.

Pure: no Home Assistant imports, no I/O, no clock reads (``now`` is passed in).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .const import (
    SETPOINT_ECHO_DEFAULT_STEP_C,
    SETPOINT_ECHO_DEFAULT_STEP_F,
    SETPOINT_ECHO_MEMORY_MAX,
    SETPOINT_ECHO_WINDOW_S,
)

#: The attributes a setpoint write can be recorded on.
SETPOINT_ATTRIBUTES = ("temperature", "target_temp_low", "target_temp_high")
_BAND_ATTRIBUTES = ("target_temp_low", "target_temp_high")


@dataclass(frozen=True)
class OwnSetpointWrite:
    """One setpoint write Climate Advisor sent (value in the thermostat's own unit)."""

    entity_id: str
    attribute: str  # one of SETPOINT_ATTRIBUTES
    value: float
    written_at: datetime
    context_id: str | None = None


@dataclass(frozen=True)
class EchoVerdict:
    """Outcome of ``decide_setpoint_echo``."""

    recognised: bool
    reason: str  # "context" | "value" | "no_change" | "no_memory" | "no_match"
    consumed: tuple[OwnSetpointWrite, ...] = ()
    #: Older remembered writes (same entity, same recorded attribute) sent BEFORE the newest write this echo
    #: answers: the thermostat has moved past them, so the caller forgets them too (owner decision, #1232 D3).
    superseded: tuple[OwnSetpointWrite, ...] = ()


def context_id_matches(
    recorded_id: str | None, event_context_id: str | None, event_context_parent_id: str | None
) -> bool:
    """True when a recorded CA command Context id is the event's context id or its parent's (Issues #482, #561).

    The ONE context predicate: ``AutomationEngine.command_context_matches`` (fan and climate kinds) and
    ``decide_setpoint_echo`` both use it.
    """
    if recorded_id is None:
        return False
    return recorded_id in (event_context_id, event_context_parent_id)


def default_step(unit: str | None) -> float:
    """Rounding step assumed for a thermostat that does not advertise ``target_temp_step``."""
    return SETPOINT_ECHO_DEFAULT_STEP_C if unit == "celsius" else SETPOINT_ECHO_DEFAULT_STEP_F


def resolve_step(raw_step: object, unit: str | None) -> float:
    """The thermostat's advertised ``target_temp_step`` when it is a usable positive number, else the default."""
    try:
        step = float(raw_step)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default_step(unit)
    if not math.isfinite(step) or step <= 0:
        return default_step(unit)
    return step


def values_match(recorded: float, observed: float, step: float) -> bool:
    """Does the thermostat's reported value equal what CA wrote, after rounding to the thermostat's step?

    A value already on the thermostat's step grid must come back exactly (so an occupant's one-step tweak
    from CA's value is never mistaken for the echo). An off-grid value (72 F converted to 22.22 C on a
    0.5 C thermostat) is rounded by the thermostat -- up, down or to nearest depending on the make -- so
    either neighbouring grid value, i.e. anything strictly within one step, is the echo.
    """
    if not (math.isfinite(recorded) and math.isfinite(observed)) or step <= 0:
        return False
    tolerance = max(1e-6, step * 0.02)
    snapped = round(recorded / step) * step
    if abs(recorded - snapped) <= tolerance:
        return abs(observed - recorded) <= tolerance
    return abs(observed - recorded) < step - tolerance


def prune_own_writes(
    writes: tuple[OwnSetpointWrite, ...] | list[OwnSetpointWrite],
    now: datetime,
    *,
    window_s: float = SETPOINT_ECHO_WINDOW_S,
    cap: int | None = SETPOINT_ECHO_MEMORY_MAX,
) -> tuple[OwnSetpointWrite, ...]:
    """Drop writes whose window has ended, and keep only the newest ``cap``. Order is preserved."""
    live = tuple(w for w in writes if (now - w.written_at).total_seconds() < window_s)
    return live if cap is None else live[-cap:]


def _applies(write: OwnSetpointWrite, attribute: str) -> bool:
    if write.attribute == attribute:
        return True
    return write.attribute == "temperature" and attribute in _BAND_ATTRIBUTES


def decide_setpoint_echo(
    *,
    writes: tuple[OwnSetpointWrite, ...] | list[OwnSetpointWrite],
    entity_id: str,
    observed: Mapping[str, object],
    now: datetime,
    step: float,
    event_context_id: str | None = None,
    event_context_parent_id: str | None = None,
    event_context_user_id: str | None = None,
    window_s: float = SETPOINT_ECHO_WINDOW_S,
) -> EchoVerdict:
    """Is this thermostat setpoint change Climate Advisor's own write coming back?

    ``observed`` maps each CHANGED setpoint attribute to its new value (thermostat unit). Recognised only
    when every changed attribute is accounted for by a remembered write inside ``window_s`` (by Context,
    else by value after rounding to ``step``). ``consumed`` lists the writes this echo answers: all
    context-matching writes plus all same-value writes for the recognised attributes (one physical echo
    answers duplicate writes of the same value); the caller removes them from its memory.

    ``superseded`` (owner decision 2026-11-02, #1232 D3): once an echo is recognised, every remembered write
    sent before the newest write it answers, for the same entity and the same recorded attribute, is dropped
    too. Without it an occupant who dials back to a value CA wrote earlier (superseded by a newer CA write,
    whose echo was the only one the thermostat reported) would be swallowed as CA's echo. Accepted cost: an
    echo arriving out of order (the older value reported after the newer one) is then judged as the occupant.
    Order is the memory's order (send order), not ``written_at``, which the engine restamps on call return.
    """
    if not observed:
        return EchoVerdict(False, "no_change")
    live = [w for w in prune_own_writes(writes, now, window_s=window_s, cap=None) if w.entity_id == entity_id]
    if not live:
        return EchoVerdict(False, "no_memory")
    consumed: list[OwnSetpointWrite] = []
    used_context = False
    for attribute, raw in observed.items():
        try:
            value = float(raw)  # type: ignore[arg-type]
        except TypeError, ValueError:
            return EchoVerdict(False, "no_match")
        accounted = False
        for write in live:
            if not _applies(write, attribute):
                continue
            if context_id_matches(write.context_id, event_context_id, event_context_parent_id):
                accounted = used_context = True
                consumed.append(write)
            elif event_context_user_id is None and values_match(write.value, value, step):
                accounted = True
                consumed.append(write)
        if not accounted:
            return EchoVerdict(False, "no_match")
    unique = tuple(dict.fromkeys(consumed))
    return EchoVerdict(True, "context" if used_context else "value", unique, _superseded_by(live, unique))


def _superseded_by(
    live: list[OwnSetpointWrite], consumed: tuple[OwnSetpointWrite, ...]
) -> tuple[OwnSetpointWrite, ...]:
    """Writes in ``live`` sent before the newest consumed write of their own recorded attribute (per attribute)."""
    newest: dict[str, int] = {}
    for index, write in enumerate(live):
        if write in consumed:
            newest[write.attribute] = index
    return tuple(
        w for index, w in enumerate(live) if w not in consumed and w.attribute in newest and index < newest[w.attribute]
    )
