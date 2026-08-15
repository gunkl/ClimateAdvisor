"""Unit tests for the override/grace joint lifecycle state derivation (Issue #639,
Block 5 Phase 3). Mirrors the direct-unit-test layer of
``tests/test_door_window_pure_modules.py``'s ``TestDeriveDoorWindowLifecycleState``.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.override_grace_lifecycle import (
    GraceState,
    OverrideConfirmState,
    OverrideGraceLifecycleInputs,
    derive_override_grace_lifecycle_state,
)


class TestDeriveOverrideGraceLifecycleState:
    @pytest.mark.parametrize(
        ("override_confirm_pending", "grace_active", "grace_protects_override", "expected"),
        [
            (False, False, False, (OverrideConfirmState.IDLE, GraceState.NONE)),
            (False, True, True, (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)),
            (False, True, False, (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)),
            (True, False, False, (OverrideConfirmState.PENDING, GraceState.NONE)),
            (True, True, True, (OverrideConfirmState.PENDING, GraceState.ACTIVE_PROTECTING_OVERRIDE)),
            (True, True, False, (OverrideConfirmState.PENDING, GraceState.ACTIVE_UNPROTECTED)),
        ],
    )
    def test_exhaustive_flag_space(self, override_confirm_pending, grace_active, grace_protects_override, expected):
        result = derive_override_grace_lifecycle_state(
            OverrideGraceLifecycleInputs(
                override_confirm_pending=override_confirm_pending,
                grace_active=grace_active,
                grace_protects_override=grace_protects_override,
            )
        )
        assert result == expected

    def test_grace_protects_override_irrelevant_when_grace_inactive(self):
        """grace_protects_override should be ignored entirely when grace_active is
        False — both cases collapse to GraceState.NONE."""
        result_true = derive_override_grace_lifecycle_state(
            OverrideGraceLifecycleInputs(
                override_confirm_pending=False, grace_active=False, grace_protects_override=True
            )
        )
        result_false = derive_override_grace_lifecycle_state(
            OverrideGraceLifecycleInputs(
                override_confirm_pending=False, grace_active=False, grace_protects_override=False
            )
        )
        assert result_true == result_false == (OverrideConfirmState.IDLE, GraceState.NONE)

    def test_returns_plain_tuple_not_merged_enum(self):
        result = derive_override_grace_lifecycle_state(
            OverrideGraceLifecycleInputs(
                override_confirm_pending=False, grace_active=False, grace_protects_override=False
            )
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], OverrideConfirmState)
        assert isinstance(result[1], GraceState)
