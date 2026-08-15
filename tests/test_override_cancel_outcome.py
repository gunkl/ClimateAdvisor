"""Unit tests for ``cancel_override()``'s outcome classification (Issue #639,
Block 5 Phase 3). Reimplements the no-op guard and had_manual/had_fan_only/
had_grace_only branches driving the supplemental ``override_cleared`` emit
decision (Issue #508).
"""

from __future__ import annotations

from custom_components.climate_advisor.override_cancel_outcome import (
    OverrideCancelOutcome,
    decide_override_cancel_outcome,
)


class TestDecideOverrideCancelOutcome:
    def test_nothing_active_noop(self):
        result = decide_override_cancel_outcome(
            manual_override_active=False, fan_override_active=False, grace_active=False
        )
        assert result == OverrideCancelOutcome.NOOP

    def test_manual_override_active_had_manual(self):
        result = decide_override_cancel_outcome(
            manual_override_active=True, fan_override_active=False, grace_active=False
        )
        assert result == OverrideCancelOutcome.HAD_MANUAL

    def test_manual_override_active_beats_fan_and_grace(self):
        """Precedence: manual_override_active wins even when fan and grace are
        also active."""
        result = decide_override_cancel_outcome(
            manual_override_active=True, fan_override_active=True, grace_active=True
        )
        assert result == OverrideCancelOutcome.HAD_MANUAL

    def test_fan_only_active_had_fan_only(self):
        result = decide_override_cancel_outcome(
            manual_override_active=False, fan_override_active=True, grace_active=False
        )
        assert result == OverrideCancelOutcome.HAD_FAN_ONLY

    def test_fan_only_beats_grace(self):
        result = decide_override_cancel_outcome(
            manual_override_active=False, fan_override_active=True, grace_active=True
        )
        assert result == OverrideCancelOutcome.HAD_FAN_ONLY

    def test_grace_only_active_had_grace_only(self):
        """Neither override flag active, only a bare grace period (e.g. a
        fan-off cooldown or window-close grace with no override behind it)."""
        result = decide_override_cancel_outcome(
            manual_override_active=False, fan_override_active=False, grace_active=True
        )
        assert result == OverrideCancelOutcome.HAD_GRACE_ONLY
