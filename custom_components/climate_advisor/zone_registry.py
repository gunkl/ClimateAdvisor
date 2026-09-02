"""Zone (config-entry) resolution for Climate Advisor's multi-zone support.

Issue #796 Gap 4: ``api.py``'s ``_get_coordinator(hass)`` used to return
``next(iter(entries.values()))`` — an arbitrary "first" entry HA's dict
iteration happened to hand back — across all 21 of its REST view classes.
This module is the single source of truth for resolving one or more zone
coordinators out of ``hass.data[DOMAIN]``, following the same precedent as
``fan_status.py``: a small, dependency-light plain-function module (no HA
subclassing) that multiple call sites can share instead of each hand-rolling
the same lookup. The functions carry no module-level globals — the one piece
of across-call bookkeeping (``get_default_coordinator()``'s warning throttle,
see its docstring) is stored under a ``hass.data`` key, scoped to that Home
Assistant instance, the same storage idiom ``log_capture.py``'s
``_HASS_DATA_KEY`` and ``__init__.py``'s ``_PANEL_HASS_DATA_KEY`` already use
for "has this already happened" tracking — not a module-level dict, which
would leak state across HA instances (and tests) sharing one process.

Deliberately NOT placed in ``api.py`` — that would force a future non-REST
caller (the Zone Influence FSM sketched in docs/multi-zone-spec.md) to import
the REST layer just to resolve a coordinator — and NOT in ``__init__.py``,
which is setup/teardown orchestration only; nothing else in this codebase
imports from ``__init__.py``. ``get_coordinator``/``iter_coordinators`` serve
both Gap 4 (dashboard/API resolving the RIGHT entry) and the future Zone
Influence feature (enumerating siblings, resolving one by entry_id) off the
same underlying data (``hass.data[DOMAIN]``, populated at
``__init__.py``'s ``hass.data[DOMAIN][entry.entry_id] = coordinator``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)

# Verification fix (Issue #796 Phase C rejection): get_default_coordinator()'s
# ambiguous-zone WARNING used to fire unconditionally on every call. With no
# dashboard zone selector yet (PR9 not shipped), the dashboard's _pollCycle
# sends no entry_id and hits 5 endpoints every 60s — with 2+ zones configured
# that's 5 warnings/min through this path alone, which fully evicted
# log_capture.py's LOG_CAPTURE_CAP=200 ring buffer roughly every 40 seconds,
# silently blanking the AI Investigator's "System Errors/Warnings" section for
# the entire life of any multi-zone install (a real occupant-facing
# regression: the operator loses error visibility, not just noisier logs).
#
# Fix: log at most once per distinct resolved outcome ("token") for a given
# hass instance, via _warn_once() below. A repeated call that resolves to the
# SAME outcome (e.g. the same fallback entry_id, poll after poll) is
# suppressed; a call that resolves to a DIFFERENT outcome (zone set changed,
# or a different one of the three fallback branches fired) still logs, since
# that's new information. __init__.py's async_unload_entry() clears this
# state in lockstep with the zone_resolution_ambiguous Repairs issue, so the
# next time the ambiguous condition recurs it warns again from a clean slate.
_WARNED_STATE_KEY = "climate_advisor_zone_registry_warned"


def _warn_once(hass: HomeAssistant, token: str, message: str, *args: object) -> None:
    """Log a WARNING at most once per distinct `token` for this hass instance.

    Re-logs when `token` differs from the last-logged token (a new/changed
    outcome) but suppresses a repeat of the identical outcome. See the
    _WARNED_STATE_KEY comment above for why this exists and why hass.data
    (not a module-level global or a time-based rate limiter) is the storage
    mechanism — no live-clock dependency, and no cross-instance/cross-test
    state leakage.
    """
    state = hass.data.setdefault(_WARNED_STATE_KEY, {})
    if state.get("token") == token:
        return
    state["token"] = token
    _LOGGER.warning(message, *args)


def reset_warning_state(hass: HomeAssistant) -> None:
    """Clear get_default_coordinator()'s WARNING throttle state.

    Called by __init__.py's async_unload_entry() in lockstep with clearing
    the zone_resolution_ambiguous Repairs issue, once zone count drops back
    to <= 1 — so if the ambiguous condition recurs later (a zone re-added),
    it warns again from a clean slate instead of staying suppressed by a
    token left over from this now-resolved episode. A no-op if no warning
    has been logged yet.
    """
    hass.data.pop(_WARNED_STATE_KEY, None)


def get_coordinator(hass: HomeAssistant, entry_id: str) -> ClimateAdvisorCoordinator | None:
    """Resolve one zone's coordinator by entry_id, or None if not found/unloaded."""
    return hass.data.get(DOMAIN, {}).get(entry_id)


