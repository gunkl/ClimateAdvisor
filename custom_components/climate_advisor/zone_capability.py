"""What this zone can do right now: heat, cool, both or neither (Issue #1699, phase 1).

Occupant view: a seasonal home with both heat and cool can say "no cooling while it is cold outside" (the
outdoor lockouts) or "heat only this season" (the season setting), and Climate Advisor then behaves exactly as it
does for a heat-only thermostat: it never arms cooling on a near-freezing night because the house sits in the
cooling margin of the sleep ceiling (#1560). A heat-only or cool-only home (#1544) gets the same answer from the same
place, so "heat-only" means one thing whatever the reason.

One resolver answers, for heat and for cool: available or not, why not (``equipment`` = the thermostat does not
advertise the mode; ``outdoor_lockout`` = outdoor is past the user's threshold; ``season`` = the user's manual Heat
only / Cool only), and since when. Equipment is what is possible; the policies only ever narrow it, never widen it.

Safety release (owner decision 2026-11-22): a POLICY lock (outdoor lockout or season) steps aside while the house is
deeper past that side's edge than the day's settling tolerance (``comfort_deadband_*_f``; the comfort-edge R1 rule,
depth strictly greater than the tolerance), and locks again once the depth is back at or inside it. Missing equipment
is never released (there is nothing to run). With an unknown indoor reading or band a policy lock stays locked.

Pure: no Home Assistant, no clock, no logging. The engine (``automation.py``) owns the in-memory lockout state and
feeds this module; spec ``docs/zone-capability-spec.md``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

FAMILY_HEAT = "heat"
FAMILY_COOL = "cool"
FAMILIES: tuple[str, str] = (FAMILY_HEAT, FAMILY_COOL)

REASON_EQUIPMENT = "equipment"
REASON_OUTDOOR_LOCKOUT = "outdoor_lockout"
REASON_SEASON = "season"

SEASON_AUTO = "auto"
SEASON_HEAT_ONLY = "heat_only"
SEASON_COOL_ONLY = "cool_only"
SEASONS: tuple[str, ...] = (SEASON_AUTO, SEASON_HEAT_ONLY, SEASON_COOL_ONLY)

# 2 °C. A cool lockout engages below X and releases at or above X + this; the heat lockout is the mirror.
LOCKOUT_RELEASE_HYSTERESIS_F = 3.6

# Modes that let the thermostat run BOTH sides on its own (a dual-setpoint band).
BAND_MODES: frozenset[str] = frozenset({"heat_cool", "auto"})


def step_cool_lockout(engaged: bool, outdoor_f: float | None, threshold_f: float | None) -> bool:
    """Next state of the cool lockout: no cooling while outdoor is below ``threshold_f``.

    Engages when ``outdoor_f < threshold_f``; once engaged it releases at ``outdoor_f >= threshold_f + 3.6`` (2 °C).
    A missing reading keeps the previous state; no threshold set means never engaged.
    """
    if threshold_f is None:
        return False
    if outdoor_f is None:
        return engaged
    if engaged:
        return outdoor_f < threshold_f + LOCKOUT_RELEASE_HYSTERESIS_F
    return outdoor_f < threshold_f


def step_heat_lockout(engaged: bool, outdoor_f: float | None, threshold_f: float | None) -> bool:
    """Next state of the heat lockout (mirror of :func:`step_cool_lockout`): no heating while outdoor is above it.

    Engages when ``outdoor_f > threshold_f``; once engaged it releases at ``outdoor_f <= threshold_f - 3.6``.
    """
    if threshold_f is None:
        return False
    if outdoor_f is None:
        return engaged
    if engaged:
        return outdoor_f > threshold_f - LOCKOUT_RELEASE_HYSTERESIS_F
    return outdoor_f > threshold_f


def lockout_thresholds_overlap(cool_lockout_f: float | None, heat_lockout_f: float | None) -> bool:
    """True when both lockouts are set and the heat lockout is at or below the cool lockout (rejected in the form).

    With heat locked out above H and cool locked out below C, H <= C leaves outdoor temperatures at which the zone
    could neither heat nor cool.
    """
    if cool_lockout_f is None or heat_lockout_f is None:
        return False
    return heat_lockout_f <= cool_lockout_f


def normalize_season(value: object) -> str:
    """The stored ``hvac_season`` option as one of ``SEASONS`` (anything else is Auto)."""
    return value if isinstance(value, str) and value in SEASONS else SEASON_AUTO


def season_locks(season: str, family: str) -> bool:
    """True when the season setting takes ``family`` away (Heat only removes cool, Cool only removes heat)."""
    if season == SEASON_HEAT_ONLY:
        return family == FAMILY_COOL
    if season == SEASON_COOL_ONLY:
        return family == FAMILY_HEAT
    return False


def depth_past_edge(
    family: str, indoor_f: float | None, floor_f: float | None, ceiling_f: float | None
) -> float | None:
    """Signed degrees past ``family``'s edge: ``floor - indoor`` for heat, ``indoor - ceiling`` for cool.

    Positive past the edge, zero or negative inside the band; None when a reading or the band is unknown.
    """
    if indoor_f is None:
        return None
    if family == FAMILY_HEAT:
        return None if floor_f is None else floor_f - indoor_f
    return None if ceiling_f is None else indoor_f - ceiling_f


@dataclass(frozen=True)
class FamilyAvailability:
    """Can the zone run one family (heat or cool) right now, and if not, why.

    ``reason`` is the first thing that removes it (``equipment`` > ``season`` > ``outdoor_lockout``), also while a
    policy lock is stepping aside (``safety_released``). ``policy_locked`` is True while a season / outdoor lock is
    engaged, whether or not the safety release lets the family run. ``since`` is when the policy state last changed
    (None when it never has in this run).
    """

    family: str
    available: bool
    reason: str | None = None
    policy_locked: bool = False
    safety_released: bool = False
    depth_f: float | None = None
    since: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        """Diagnostics / status shape (``since`` as ISO text)."""
        return {
            "available": self.available,
            "reason": self.reason,
            "policy_locked": self.policy_locked,
            "safety_released": self.safety_released,
            "depth_f": None if self.depth_f is None else round(self.depth_f, 2),
            "since": self.since.isoformat() if self.since is not None else None,
        }


@dataclass(frozen=True)
class ZoneCapability:
    """The resolver's answer for both families."""

    heat: FamilyAvailability
    cool: FamilyAvailability

    def family(self, family: str) -> FamilyAvailability:
        return self.heat if family == FAMILY_HEAT else self.cool

    def mode_allowed(self, mode: str | None) -> bool:
        """May Climate Advisor command ``mode`` now? heat/cool need their family; a band mode needs both."""
        if mode == FAMILY_HEAT:
            return self.heat.available
        if mode == FAMILY_COOL:
            return self.cool.available
        if mode in BAND_MODES:
            return self.heat.available and self.cool.available
        return True

    def bank_allowed(self, family: str) -> bool:
        """May an optional bank (TOU pre-conditioning, overnight pre-cool) run ``family`` now?

        Stricter than :meth:`mode_allowed`: a bank is savings, not comfort defence, so it stays off while a policy
        lock is engaged even when the safety release lets the selector defend the edge.
        """
        fam = self.family(family)
        return fam.available and not fam.policy_locked


