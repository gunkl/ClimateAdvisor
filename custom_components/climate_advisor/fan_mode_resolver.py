"""Fan-mode vocabulary resolver for Climate Advisor (Issue #968).

Home Assistant's climate-entity ``fan_mode``/``fan_modes`` is integration-defined free
text, not a fixed enum (confirmed against
https://developers.home-assistant.io/docs/core/entity/climate/ — HA ships ten optional
built-in constants, on/off/auto/low/medium/high/top/middle/focus/diffuse, and explicitly
allows custom values on top of those). CA previously assumed a fixed two-value
``{"on", "auto"}`` vocabulary at both the write side (``climate.set_fan_mode`` call
sites) and the read side ("is the fan physically running" predicates) — this broke on
any thermostat whose real ``fan_modes`` list has no literal ``"on"``, e.g.
``[auto, low, medium, high]`` (Issue #893's linked comment). This module is the single
source of truth for both directions, mirroring the existing ``fan_status.py``/
``temperature.py`` precedent: a tiny, dependency-free utility module imported by both
``automation.py`` and ``coordinator.py``.

Issue #1556/#1566: the meaning of each value now comes from ``thermostat_profile.py`` (a name guess,
overridden by what the thermostat itself reports, under consistency rules), not from fixed English
preference lists. Every function here takes an optional learned ``profile``; without one it uses
the name guess for the values given.
"""

from __future__ import annotations

from typing import Literal

from .const import FAN_MODE_BOTH, FAN_MODE_DISABLED, FAN_MODE_HVAC
from .thermostat_profile import (
    IDLE_ROLES,
    FanProfile,
    build_fan_profile,
    fan_mode_runs_blower,
    role_of,
)


def is_thermostat_schedule_signal(fan_mode_attr: str, hvac_action_attr: str, profile: FanProfile | None = None) -> bool:
    """True when the thermostat's own schedule is the only reason its blower runs.

    ``hvac_action == "fan"`` while the ``fan_mode`` attribute is an idle mode (empty/auto/off, or a vendor auto
    variant such as ``Auto low``, #1556): e.g. an Ecobee fan-min-on-time or the post-compressor tail. A
    ``fan_mode`` attribute that reads active ("on", a named speed) is a fan someone commanded, not the
    thermostat's schedule.
    """
    role = role_of(fan_mode_attr, profile)
    return str(hvac_action_attr).strip().lower() == "fan" and role in IDLE_ROLES


def fan_control_enabled(fan_mode: str) -> bool:
    """True when Climate Advisor controls a fan at all (any configured fan_mode but disabled).

    Single predicate for "is fan control configured" — a disabled install owns no fan, so the
    thermostat's blower is never CA's to adopt, time, or override-track.
    """
    return fan_mode != FAN_MODE_DISABLED


def thermostat_blower_is_ca_fan(fan_mode: str) -> bool:
    """True when the thermostat's own blower (its ``fan_mode`` attribute) is a fan Climate Advisor owns (Issue #1197).

    ``hvac_fan`` (and the unsupported, unchanged ``both``): the blower IS CA's fan, so an occupant's press is an
    override of it. ``whole_house_fan``: CA's fan is the separate whole-house-fan entity, and the thermostat's blower
    is only the occupant's own equipment -- a press creates no override, no grace and no fan-off handling.
    ``disabled``: CA owns no fan at all. The one predicate the thermostat fan_mode-change handler consults.
    """
    return fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH)


def resolve_fan_mode_command(
    fan_modes: list[str] | None, intent: Literal["on", "off"], profile: FanProfile | None = None
) -> str | None:
    """Return the fan_mode value to command for the given intent, or None if no value is safe.

    Reads the entity's live ``fan_modes`` list each time (never cached/configured). Matching is case-insensitive
    and the original casing is returned, so the service call uses a value the entity actually advertises.

    The choice is :func:`thermostat_profile.build_fan_profile`'s, under its consistency rules (Issue #1556):

    ``intent="off"``: a literal ``auto``, then another ``auto*``/``off`` mode, then a mode the thermostat was
    observed not to run continuously. Never a named speed or circulate mode -- on a Honeywell T6 Pro
    (``["Auto low", "Low", "Circulation"]``) ``Low`` is fan ON, and the old ``auto, off, low`` fallback latched
    the blower on every time CA turned its fan off.

    ``intent="on"``: a literal ``"on"``, else the highest named speed tier present (high > medium > low), else
    any other mode that runs the blower. ``None`` means "cannot express this intent for this entity": callers
    log and send nothing rather than a value the thermostat would misread or reject (Issue #893).

    ``profile``: the learned profile for this thermostat (its evidence overrides the name guess). It is used only
    when its mode list is this ``fan_modes`` list; otherwise a name-only profile is built from ``fan_modes``.
    """
    if not fan_modes:
        return None
    modes = [str(m) for m in fan_modes]
    resolved = profile if isinstance(profile, FanProfile) and list(profile.modes) == modes else build_fan_profile(modes)
    return resolved.on_value if intent == "on" else resolved.off_value


def is_thermostat_fan_physically_active(
    fan_mode_attr: str, hvac_action_attr: str, profile: FanProfile | None = None
) -> bool:
    """True if the thermostat's own attributes indicate its fan is physically running.

    Replaces the previous ``fan_mode_attr == "on"`` strict-equality checks scattered across coordinator.py
    (Issue #968). An idle mode (empty/auto/off, or a vendor auto variant like ``Auto low``, #1556) runs the blower
    only when the thermostat reports ``hvac_action == "fan"``; a circulate mode follows the reported
    ``hvac_action`` when there is one; a continuous/named-speed or unrecognised value reads as running. A learned
    ``profile`` overrides the name guess with what this thermostat was observed to do.
    """
    return fan_mode_runs_blower(fan_mode_attr, hvac_action_attr, profile)
