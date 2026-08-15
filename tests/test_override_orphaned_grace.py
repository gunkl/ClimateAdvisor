"""Unit tests for the two mirror-image grace/override watchdogs (Issue #639,
Block 5 Phase 3): orphaned grace (#508/#530) and stuck override (#321).

Includes a property-style mutual-exclusion proof: for every reachable
combination of the shared/relevant boolean flags, ``decide_orphaned_grace()``
and ``decide_stuck_override()`` must never both return True for the same input
tuple. They are structurally exclusive by construction — ``decide_orphaned_grace``
requires ``grace_active=True``, while ``decide_stuck_override`` short-circuits
to False whenever ``grace_active=True`` (it only fires when grace_active=False)
— so no shared flag combination can satisfy both at once.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.override_orphaned_grace import (
    decide_orphaned_grace,
    decide_stuck_override,
)

_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
_PAST = _NOW - timedelta(minutes=10)
_FUTURE = _NOW + timedelta(minutes=10)


class TestDecideOrphanedGrace:
    def test_grace_protecting_with_no_override_left_is_orphaned(self):
        result = decide_orphaned_grace(
            grace_active=True,
            grace_protects_override=True,
            manual_override_active=False,
            fan_override_active=False,
        )
        assert result is True

    def test_grace_inactive_never_orphaned(self):
        result = decide_orphaned_grace(
            grace_active=False,
            grace_protects_override=True,
            manual_override_active=False,
            fan_override_active=False,
        )
        assert result is False

    def test_grace_not_protecting_override_never_orphaned(self):
        """Fan-off/window-close/dashboard-resume grace (never override-protecting)
        must never read as orphaned even with no override flags set."""
        result = decide_orphaned_grace(
            grace_active=True,
            grace_protects_override=False,
            manual_override_active=False,
            fan_override_active=False,
        )
        assert result is False

    def test_manual_override_still_active_not_orphaned(self):
        result = decide_orphaned_grace(
            grace_active=True,
            grace_protects_override=True,
            manual_override_active=True,
            fan_override_active=False,
        )
        assert result is False

    def test_fan_override_still_active_not_orphaned(self):
        result = decide_orphaned_grace(
            grace_active=True,
            grace_protects_override=True,
            manual_override_active=False,
            fan_override_active=True,
        )
        assert result is False


class TestDecideStuckOverride:
    def test_manual_override_active_grace_not_running_end_time_past(self):
        result = decide_stuck_override(manual_override_active=True, grace_active=False, grace_end_time=_PAST, now=_NOW)
        assert result is True

    def test_no_manual_override_not_stuck(self):
        result = decide_stuck_override(manual_override_active=False, grace_active=False, grace_end_time=_PAST, now=_NOW)
        assert result is False

    def test_grace_still_running_not_stuck(self):
        result = decide_stuck_override(manual_override_active=True, grace_active=True, grace_end_time=_PAST, now=_NOW)
        assert result is False

    def test_no_grace_end_time_not_stuck(self):
        result = decide_stuck_override(manual_override_active=True, grace_active=False, grace_end_time=None, now=_NOW)
        assert result is False

    def test_grace_end_time_still_in_future_not_stuck(self):
        result = decide_stuck_override(
            manual_override_active=True, grace_active=False, grace_end_time=_FUTURE, now=_NOW
        )
        assert result is False

    def test_grace_end_time_exactly_now_not_stuck(self):
        """now > grace_end_time is strict — exactly equal is not yet stuck."""
        result = decide_stuck_override(manual_override_active=True, grace_active=False, grace_end_time=_NOW, now=_NOW)
        assert result is False


class TestMutualExclusion:
    def test_never_both_true_across_all_reachable_flag_combinations(self):
        """Exhaustive sweep of the shared boolean flags (grace_active,
        manual_override_active, fan_override_active, grace_protects_override)
        plus every grace_end_time shape (past/future/None/exactly-now) — proves
        the two predicates never simultaneously fire for any input tuple."""
        bool_flags = list(itertools.product([True, False], repeat=4))
        end_times = [_PAST, _FUTURE, None, _NOW]

        violations = []
        for grace_active, manual_override_active, fan_override_active, grace_protects_override in bool_flags:
            for grace_end_time in end_times:
                orphaned = decide_orphaned_grace(
                    grace_active=grace_active,
                    grace_protects_override=grace_protects_override,
                    manual_override_active=manual_override_active,
                    fan_override_active=fan_override_active,
                )
                stuck = decide_stuck_override(
                    manual_override_active=manual_override_active,
                    grace_active=grace_active,
                    grace_end_time=grace_end_time,
                    now=_NOW,
                )
                if orphaned and stuck:
                    violations.append(
                        (
                            grace_active,
                            manual_override_active,
                            fan_override_active,
                            grace_protects_override,
                            grace_end_time,
                        )
                    )

        assert not violations, f"Found {len(violations)} tuples where both predicates fired: {violations}"

    def test_docstring_reasoning_holds_for_the_two_fixed_documented_scenarios(self):
        """Direct check of the module docstring's own claim: decide_orphaned_grace
        requires grace_active=True; decide_stuck_override requires
        grace_active=False. No single grace_active value can satisfy both."""
        orphaned = decide_orphaned_grace(
            grace_active=True,
            grace_protects_override=True,
            manual_override_active=False,
            fan_override_active=False,
        )
        stuck = decide_stuck_override(manual_override_active=True, grace_active=True, grace_end_time=_PAST, now=_NOW)
        assert orphaned is True
        assert stuck is False  # can't be stuck while grace_active is True
