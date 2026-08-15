"""Pure decision core for override detection and Issue #282's "second override during
active grace" supersession branch, both in ``coordinator._async_thermostat_changed()``
(Issue #639, Block 5 Phase 3).

**Explicit scope boundary.** ``_async_thermostat_changed()``'s elif chain (coordinator.py
~3990-4096) is a long guard chain of already-known plumbing booleans — is a command
currently pending, was a command issued in the last few seconds, does this confirm an
automation-initiated change, has the mode string actually changed, is the new state
unavailable/unknown. None of that is business logic; it is caller-resolved exactly the
way ``door_window_open_response.py`` treats ``sensor_debounce_pending`` as a plain input
(Issue #623's exclusion). This module extracts only the two genuine decisions buried
inside that chain — the ones with real historical bug pressure (Issue #282, Issue #618's
"Bug C"), not the surrounding plumbing.

Function 1, ``decide_override_detected``, captures the target-mode resolution Issue #618
("Bug C") fixed: compare against ``_last_commanded_hvac_mode`` first, falling back to
``classification.hvac_mode`` only when CA has never issued a command — comparing against
classification first re-introduced Bug C (a legitimate heat_cool-vs-classification's
simplified heat/cool/off difference read as a false override).

Function 2, ``decide_override_supersession``, captures Issue #282's "Fix D": a mode change
to a mode different from the currently-overridden mode, while that override's grace is
still running, is a NEW override superseding the old one — the caller clears the old one
and re-detects the new one from scratch, restarting the grace/confirm cycle.
"""

from __future__ import annotations


def decide_override_detected(
    *,
    new_mode: str,
    last_commanded_hvac_mode: str | None,
    classification_mode: str | None,
) -> bool:
    """Pure reimplementation of the plain-detection branch's target-mode comparison
    (coordinator.py ~4064-4095, Issue #618's "Bug C" fix).

    Field-by-field correspondence:
      new_mode                  -> new_state.state
      last_commanded_hvac_mode   -> self.automation_engine._last_commanded_hvac_mode
      classification_mode        -> self._current_classification.hvac_mode

    Caller is responsible for the surrounding guard chain (mode actually changed, not
    unavailable/unknown, no override currently active, no command pending, not a recent
    command, not an expected confirmation, classification present) — this function only
    answers "does the new mode disagree with what CA is actively controlling toward,"
    which is the one comparison Issue #618 found order-sensitive.
    """
    effective_expected_mode = last_commanded_hvac_mode or classification_mode
    return new_mode != effective_expected_mode


def decide_override_supersession(
    *,
    new_mode: str,
    current_manual_override_mode: str | None,
) -> bool:
    """Pure reimplementation of Issue #282's "Fix D" supersession branch
    (coordinator.py ~4037-4058).

    Field-by-field correspondence:
      new_mode                      -> new_state.state
      current_manual_override_mode   -> self.automation_engine._manual_override_mode

    Caller is responsible for the surrounding guard chain (mode actually changed, not
    unavailable/unknown, an override is currently active, no command pending, not a
    recent command, not an expected confirmation) — this function only answers "is the
    new mode a DIFFERENT mode than the one currently being overridden," which is the
    supersession trigger itself.
    """
    return new_mode != current_manual_override_mode
