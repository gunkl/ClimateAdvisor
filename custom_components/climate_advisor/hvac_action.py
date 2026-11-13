"""Shared ``hvac_action`` active-value predicates for Climate Advisor.

HA's ``hvac_action`` climate attribute reports one of eight values (off,
preheating, heating, cooling, drying, fan, idle, defrosting — see
https://developers.home-assistant.io/docs/core/entity/climate/). Multiple call
sites across the integration need to answer one of two distinct questions
about that value, and each has historically hand-rolled its own inline
set/tuple literal to do it (Issue #969, following the same "sibling threshold
drift" pattern as #400/#402/#417/#456/#458): a full-package audit for #969
found 12 independent re-declarations across ``coordinator.py``,
``automation.py``, ``ai_skills_context.py``, and ``invariant_watchdog.py``.
This module is the single source of truth for both predicates, mirroring the
existing ``fan_status.py``/``temperature.py`` precedent: a tiny,
dependency-free utility module.

There are deliberately **two** constants here, not one — they answer
different questions and must never be merged:

- ``HVAC_ACTION_COMPRESSOR_ACTIVE`` — "is the compressor itself actively
  driving" (heating/cooling only, fan-only excluded). Used by guards that must
  not fire while the compressor is running, and by observation/session
  tracking that must not count blower-only runtime as a heat/cool cycle.
- ``HVAC_ACTION_ANY_ACTIVE`` — "is any HVAC output, including the blower,
  active" (heating/cooling/fan). Used by the ``hvac_mode == "off"`` state-
  contradiction check, where a fan-only run while mode is "off" is just as
  much a contradiction as heating/cooling would be.

Collapsing these into one set would be a silent behavior change: e.g. the
30-min untracked-fan backstop guard (``coordinator.py``) and the post-grace-
expiry fan reconcile guard both use ``HVAC_ACTION_COMPRESSOR_ACTIVE``
specifically so they keep reconciling while the thermostat's blower is merely
spinning post-compressor — using the broader any-active set there would break
the whole-house-fan mutex protection those guards exist for.
"""

from __future__ import annotations

HVAC_ACTION_COMPRESSOR_ACTIVE: frozenset[str] = frozenset({"heating", "cooling"})

HVAC_ACTION_ANY_ACTIVE: frozenset[str] = HVAC_ACTION_COMPRESSOR_ACTIVE | frozenset({"fan"})

# Issue #1234: values by which a thermostat POSITIVELY reports that its equipment is not running.
# A hvac_mode change that arrives while hvac_action reads one of these is a mode selection, not a
# heat/cool session (the "phantom" session the state-based fallback used to open). Deliberately
# excludes "fan" and the missing/empty value: those carry no heating/cooling signal at all.
HVAC_ACTION_REPORTS_NOT_RUNNING: frozenset[str] = frozenset({"idle", "off"})


def is_hvac_compressor_active(hvac_action: str | None) -> bool:
    """True if `hvac_action` indicates the compressor itself is actively driving.

    Excludes ``"fan"`` (blower-only, no compressor load) and correctly treats
    ``"idle"``/``"off"``/``""``/unknown values as not active by virtue of not
    being in :data:`HVAC_ACTION_COMPRESSOR_ACTIVE`.
    """
    return str(hvac_action).lower() in HVAC_ACTION_COMPRESSOR_ACTIVE


def is_hvac_action_active(hvac_action: str | None) -> bool:
    """True if `hvac_action` indicates any active HVAC output, including the blower.

    Includes ``"fan"`` alongside ``"heating"``/``"cooling"`` — use this (not
    :func:`is_hvac_compressor_active`) for checks where a fan-only run while
    ``hvac_mode == "off"`` should still count as active (e.g. the state-
    contradiction warning).
    """
    return str(hvac_action).lower() in HVAC_ACTION_ANY_ACTIVE


def is_hvac_action_reported_idle(hvac_action: str | None) -> bool:
    """True if `hvac_action` positively reports the equipment is not running (``idle``/``off``)."""
    return str(hvac_action).lower() in HVAC_ACTION_REPORTS_NOT_RUNNING


def is_fan_only_session(hvac_mode: str | None, hvac_action: str | None) -> bool:
    """The thermostat's own fan_only session (Issue #912): mode ``fan_only`` and hvac_action not reporting idle/off.

    A fan session, never heating or cooling. Unchanged by the owner decision of 2026-11-13 below."""
    return str(hvac_mode) == "fan_only" and not is_hvac_action_reported_idle(hvac_action)


def is_session_running(hvac_mode: str | None, hvac_action: str | None) -> bool:
    """Whether ONE side of a thermostat state change shows a running session (owner decision 2026-11-13).

    A heating or cooling run exists ONLY when the thermostat reports ``hvac_action`` heating or cooling. A ``fan``, a
    missing/empty hvac_action, or "a mode is selected and it is not idle" never counts as a run, for any thermostat
    (the #93 mode fallback and its #1240 refinement are superseded: "Never, remove it."). The fan_only session
    (#912) stays: it is a fan session, not heating/cooling."""
    return is_hvac_compressor_active(hvac_action) or is_fan_only_session(hvac_mode, hvac_action)


def thermostat_reports_running(hvac_mode: str | None, hvac_action: str | None, *, ca_ventilating: bool) -> bool:
    """Whether ONE thermostat snapshot shows a running session, by the session-edge rule (Issue #1241).

    The same rule ``_async_thermostat_changed`` applies to each side of an edge (``is_session_running``); CA's own fan
    or natural ventilation is never the thermostat's fan_only session (``ca_ventilating``, Issue #986)."""
    if is_hvac_compressor_active(hvac_action):
        return True
    return is_fan_only_session(hvac_mode, hvac_action) and not ca_ventilating
