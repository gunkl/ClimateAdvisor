"""Pure decision core for the "house is off-target and no equipment is running" stall alert.

Issue #1048 change 9 (tracking #1050).  Occupant outcome: a cold (or hot) house can go
unnoticed for days when the thermostat never starts the equipment -- a thermostat left Off,
one that ignores the mode, a dead heat source.  The existing comfort incident only records
it; nothing tells the owner.  This decision says when the house has been outside the active
comfort band by more than a margin, continuously for the sustain period, with **no equipment
active in the needed direction** (heat when below the band, cool when above) -- the one
signature all those failures share and which a thermostat that is merely slow never produces.

Stateless per call, like ``confirmed_transition.py`` whose ``resolve_candidate_since`` /
``is_confirmed`` it reuses for the clock.  The shell (``coordinator.py``
``_update_comfort_stall``) owns the clock state, the announcement/reminder bookkeeping and the
side effects.  An exclusion resets the clock (the sustain period restarts when the exclusion
ends) -- except a mode-not-obeyed episode, which the shell handles itself by suppressing the
announcement while keeping this clock running.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import NamedTuple

from .confirmed_transition import is_confirmed, resolve_candidate_since

DIRECTION_HEAT = "heat"
DIRECTION_COOL = "cool"

# hvac_modes that can deliver each direction.
_MODES_FOR_DIRECTION: dict[str, frozenset[str]] = {
    DIRECTION_HEAT: frozenset({"heat", "heat_cool", "auto"}),
    DIRECTION_COOL: frozenset({"cool", "heat_cool", "auto"}),
}


class StallReason(Enum):
    """Why the decision came out as it did (shell uses it for recovery vs. quiet)."""

    STALLED = "stalled"  # confirmed: announce / remind
    PENDING = "pending"  # outside the band, sustain period not yet elapsed (or equipment ran recently)
    IN_BAND = "in_band"  # within margin of the band -- nothing to time
    EXCLUDED = "excluded"  # a quiet-while condition holds -- clock reset


class StallDecision(NamedTuple):
    """Result: the standing candidate direction, its raw start, and the verdict."""

    direction: str | None  # "heat" / "cool" while a candidate exists, else None
    since: datetime | None  # raw start of the outside-band candidate (store as previous_since)
    effective_since: datetime | None  # max(since, last equipment run in that direction)
    reason: StallReason
    excluded_by: str | None  # which exclusion, when reason is EXCLUDED
    confirmed: bool


def _supports(direction: str, hvac_modes: Iterable[str] | None) -> bool:
    """True unless the thermostat positively lacks every mode for ``direction``.

    An empty / unknown mode list means "can't tell" and does not exclude -- an exclusion must
    be based on evidence, or a missing capability read would silently disable the alert.
    """
    if not hvac_modes:
        return True
    return bool(_MODES_FOR_DIRECTION[direction] & set(hvac_modes))


def stall_margins(native_family: str | None, deadband_f: float, margin_f: float) -> tuple[float, float]:
    """Per-direction margins ``(heat_margin_f, cool_margin_f)`` outside the band before a stall counts.

    Climate Advisor deliberately does NOT act against the day's grain until the house is past
    the day-type comfort deadband (``comfort_family_decision``: heat on a hot day, cool on a
    cold day; a mild day has no native family so both directions are against-grain).  An alert
    for a direction CA itself would not yet act on is a false alarm -- e.g. a warm house on a
    cold sunny day with the thermostat idle in Heat.  So the against-the-grain direction waits
    for ``deadband_f + margin_f`` (CA would have escalated and the equipment still isn't
    running); the with-the-grain direction keeps the flat ``margin_f``.

    ``native_family`` is ``comfort_family_decision._native_family(day_type)`` ("heating" /
    "cooling" / None) -- the one place that direction is defined.
    """
    heat = margin_f if native_family == "heating" else margin_f + deadband_f
    cool = margin_f if native_family == "cooling" else margin_f + deadband_f
    return heat, cool


def decide_comfort_stall(
    *,
    indoor: float | None,
    comfort_heat: float | None,
    comfort_cool: float | None,
    now: datetime,
    heat_margin_f: float,
    cool_margin_f: float,
    sustain_seconds: float,
    previous_direction: str | None,
    previous_since: datetime | None,
    last_active: dict[str, datetime | None],
    thermostat_available: bool,
    has_hvac_action: bool,
    hvac_modes: Iterable[str] | None,
    manual_control: bool,
    sensor_open: bool,
    startup_coalescing: bool,
    nat_vent_tolerated: bool,
    thermostat_swing_tolerated: bool = False,
) -> StallDecision:
    """Decide whether the house is stalled outside the active comfort band.

    ``indoor``/``comfort_*``/``*_margin_f`` share one unit (internal Fahrenheit); see ``stall_margins``.
    ``last_active`` maps direction -> the last time equipment (``hvac_action`` heating /
    cooling) was seen in that direction.  Exclusions (all reset the clock):
    thermostat or indoor unavailable, thermostat without ``hvac_action`` (can't prove
    equipment is idle), manual control, an open door/window, startup coalescing, the
    needed mode not offered by the thermostat, an in-tolerance nat-vent deviation, and a deviation
    only inside the thermostat's own learned swing in the matching mode (Issue #1258,
    ``excluded_by="thermostat_swing"``; nat-vent is reported first when both hold).
    """
    excluded_by: str | None = None
    if indoor is None or comfort_heat is None or comfort_cool is None:
        excluded_by = "indoor_unavailable"
    elif not thermostat_available:
        excluded_by = "thermostat_unavailable"
    elif startup_coalescing:
        excluded_by = "startup_coalescing"
    elif not has_hvac_action:
        excluded_by = "no_hvac_action"
    elif manual_control:
        excluded_by = "manual_control"
    elif sensor_open:
        excluded_by = "sensor_open"
    elif nat_vent_tolerated:
        excluded_by = "nat_vent_tolerated"
    elif thermostat_swing_tolerated:
        excluded_by = "thermostat_swing"

    direction: str | None = None
    if excluded_by is None:
        assert indoor is not None and comfort_heat is not None and comfort_cool is not None  # for type checkers
        if indoor < comfort_heat - heat_margin_f:
            direction = DIRECTION_HEAT
        elif indoor > comfort_cool + cool_margin_f:
            direction = DIRECTION_COOL
        if direction is not None and not _supports(direction, hvac_modes):
            excluded_by = "mode_unsupported"
            direction = None

    if excluded_by is not None:
        return StallDecision(None, None, None, StallReason.EXCLUDED, excluded_by, False)
    if direction is None:
        return StallDecision(None, None, None, StallReason.IN_BAND, None, False)

    since = resolve_candidate_since(
        candidate=direction, previous_candidate=previous_direction, previous_since=previous_since, now=now
    )
    # Equipment that ran in the needed direction after the house left the band restarts the
    # count: the thermostat is doing its job (even if undersized) -- that is a different problem.
    ran = last_active.get(direction)
    effective = since
    if since is not None and ran is not None and ran > since:
        effective = ran
    confirmed = is_confirmed(candidate=direction, candidate_since=effective, now=now, sustain_seconds=sustain_seconds)
    return StallDecision(
        direction,
        since,
        effective,
        StallReason.STALLED if confirmed else StallReason.PENDING,
        None,
        confirmed,
    )
