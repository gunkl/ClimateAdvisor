"""Unit tests for the override confirmation window's PATH A/PATH B expiry split
(Issue #639, Block 5 Phase 3). Reimplements ``_confirm_override_expired()``'s
PATH A (CONFIRM) / PATH B (DISCARD_SELF_RESOLVED) branch.
"""

from __future__ import annotations

from custom_components.climate_advisor.override_confirm_split import (
    OverrideConfirmPathOutcome,
    decide_override_confirm_path,
)


class TestDecideOverrideConfirmPath:
    def test_setpoint_override_always_confirms(self):
        """A deliberate setpoint-only override always takes PATH A regardless of
        mode — even when current_mode == classification_mode."""
        result = decide_override_confirm_path(
            setpoint_override=True,
            current_mode="cool",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.CONFIRM

    def test_mode_still_divergent_confirms(self):
        result = decide_override_confirm_path(
            setpoint_override=False,
            current_mode="heat",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.CONFIRM

    def test_mode_resolved_to_match_classification_discards(self):
        result = decide_override_confirm_path(
            setpoint_override=False,
            current_mode="cool",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    def test_mode_unavailable_discards(self):
        """unavailable/unknown states are excluded from the divergence check —
        treated as self-resolved rather than confirmed."""
        result = decide_override_confirm_path(
            setpoint_override=False,
            current_mode="unavailable",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    def test_mode_unknown_discards(self):
        result = decide_override_confirm_path(
            setpoint_override=False,
            current_mode="unknown",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED

    def test_classification_mode_none_and_current_diverges_confirms(self):
        """No classification available: current_mode != None is still divergent
        (None is a valid classification_mode value, never equal to a real mode
        string), so this still confirms."""
        result = decide_override_confirm_path(
            setpoint_override=False,
            current_mode="heat",
            classification_mode=None,
        )
        assert result == OverrideConfirmPathOutcome.CONFIRM

    def test_setpoint_override_beats_unavailable_current_mode(self):
        """Precedence: setpoint_override short-circuits before the
        unavailable/unknown exclusion is even consulted."""
        result = decide_override_confirm_path(
            setpoint_override=True,
            current_mode="unavailable",
            classification_mode="cool",
        )
        assert result == OverrideConfirmPathOutcome.CONFIRM
