"""Unit tests for "does this grace trigger protect a real override?" (Issue #639,
Block 5 Phase 3). Reimplements ``_start_grace_period()``'s one-line
``_grace_protects_override = trigger in _GRACE_TRIGGERS_PROTECTING_OVERRIDE``
determination (Issue #530).
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.override_grace_start import (
    GRACE_TRIGGERS_PROTECTING_OVERRIDE,
    decide_grace_protects_override,
)


class TestDecideGraceProtectsOverride:
    @pytest.mark.parametrize(
        ("trigger", "expected"),
        [
            ("fan_manual_override", True),
            ("override_confirmed", True),
            ("fan_off", False),
            ("dashboard_resume", False),
            ("sensor_closed_resume", False),
            ("nat_vent_exit_resume", False),
            ("physical_drift_correction", False),
            ("some_unknown_trigger", False),
        ],
    )
    def test_default_protecting_triggers(self, trigger, expected):
        assert decide_grace_protects_override(trigger) == expected

    def test_default_protecting_set_contents(self):
        assert frozenset({"fan_manual_override", "override_confirmed"}) == GRACE_TRIGGERS_PROTECTING_OVERRIDE

    def test_custom_protecting_triggers_override_default(self):
        custom = frozenset({"custom_trigger"})
        assert decide_grace_protects_override("custom_trigger", protecting_triggers=custom) is True
        assert decide_grace_protects_override("fan_manual_override", protecting_triggers=custom) is False
