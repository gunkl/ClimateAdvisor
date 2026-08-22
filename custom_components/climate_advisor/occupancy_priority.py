"""Occupancy toggle priority resolution (Issue #744, strangler-fig completion Phase 4).

Pure extraction of ``ClimateAdvisorCoordinator._compute_occupancy_mode()``'s priority
logic — guest > vacation > home/away > default home. Zero hidden reads: the caller
(coordinator.py) resolves each toggle's effective on/off state via its own
``_is_toggle_on()`` (which itself reads ``hass.states`` and applies the invert flag —
that HA state lookup is NOT something this module reaches for) and passes the already-
resolved booleans in. This is the same "explicit Inputs dataclass, zero hidden reads"
contract every other pure leaf in this strangler-fig program uses.

**Not flag-gated (no ``_occupancy_fsm_authoritative``-style dual path).** Unlike the
away/vacation/home dispatch logic in ``occupancy_fsm.py``, this extraction has no
behavioral branching to prove safe via A/B replay — ``_compute_occupancy_mode()``'s
15-line body was already effectively pure (no state mutation, no HA service calls, no
logging beyond the toggle-unavailable warning which stays in ``_is_toggle_on()``, not
here). Extracting it is a straight, semantics-preserving refactor, the same footing as
``select_comfort_band()``/``should_defer_to_occupancy_setback()`` in automation.py,
both of which are called directly and unconditionally rather than behind a shadow flag.
``coordinator._compute_occupancy_mode()`` now just resolves the three toggle booleans
and calls ``decide_occupancy_priority()`` — no dormant/live branch, no flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import OCCUPANCY_AWAY, OCCUPANCY_GUEST, OCCUPANCY_HOME, OCCUPANCY_VACATION


@dataclass(frozen=True)
class OccupancyPriorityInputs:
    """Every live reading this leaf's decision may consult — explicit, nothing hidden.

    Each ``*_configured`` field mirrors ``cfg.get(CONF_*_TOGGLE)`` being truthy (an
    entity_id is configured for that toggle); each ``*_on`` field mirrors
    ``coordinator._is_toggle_on(entity_id, invert)``'s resolved boolean for that
    entity — already accounting for the "unavailable/unknown → False" fallback and the
    invert flag. This module never touches ``hass.states`` itself.
    """

    guest_configured: bool
    guest_on: bool
    vacation_configured: bool
    vacation_on: bool
    home_configured: bool
    home_on: bool


def decide_occupancy_priority(inputs: OccupancyPriorityInputs) -> str:
    """Return the effective occupancy mode — guest > vacation > home/away > default home.

    Pure mirror of ``ClimateAdvisorCoordinator._compute_occupancy_mode()``'s exact
    priority chain:
    1. Guest toggle configured AND on → ``OCCUPANCY_GUEST`` (highest priority).
    2. Vacation toggle configured AND on → ``OCCUPANCY_VACATION``.
    3. Home toggle configured → ``OCCUPANCY_HOME`` if on, else ``OCCUPANCY_AWAY``.
    4. No toggles configured at all → ``OCCUPANCY_HOME`` (default).
    """
    if inputs.guest_configured and inputs.guest_on:
        return OCCUPANCY_GUEST
    if inputs.vacation_configured and inputs.vacation_on:
        return OCCUPANCY_VACATION
    if inputs.home_configured:
        return OCCUPANCY_HOME if inputs.home_on else OCCUPANCY_AWAY
    return OCCUPANCY_HOME
