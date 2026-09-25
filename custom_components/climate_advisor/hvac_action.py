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