def resolve_family(
    family: str,
    *,
    equipment: bool,
    season: str,
    outdoor_locked: bool,
    indoor_f: float | None,
    floor_f: float | None,
    ceiling_f: float | None,
    tolerance_f: float | None,
    since: datetime | None = None,
) -> FamilyAvailability:
    """Availability of one family. See the module docstring for the rules."""
    if not equipment:
        return FamilyAvailability(family, False, REASON_EQUIPMENT, since=since)
    reason: str | None = None
    if season_locks(season, family):
        reason = REASON_SEASON
    elif outdoor_locked:
        reason = REASON_OUTDOOR_LOCKOUT
    if reason is None:
        return FamilyAvailability(family, True, since=since)
    depth = depth_past_edge(family, indoor_f, floor_f, ceiling_f)
    released = depth is not None and tolerance_f is not None and depth > tolerance_f
    return FamilyAvailability(
        family, released, reason, policy_locked=True, safety_released=released, depth_f=depth, since=since
    )


def resolve_zone_capability(
    *,
    equipment_heat: bool,
    equipment_cool: bool,
    season: str = SEASON_AUTO,
    cool_lockout_engaged: bool = False,
    heat_lockout_engaged: bool = False,
    indoor_f: float | None = None,
    floor_f: float | None = None,
    ceiling_f: float | None = None,
    tolerance_f: float | None = None,
    heat_since: datetime | None = None,
    cool_since: datetime | None = None,
) -> ZoneCapability:
    """The zone's answer for heat and cool. Equipment only ever narrows; the policies never widen it."""
    common = {"season": normalize_season(season), "indoor_f": indoor_f, "floor_f": floor_f, "ceiling_f": ceiling_f}
    return ZoneCapability(
        heat=resolve_family(
            FAMILY_HEAT,
            equipment=equipment_heat,
            outdoor_locked=heat_lockout_engaged,
            tolerance_f=tolerance_f,
            since=heat_since,
            **common,
        ),
        cool=resolve_family(
            FAMILY_COOL,
            equipment=equipment_cool,
            outdoor_locked=cool_lockout_engaged,
            tolerance_f=tolerance_f,
            since=cool_since,
            **common,
        ),
    )


def masked_modes(modes: Iterable[str], zone: ZoneCapability) -> tuple[str, ...]:
    """The thermostat's advertised modes with what the zone cannot use now removed.

    An unavailable family's mode goes; a band mode (heat_cool / auto) goes when either family is unavailable, so a
    locked side behaves exactly like a heat-only or cool-only thermostat. Every other mode (off, fan_only, dry) stays.
    """
    return tuple(m for m in modes if zone.mode_allowed(m))
