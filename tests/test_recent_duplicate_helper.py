"""Tests for AutomationEngine._recent_duplicate() (Issue #591).

The shared decision-record dedup helper, generalizing Issue #444's
_last_comfort_band_signature pattern to cover all 8 confirmed-safe sites migrated
in #591 (2 of the original 10 candidates — occupancy_setback and
hvac_write_blocked_whf_active — were found NOT to be safe to dedup and were
reverted; see the comments at their call sites in automation.py and
tests/test_pending_scenarios.py's wakeup_preserves_whf_manual_override /
away_morning_wakeup_skipped_assertion goldens, which caught the regression).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import patch

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402


def _bare_engine() -> AutomationEngine:
    """A bare, un-initialized AutomationEngine — _recent_duplicate() must work even
    when __init__ never ran (mirrors real production instantiation via HA config flow
    as well as the test-suite's object.__new__() partial-instantiation pattern)."""
    return object.__new__(AutomationEngine)


class TestRecentDuplicateContentKeyed:
    """window_seconds=None (the default) — permanent, content-keyed dedup."""

    def test_first_call_is_never_a_duplicate(self):
        engine = _bare_engine()
        assert engine._recent_duplicate("k", ("a", 1)) is False

    def test_identical_signature_is_duplicate_regardless_of_elapsed_time(self):
        engine = _bare_engine()
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        ):
            assert engine._recent_duplicate("k", ("a", 1)) is False
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC),  # 5 hours later
        ):
            assert engine._recent_duplicate("k", ("a", 1)) is True

    def test_changed_signature_is_never_a_duplicate(self):
        engine = _bare_engine()
        assert engine._recent_duplicate("k", ("a", 1)) is False
        assert engine._recent_duplicate("k", ("a", 2)) is False

    def test_signature_change_then_repeat_is_duplicate_again(self):
        engine = _bare_engine()
        assert engine._recent_duplicate("k", ("a", 1)) is False
        assert engine._recent_duplicate("k", ("a", 2)) is False
        assert engine._recent_duplicate("k", ("a", 2)) is True

    def test_independent_keys_do_not_interfere(self):
        engine = _bare_engine()
        assert engine._recent_duplicate("key_one", ("a", 1)) is False
        assert engine._recent_duplicate("key_two", ("a", 1)) is False
        assert engine._recent_duplicate("key_two", ("a", 1)) is True
        # key_one's own signature is untouched by key_two's activity.
        assert engine._recent_duplicate("key_one", ("a", 1)) is True


class TestRecentDuplicateWindowed:
    """window_seconds set — Issue #591's state_contradiction_warning shape (30 min)."""

    def test_duplicate_within_window(self):
        engine = _bare_engine()
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        ):
            assert engine._recent_duplicate("k", ("a",), window_seconds=1800) is False
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),  # 10 min later
        ):
            assert engine._recent_duplicate("k", ("a",), window_seconds=1800) is True

    def test_not_duplicate_after_window_expires(self):
        engine = _bare_engine()
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        ):
            assert engine._recent_duplicate("k", ("a",), window_seconds=1800) is False
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 1, 1, 0, 31, 0, tzinfo=UTC),  # 31 min later
        ):
            assert engine._recent_duplicate("k", ("a",), window_seconds=1800) is False


class TestRecentDuplicateBareInstanceSafety:
    """Must not raise on an engine that never ran __init__ or on a MagicMock self —
    both real production and test-suite instantiation patterns rely on this."""

    def test_works_on_object_new_without_init(self):
        engine = object.__new__(AutomationEngine)
        # No AttributeError from missing _dedup_signatures/_dedup_timestamps.
        assert engine._recent_duplicate("k", ("a",)) is False
        assert engine._recent_duplicate("k", ("a",)) is True
