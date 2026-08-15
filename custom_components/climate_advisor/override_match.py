"""Pure decision core for "does the active override already match automation's current
decision?" (Issue #639, Block 5 Phase 3).

Reimplements ``_override_matches_current_decision()`` (automation.py:1612-1680, Issue
#483: "adopt matching decision instead of continuing a grace period"). This is the piece
that FEEDS ``door_window_grace_expiry.decide_grace_expiry_outcome()``'s
``override_matches_current_decision`` input — Phase 2 built the expiry chain that
*consumes* this bool as caller-resolved; Phase 3 builds the piece that resolves it,
per that module's own "no logic duplicated across modules" rule.

**Explicit scope boundary.** The live ``select_comfort_band()`` call and the current
thermostat-setpoint read are NOT re-derived here — the caller resolves
``current_setpoint_f``/``target_setpoint_f`` (the target already the exact
``band.floor``/``band.ceiling`` value ``_apply_comfort_band()`` would command) and passes
them in as plain floats, same discipline ``door_window_grace_expiry.py`` itself uses for
its own caller-resolved boolean.
"""

from __future__ import annotations

OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F = 1.0
"""Mirrors automation.py's OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F — duplicated as a default
only; callers should pass the real constant explicitly via ``tolerance_f`` rather than
relying on this value drifting in sync."""


def decide_override_matches_decision(
    *,
    manual_override_active: bool,
    manual_override_mode: str | None,
    manual_override_source: str | None,
    classification_mode: str | None,
    current_setpoint_f: float | None,
    target_setpoint_f: float | None,
    tolerance_f: float = OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
) -> bool:
    """Pure reimplementation of ``_override_matches_current_decision()``.

    Field-by-field correspondence:
      manual_override_active -> self._manual_override_active
      manual_override_mode    -> self._manual_override_mode
      manual_override_source  -> self._manual_override_source
      classification_mode     -> classification.hvac_mode (None if classification is None)
      current_setpoint_f      -> to_fahrenheit(thermostat's live "temperature" attribute)
                                 (None if unavailable or unparseable — treated as "no live
                                 setpoint to compare", same as the real method's own
                                 fallback: mode match alone is sufficient evidence)
      target_setpoint_f       -> band.floor (heat) / band.ceiling (cool) from
                                 select_comfort_band() for classification's mode — always
                                 None when classification_mode is not "heat"/"cool"
                                 (unused in that branch)

    Precedence, exactly mirroring the real method:
      1. No active mode override, or setpoint-only override -> False.
      2. No classification, or mode mismatch -> False.
      3. Non-heat/cool mode match -> True (no setpoint to compare).
      4. Heat/cool: no live setpoint reading -> True (mode match is the best evidence
         available).
      5. Heat/cool with a live setpoint -> True iff within ``tolerance_f`` of target.
    """
    if not manual_override_active or manual_override_mode is None:
        return False
    if manual_override_source == "setpoint":
        return False
    if classification_mode is None:
        return False
    if manual_override_mode != classification_mode:
        return False
    if classification_mode not in ("heat", "cool"):
        return True
    if current_setpoint_f is None:
        return True
    if target_setpoint_f is None:
        return True
    return abs(current_setpoint_f - target_setpoint_f) <= tolerance_f
