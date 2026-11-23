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
depth strictly greater than the tolerance), and locks again only once the house is back at the edge (depth <= 0) and
the mode-switch dwell allows a routine switch -- a hysteresis, so a house hovering around edge + tolerance does not
flip heat and cool on every reading. Missing equipment is never released (there is nothing to run). With an unknown
indoor reading or band a policy lock stays locked. The release state is owned by the comfort-edge selector (the band
it decides on); every other reader uses the recorded state.

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
    """True when both lockouts are set and could be engaged at once (rejected in the form).

    The cool lockout stays engaged up to C + 3.6 °F once engaged and the heat lockout down to H - 3.6 °F, so with
    H < C + 3.6 (2 °C) there are outdoor temperatures between H and C + 3.6 at which, coming from below, the zone could
    neither heat nor cool. The heat lockout must be at least 2 °C above the cool lockout.
    """
    if cool_lockout_f is None or heat_lockout_f is None:
        return False
    return heat_lockout_f < cool_lockout_f + LOCKOUT_RELEASE_HYSTERESIS_F - 1e-6


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


def next_safety_release(
    released_before: bool, depth_f: float | None, tolerance_f: float | None, *, relock_allowed: bool = True
) -> bool:
    """Next state of a policy lock's safety release, with hysteresis (review of #1699).

    Releases when the house is deeper past the edge than the tolerance (``depth > tolerance``, the R1 boundary).
    Once released it stays released until the house is back at the edge (``depth <= 0``), so a house hovering around
    edge + tolerance does not flip the mode on every reading; and, at the edge, only once the mode-switch dwell
    allows a routine switch (``relock_allowed``). An unknown reading, band or tolerance locks.
    """
    if depth_f is None or tolerance_f is None:
        return False
    if released_before:
        return depth_f > 0.0 or not relock_allowed
    return depth_f > tolerance_f


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

    @property
    def policy_engaged(self) -> bool:
        """True while an outdoor lockout or the season setting takes either side away."""
        return self.heat.policy_locked or self.cool.policy_locked

    def mode_allowed(self, mode: str | None) -> bool:
        """May Climate Advisor command ``mode`` now?

        heat / cool need their family. A band mode (heat_cool / auto) the thermostat advertises stays allowed on
        equipment alone (on a heat-only Tado or Netatmo ``auto`` is its schedule mode); it is refused only while a
        policy lock is engaged on either side, because the thermostat would then run the locked side by itself.
        """
        if mode == FAMILY_HEAT:
            return self.heat.available
        if mode == FAMILY_COOL:
            return self.cool.available
        if mode in BAND_MODES:
            return not self.policy_engaged
        return True

    @property
    def none_available(self) -> bool:
        """Neither family can run now (e.g. cool-only equipment plus a cool lockout)."""
        return not self.heat.available and not self.cool.available

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
    released_before: bool = False,
    relock_allowed: bool = True,
    safety_released: bool | None = None,
) -> FamilyAvailability:
    """Availability of one family. See the module docstring for the rules.

    The safety release is stateful: ``released_before`` is the family's previous release state and the answer is
    :func:`next_safety_release`. A caller that must not move that state (any path other than the comfort-edge
    selector, which owns it) passes ``safety_released`` to use the recorded decision as is.
    """
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
    released = (
        safety_released
        if safety_released is not None
        else next_safety_release(released_before, depth, tolerance_f, relock_allowed=relock_allowed)
    )
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
    heat_released_before: bool = False,
    cool_released_before: bool = False,
    relock_allowed: bool = True,
    use_recorded_release: bool = False,
) -> ZoneCapability:
    """The zone's answer for heat and cool. Equipment only ever narrows; the policies never widen it.

    ``*_released_before`` is each family's recorded safety-release state. ``use_recorded_release`` answers with it as
    is (every reader except the selector); otherwise the release steps through :func:`next_safety_release`.
    """
    season = normalize_season(season)
    return ZoneCapability(
        heat=resolve_family(
            FAMILY_HEAT,
            equipment=equipment_heat,
            outdoor_locked=heat_lockout_engaged,
            tolerance_f=tolerance_f,
            since=heat_since,
            released_before=heat_released_before,
            relock_allowed=relock_allowed,
            safety_released=heat_released_before if use_recorded_release else None,
            season=season,
            indoor_f=indoor_f,
            floor_f=floor_f,
            ceiling_f=ceiling_f,
        ),
        cool=resolve_family(
            FAMILY_COOL,
            equipment=equipment_cool,
            outdoor_locked=cool_lockout_engaged,
            tolerance_f=tolerance_f,
            since=cool_since,
            released_before=cool_released_before,
            relock_allowed=relock_allowed,
            safety_released=cool_released_before if use_recorded_release else None,
            season=season,
            indoor_f=indoor_f,
            floor_f=floor_f,
            ceiling_f=ceiling_f,
        ),
    )


def masked_modes(modes: Iterable[str], zone: ZoneCapability) -> tuple[str, ...]:
    """The thermostat's advertised modes with what the zone cannot use now removed.

    An unavailable family's mode goes; a band mode (heat_cool / auto) goes while a policy lock is engaged, so a
    locked side behaves exactly like a heat-only or cool-only thermostat. Every other mode (off, fan_only, dry) stays.
    """
    return tuple(m for m in modes if zone.mode_allowed(m))


# ---- Phase 2 (#1544): what the plan, briefing and judges may count on ----------------------------------------------


@dataclass(frozen=True)
class ZoneService:
    """Which families the zone SERVES for planning, promising and judging (Issue #1544, phase 2).

    Occupant view: a heat-only home is never promised air conditioning, planned with AC runs or told that Climate
    Advisor failed to keep it cool; the side it cannot serve becomes advice (windows, blinds, ventilation; "close
    up, the sun is your heat" for a cool-only home).

    A family is served when it is available AND no policy lock (outdoor lockout, season) is engaged: the safety
    release is a backstop the selector applies past the tolerance, never something to plan or promise. ``*_reason``
    names why a family is not served (``equipment`` / ``outdoor_lockout`` / ``season``); None when it is served.

    ``defended_*`` (None = same as served) is the JUDGING view: a family is defended while it can run right now,
    which includes a policy lock stepping aside for the safety release. Incidents, the Comfort Score, the stall alert
    and P7 judge a defended edge (a release that runs and still fails is a real failure); planning and promises use
    the served view.
    """

    heat: bool = True
    cool: bool = True
    heat_reason: str | None = None
    cool_reason: str | None = None
    defended_heat: bool | None = None
    defended_cool: bool | None = None

    @property
    def full(self) -> bool:
        """Both families are served (every surface behaves exactly as before #1544)."""
        return self.heat and self.cool

    def serves(self, family: str | None) -> bool:
        """True for a served family; any other mode (off, fan_only, a band mode, unknown) is not a family."""
        if family == FAMILY_HEAT:
            return self.heat
        if family == FAMILY_COOL:
            return self.cool
        return True

    def defends(self, family: str | None) -> bool:
        """True while ``family`` can run now (served, or a policy lock released by the safety rule); judging view."""
        if family == FAMILY_HEAT:
            return self.heat if self.defended_heat is None else self.defended_heat
        if family == FAMILY_COOL:
            return self.cool if self.defended_cool is None else self.defended_cool
        return True

    @property
    def defends_both(self) -> bool:
        """Both families can run now (nothing is excused from judging)."""
        return self.defends(FAMILY_HEAT) and self.defends(FAMILY_COOL)

    def released(self, family: str) -> bool:
        """``family`` is not served (planned) but runs now: a policy lock stepping aside for the safety release."""
        return not self.serves(family) and self.defends(family)

    def reason(self, family: str) -> str | None:
        """Why ``family`` is not served (None when it is)."""
        return self.heat_reason if family == FAMILY_HEAT else self.cool_reason

    def unserved_reason(self) -> tuple[str, str] | None:
        """``(family, reason)`` of the first family the zone does not serve (cool, then heat), or None."""
        if not self.cool:
            return FAMILY_COOL, self.cool_reason or REASON_EQUIPMENT
        if not self.heat:
            return FAMILY_HEAT, self.heat_reason or REASON_EQUIPMENT
        return None

    def shown_mode(self, mode: str | None) -> str | None:
        """The day mode as this zone will actually run it (the classifier's ``hvac_mode`` for display and planning).

        Both families served: unchanged. One family: that family, whatever the day says (a heat-only zone holds heat
        at the floor on a hot day, and on a warm "off" day the single-family leaf arms that family). Neither: off.
        """
        if self.full:
            return mode
        if self.heat:
            return FAMILY_HEAT
        if self.cool:
            return FAMILY_COOL
        return "off"


FULL_SERVICE = ZoneService()


def service_from(zone: ZoneCapability) -> ZoneService:
    """The resolver's answer for display: served = available and not policy-locked (``bank_allowed``, planning and
    promises); defended = available now, safety release included (judging)."""
    heat = zone.bank_allowed(FAMILY_HEAT)
    cool = zone.bank_allowed(FAMILY_COOL)
    return ZoneService(
        heat=heat,
        cool=cool,
        heat_reason=None if heat else (zone.heat.reason or REASON_EQUIPMENT),
        cool_reason=None if cool else (zone.cool.reason or REASON_EQUIPMENT),
        defended_heat=zone.heat.available,
        defended_cool=zone.cool.available,
    )
