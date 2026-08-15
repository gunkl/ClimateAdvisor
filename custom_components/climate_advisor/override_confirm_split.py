"""Pure decision core for override confirmation's PATH A/PATH B split (Issue #639,
Block 5 Phase 3).

Reimplements the one piece of ``start_override_confirmation()``'s inner
``_confirm_override_expired()`` closure (automation.py:1550-1608) that is not yet pure:
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


class OverrideConfirmPathOutcome(Enum):
    """Which branch the confirmation window's expiry takes."""

    CONFIRM = "confirm"
    DISCARD_SELF_RESOLVED = "discard_self_resolved"


def decide_override_confirm_path(
    *,
    setpoint_override: bool,
    current_mode: str,
    classification_mode: str | None,
) -> OverrideConfirmPathOutcome:
    """Pure reimplementation of ``_confirm_override_expired()``'s PATH A/B split.

    Field-by-field correspondence:
      setpoint_override    -> source == "setpoint" (a deliberate setpoint-only override
                               always takes PATH A regardless of mode)
      current_mode          -> self.hass.states.get(self.climate_entity).state at expiry
      classification_mode   -> self._current_classification.hvac_mode at expiry

    PATH A (CONFIRM): setpoint override, or the mode is still divergent from what
    classification wants (and not unavailable/unknown) — formally confirm the override.
    PATH B (DISCARD_SELF_RESOLVED): the state has resolved back to what classification
    wants on its own — no real override, treated as transient.
    """
    if setpoint_override:
        return OverrideConfirmPathOutcome.CONFIRM

    if current_mode not in ("unavailable", "unknown") and current_mode != classification_mode:
        return OverrideConfirmPathOutcome.CONFIRM

    return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED
