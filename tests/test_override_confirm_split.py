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


class TestOccupantOffIssue1159:
    """An "off" equal to the classification LABEL is a real change unless CA has paused HVAC for a door/window."""

    def test_off_on_off_labelled_day_confirms_when_ca_is_not_holding_off(self):
        assert (
            decide_override_confirm_path(
                setpoint_override=False,
                current_mode="off",
                classification_mode="off",
            )
            == OverrideConfirmPathOutcome.CONFIRM
        )

    def test_off_equal_to_a_non_off_label_is_still_a_plain_self_resolve(self):
        assert (
            decide_override_confirm_path(
                setpoint_override=False,
                current_mode="cool",
                classification_mode="cool",
            )
            == OverrideConfirmPathOutcome.DISCARD_SELF_RESOLVED
        )


class TestIsAlwaysConfirmOrigin:
    """Issue #1158: a press made during a door/window pause is the occupant's own change whatever the label says."""

    def test_pause_and_setpoint_origins_always_confirm(self):
        from custom_components.climate_advisor.override_confirm_split import is_always_confirm_origin

        assert is_always_confirm_origin("pause")
        assert is_always_confirm_origin("setpoint")

    def test_ordinary_mode_change_still_compares_against_the_classification(self):
        from custom_components.climate_advisor.override_confirm_split import is_always_confirm_origin

        assert not is_always_confirm_origin("normal")
        assert not is_always_confirm_origin(None)
