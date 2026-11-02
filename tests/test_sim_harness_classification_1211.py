"""Issue #1211 (C6): the scenario harness fills UNSTATED classification fields from the real classifier.

Expected values are hand-computed from the documented rule (comfort 70/76 in the harness defaults:
outside air helps when high >= 70 and low <= 73), not read back from the code under test.
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace

from tools.sim_harness.scenario_model import build_classification_from_event, pin_scenario_thermal_model

_CFG = {"comfort_heat": 70.0, "comfort_cool": 76.0}


def _build(event, cfg=_CFG):
    return build_classification_from_event({"type": "classification", **event}, cfg)


def test_warm_day_with_nothing_else_stated_gets_the_classifiers_answer():
    c = _build({"day_type": "warm"})
    assert c.hvac_mode == "off"
    assert c.windows_recommended is True  # representative warm day: low 60 <= 73, high 80 >= 70
    assert c.window_open_time == time(6, 0)
    assert c.window_close_time == time(10, 0)


def test_mild_day_with_nothing_else_stated_reaches_the_configured_floor():
    c = _build({"day_type": "mild"})
    assert c.today_high >= 70.0  # floor 70
    assert c.windows_recommended is True
    assert (c.window_open_time, c.window_close_time) == (time(10, 0), time(17, 0))


def test_default_forecast_matches_the_day_type_not_a_blanket_75_60():
    assert _build({"day_type": "cold"}).today_high < 45
    assert _build({"day_type": "cool"}).today_high < 60
    assert _build({"day_type": "hot"}).today_high >= 85


def test_hot_day_does_not_silently_gain_a_window_opportunity():
    c = _build({"day_type": "hot"})
    assert c.hvac_mode == "cool"
    assert c.window_opportunity_morning is False
    assert c.window_opportunity_evening is False


def test_stated_fields_win_over_the_classifier():
    c = _build(
        {
            "day_type": "warm",
            "hvac_mode": "heat",
            "windows_recommended": False,
            "window_open_time": "07:30",
            "window_close_time": "09:15",
            "setback_modifier": -2.0,
        }
    )
    assert c.hvac_mode == "heat"
    assert c.windows_recommended is False
    assert c.forecast_windows_recommended is False
    assert (c.window_open_time, c.window_close_time) == (time(7, 30), time(9, 15))
    assert c.setback_modifier == -2.0


def test_stated_forecast_goes_through_the_classifier_rule():
    # the 10-27 day: mild, high 65 never reaches the 70 floor in this config
    assert _build({"day_type": "mild", "today_high": 65, "today_low": 49}).windows_recommended is False
    assert _build({"day_type": "mild", "today_high": 71, "today_low": 49}).windows_recommended is True


def test_scenario_comfort_band_is_used():
    event = {"day_type": "mild", "today_high": 66, "today_low": 50}
    assert _build(event, {"comfort_heat": 64.0, "comfort_cool": 74.0}).windows_recommended is True
    assert _build(event, {"comfort_heat": 68.0, "comfort_cool": 74.0}).windows_recommended is False


def test_pin_scenario_thermal_model_survives_a_coordinator_refresh():
    learning = SimpleNamespace(get_thermal_model=lambda *a, **k: {})
    coordinator = SimpleNamespace(learning=learning)
    model = {"confidence": "high", "k_passive": -0.18}
    pin_scenario_thermal_model(coordinator, model)
    # _async_update_data_impl calls it with keyword arguments every cycle
    assert coordinator.learning.get_thermal_model(outdoor_temp_f=70.0, solar_factor=0.5) is model
