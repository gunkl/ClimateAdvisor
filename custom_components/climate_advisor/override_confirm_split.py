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


def ca_expected_mode(commanded_mode: str | None, classification_mode: str | None) -> str | None:
    """What Climate Advisor expects the thermostat to be in: its commanded mode, else the day's label (Issue #1209).

    The single definition behind the coordinator's override DETECTION (a change is an override only when the live mode
    differs from this) and the expiry rule in ``decide_override_confirm_path`` (which takes the commanded mode itself,
    never this fallback, so the label-only "off" of Issue #1159 stays confirmed). The commanded mode comes first
    because on a warm or mild day the label is "off" while CA runs cool or heat on the comfort band, and "heat_cool"
    is never a label.
    """
    return commanded_mode or classification_mode


def decide_override_confirm_path(
    *,
    setpoint_override: bool,
    current_mode: str,
    classification_mode: str | None,
    commanded_mode: str | None = None,
) -> OverrideConfirmPathOutcome:
    """Pure reimplementation of ``_confirm_override_expired()``'s PATH A/B split.

    Field-by-field correspondence:
      setpoint_override    -> is_always_confirm_origin(origin) (a deliberate setpoint-only override, or a press
                               made during a door/window pause, always takes PATH A regardless of mode)
      current_mode          -> self.hass.states.get(self.climate_entity).state at expiry
      classification_mode   -> self._current_classification.hvac_mode at expiry
      commanded_mode        -> AutomationEngine._ca_commanded_mode() at expiry (Issue #1209): the mode Climate Advisor
                               itself last commanded, read live; None when it never commanded one

    PATH A (CONFIRM): setpoint override, or the mode is still divergent from what
    classification wants (and not unavailable/unknown) — formally confirm the override.
    Issue #1159: an "off" is also ALWAYS confirmed when classification's label is "off". Since #249 an
    "off"-classified (mild) day arms the comfort band, so the occupant's Off is a real, divergent change
    exactly as on a hot/cold day; "off" equals the classification only as a label.
    Issue #1209: PATH B also when the live mode equals the mode Climate Advisor COMMANDED. On a warm or mild day the
    label is "off" while CA runs cool (or heat) on the comfort band; override detection already judges a change against
    the commanded mode first, so a glitch that returns to that mode is not the occupant's change and must not start a
    1 h manual-change pause. The "off" rule above is unaffected: the commanded-mode match never applies to an Off, so an
    Off is confirmed even when CA's own last command was Off (nat-vent exit Off-hold, door/window pause), where the
    occupant reaches it from their own heat/cool inside the pause.
    PATH B (DISCARD_SELF_RESOLVED): the state has resolved back to what Climate Advisor commanded, or to what
    classification wants, on its own — no real override, treated as transient.
    """
    if setpoint_override:
        return OverrideConfirmPathOutcome.CONFIRM

    if is_unavailable_state(current_mode):
        return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    # Issue #1209: back on the running mode CA itself commanded -> nothing the occupant changed. Never for "off"
    # (Issue #1159): CA's last command is often an Off (nat-vent exit Off-hold, door/window pause) and the occupant can
    # reach Off again from their own heat/cool inside that pause (detection then compares with their override mode),
    # so a live Off equal to a commanded Off is still the occupant's decision.
    if commanded_mode is not None and current_mode == commanded_mode and current_mode != "off":
        return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    if current_mode != classification_mode:
        return OverrideConfirmPathOutcome.CONFIRM

    # Label match only: classification "off" is not a plan to keep HVAC off.
    if current_mode == "off":
        return OverrideConfirmPathOutcome.CONFIRM

    return OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED
