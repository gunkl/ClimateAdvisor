"""Issue #1519: the nat-vent plan's pre-cool lead gets the decisions' cooling rate and closed-house rate.

The coordinator used to call compute_nat_vent_plan() without k_active_cool, so the plan's pre-cool start was always
the 120-minute fallback, whatever the AC could do.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor.const import DAY_TYPE_HOT, DAY_TYPE_WARM


def _stub(day_type: str):
    stub = types.SimpleNamespace()
    stub._current_classification = types.SimpleNamespace(
        day_type=day_type, window_opportunity_morning_start=None, window_open_time=None
    )
    stub.control_predicted_indoor = MagicMock(return_value=[{"ts": "2026-07-13T14:00:00+00:00", "temp": 75.0}])
    stub.control_house_stepper = MagicMock(return_value=None)
    stub.control_thermal_model = MagicMock(return_value={"k_active_cool": -3.2, "k_passive": -0.05})
    stub._thermal_model = {}
    stub._hourly_forecast_temps = []
    stub.config = {"comfort_cool": 76, "comfort_heat": 68}
    stub._stabilize_nat_vent_cutoff_reason = lambda plan: plan
    return stub


def _run(day_type: str) -> dict:
    stub = _stub(day_type)
    with (
        patch.object(coord_mod, "compute_nat_vent_plan", return_value={}) as plan,
        patch.object(coord_mod, "_build_future_forecast_outdoor", return_value=[]),
    ):
        coord_mod.ClimateAdvisorCoordinator._compute_and_cache_nat_vent_plan(stub)
    return plan.call_args.kwargs


def test_hot_day_plan_gets_the_cooling_and_closed_house_rates():
    kw = _run(DAY_TYPE_HOT)
    assert kw["k_active_cool"] == -3.2 and kw["k_passive"] == -0.05


def test_warm_day_plan_gets_the_cooling_and_closed_house_rates():
    kw = _run(DAY_TYPE_WARM)
    assert kw["k_active_cool"] == -3.2 and kw["k_passive"] == -0.05
