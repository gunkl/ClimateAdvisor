"""Shared sensor-failure helpers in ``temperature.py`` (Issues #1032/#1033).

``next_fallback_episode`` is the one once-per-episode transition used by both the outdoor (#1032)
and indoor (#1033) sensor trackers; ``is_sensor_stale`` and ``read_attribute_temp_f`` are the
shared staleness and attribute-parse primitives.
"""

from __future__ import annotations

import datetime as dt
import sys
from types import SimpleNamespace

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest

from custom_components.climate_advisor.temperature import (
    is_sensor_stale,
    next_fallback_episode,
    read_attribute_temp_f,
    read_state_temp_f,
)

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
EARLIER = NOW - dt.timedelta(minutes=42)


class TestNextFallbackEpisode:
    def test_healthy_and_nothing_outstanding_is_a_noop(self):
        step = next_fallback_episode(None, None, None, NOW)
        assert (step.kind, step.announced, step.since, step.minutes_down) == ("none", None, None, None)

    def test_first_failure_is_a_fallback_and_starts_the_clock(self):
        step = next_fallback_episode("unavailable", None, None, NOW)
        assert (step.kind, step.announced, step.since) == ("fallback", "unavailable", NOW)

    def test_same_reason_again_is_silent_and_keeps_the_original_start(self):
        step = next_fallback_episode("unavailable", "unavailable", EARLIER, NOW)
        assert (step.kind, step.announced, step.since) == ("none", "unavailable", EARLIER)

    def test_reason_change_is_announced_without_resetting_the_clock(self):
        step = next_fallback_episode("stale", "unavailable", EARLIER, NOW)
        assert (step.kind, step.announced, step.since) == ("changed", "stale", EARLIER)

    def test_recovery_reports_minutes_down_and_clears_state(self):
        step = next_fallback_episode(None, "stale", EARLIER, NOW)
        assert (step.kind, step.announced, step.since, step.minutes_down) == ("recovered", None, None, 42)

    def test_recovery_with_unknown_start_has_no_minutes(self):
        step = next_fallback_episode(None, "stale", None, NOW)
        assert (step.kind, step.minutes_down) == ("recovered", None)


class TestIsSensorStale:
    def _state(self, *, reported=None, updated=None):
        return SimpleNamespace(last_reported=reported, last_updated=updated)

    def test_old_report_is_stale_and_recent_is_not(self):
        assert is_sensor_stale(self._state(reported=NOW - dt.timedelta(hours=7)), 6.0, NOW) is True
        assert is_sensor_stale(self._state(reported=NOW - dt.timedelta(hours=5)), 6.0, NOW) is False

    def test_last_reported_wins_over_last_updated(self):
        state = self._state(reported=NOW - dt.timedelta(minutes=5), updated=NOW - dt.timedelta(hours=20))
        assert is_sensor_stale(state, 6.0, NOW) is False

    def test_falls_back_to_last_updated_when_last_reported_missing(self):
        assert is_sensor_stale(self._state(updated=NOW - dt.timedelta(hours=20)), 6.0, NOW) is True

    @pytest.mark.parametrize("state", [None, SimpleNamespace(), SimpleNamespace(last_reported="x", last_updated=None)])
    def test_missing_or_non_datetime_timestamps_fail_open(self, state):
        assert is_sensor_stale(state, 6.0, NOW) is False

    def test_incomparable_clock_fails_open(self):
        naive = dt.datetime(2026, 10, 9, 5, 0)  # naive vs aware raises TypeError on subtraction
        assert is_sensor_stale(SimpleNamespace(last_reported=naive), 6.0, NOW) is False


class TestReadAttributeTempF:
    def _s(self, **attrs):
        return SimpleNamespace(attributes=attrs)

    def test_reasons(self):
        assert read_attribute_temp_f(None, "t", "fahrenheit") == (None, "entity_missing")
        assert read_attribute_temp_f(self._s(), "t", "fahrenheit") == (None, "no_reading")
        assert read_attribute_temp_f(self._s(t=None), "t", "fahrenheit") == (None, "no_reading")
        assert read_attribute_temp_f(self._s(t="warm"), "t", "fahrenheit") == (None, "non_numeric")
        assert read_attribute_temp_f(self._s(t=float("nan")), "t", "fahrenheit") == (None, "non_finite")
        assert read_attribute_temp_f(self._s(t="inf"), "t", "fahrenheit") == (None, "non_finite")

    def test_value_and_celsius_conversion(self):
        assert read_attribute_temp_f(self._s(t=71), "t", "fahrenheit") == (71.0, None)
        value, reason = read_attribute_temp_f(self._s(t=20), "t", "celsius")
        assert reason is None and abs(value - 68.0) < 1e-9

    def test_read_state_temp_f_is_a_delegate_and_now_rejects_nan(self):
        assert read_state_temp_f(self._s(t=71), "t", "fahrenheit") == 71.0
        assert read_state_temp_f(self._s(t="x"), "t", "fahrenheit") is None
        assert read_state_temp_f(self._s(t=float("nan")), "t", "fahrenheit") is None
        assert read_state_temp_f(None, "t", "fahrenheit") is None
