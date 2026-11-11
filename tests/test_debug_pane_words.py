"""Debug tab in words: the v4 grade's reason, the rates the decisions read and their source, and the window rule.

Occupant view: the Debug tab showed v3's learned rates under "Prediction engines" with no v3 label while v4 ran the
house, gave no reason for v4's "low" grade, and showed fixed window times and a hot-day-only 80 F threshold on a mild
day as if they were today's recommendation. These pin the payload the tab now words those rows from.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import prediction_engines, v4_shadow
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    ECONOMIZER_TEMP_DELTA,
    WINDOW_OPPORTUNITY_MAX_LOW_F,
    WINDOWS_RECOMMENDED_HYSTERESIS_F,
)
from custom_components.climate_advisor.coordinator import _REJECTION_LOG_CAP, ClimateAdvisorCoordinator

from .test_nat_vent_dashboard_target import _make_nat_vent_coord_stub

# The live Zone 1 fit of 2026-11-08 (climate_advisor_v4_model_<zone>.json), trimmed to what the summary reads.
_LIVE_FIT = {
    "span_days": 29.97,
    "sample_days": 29.97,
    "n_anchors": 160,
    "mae_by_lead": {"1h": 0.543, "4h": 0.761, "12h": 0.744},
    "heat_seen": True,
    "cool_seen": True,
    "starts_accepted": {"heat": 45, "cool": 0},
    "starts_total": {"heat": 61, "cool": 2},
    "reason": "too few accepted thermostat starts in a used mode",
}


def _summary(fit: dict, confidence: str = "low") -> dict:
    return v4_shadow.model_summary({"confidence": confidence, "fitted_at": "x", "primary": "_ca", "fit": fit})


class TestV4GradeWords:
    def test_live_low_grade_names_the_cooling_starts(self):
        why, by_mode = v4_shadow.describe_grade(_summary(_LIVE_FIT))
        assert why == "too few usable cooling starts (ok needs 5 in each mode it has seen)"
        assert by_mode == (
            "heating 45 usable of 61 starts (enough) · cooling 0 usable of 2 starts (needs 5) in the last 30 days"
        )

    def test_summary_carries_the_reason_and_mode_counts(self):
        s = _summary(_LIVE_FIT)
        assert s["confidence_reason"] == _LIVE_FIT["reason"]
        assert s["modes"]["cool"] == {
            "seen": True, "starts": 2, "accepted": 0, "grade": None, "reason": None,
            "carried": False, "keep": None,  # Issue #1525: not carried from an earlier season
        }  # fmt: skip

    def test_a_home_that_never_cooled_says_cooling_not_seen(self):
        fit = dict(_LIVE_FIT, cool_seen=False, starts_total={"heat": 61, "cool": 0}, starts_accepted={"heat": 45})
        fit["reason"] = "heating or cooling not yet observed"
        why, by_mode = v4_shadow.describe_grade(_summary(fit))
        assert why.startswith("cooling not seen in the last 30 days") and "both heating and cooling" in why
        assert "cooling not seen" in by_mode

    def test_ok_grade_says_every_check_passes(self):
        fit = dict(_LIVE_FIT, reason="", starts_accepted={"heat": 45, "cool": 9})
        assert v4_shadow.describe_grade(_summary(fit, "ok"))[0] == "every check passes"

    def test_short_history(self):
        fit = dict(_LIVE_FIT, sample_days=6.4, reason="under 10 days of samples")
        assert v4_shadow.describe_grade(_summary(fit))[0] == "only 6.4 days of history (needs 10 for ok)"

    def test_no_model_is_empty(self):
        assert v4_shadow.describe_grade(v4_shadow.model_summary(None)) == ("", "")


class TestDecisionRates:
    """decision_rates_summary(): v4's own scalars are "v4", the rest of the model is v3's learned observations."""

    _V3 = {
        "confidence": "high",
        "k_passive": -0.0153,
        "k_active_heat": 5.58,
        "k_active_cool": -6.13,
        "k_solar": 0.716,
        "solar_phase_offset_h": 3.23,
        "k_vent_window": -0.138,
        "k_vent_fan": None,
    }
    _V4 = {"k_passive": -0.034, "k_active_heat": 4.52, "k_active_cool": -5.0, "confidence": "low"}

    def _rows(self, monkeypatch, *, primary: str, ready: bool) -> dict:
        coord = SimpleNamespace(config={"primary_engine": primary, "temp_unit": "fahrenheit"}, _thermal_model=self._V3)
        coord.control_thermal_model = lambda base: ClimateAdvisorCoordinator.control_thermal_model(coord, base)
        # Issue #1512: decision_rates_summary also appends the cycle model's rows (real method, no learning on the stub)
        coord._cycle_model_rows = lambda unit: ClimateAdvisorCoordinator._cycle_model_rows(coord, unit)
        monkeypatch.setattr(prediction_engines, "control_ready", lambda source, config: ready)
        v4 = prediction_engines.ENGINES["v4"]
        monkeypatch.setattr(v4, "state_of", lambda source: {})
        monkeypatch.setattr(v4, "control_model", lambda state, base: {**dict(base or {}), **self._V4})
        rows = ClimateAdvisorCoordinator.decision_rates_summary(coord)
        return {r["key"]: (r["value"], r["source"]) for r in rows}

    def test_usable_v4_rates_are_v4s_and_vent_solar_are_v3s(self, monkeypatch):
        rows = self._rows(monkeypatch, primary="v4", ready=True)
        assert rows["k_passive"] == (-0.034, "v4")
        assert rows["k_active_heat"] == (4.52, "v4")
        assert rows["confidence"] == ("low", "v4")
        assert rows["k_vent_window"] == (-0.138, "v3")
        assert rows["solar_phase_offset_h"] == (3.23, "v3")
        assert rows["k_vent_fan"] == (None, "none")

    def test_v4_still_learning_reads_as_defaults(self, monkeypatch):
        rows = self._rows(monkeypatch, primary="v4", ready=False)
        assert rows["k_passive"] == (None, "default")
        assert rows["confidence"][1] == "default"
        # Issue #1505: v4 supplies the window and fan rates too, so while it learns they are blanked with the rest. The
        # old expectation (v3's -0.138 shown as in use) put a v3 windows rate beside a v4/default envelope rate: two
        # models in one comparison, the defect behind Zone 1's "windows and fan 3.1x faster" line.
        assert rows["k_vent_window"] == (None, "default")
        assert rows["k_vent_fan"][1] == "default"

    def test_v3_primary_is_all_v3(self, monkeypatch):
        rows = self._rows(monkeypatch, primary="v3", ready=True)
        assert rows["k_passive"] == (-0.0153, "v3") and rows["confidence"] == ("high", "v3")


def _mild_day(**over) -> DayClassification:
    kw = dict(
        day_type="mild",
        trend_direction="warming",
        trend_magnitude=3.8,
        today_high=65.0,
        today_low=49.0,
        tomorrow_high=66.0,
        tomorrow_low=51.2,
        comfort_heat=68.0,
        comfort_cool=74.0,
    )
    kw.update(over)
    return DayClassification(**kw)


class TestWindowRule:
    def test_mild_day_rule_is_comfort_heat_and_comfort_cool_minus_delta(self):
        rule = ClimateAdvisorCoordinator._debug_window_rule(_mild_day(), "fahrenheit")
        assert rule["kind"] == "range"
        assert rule["high_at_least"] == 68.0
        assert rule["low_at_most"] == 74.0 - ECONOMIZER_TEMP_DELTA
        assert rule["opportunity_low_at_most"] == WINDOW_OPPORTUNITY_MAX_LOW_F

    def test_hysteresis_widens_both_edges(self):
        rule = ClimateAdvisorCoordinator._debug_window_rule(_mild_day(windows_hysteresis_seed=True), "fahrenheit")
        assert rule["high_at_least"] == 68.0 - WINDOWS_RECOMMENDED_HYSTERESIS_F and rule["hysteresis"] is True

    def test_hot_and_cool_days(self):
        rule = ClimateAdvisorCoordinator._debug_window_rule
        assert rule(_mild_day(day_type="hot"), "fahrenheit")["kind"] == "opportunity"
        assert rule(_mild_day(day_type="cool"), "fahrenheit")["kind"] is None

    def test_celsius(self):
        rule = ClimateAdvisorCoordinator._debug_window_rule(_mild_day(), "celsius")
        assert rule["high_at_least"] == pytest.approx(20.0, abs=0.1)


class TestDebugState:
    def _state(self, **attrs):
        coord = _make_nat_vent_coord_stub(config={})
        coord.automation_engine._current_thermostat_family = MagicMock(return_value=None)
        for k, v in attrs.items():
            setattr(coord, k, v)
        return coord, coord.get_debug_state

    def test_mild_day_carries_high_and_rule(self):
        _coord, get = self._state(_current_classification=_mild_day())
        c = get()["classification"]
        assert c["today_high"] == 65.0 and c["windows_recommended"] is False
        assert c["window_rule"]["high_at_least"] == 68.0
        assert c["trend_thresholds"] == {"moderate": 5, "significant": 10}

    def test_door_sensor_uses_the_ha_friendly_name(self):
        coord, get = self._state(_resolved_sensors=["binary_sensor.group_contact"])
        coord._is_sensor_open = lambda sid: True
        coord.hass.states.get = lambda sid: SimpleNamespace(attributes={"friendly_name": "Patio doors"})
        assert get()["door_window_sensors"]["binary_sensor.group_contact"]["friendly_name"] == "Patio doors"

    def test_door_sensor_without_a_state_words_the_entity_id(self):
        coord, get = self._state(_resolved_sensors=["binary_sensor.group_contact"])
        coord._is_sensor_open = lambda sid: False
        coord.hass.states.get = lambda sid: None
        assert get()["door_window_sensors"]["binary_sensor.group_contact"]["friendly_name"] == "Group Contact"


def test_thermal_pipeline_reports_the_rejection_cap():
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._pending_observations = {}
    coord._rejection_log = {"solar_gain": [{}] * _REJECTION_LOG_CAP}
    out = ClimateAdvisorCoordinator._build_thermal_pipeline_summary(coord)
    assert out["rejection_log_cap"] == _REJECTION_LOG_CAP == out["rejection_log_counts"]["solar_gain"]
