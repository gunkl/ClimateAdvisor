"""Pure decision core for override confirmation's PATH A/PATH B split (Issue #639,
Block 5 Phase 3).

Reimplements the one piece of ``start_override_confirmation()``'s inner
``_confirm_override_expired()`` closure (automation.py, `start_override_confirmation()`) that is not yet pure:
whether the confirmation window's expiry formally confirms the override (PATH A) or
discards it as self-resolved (PATH B).

**Explicit scope boundary.** ``desired_state.decide_override_confirm()`` already covers
the disabled-vs-pending branch at confirmation START (confirm_seconds <= 0 -> accept
immediately). This module covers the decision at confirmation EXPIRY only — a distinct
moment, a distinct question. The event emission (``override_confirmed`` /
``override_self_resolved``), the transient notification, and the actual
``_confirm_override()``/state-clearing side effects all stay shell-side.
"""

from __future__ import annotations

from enum import Enum

from .const import is_unavailable_state


class OverrideConfirmPathOutcome(Enum):
    """Which branch the confirmation window's expiry takes."""

    CONFIRM = "confirm"
    DISCARD_SELF_RESOLVED = "discard_self_resolved"


# Override origins whose expiry always confirms, whatever the classification label says.
# "setpoint": the occupant deliberately moved the target. "pause" (Issue #1158): the occupant pressed heat/cool on a
# thermostat Climate Advisor had paused for an open door/window -- CA's plan there is "paused", not the day's
# classification label, so a press equal to that label (heat on a cold day) is still the occupant's own change.
_ALWAYS_CONFIRM_ORIGINS = frozenset({"setpoint", "pause"})


def is_always_confirm_origin(origin: str | None) -> bool:
    """Whether an override of this origin is confirmed at expiry regardless of the classification label."""
    return origin in _ALWAYS_CONFIRM_ORIGINS


def decide_override_confirm_path(
    *,
    setpoint_override: bool,
    current_mode: str,
    classification_mode: str | None,
) -> OverrideConfirmPathOutcome:
    """Pure reimplementation of ``_confirm_override_expired()``'s PATH A/B split.

    Field-by-field correspondence:
      setpoint_override    -> is_always_confirm_origin(origin) (a deliberate setpoint-only override, or a press
                               made during a door/window pause, always takes PATH A regardless of mode)
      current_mode          -> self.hass.states.get(self.climate_entity).state at expiry
      classification_mode   -> self._current_classification.hvac_mode at expiry

    PATH A (CONFIRM): setpoint override, or the mode is still divergent from what
    classification wants (and not unavailable/unknown) — formally confirm the override.
    Issue #1159: an "off" is also ALWAYS confirmed when classification's label is "off". Since #249 an
    "off"-classified (mild) day arms the comfort band, so the occupant's Off is a real, divergent change
    exactly as on a hot/cold day; "off" equals the classification only as a label.
    PATH B (DISCARD_SELF_RESOLVED): the state has resolved back to what classification
    wants on its own — no real override, treated as transient.
    """
    if setpoint_override:
        return OverrideConfirmPathOutcome.CONFIRM

    if is_unavailable_state(current_mode):
        return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    if current_mode != classification_mode:
        return OverrideConfirmPathOutcome.CONFIRM

    # Label match only: classification "off" is not a plan to keep HVAC off.
    if current_mode == "off":
        return OverrideConfirmPathOutcome.CONFIRM

    return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED
