"""Tests for TOU pre-conditioning extended to Away/Vacation occupancy (Issue #899).

Two layers, tested separately per this project's existing scheduler.py convention:
- ``resolve_tou_away_vacation_phase()`` — pure, no HA state.
- ``select_comfort_band()``'s new ``tou_expansion_f`` parameter — pure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.automation import ComfortBand, select_comfort_band
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DAY_TYPE_COLD,
    DAY_TYPE_HOT,
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
    TOU_SETBACK_PRECOND_MAX_DELTA_F,
    VACATION_SETBACK_EXTRA,
)
from custom_components.climate_advisor.scheduler import (
    COST_TAG_HIGH,
    Schedule,
    TOUPhase,
    resolve_tou_away_vacation_phase,
)

_CONFIG = {
    "setback_heat": 63.0,
    "setback_cool": 79.0,
}


def _dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _classification(day_type: str) -> DayClassification:
    return DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=95.0,
        today_low=70.0,
        tomorrow_high=95.0,
        tomorrow_low=70.0,
    )


class TestResolveTouAwayVacationPhase:
    def test_off_hvac_mode_yields_none(self):
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:24", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "off", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.NONE

    def test_low_tag_schedule_never_triggers(self):
        from custom_components.climate_advisor.scheduler import COST_TAG_LOW

        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:24", cost_tag=COST_TAG_LOW)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.NONE

    def test_cooling_worked_example_from_issue_892(self):
        """Issue #892's own real numbers: setpoint 76F (custom setback_cool), ~6F predicted
        drift over a 4.4hr window -> precool to 73F, ceiling widens to 79F for the window."""
        config = {**_CONFIG, "setback_cool": 76.0}
        # k_passive * (indoor - outdoor) = -0.15 * (76 - 95) = 2.85 F/hr (close enough to the
        # issue's own ~1.37 F/hr at a smaller indoor/outdoor delta -- picked here so
        # required_delta lands close to 2.85 * 4.4 = ~12.5, capped at 6.0, to exercise the cap).
        thermal_model = {"k_passive": -0.15, "confidence_k_passive": "high", "k_active_cool": -2.0}
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:24", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "cool", thermal_model, config, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.expansion_f == TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0  # capped at 6.0 -> half=3.0
        assert resolution.target == 76.0 - 3.0  # precool_target
        assert resolution.mode == "cool"
        assert resolution.window_end == _dt(2026, 1, 5, 20, 24)  # start(16:00) + 4.4hr

    def test_heating_symmetric_worked_example(self):
        """Heating-direction counterpart: setback_heat=63F, house losing heat -> preheat
        above 63F, floor lowers below 63F by the same half-delta for the window."""
        thermal_model = {"k_passive": -0.25, "confidence_k_passive": "medium", "k_active_heat": 3.0}
        # k_passive * (indoor - outdoor) = -0.25 * (63 - 20) = -10.75 F/hr passive loss.
        schedule = Schedule(id="s1", name="s", days=("all",), start="17:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 16, 0), 63.0, 20.0, "heat", thermal_model, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        half = TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0  # required_delta (10.75*4=43) capped at 6.0
        assert resolution.expansion_f == half
        assert resolution.target == 63.0 + half  # preheat_target
        assert resolution.mode == "heat"

    def test_required_delta_uses_passive_not_active_rate(self):
        """Regression test (Issue #899 caught error): required_delta must come from
        k_passive * (indoor - outdoor), NOT k_active_cool/k_active_heat. Pick values where
        the two rates diverge enough that using the wrong one changes the result."""
        # passive: -0.05 * (76 - 80) = 0.2 F/hr -> required_delta = 0.2 * 2 = 0.4F (uncapped, small)
        # active_cool: -8.0 F/hr -- if wrongly used for required_delta, would compute an
        # enormous delta immediately clamped to the 6.0F cap, giving half=3.0 instead of 0.2.
        thermal_model = {"k_passive": -0.05, "confidence_k_passive": "high", "k_active_cool": -8.0}
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="18:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 45), 76.0, 80.0, "cool", thermal_model, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.expansion_f == 0.2  # NOT 3.0 (which would result from using k_active_cool)

    def test_fallback_to_cap_when_passive_rate_unknown(self):
        """No confident k_passive -> required_delta falls back to the cap itself, feature
        still acts (does not go inert). No k_active_cool either -> lead time falls back to
        the default 45min -> precondition window opens at 15:15; now=15:30 is inside it."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 30), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.expansion_f == TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0
        assert resolution.target == 79.0 - TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0

    def test_low_confidence_also_falls_back_to_cap(self):
        thermal_model = {"k_passive": -0.15, "confidence_k_passive": "low"}
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "cool", thermal_model, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.expansion_f == TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0

    def test_vacation_mode_uses_wider_setback_extra(self):
        """Vacation's setpoint is setback +/- VACATION_SETBACK_EXTRA, not the plain away edge."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_VACATION
        )
        expected_setpoint = 79.0 + VACATION_SETBACK_EXTRA
        half = TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0
        assert resolution.target == expected_setpoint - half

    def test_window_end_and_expansion_populated_even_when_not_preconditioning(self):
        """Same convention as resolve_tou_phase(): window shape fields are populated
        whenever a qualifying schedule was found (within the lookahead), regardless of
        phase. now=13:00 is within the 4hr lookahead of the 16:00 start but before the
        (no-rate-fallback) 15:15 precondition window opens -> NONE, fields still set."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 13, 0), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.NONE
        assert resolution.window_end == _dt(2026, 1, 5, 20, 0)
        assert resolution.expansion_f is not None

    def test_currently_active_window_resolves_directly_not_via_upcoming_search(self):
        """Regression test: once a window's own start has passed, _next_start_within()
        can no longer find it (future-only search) -- the resolver must fall back to
        resolving directly from the ACTIVE schedule so window_end/expansion_f are still
        populated for the window the coordinator is currently inside."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_away_vacation_phase(
            [schedule], _dt(2026, 1, 5, 17, 0), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.NONE  # not "preconditioning" -- already inside the window
        assert resolution.schedule_id == "s1"
        assert resolution.schedule_start == _dt(2026, 1, 5, 16, 0)
        assert resolution.window_end == _dt(2026, 1, 5, 20, 0)
        assert resolution.expansion_f == TOU_SETBACK_PRECOND_MAX_DELTA_F / 2.0

    def test_no_qualifying_schedule_yields_none_with_no_fields(self):
        resolution = resolve_tou_away_vacation_phase(
            [], _dt(2026, 1, 5, 15, 0), 76.0, 95.0, "cool", {}, _CONFIG, OCCUPANCY_AWAY
        )
        assert resolution.phase == TOUPhase.NONE
        assert resolution.window_end is None
        assert resolution.expansion_f is None


class TestSelectComfortBandTouExpansion:
    def test_default_zero_expansion_matches_pre_899_behavior(self):
        """tou_expansion_f defaults to 0.0 -- away band is byte-identical to before this
        feature existed (regression guard for the other 9 callers)."""
        band = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_AWAY,
            in_sleep_window=False,
            aggressive_savings=False,
        )
        assert band.floor == 63.0
        assert band.ceiling == 79.0

    def test_cooling_day_away_widens_ceiling_only(self):
        band = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_AWAY,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=3.0,
        )
        assert band.ceiling == 79.0 + 3.0
        assert band.floor == 63.0  # floor untouched on a cooling day

    def test_heating_day_away_widens_floor_only(self):
        band = select_comfort_band(
            _classification(DAY_TYPE_COLD),
            _CONFIG,
            occupancy_mode=OCCUPANCY_AWAY,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=2.0,
        )
        assert band.floor == 63.0 - 2.0
        assert band.ceiling == 79.0  # ceiling untouched on a heating day

    def test_vacation_expansion_applies_on_top_of_vacation_setback_extra(self):
        band = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_VACATION,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=3.0,
        )
        assert band.ceiling == 79.0 + VACATION_SETBACK_EXTRA + 3.0

    def test_expansion_is_noop_for_home_occupancy(self):
        """tou_expansion_f only ever matters for away/vacation -- Home/Guest's comfort
        band must be completely unaffected even if a nonzero value is passed."""
        band_with = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_HOME,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=5.0,
        )
        band_without = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_HOME,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=0.0,
        )
        assert band_with.floor == band_without.floor
        assert band_with.ceiling == band_without.ceiling

    def test_returns_comfort_band_instance(self):
        band = select_comfort_band(
            _classification(DAY_TYPE_HOT),
            _CONFIG,
            occupancy_mode=OCCUPANCY_AWAY,
            in_sleep_window=False,
            aggressive_savings=False,
            tou_expansion_f=1.0,
        )
        assert isinstance(band, ComfortBand)
        assert "tou_expansion=1.0" in band.reason
