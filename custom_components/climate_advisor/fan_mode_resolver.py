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
"""

from __future__ import annotations

from typing import Literal

# Preference order when resolving "turn the fan on" against a real fan_modes list —
# checked case-insensitively, first match wins.
_ON_PREFERENCE = ("on", "high", "medium", "low")
# Preference order for "turn the fan off" (return to idle/circulation-only).
_OFF_PREFERENCE = ("auto", "off", "low")

# Values that mean "fan is not actively running" when read back from a real entity —
# anything NOT in this set (and non-empty) is treated as an active/running fan mode.
_INACTIVE_FAN_MODE_VALUES = frozenset({"", "auto", "off"})


def resolve_fan_mode_command(fan_modes: list[str] | None, intent: Literal["on", "off"]) -> str | None:
    """Return the fan_mode value to command for the given intent, or None if impossible.

    Reads the entity's live ``fan_modes`` list each time (never cached/configured) —
    the whole point is to stop assuming a fixed vocabulary. Matching is
    case-insensitive against the real list; the original casing from `fan_modes` is
    returned so the service call uses a value the entity actually advertises.

    ``intent="off"``: prefers ``"auto"``, then ``"off"``, then the lowest named speed
    tier present — a thermostat almost always has *some* safe idle value.

    ``intent="on"``: prefers a literal ``"on"``, else the highest named speed tier
    present (high > medium > low) — the direction with no guaranteed-safe fallback.
    Returns ``None`` when `fan_modes` has no literal "on" and none of the named speed
    tiers either (e.g. an empty/unknown list); callers must treat ``None`` as "cannot
    express fan-on for this entity" and log an error rather than sending an invalid
    value the thermostat will silently reject (Issue #893's original failure mode).
    """
    if not fan_modes:
        return None
    lower_to_original = {str(m).lower(): m for m in fan_modes}
    preference = _ON_PREFERENCE if intent == "on" else _OFF_PREFERENCE
    for candidate in preference:
        if candidate in lower_to_original:
            return lower_to_original[candidate]
    return None


def is_thermostat_fan_physically_active(fan_mode_attr: str, hvac_action_attr: str) -> bool:
    """True if the thermostat's own attributes indicate its fan is physically running.

    Replaces the previous ``fan_mode_attr == "on"`` strict-equality checks scattered
    across coordinator.py (Issue #968) — those never matched a named speed tier like
    "low"/"medium"/"high", so a fan genuinely running at a named speed was
    misread as idle, feeding the repeating "fan found running without a CA-owned
    session" grace-period churn seen in Issue #893.

    Any fan_mode value other than empty/"auto"/"off" is treated as active — this
    mirrors the same active-set-membership pattern already proven correct for
    hvac_action off/idle detection elsewhere in this codebase, applied to the
    read side of fan_mode for the first time.
    """
    normalized = str(fan_mode_attr).strip().lower()
    if normalized not in _INACTIVE_FAN_MODE_VALUES:
        return True
    return str(hvac_action_attr).strip().lower() == "fan"
