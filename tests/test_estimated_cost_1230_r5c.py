"""#1230 R5c (DP-10): the optional energy inputs and the daily estimated cost.

What the occupant sees: the Analysis days table shows what a day's heating and cooling cost, but only
once they have told Climate Advisor their energy rate and their equipment's power. Without them the cell
stays blank; no number is ever invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import DailyRecord, estimate_daily_cost


class TestEstimateDailyCost:
    def test_prices_each_mode_at_its_own_power(self):
        # 2 h heating at 3 kW + 1 h cooling at 2 kW, 0.20 per kWh -> (6 + 2) * 0.20
        assert estimate_daily_cost(120, 60, 180, 0.20, 3.0, 2.0) == pytest.approx(1.60)

    @pytest.mark.parametrize("rate", [None, 0, 0.0, -1])
    def test_no_rate_means_no_cost(self, rate):
        assert estimate_daily_cost(60, 0, 60, rate, 3.0, 2.0) is None

    def test_a_mode_that_ran_without_a_power_rating_means_no_cost(self):
        assert estimate_daily_cost(60, 30, 90, 0.2, 3.0, None) is None
        assert estimate_daily_cost(60, 30, 90, 0.2, None, 2.0) is None

    def test_a_mode_that_did_not_run_needs_no_rating(self):
        assert estimate_daily_cost(60, 0, 60, 0.2, 3.0, None) == pytest.approx(0.60)

    def test_record_from_before_the_per_mode_split_is_not_guessed(self):
        # 90 min of runtime but no per-mode minutes: cannot be priced.
        assert estimate_daily_cost(0, 0, 90, 0.2, 3.0, 2.0) is None

    def test_a_day_nothing_ran_costs_zero(self):
        assert estimate_daily_cost(0, 0, 0, 0.2, None, None) == 0.0


def _coord(config: dict) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = config
    return coord


class TestCoordinatorWiring:
    def test_flush_splits_runtime_by_mode(self):
        coord = _coord({})
        coord._today_record = DailyRecord(date="2026-04-08", day_type="cold", trend_direction="stable")
        start = datetime(2026, 4, 8, 6, 0)
        for mode, minutes in (("heat", 30), ("cool", 12)):
            coord._hvac_on_since = start
            with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
                dt.now.return_value = start + timedelta(minutes=minutes)
                coord._flush_runtime_for_mode(mode)
        rec = coord._today_record
        assert rec.hvac_runtime_minutes == pytest.approx(42)
        assert rec.hvac_heat_runtime_minutes == pytest.approx(30)
        assert rec.hvac_cool_runtime_minutes == pytest.approx(12)

    def test_fan_only_never_touches_the_cost_inputs(self):
        coord = _coord({})
        coord._today_record = DailyRecord(date="2026-04-08", day_type="cold", trend_direction="stable")
        coord._thermostat_fan_only_on_since = datetime(2026, 4, 8, 6, 0)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = datetime(2026, 4, 8, 7, 0)
            coord._flush_runtime_for_mode("fan_only")
        rec = coord._today_record
        assert rec.hvac_heat_runtime_minutes == rec.hvac_cool_runtime_minutes == 0.0

    def test_cost_filled_when_inputs_exist_and_none_otherwise(self):
        rec = DailyRecord(
            date="2026-04-08",
            day_type="cold",
            trend_direction="stable",
            hvac_runtime_minutes=60.0,
            hvac_heat_runtime_minutes=60.0,
        )
        _coord({"energy_rate": 0.25, "heat_power_kw": 4.0})._apply_estimated_cost(rec)
        assert rec.estimated_cost == pytest.approx(1.0)
        _coord({"heat_power_kw": 4.0})._apply_estimated_cost(rec)
        assert rec.estimated_cost is None
        _coord({})._apply_estimated_cost(rec)
        assert rec.estimated_cost is None