def iter_coordinators(hass: HomeAssistant) -> Iterable[ClimateAdvisorCoordinator]:
    """All currently-loaded zone coordinators."""
    return hass.data.get(DOMAIN, {}).values()


def get_default_coordinator(hass: HomeAssistant) -> ClimateAdvisorCoordinator | None:
    """Single-zone convenience path.

    Returns the coordinator when exactly one zone is loaded — this is the
    common case and preserves today's single-zone behavior exactly.

    When zero zones are loaded, returns None (nothing to resolve).

    When more than one zone is loaded, this does NOT return None — see
    "Transitional Safety Window" in docs/multi-zone-spec.md. Any caller that
    doesn't (or can't yet) pass an explicit entry_id degrades to a
    deterministic first-entry selection, via
    ``hass.config_entries.async_entries(DOMAIN)``'s stable order (NOT
    dict-iteration order, which is unstable across restarts — this exact
    precedent is already used in this codebase at ``repairs.py:38,77``),
    plus a WARNING log — throttled to once per distinct resolved outcome per
    hass instance (see ``_warn_once()`` above), not once per call, since every
    caller that doesn't yet send an entry_id (the dashboard's 60s/5-endpoint
    ``_pollCycle`` before PR9 ships) would otherwise re-trigger it constantly.
    The native HA Repairs issue (``zone_resolution_ambiguous``) that also
    signals this condition is raised/cleared at the config-entry
    setup/unload lifecycle points in ``__init__.py``, not here — this
    function only performs the fallback selection and logs it.
    """
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    if len(entries) == 1:
        return next(iter(entries.values()))

    ordered_entries = hass.config_entries.async_entries(DOMAIN)
    if not ordered_entries:
        # Defensive: hass.data[DOMAIN] is non-empty but config_entries has no
        # matching entries — shouldn't happen in practice. This is the LEAST
        # deterministic of the three branches here (dict-iteration order is
        # not guaranteed stable across restarts), so unlike the pre-fix code
        # it's no longer silent — logged (throttled) rather than returning
        # None outright.
        _warn_once(
            hass,
            "defensive_empty_config_entries",
            "Climate Advisor zone fallback: %d zone(s) loaded but "
            "hass.config_entries.async_entries() returned none — falling "
            "back to dict-insertion order, which is NOT guaranteed stable "
            "across restarts. This should not normally happen; please file "
            "a bug report if it persists.",
            len(entries),
        )
        return next(iter(entries.values()))

    for entry in ordered_entries:
        coordinator = entries.get(entry.entry_id)
        if coordinator is not None:
            _warn_once(
                hass,
                f"ambiguous:{entry.entry_id}",
                "Multiple Climate Advisor zones are loaded and this request did not "
                "specify a zone — defaulting to zone entry_id=%s. Pass an explicit "
                "entry_id to target a specific zone. See Settings > Repairs for details.",
                entry.entry_id,
            )
            return coordinator

    # Every ordered entry is unloaded/missing from hass.data — fall back to
    # dict order rather than returning None outright. Also the least
    # deterministic of the three branches (same caveat as above), so also
    # logged (throttled) rather than silent.
    _warn_once(
        hass,
        "defensive_no_matching_loaded_entry",
        "Climate Advisor zone fallback: none of the %d ordered config entries "
        "matched a loaded coordinator in hass.data — falling back to "
        "dict-insertion order, which is NOT guaranteed stable across "
        "restarts. This should not normally happen; please file a bug "
        "report if it persists.",
        len(ordered_entries),
    )
    return next(iter(entries.values()))
