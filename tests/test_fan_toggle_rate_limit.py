"""Tests for Issue #731 Phase 1: fan toggle rapid-cycling rate-limit decision.

Pure reimplementation of _fan_toggle_rate_limited() (Issue #641/#649). Exhaustive
over the ALLOW/DEFER_NEW/DEFER_DUPLICATE outcome space, including the exact
elapsed-time boundary and the direction-mismatch case that distinguishes
DEFER_NEW from DEFER_DUPLICATE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.climate_advisor.fan_toggle_rate_limit import (
    FanToggleRateLimitInputs,
    FanToggleRateLimitOutcome,
    decide_fan_toggle_rate_limit,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_MIN_INTERVAL_S = 300.0


def _inputs(
    *,
    last_toggle_command_time: datetime | None = None,
    action: str = "activate",
    rate_limited_until: datetime | None = None,
    rate_limited_direction: str | None = None,
    min_interval_s: float = _MIN_INTERVAL_S,
    now: datetime = _NOW,
) -> FanToggleRateLimitInputs:
    return FanToggleRateLimitInputs(
        last_toggle_command_time=last_toggle_command_time,
        action=action,
        rate_limited_until=rate_limited_until,
        rate_limited_direction=rate_limited_direction,
        min_interval_s=min_interval_s,
        now=now,
    )


class TestAllow:
    def test_allow_when_no_prior_command_none(self) -> None:
        outcome, applies_at = decide_fan_toggle_rate_limit(_inputs(last_toggle_command_time=None))
        assert outcome == FanToggleRateLimitOutcome.ALLOW
        assert applies_at is None

    def test_allow_when_prior_command_is_not_a_datetime(self) -> None:
        # isinstance guard: a MagicMock (unpatched clock in some other test's stub)
        # must be treated as "no prior command", never raise.
        outcome, applies_at = decide_fan_toggle_rate_limit(_inputs(last_toggle_command_time=MagicMock()))
        assert outcome == FanToggleRateLimitOutcome.ALLOW
        assert applies_at is None

    def test_allow_when_elapsed_exceeds_min_interval(self) -> None:
        last = _NOW - timedelta(seconds=_MIN_INTERVAL_S + 60)
        outcome, applies_at = decide_fan_toggle_rate_limit(_inputs(last_toggle_command_time=last))
        assert outcome == FanToggleRateLimitOutcome.ALLOW
        assert applies_at is None

    def test_allow_on_exact_boundary_elapsed_equals_min_interval(self) -> None:
        last = _NOW - timedelta(seconds=_MIN_INTERVAL_S)
        outcome, applies_at = decide_fan_toggle_rate_limit(_inputs(last_toggle_command_time=last))
        assert outcome == FanToggleRateLimitOutcome.ALLOW
        assert applies_at is None


class TestDeferNew:
    def test_defer_new_on_first_call_within_window(self) -> None:
        last = _NOW - timedelta(seconds=60)
        outcome, applies_at = decide_fan_toggle_rate_limit(
            _inputs(last_toggle_command_time=last, action="activate", rate_limited_until=None)
        )
        assert outcome == FanToggleRateLimitOutcome.DEFER_NEW
        assert applies_at == last + timedelta(seconds=_MIN_INTERVAL_S)

    def test_defer_new_just_under_boundary(self) -> None:
        last = _NOW - timedelta(seconds=_MIN_INTERVAL_S - 1)
        outcome, applies_at = decide_fan_toggle_rate_limit(_inputs(last_toggle_command_time=last))
        assert outcome == FanToggleRateLimitOutcome.DEFER_NEW
        assert applies_at == last + timedelta(seconds=_MIN_INTERVAL_S)

    def test_defer_new_when_direction_differs_even_with_matching_applies_at(self) -> None:
        last = _NOW - timedelta(seconds=60)
        applies_at = last + timedelta(seconds=_MIN_INTERVAL_S)
        outcome, result_applies_at = decide_fan_toggle_rate_limit(
            _inputs(
                last_toggle_command_time=last,
                action="deactivate",
                rate_limited_until=applies_at,
                rate_limited_direction="activate",
            )
        )
        assert outcome == FanToggleRateLimitOutcome.DEFER_NEW
        assert result_applies_at == applies_at


class TestDeferDuplicate:
    def test_defer_duplicate_on_second_call_same_window_same_action(self) -> None:
        last = _NOW - timedelta(seconds=60)
        applies_at = last + timedelta(seconds=_MIN_INTERVAL_S)
        outcome, result_applies_at = decide_fan_toggle_rate_limit(
            _inputs(
                last_toggle_command_time=last,
                action="activate",
                rate_limited_until=applies_at,
                rate_limited_direction="activate",
            )
        )
        assert outcome == FanToggleRateLimitOutcome.DEFER_DUPLICATE
        assert result_applies_at == applies_at

    def test_defer_new_not_duplicate_when_rate_limited_until_does_not_match(self) -> None:
        # rate_limited_until points at a DIFFERENT (earlier/stale) window than the
        # one this call would compute -> not a duplicate of THIS window.
        last = _NOW - timedelta(seconds=60)
        stale_applies_at = last - timedelta(seconds=10)
        outcome, applies_at = decide_fan_toggle_rate_limit(
            _inputs(
                last_toggle_command_time=last,
                action="activate",
                rate_limited_until=stale_applies_at,
                rate_limited_direction="activate",
            )
        )
        assert outcome == FanToggleRateLimitOutcome.DEFER_NEW
        assert applies_at == last + timedelta(seconds=_MIN_INTERVAL_S)
