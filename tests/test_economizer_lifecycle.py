"""Inline unit tests for the pure economizer lifecycle-state derivation
(strangler-fig completion program, Phase 5, Issue #746).
"""

from __future__ import annotations

from custom_components.climate_advisor.economizer_lifecycle import (
    EconomizerLifecycleInputs,
    EconomizerLifecycleState,
    derive_economizer_lifecycle_state,
)


def _inputs(**overrides) -> EconomizerLifecycleInputs:
    base = {"economizer_active": True, "economizer_phase": "cool-down"}
    return EconomizerLifecycleInputs(**{**base, **overrides})


class TestDeriveEconomizerLifecycleState:
    def test_inactive_when_flag_false_regardless_of_phase(self):
        assert (
            derive_economizer_lifecycle_state(_inputs(economizer_active=False, economizer_phase="cool-down"))
            is EconomizerLifecycleState.INACTIVE
        )

    def test_cool_down(self):
        assert (
            derive_economizer_lifecycle_state(_inputs(economizer_active=True, economizer_phase="cool-down"))
            is EconomizerLifecycleState.COOL_DOWN
        )

    def test_maintain(self):
        assert (
            derive_economizer_lifecycle_state(_inputs(economizer_active=True, economizer_phase="maintain"))
            is EconomizerLifecycleState.MAINTAIN
        )

    def test_active_true_but_phase_inactive_falls_back_inactive(self):
        # Defensive path — should never happen in production (the two fields are
        # always set in lockstep), but must not raise.
        assert (
            derive_economizer_lifecycle_state(_inputs(economizer_active=True, economizer_phase="inactive"))
            is EconomizerLifecycleState.INACTIVE
        )

    def test_unrecognized_phase_falls_back_inactive(self):
        assert (
            derive_economizer_lifecycle_state(_inputs(economizer_active=True, economizer_phase="bogus"))
            is EconomizerLifecycleState.INACTIVE
        )
