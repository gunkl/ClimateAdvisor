"""Tests for AC duty-cycle secondary solar phase estimator (Issue #312).

Covers:
  - _is_ac_duty_solar_day(): quality filter (5 reject paths + 1 pass)
  - _estimate_ac_duty_solar_phase(): peak-hour duty-fraction estimator
  - _resolve_solar_phase_offset(): resolver in learning.py
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entries(
    hours_hvac: dict,
    setpoint: float = 74.0,
    indoor_base: float = 74.0,
    outdoor: float = 85.0,
) -> list[dict]:
    """Build synthetic chart_log entries for a single day.

    hours_hvac maps hour (int) -> 'cool' | 'off'.
    Covers 08:30-21:30 (one entry per hour) to give a realistic daily spread.
    """
    entries = []
    for h in range(8, 22):
        hvac = hours_hvac.get(h, "off")
        indoor = indoor_base + (0.5 if hvac == "cool" else 0.0)
        entry = {
            "ts": f"2026-06-13T{h:02d}:30:00-07:00",
            "hvac": hvac,
            "indoor": indoor,
            "outdoor": outdoor,
            "fan": "off",
            "windows_open": False,
            "setpoint_cool": setpoint,
        }
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Import production functions (will fail until Phase 1 implementation)
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY,
    REJECT_AC_NO_COOL_SETPOINTS,
    REJECT_AC_NO_SETPOINT_BREACH,
    REJECT_AC_SETPOINT_OUT_OF_RANGE,
    REJECT_AC_SETPOINT_UNSTABLE,
    THERMAL_SOLAR_PHASE_OFFSET_MIN,
)
from custom_components.climate_advisor.coordinator import (  # noqa: E402
    _estimate_ac_duty_solar_phase,
    _is_ac_duty_solar_day,
)

# ---------------------------------------------------------------------------
# Tests: _is_ac_duty_solar_day
# ---------------------------------------------------------------------------


class TestIsAcDutySolarDay:
    def test_reject_no_cool_setpoints(self):
        """REJECT when no entry has a setpoint_cool field."""
        entries = _make_entries({11: "cool", 12: "cool", 13: "cool", 14: "cool"})
        for e in entries:
            e.pop("setpoint_cool", None)
        ok, reason = _is_ac_duty_solar_day(entries)
        assert not ok
        assert reason == REJECT_AC_NO_COOL_SETPOINTS

    def test_reject_setpoint_out_of_range(self):
        """REJECT when setpoint is outside [68, 80]F."""
        entries = _make_entries(
            {11: "cool", 12: "cool", 13: "cool", 14: "cool"},
            setpoint=85.0,
        )
        ok, reason = _is_ac_duty_solar_day(entries)
        assert not ok
        assert reason == REJECT_AC_SETPOINT_OUT_OF_RANGE

    def test_reject_setpoint_unstable(self):
        """REJECT when setpoint varies > 1.5F during 11:00-18:00 window."""
        entries = _make_entries(
            {11: "cool", 12: "cool", 13: "cool", 14: "cool"},
            setpoint=74.0,
        )
        # Introduce a 4F spread within the stability window
        for e in entries:
            if e["ts"].startswith("2026-06-13T15"):
                e["setpoint_cool"] = 78.0
        ok, reason = _is_ac_duty_solar_day(entries)
        assert not ok
        assert reason == REJECT_AC_SETPOINT_UNSTABLE

    def test_reject_insufficient_midday_activity(self):
        """REJECT when fewer than 4 cool entries in 11:00-16:00."""
        # Only 3 cool entries in 11-16 window
        entries = _make_entries(
            {11: "cool", 12: "cool", 13: "cool"},
            setpoint=74.0,
            indoor_base=75.5,
        )
        ok, reason = _is_ac_duty_solar_day(entries)
        assert not ok
        assert reason == REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY

    def test_reject_no_setpoint_breach(self):
        """REJECT when no 11-16 entry has indoor > setpoint."""
        # indoor_base=74.0 < setpoint=78.0 — no breach possible
        entries = _make_entries(
            {11: "cool", 12: "cool", 13: "cool", 14: "cool"},
            setpoint=78.0,
            indoor_base=74.0,
        )
        ok, reason = _is_ac_duty_solar_day(entries)
        assert not ok
        assert reason == REJECT_AC_NO_SETPOINT_BREACH

    def test_pass_valid_ac_duty_day(self):
        """PASS when all quality criteria are met."""
        entries = _make_entries(
            {11: "cool", 12: "cool", 13: "cool", 14: "cool"},
            setpoint=74.0,
            indoor_base=75.5,  # 1.5F above setpoint -> breach confirmed
        )
        ok, reason = _is_ac_duty_solar_day(entries)
        assert ok, f"Expected pass, got reject: {reason}"


# ---------------------------------------------------------------------------
# Tests: _estimate_ac_duty_solar_phase
# ---------------------------------------------------------------------------


class TestEstimateAcDutySolarPhase:
    def _build_entries(self, cool_hours: list[int], all_hours: list[int]) -> list[dict]:
        """Build entries with cool at cool_hours and 'off' elsewhere, all within all_hours."""
        entries = []
        for h in all_hours:
            for m in [0, 30]:
                hvac = "cool" if h in cool_hours else "off"
                entries.append(
                    {
                        "ts": f"2026-06-13T{h:02d}:{m:02d}:00-07:00",
                        "hvac": hvac,
                    }
                )
        return entries

    def test_estimate_peak_at_14(self):
        """Peak duty at hour 14 -> offset = 14 - 13 = 1.0."""
        # Only 14 is "cool"; 11, 12, 13, 15 are "off" — duty fraction highest at 14
        entries = self._build_entries(cool_hours=[14], all_hours=[11, 12, 13, 14, 15])
        result = _estimate_ac_duty_solar_phase(entries)
        assert result == pytest.approx(1.0)

    def test_estimate_peak_at_13(self):
        """Peak duty at hour 13 -> offset clamped to THERMAL_SOLAR_PHASE_OFFSET_MIN."""
        entries = self._build_entries(cool_hours=[13], all_hours=[11, 12, 13, 14, 15])
        result = _estimate_ac_duty_solar_phase(entries)
        assert result is not None
        # offset = 13 - 13 = 0, clamped to MIN
        assert result >= THERMAL_SOLAR_PHASE_OFFSET_MIN

    def test_estimate_no_cool_entries(self):
        """Returns None when no cool entries exist in the peak window."""
        entries = [{"ts": "2026-06-13T12:00:00-07:00", "hvac": "off"}]
        result = _estimate_ac_duty_solar_phase(entries)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _resolve_solar_phase_offset (in learning.py)
# ---------------------------------------------------------------------------


class TestResolveSolarPhaseOffset:
    def setup_method(self):
        import datetime

        from custom_components.climate_advisor.const import THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        self._resolve = _resolve_solar_phase_offset
        self._default = THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
        self._today = datetime.date.today().isoformat()

    def test_primary_wins(self):
        """Primary solar_phase_offset_h (fresh) is used when not None, even if secondary present."""
        cache = {
            "solar_phase_offset_h": 3.0,
            "solar_phase_offset_last_obs_date": self._today,
            "solar_phase_offset_ac_h": 1.0,
            "solar_phase_offset_ac_last_obs_date": self._today,
            "solar_phase_offset_ac_obs_count": 5,
        }
        assert self._resolve(cache) == pytest.approx(3.0)

    def test_secondary_fallback_when_enough_obs(self):
        """Secondary (AC, fresh) is used when primary is None and obs_count >= 3."""
        cache = {
            "solar_phase_offset_h": None,
            "solar_phase_offset_last_obs_date": None,
            "solar_phase_offset_ac_h": 1.5,
            "solar_phase_offset_ac_last_obs_date": self._today,
            "solar_phase_offset_ac_obs_count": 3,
        }
        assert self._resolve(cache) == pytest.approx(1.5)

    def test_default_when_both_absent(self):
        """Default returned when both primary and secondary are None."""
        cache = {
            "solar_phase_offset_h": None,
            "solar_phase_offset_last_obs_date": None,
            "solar_phase_offset_ac_h": None,
            "solar_phase_offset_ac_last_obs_date": None,
            "solar_phase_offset_ac_obs_count": 0,
        }
        assert self._resolve(cache) == pytest.approx(self._default)

    def test_default_when_secondary_insufficient_obs(self):
        """Default returned when secondary has value but obs_count < 3."""
        cache = {
            "solar_phase_offset_h": None,
            "solar_phase_offset_last_obs_date": None,
            "solar_phase_offset_ac_h": 1.0,
            "solar_phase_offset_ac_last_obs_date": self._today,
            "solar_phase_offset_ac_obs_count": 2,
        }
        assert self._resolve(cache) == pytest.approx(self._default)


# ---------------------------------------------------------------------------
# Tests: staleness gate in _resolve_solar_phase_offset (Issue #312 Phase 2)
# ---------------------------------------------------------------------------


class TestSolarPhaseOffsetStaleness:
    """_resolve_solar_phase_offset must treat parameters older than THERMAL_PARAM_STALE_DAYS as None."""

    def _cache(self, primary=None, primary_date=None, secondary=None, secondary_date=None, ac_obs=0):
        return {
            "solar_phase_offset_h": primary,
            "solar_phase_offset_last_obs_date": primary_date,
            "solar_phase_offset_ac_h": secondary,
            "solar_phase_offset_ac_last_obs_date": secondary_date,
            "solar_phase_offset_ac_obs_count": ac_obs,
        }

    def _today(self):
        from datetime import date

        return date.today().isoformat()

    def _days_ago(self, n):
        from datetime import date, timedelta

        return (date.today() - timedelta(days=n)).isoformat()

    def test_fresh_primary_is_used(self):
        """Primary with recent last_obs_date (within 90 days) must be returned."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(primary=3.0, primary_date=self._days_ago(1))
        assert _resolve_solar_phase_offset(cache) == pytest.approx(3.0)

    def test_stale_primary_falls_to_secondary(self):
        """Primary older than 90 days must be masked; fresh secondary with obs>=3 used instead."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(
            primary=3.0,
            primary_date=self._days_ago(91),
            secondary=1.5,
            secondary_date=self._days_ago(1),
            ac_obs=3,
        )
        assert _resolve_solar_phase_offset(cache) == pytest.approx(1.5)

    def test_primary_with_no_date_falls_to_stale_value(self):
        """Primary with last_obs_date=None (pre-312 migration) is stale but still used — better than default."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(primary=3.0, primary_date=None)  # no date = stale, but value exists
        # Stale home-specific data beats generic default
        assert _resolve_solar_phase_offset(cache) == pytest.approx(3.0)

    def test_stale_primary_used_over_stale_secondary(self):
        """Both primary and secondary stale → stale primary returned (better than generic default)."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(
            primary=3.0,
            primary_date=self._days_ago(91),
            secondary=1.5,
            secondary_date=self._days_ago(91),
            ac_obs=5,
        )
        assert _resolve_solar_phase_offset(cache) == pytest.approx(3.0)

    def test_stale_primary_secondary_insufficient_obs(self):
        """Stale primary + secondary with ac_obs < 3 → stale primary used (obs gate still applies to secondary)."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(
            primary=3.0,
            primary_date=self._days_ago(91),
            secondary=1.5,
            secondary_date=self._days_ago(1),
            ac_obs=2,
        )
        # Secondary disqualified by obs count; stale primary is the best available data
        assert _resolve_solar_phase_offset(cache) == pytest.approx(3.0)

    def test_primary_exactly_at_threshold_is_fresh(self):
        """Primary at exactly 90 days (not 91) must still be returned (inclusive threshold)."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(primary=3.0, primary_date=self._days_ago(90))
        assert _resolve_solar_phase_offset(cache) == pytest.approx(3.0)

    def test_stale_secondary_used_when_no_primary(self):
        """Stale secondary with obs>=3 and no primary → stale secondary used (better than default)."""
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(
            primary=None,
            primary_date=None,
            secondary=1.5,
            secondary_date=self._days_ago(91),
            ac_obs=5,
        )
        assert _resolve_solar_phase_offset(cache) == pytest.approx(1.5)

    def test_nothing_learned_returns_default(self):
        """When both primary and secondary are None (never observed), return the generic default."""
        from custom_components.climate_advisor.const import THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
        from custom_components.climate_advisor.learning import _resolve_solar_phase_offset

        cache = self._cache(primary=None, primary_date=None, secondary=None, secondary_date=None, ac_obs=0)
        assert _resolve_solar_phase_offset(cache) == pytest.approx(THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT)
