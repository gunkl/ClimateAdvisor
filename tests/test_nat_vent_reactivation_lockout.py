"""Inline unit tests for the pure nat-vent reactivation lockout core (architecture-reset Step 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.nat_vent_reactivation_lockout import is_reactivation_locked_out

_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def test_no_lockout_when_no_prior_exit():
    assert is_reactivation_locked_out(outdoor_exit_time=None, now=_NOW, lockout_seconds=600) is False


def test_locked_out_when_elapsed_less_than_lockout():
    exit_time = _NOW - timedelta(seconds=100)
    assert is_reactivation_locked_out(outdoor_exit_time=exit_time, now=_NOW, lockout_seconds=600) is True


def test_boundary_elapsed_equal_to_lockout_is_not_locked_out():
    """Non-strict '<': elapsed == lockout_seconds means the window just closed."""
    exit_time = _NOW - timedelta(seconds=600)
    assert is_reactivation_locked_out(outdoor_exit_time=exit_time, now=_NOW, lockout_seconds=600) is False


def test_not_locked_out_when_elapsed_exceeds_lockout():
    exit_time = _NOW - timedelta(seconds=601)
    assert is_reactivation_locked_out(outdoor_exit_time=exit_time, now=_NOW, lockout_seconds=600) is False
