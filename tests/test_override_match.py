"""Unit tests for "does the active override already match automation's current
decision?" (Issue #639, Block 5 Phase 3). Reimplements
``_override_matches_current_decision()``'s 5-branch precedence chain (Issue #483).
"""

from __future__ import annotations

from custom_components.climate_advisor.override_match import (
    OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
    decide_override_matches_decision,
)


def _base(**overrides):
    base = dict(
        manual_override_active=True,
        manual_override_mode="heat",
        manual_override_source="normal",
        classification_mode="heat",
        current_setpoint_f=70.0,
        target_setpoint_f=70.0,
    )
    base.update(overrides)
    return decide_override_matches_decision(**base)


class TestDecideOverrideMatchesDecision:
    # Branch 1: no active mode override, or setpoint-only override -> False.
    def test_no_active_override_false(self):
        assert _base(manual_override_active=False) is False

    def test_no_manual_override_mode_false(self):
        assert _base(manual_override_mode=None) is False

    def test_setpoint_only_source_false(self):
        assert _base(manual_override_source="setpoint") is False

    # Branch 2: no classification, or mode mismatch -> False.
    def test_no_classification_false(self):
        assert _base(classification_mode=None) is False

    def test_mode_mismatch_false(self):
        assert _base(manual_override_mode="heat", classification_mode="cool") is False

    # Branch 3: non-heat/cool mode match -> True (no setpoint to compare).
    def test_off_mode_match_true(self):
        assert (
            _base(
                manual_override_mode="off",
                classification_mode="off",
                current_setpoint_f=None,
                target_setpoint_f=None,
            )
            is True
        )

    def test_heat_cool_mode_match_true_ignores_setpoint(self):
        """Non-heat/cool mode match returns True even if setpoint fields would
        otherwise disagree — proves the branch short-circuits before comparing."""
        result = decide_override_matches_decision(
            manual_override_active=True,
            manual_override_mode="fan_only",
            manual_override_source="normal",
            classification_mode="fan_only",
            current_setpoint_f=60.0,
            target_setpoint_f=90.0,
        )
        assert result is True

    # Branch 4: heat/cool, no live setpoint reading -> True.
    def test_heat_cool_no_current_setpoint_true(self):
        assert _base(current_setpoint_f=None) is True

    def test_heat_cool_no_target_setpoint_true(self):
        assert _base(target_setpoint_f=None) is True

    # Branch 5: heat/cool with a live setpoint -> True iff within tolerance.
    def test_within_tolerance_true(self):
        assert _base(current_setpoint_f=70.5, target_setpoint_f=70.0) is True

    def test_exactly_at_tolerance_boundary_true(self):
        assert _base(current_setpoint_f=71.0, target_setpoint_f=70.0) is True

    def test_beyond_tolerance_false(self):
        assert _base(current_setpoint_f=72.0, target_setpoint_f=70.0) is False

    def test_cool_mode_within_tolerance_true(self):
        assert (
            _base(
                manual_override_mode="cool",
                classification_mode="cool",
                current_setpoint_f=78.5,
                target_setpoint_f=78.0,
            )
            is True
        )

    def test_custom_tolerance_respected(self):
        result = _base(current_setpoint_f=72.0, target_setpoint_f=70.0, tolerance_f=3.0)
        assert result is True

    def test_default_tolerance_constant_matches_automation_py(self):
        assert OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F == 1.0
