"""Tests for the thermostat program-selection function (Issue #249, Phase 2).

`select_thermostat_program` is the pure decision layer: it maps a day classification + occupancy +
sleep window + savings posture + thermostat capability into a concrete `ThermostatProgram` (mode +
band). It performs NO HA state access and changes NO production behavior in this phase (it is unused
until P3 wiring). These tests are the all-homes matrix: thermostat type × day type × occupancy ×
sleep × aggressive.

Owner-refined model under test: a band-capable thermostat stays in ONE stable `heat_cool` mode and
the plan is expressed purely as the [low, high] band, with a side "disabled" by pushing its setpoint
to a safety setback (the "heat=61 on a warm day" rule) rather than switching modes. Single-mode
thermostats arm the single threatened edge; `mode="off"` is the graceful fallback.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.climate_advisor.automation import (
    _in_sleep_window,
    parse_thermostat_capabilities,
    select_thermostat_program,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
)

# Fixed config so expected band edges are explicit numbers.
CONFIG = {
    "comfort_heat": 70.0,
    "comfort_cool": 74.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_heat": 66.0,
    "sleep_cool": 78.0,
}

# Capability fixtures (built through the P1 parser for realism).
BAND = parse_thermostat_capabilities(["off", "heat", "cool", "heat_cool"], CLIMATE_FEATURE_TARGET_TEMP_RANGE)
COOL_ONLY = parse_thermostat_capabilities(["off", "cool"], 1)
HEAT_ONLY = parse_thermostat_capabilities(["off", "heat"], 1)
UNKNOWN = parse_thermostat_capabilities(None, None)


def _classification(day_type: str, *, pre_condition_target: float | None = None) -> DayClassification:
    """Build a real DayClassification; hvac_mode is computed from day_type by the classifier."""
    c = DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0,
        today_low=60.0,
        tomorrow_high=80.0,
        tomorrow_low=60.0,
    )
    if pre_condition_target is not None:
        c.pre_condition = True
        c.pre_condition_target = pre_condition_target
    return c


def _program(day_type, caps, *, occupancy=OCCUPANCY_HOME, sleep=False, aggressive=False, pre=None):
    return select_thermostat_program(
        _classification(day_type, pre_condition_target=pre),
        CONFIG,
        occupancy_mode=occupancy,
        capabilities=caps,
        in_sleep_window=sleep,
        aggressive_savings=aggressive,
    )


# ---------------------------------------------------------------------------
# Band-capable thermostat (heat_cool) — the preferred, always-on band
# ---------------------------------------------------------------------------


class TestBandCapable:
    def test_warm_day_arms_ceiling_suppresses_floor(self):
        """WARM (off): hold comfort_cool ceiling; floor pushed to setback_heat (the heat=61 rule)."""
        p = _program(DAY_TYPE_WARM, BAND)
        assert p.mode == "heat_cool"
        assert p.setpoint_low == 60.0  # suppressed floor (safety)
        assert p.setpoint_high == 74.0  # active ceiling = comfort_cool
        assert p.setpoint is None

    def test_mild_day_same_as_warm(self):
        p = _program(DAY_TYPE_MILD, BAND)
        assert (p.mode, p.setpoint_low, p.setpoint_high) == ("heat_cool", 60.0, 74.0)

    def test_hot_day_defends_ceiling(self):
        """HOT (cool) defends the ceiling. The classifier sets a pre-cool offset on hot days, so the
        armed ceiling is comfort_cool plus that (negative) offset; the floor is suppressed."""
        c = _classification(DAY_TYPE_HOT)
        p = select_thermostat_program(
            c,
            CONFIG,
            occupancy_mode=OCCUPANCY_HOME,
            capabilities=BAND,
            in_sleep_window=False,
            aggressive_savings=False,
        )
        offset = c.pre_condition_target if (c.pre_condition_target and c.pre_condition_target < 0) else 0.0
        assert p.setpoint_high == 74.0 + offset
        assert p.setpoint_high <= 74.0  # never above the comfort ceiling on a hot day
        assert p.setpoint_low == 60.0

    def test_hot_day_precool_lowers_ceiling(self):
        p = _program(DAY_TYPE_HOT, BAND, pre=-2.0)
        assert p.setpoint_high == 72.0  # comfort_cool 74 + (-2) pre-cool offset
        assert p.setpoint_low == 60.0

    def test_cold_day_arms_floor_suppresses_ceiling(self):
        """COLD (heat): hold comfort_heat floor; ceiling pushed to setback_cool."""
        p = _program(DAY_TYPE_COLD, BAND)
        assert (p.setpoint_low, p.setpoint_high) == (70.0, 80.0)

    def test_cool_day_arms_floor(self):
        p = _program(DAY_TYPE_COOL, BAND)
        assert (p.setpoint_low, p.setpoint_high) == (70.0, 80.0)

    def test_aggressive_savings_widens_active_ceiling(self):
        """Warm + aggressive → ceiling raised by the savings margin (74 + 2)."""
        p = _program(DAY_TYPE_WARM, BAND, aggressive=True)
        assert p.setpoint_high == 76.0
        assert p.setpoint_low == 60.0  # suppressed floor unaffected by margin

    def test_aggressive_savings_widens_active_floor(self):
        """Cold + aggressive → floor lowered by the savings margin (70 - 2)."""
        p = _program(DAY_TYPE_COLD, BAND, aggressive=True)
        assert p.setpoint_low == 68.0
        assert p.setpoint_high == 80.0  # suppressed ceiling unaffected

    def test_away_uses_setback_band(self):
        p = _program(DAY_TYPE_WARM, BAND, occupancy=OCCUPANCY_AWAY)
        assert (p.setpoint_low, p.setpoint_high) == (60.0, 80.0)

    def test_vacation_uses_deeper_setback_band(self):
        p = _program(DAY_TYPE_WARM, BAND, occupancy=OCCUPANCY_VACATION)
        assert (p.setpoint_low, p.setpoint_high) == (57.0, 83.0)  # ±VACATION_SETBACK_EXTRA(3)

    def test_sleep_window_uses_sleep_band(self):
        p = _program(DAY_TYPE_WARM, BAND, sleep=True)
        assert (p.setpoint_low, p.setpoint_high) == (66.0, 78.0)

    def test_sleep_window_cold_night_same_band(self):
        p = _program(DAY_TYPE_COLD, BAND, sleep=True)
        assert (p.setpoint_low, p.setpoint_high) == (66.0, 78.0)

    def test_away_overrides_aggressive(self):
        """Savings margin widens only the comfort band, never the away setback band."""
        p = _program(DAY_TYPE_WARM, BAND, occupancy=OCCUPANCY_AWAY, aggressive=True)
        assert (p.setpoint_low, p.setpoint_high) == (60.0, 80.0)


# ---------------------------------------------------------------------------
# Single-mode thermostats — arm the threatened edge only
# ---------------------------------------------------------------------------


class TestSingleMode:
    def test_cool_only_warm_arms_ceiling(self):
        p = _program(DAY_TYPE_WARM, COOL_ONLY)
        assert p.mode == "cool"
        assert p.setpoint == 74.0
        assert p.setpoint_low is None and p.setpoint_high is None

    def test_cool_only_warm_aggressive(self):
        p = _program(DAY_TYPE_WARM, COOL_ONLY, aggressive=True)
        assert (p.mode, p.setpoint) == ("cool", 76.0)

    def test_cool_only_cold_day_cannot_heat_falls_back_off(self):
        """A cool-only unit cannot defend the floor on a cold day → off sentinel (no false promise)."""
        p = _program(DAY_TYPE_COLD, COOL_ONLY)
        assert p.mode == "off"

    def test_heat_only_cold_arms_floor(self):
        p = _program(DAY_TYPE_COLD, HEAT_ONLY)
        assert (p.mode, p.setpoint) == ("heat", 70.0)

    def test_heat_only_warm_day_cannot_cool_falls_back_off(self):
        p = _program(DAY_TYPE_WARM, HEAT_ONLY)
        assert p.mode == "off"

    def test_cool_only_away_defends_ceiling(self):
        p = _program(DAY_TYPE_WARM, COOL_ONLY, occupancy=OCCUPANCY_AWAY)
        assert (p.mode, p.setpoint) == ("cool", 80.0)  # wide setback ceiling

    def test_heat_only_away_defends_floor(self):
        p = _program(DAY_TYPE_WARM, HEAT_ONLY, occupancy=OCCUPANCY_AWAY)
        assert (p.mode, p.setpoint) == ("heat", 60.0)  # setback floor

    def test_unknown_thermostat_falls_back_off(self):
        p = _program(DAY_TYPE_WARM, UNKNOWN)
        assert p.mode == "off"
        assert p.setpoint is None


# ---------------------------------------------------------------------------
# ThermostatProgram invariant
# ---------------------------------------------------------------------------


def test_program_is_frozen():
    p = _program(DAY_TYPE_WARM, BAND)
    try:
        p.mode = "cool"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower() or "attribute" in str(exc).lower()
    else:
        raise AssertionError("ThermostatProgram should be immutable")


def test_reason_is_descriptive():
    p = _program(DAY_TYPE_WARM, BAND)
    assert "comfort" in p.reason and "74" in p.reason


# ---------------------------------------------------------------------------
# _in_sleep_window
# ---------------------------------------------------------------------------


class TestInSleepWindow:
    CFG = {"sleep_time": "22:30", "wake_time": "07:00"}

    def test_late_evening_in_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), self.CFG) is True

    def test_early_morning_in_window_wraparound(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 6, 0), self.CFG) is True

    def test_midday_out_of_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 12, 0), self.CFG) is False

    def test_exactly_at_sleep_time_in_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 22, 30), self.CFG) is True

    def test_exactly_at_wake_time_out_of_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 7, 0), self.CFG) is False

    def test_missing_wake_time_returns_false(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), {"sleep_time": "22:30"}) is False

    def test_malformed_time_returns_false(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), {"sleep_time": "bad", "wake_time": "07:00"}) is False
