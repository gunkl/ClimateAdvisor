"""Tests for the CA-fan-running suppression predicate (Issue #458).

Consolidates two independently-drifted implementations (coordinator.py's
flag-based check, ai_skills_activity.py's status-string allow-list — the
latter was missing "active (unconfirmed)", added by #423) into a single
source of truth: fan_status.is_ca_fan_running().
"""

from __future__ import annotations

from custom_components.climate_advisor.fan_status import (
    FAN_STATUS_ACTIVE_VALUES,
    is_ca_fan_running,
)

# All seven documented fan-status values (CLAUDE.md "Fan Status Values" table).
_ALL_FAN_STATUS_VALUES = {
    "active",
    "active (unconfirmed)",
    "running (manual override)",
    "running (untracked)",
    "inactive",
    "off (manual override)",
    "disabled",
}

_EXPECTED_ACTIVE = {
    "active",
    "active (unconfirmed)",
    "running (manual override)",
    "running (untracked)",
}


class TestIsCaFanRunning:
    def test_active_values_return_true(self):
        for status in _EXPECTED_ACTIVE:
            assert is_ca_fan_running(status) is True, f"{status!r} must be treated as CA fan running"

    def test_non_active_values_return_false(self):
        for status in _ALL_FAN_STATUS_VALUES - _EXPECTED_ACTIVE:
            assert is_ca_fan_running(status) is False, f"{status!r} must NOT be treated as CA fan running"

    def test_active_unconfirmed_regression(self):
        """Issue #458: this specific value was missing from ai_skills_activity.py's
        allow-list — the concrete bug this consolidation fixes."""
        assert is_ca_fan_running("active (unconfirmed)") is True

    def test_unknown_status_returns_false(self):
        assert is_ca_fan_running("some-unexpected-string") is False

    def test_constant_matches_function(self):
        """FAN_STATUS_ACTIVE_VALUES and is_ca_fan_running() must agree — the
        constant is exposed for callers that need the set itself, not just
        membership testing."""
        for status in _ALL_FAN_STATUS_VALUES:
            assert (status in FAN_STATUS_ACTIVE_VALUES) == is_ca_fan_running(status)
