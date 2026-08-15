"""Unit tests for override detection and supersession (Issue #639, Block 5 Phase 3).

Reimplements the two genuine decisions inside
``coordinator._async_thermostat_changed()``'s elif chain: plain-detection target-mode
resolution (Issue #618 "Bug C") and Issue #282's "Fix D" supersession trigger.

NOTE (scope, per the executor briefing): ``decide_override_detected`` and
``decide_override_supersession`` are coordinator-routing helpers, NOT called from
``override_grace_fsm.py``'s ``transition()`` — no FSM wiring is exercised here, only
the two pure functions' own logic.
"""

from __future__ import annotations

from custom_components.climate_advisor.override_supersession import (
    decide_override_detected,
    decide_override_supersession,
)


class TestDecideOverrideDetected:
    def test_new_mode_matches_last_commanded_no_override(self):
        result = decide_override_detected(
            new_mode="cool",
            last_commanded_hvac_mode="cool",
            classification_mode="heat",
        )
        assert result is False

    def test_new_mode_diverges_from_last_commanded_detected(self):
        result = decide_override_detected(
            new_mode="heat",
            last_commanded_hvac_mode="cool",
            classification_mode="cool",
        )
        assert result is True

    def test_no_last_commanded_falls_back_to_classification_match(self):
        """Issue #618 Bug C: when CA has never issued a command, fall back to
        classification.hvac_mode rather than always flagging an override."""
        result = decide_override_detected(
            new_mode="heat",
            last_commanded_hvac_mode=None,
            classification_mode="heat",
        )
        assert result is False

    def test_no_last_commanded_falls_back_to_classification_mismatch(self):
        result = decide_override_detected(
            new_mode="cool",
            last_commanded_hvac_mode=None,
            classification_mode="heat",
        )
        assert result is True

    def test_last_commanded_takes_precedence_over_classification(self):
        """Bug C fix: comparing against classification FIRST reintroduces the bug
        — last_commanded_hvac_mode must win when both are present, even if it
        disagrees with classification (a legitimate heat_cool-vs-simplified-mode
        difference should not read as a false override)."""
        result = decide_override_detected(
            new_mode="heat_cool",
            last_commanded_hvac_mode="heat_cool",
            classification_mode="heat",
        )
        assert result is False

    def test_neither_last_commanded_nor_classification_present(self):
        result = decide_override_detected(
            new_mode="heat",
            last_commanded_hvac_mode=None,
            classification_mode=None,
        )
        assert result is True


class TestDecideOverrideSupersession:
    def test_new_mode_same_as_current_override_no_supersession(self):
        result = decide_override_supersession(new_mode="heat", current_manual_override_mode="heat")
        assert result is False

    def test_new_mode_different_from_current_override_supersedes(self):
        result = decide_override_supersession(new_mode="cool", current_manual_override_mode="heat")
        assert result is True

    def test_no_current_override_mode_treated_as_different(self):
        result = decide_override_supersession(new_mode="heat", current_manual_override_mode=None)
        assert result is True
