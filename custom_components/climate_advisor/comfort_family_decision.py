"""Comfort-family decision leaf -- nearest-edge arming (Issue #1066) plus the settling guard (Issue #1073).

A single-setpoint thermostat can only guard ONE edge of the comfort band at a
time: the floor (heat mode) or the ceiling (cool mode). This pure leaf decides
which one, every cycle, from the indoor temperature and a few pre-computed facts
(the shell reads the clocks; the leaf never touches Home Assistant, the thermal
model or any timer). Supersedes the #821/#827/#843/#858/#867 against-grain
machinery, which left the thermostat sitting in COOL below the comfort floor.

Rule (symmetric; all temperatures internal Fahrenheit). ``m`` is the fixed arming
margin (``effective_margin``: ``COMFORT_MIN_ARMING_MARGIN_F`` clamped to a third of
the band; it is NOT a user setting):

- ``indoor < floor``   -> heating (breach);
- ``indoor > ceiling`` -> cooling (breach);
- otherwise ``indoor <= floor + m`` -> heating, ``indoor >= ceiling - m`` -> cooling,
  strictly between -> keep the current family (hysteresis memory), or
  ``default_family`` when the current family is unknown.

Settling guard (Issue #1073, ``docs/comfort-edge-selection-spec.md`` R1/R2): an
appliance overshoots (a compressor stops about half a degree past its setpoint, a
thermostat reports whole degrees), so a reading at or past an edge right after the
OPPOSITE family ran is usually that overshoot, not a real need. After a cool bank the
house typically reads 68-70 first (INSIDE the arming margin, not yet a breach), so the
guard covers the margin zone as well as the breach zone. A SWITCH away from "the family being left" is
HELD (outcome HOLD, target = that family, ``held=True``) when ALL of:

  (a) the switch is against the day's grain -- the target is not ``native_family``
      (the native side has tolerance 0 and is never delayed; a day type with no
      native side makes both sides against-grain);
  (b) the family being left ran or was DELIBERATELY commanded (user / TOU bank / ODE guard;
      not a selector or restore write) within ``recency_window_min``
      (``opposite_ran_min_ago``, computed by the shell; None/unknown = not recent);
  (c) the house has not gone more than ``tolerance_f`` PAST the target edge. The signed
      distance past the edge is floor - indoor (heat) or indoor - ceiling (cool): positive
      in the breach zone, zero or negative inside the band (the margin zone). The hold
      decision exposes ``hold_zone`` ("breach" or "margin") and a non-negative
      ``hold_depth_f`` (0.0 inside the band).

Going deeper than the tolerance past the edge ALWAYS switches at once (R1). The
hysteresis-gap ("keep current family") path is unchanged and a hold never moves the dwell clock.

"The family being left" is the thermostat's live family (``current_family``). When the live family is
UNKNOWN (Off / heat_cool / auto / unavailable -- the occupant switched it Off, or it just came back
online) the shell names the family that ran or was deliberately commanded most recently
(``settling_from_family``) and the guard treats THAT as the family being left, so the same hold applies
(Issue #1149: otherwise a thermostat switched Off right after the AC ran was heated for a shallow dip).
With neither a live family nor a recent run there is nothing to hold and the selector adopts normally.
When the window lapses a margin switch proceeds normally (the 600 s dwell lock still applies to non-breach switches).

Capabilities: heat-only thermostat -> heating; cool-only -> cooling; neither ->
NOT_APPLICABLE. Nat-vent or whole-house-fan owning the HVAC, or missing
indoor/floor/ceiling -> NOT_APPLICABLE. Manual override, door/window pause and
occupancy are NOT inputs: callers run before this leaf and skip it.

TOU banking (Issue #1066 F1): an ACTIVE time-of-use precondition (the scheduler's
``PRECONDITIONING`` phase) makes the leaf NOT_APPLICABLE, exactly like nat-vent/WHF;
once the phase ends the selector resumes and the settling guard holds the shallow
bank overshoot (Issue #1071 is closed by the guard, not by a TOU special case).

The leaf is stateless and has no Home Assistant imports. It never enforces a
dwell lock -- ``comfort_family_fsm.py`` owns the anti-chatter timer and lets any
breach decision through regardless of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .const import DAY_TYPE_COLD, DAY_TYPE_COOL, DAY_TYPE_HOT, DAY_TYPE_WARM

_FAMILY_HEATING = "heating"
_FAMILY_COOLING = "cooling"

# The arming margin is never smaller than this, even if the configured margin
# is: sensor noise / 1F thermostat resolution would otherwise flicker the arm.
COMFORT_MIN_ARMING_MARGIN_F = 2.0
# If the clamped margin (capped at one third of the band width) falls below
# this, the band is too narrow to flip safely in its interior: only breaches
# switch the family.
COMFORT_MIN_USABLE_MARGIN_F = 1.0


class ComfortFamilyOutcome(Enum):
    NOT_APPLICABLE = "not_applicable"
    HOLD = "hold"
    SWITCH = "switch"


@dataclass(frozen=True)
class ComfortFamilyDecision:
    outcome: ComfortFamilyOutcome
    target_family: str | None = None
    reason: str = ""
    # indoor is outside [floor, ceiling]
    breach: bool = False
    # The effective margin used; None when not computed or band too narrow.
    margin_applied_f: float | None = None
    heat_zone_limit_f: float | None = None  # floor + m
    cool_zone_limit_f: float | None = None  # ceiling - m
    # Issue #1073 settling guard: True when a breach SWITCH was held. ``target_family`` is then the
    # current family; the fields below describe why (for the shell's log / activity event).
    held: bool = False
    hold_candidate_family: str | None = None  # the family the breach wanted
    hold_depth_f: float | None = None  # non-negative; 0.0 inside the band
    hold_zone: str | None = None  # "breach" (past the edge) or "margin" (inside the band)
    hold_tolerance_f: float | None = None
    opposite_ran_min_ago: float | None = None
    hold_window_min: float | None = None


@dataclass(frozen=True)
class ComfortFamilyInputs:
    # Family the thermostat is currently in; None = unknown/off/heat_cool/unavailable.
    current_family: str | None
    # Tie-break inside the hysteresis gap when current_family is None.
    default_family: str
    indoor: float | None
    floor: float | None
    ceiling: float | None
    margin_f: float
    natural_vent_active: bool
    whf_owns_hvac: bool
    now: datetime
    supports_heat: bool = True
    supports_cool: bool = True
    # A TOU precondition (banking toward a high-cost window) owns the thermostat.
    tou_precondition_active: bool = False
    # Issue #1073 settling guard inputs (all pre-computed by the shell; the leaf reads no clock).
    # tolerance_f: the day type's comfort_deadband_*_f. Note: only a non-positive ``recency_window_min`` (or
    # an unknown ran time) disables the guard; tolerance 0.0 still HOLDS the margin zone (distance <= 0) and
    # only releases once the house is any distance past the edge.
    tolerance_f: float = 0.0
    # The day's native family ("cooling" hot/warm, "heating" cool/cold, None mild/unknown).
    native_family: str | None = None
    # Minutes since the family being LEFT ran or was commanded; None = unknown. The family being left is
    # ``current_family`` when known, else ``settling_from_family``.
    opposite_ran_min_ago: float | None = None
    # comfort_family_recency_window_min; 0.0 (default) disables the guard.
    recency_window_min: float = 0.0
    # Issue #1149: stand-in for "the family being left" while ``current_family`` is None (thermostat Off,
    # heat_cool, auto or unavailable): the family that ran / was deliberately commanded most recently
    # inside the window, chosen by the shell. Ignored when ``current_family`` is known. None = nothing ran.
    settling_from_family: str | None = None


def _native_family(day_type: str | None) -> str | None:
    """The day's native comfort family, or ``None`` for a day type with no strong preference.

    Not part of nearest-edge arming. Used by the settling guard (Issue #1073: the native side has
    tolerance 0) and by the comfort-stall detector (``comfort_stall_decision.stall_margins``), which asks
    which direction a day type favors when it scales its per-direction stall margins.
    """
    if day_type in (DAY_TYPE_HOT, DAY_TYPE_WARM):
        return _FAMILY_COOLING
    if day_type in (DAY_TYPE_COOL, DAY_TYPE_COLD):
        return _FAMILY_HEATING
    return None


def effective_margin(margin_f: float, floor: float, ceiling: float) -> float | None:
    """Clamp the configured margin to ``[COMFORT_MIN_ARMING_MARGIN_F, width/3]``.

    Returns None when the result is below ``COMFORT_MIN_USABLE_MARGIN_F`` (band
    too narrow for interior flipping).
    """
    width = ceiling - floor
    m = min(max(margin_f, COMFORT_MIN_ARMING_MARGIN_F), width / 3.0)
    if m < COMFORT_MIN_USABLE_MARGIN_F:
        return None
    return m


def _settling_hold(
    inputs: ComfortFamilyInputs, target: str, past_edge: float, margin: float | None
) -> ComfortFamilyDecision | None:
    """Issue #1073: the settling guard. Returns a HOLD decision, or None when the switch proceeds.

    ``past_edge`` is the SIGNED distance past the target edge: positive when the house is beyond
    it (breach zone), zero or negative when still inside the band (margin zone).
    """
    # The live family when known; else (Issue #1149) the family that most recently ran, as named by the shell.
    current = inputs.current_family if inputs.current_family is not None else inputs.settling_from_family
    if current is None or target == current:
        return None  # nothing ran and the family is unknown (plain adoption), or no switch: nothing to hold
    if inputs.native_family is not None and target == inputs.native_family:
        return None  # the day's native side is never delayed (tolerance 0)
    ran = inputs.opposite_ran_min_ago
    if ran is None or ran < 0.0 or inputs.recency_window_min <= 0.0 or ran >= inputs.recency_window_min:
        return None  # the family being left did not run recently
    if past_edge > inputs.tolerance_f:
        return None  # R1: deeper than the tolerance past the edge always switches at once
    depth = max(0.0, past_edge)
    zone = "breach" if past_edge > 0.0 else "margin"
    return ComfortFamilyDecision(
        outcome=ComfortFamilyOutcome.HOLD,
        target_family=current,
        reason=(
            f"settling guard: {current} ran {ran:.0f} min ago (window {inputs.recency_window_min:.0f} min),"
            f" {zone} zone depth {depth:.1f}F <= tolerance {inputs.tolerance_f:.1f}F -- holding {current}"
        ),
        breach=past_edge > 0.0,
        margin_applied_f=margin,
        held=True,
        hold_candidate_family=target,
        hold_depth_f=depth,
        hold_zone=zone,
        hold_tolerance_f=inputs.tolerance_f,
        opposite_ran_min_ago=ran,
        hold_window_min=inputs.recency_window_min,
    )


def _decision(
    inputs: ComfortFamilyInputs,
    target: str,
    reason: str,
    *,
    breach: bool = False,
    margin: float | None = None,
    heat_limit: float | None = None,
    cool_limit: float | None = None,
) -> ComfortFamilyDecision:
    switch = inputs.current_family is not None and target != inputs.current_family
    return ComfortFamilyDecision(
        outcome=ComfortFamilyOutcome.SWITCH if switch else ComfortFamilyOutcome.HOLD,
        target_family=target,
        reason=reason,
        breach=breach,
        margin_applied_f=margin,
        heat_zone_limit_f=heat_limit,
        cool_zone_limit_f=cool_limit,
    )


def decide_comfort_family(inputs: ComfortFamilyInputs) -> ComfortFamilyDecision:
    """Return which comfort edge the thermostat should guard this cycle."""
    if inputs.natural_vent_active or inputs.whf_owns_hvac:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="nat-vent/WHF owns HVAC — comfort family defers",
        )
    if inputs.tou_precondition_active:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="TOU precondition owns HVAC — comfort family defers",
        )
    if inputs.indoor is None or inputs.floor is None or inputs.ceiling is None:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="missing indoor temperature or comfort band",
        )

    indoor, floor, ceiling = inputs.indoor, inputs.floor, inputs.ceiling
    breach = indoor < floor or indoor > ceiling

    if not inputs.supports_heat and not inputs.supports_cool:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="thermostat supports neither heat nor cool",
            breach=breach,
        )
    if inputs.supports_heat and not inputs.supports_cool:
        return _decision(inputs, _FAMILY_HEATING, "heat-only thermostat", breach=breach)
    if inputs.supports_cool and not inputs.supports_heat:
        return _decision(inputs, _FAMILY_COOLING, "cool-only thermostat", breach=breach)

    m = effective_margin(inputs.margin_f, floor, ceiling)
    heat_limit = floor + m if m is not None else None
    cool_limit = ceiling - m if m is not None else None

    if indoor < floor:
        held = _settling_hold(inputs, _FAMILY_HEATING, floor - indoor, m)
        if held is not None:
            return held
        return _decision(
            inputs,
            _FAMILY_HEATING,
            "floor breach — arming heat immediately",
            breach=True,
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )
    if indoor > ceiling:
        held = _settling_hold(inputs, _FAMILY_COOLING, indoor - ceiling, m)
        if held is not None:
            return held
        return _decision(
            inputs,
            _FAMILY_COOLING,
            "ceiling breach — arming cool immediately",
            breach=True,
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )

    if heat_limit is None or cool_limit is None:
        target = inputs.current_family or inputs.default_family
        return _decision(inputs, target, "band too narrow for interior switching", margin=m)

    if indoor <= heat_limit:
        held = _settling_hold(inputs, _FAMILY_HEATING, floor - indoor, m)
        if held is not None:
            return held
        return _decision(
            inputs,
            _FAMILY_HEATING,
            "within heat arming margin",
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )
    if indoor >= cool_limit:
        held = _settling_hold(inputs, _FAMILY_COOLING, indoor - ceiling, m)
        if held is not None:
            return held
        return _decision(
            inputs,
            _FAMILY_COOLING,
            "within cool arming margin",
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )

    target = inputs.current_family or inputs.default_family
    return _decision(
        inputs,
        target,
        "in hysteresis gap — keeping current family",
        margin=m,
        heat_limit=heat_limit,
        cool_limit=cool_limit,
    )
