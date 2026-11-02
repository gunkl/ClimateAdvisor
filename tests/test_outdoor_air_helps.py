"""Issue #1211: the shared "would outside air help?" rule in temperature.py.

Expected values are hand-computed from the documented rule (comfort 68-74, delta 3 -> a day
helps when high >= 68 and low <= 71), not derived by re-running the formula.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.temperature import outdoor_air_helps, outdoor_direction_ok

HEAT, COOL = 68.0, 74.0


class TestRangeStage:
    @pytest.mark.parametrize(
        ("low", "high", "expected"),
        [
            (49, 65, False),  # the 10-27 day: high never reaches the 68 floor
            (49, 67.9, False),
            (49, 68, True),  # boundary: high == floor helps
            (49, 72, True),
            (71, 80, True),  # boundary: low == ceiling - 3 helps
            (71.1, 80, False),  # warm night: low too warm to cool the house
            (74, 80, False),  # the 80/74 warm day from the report
            (60, 90, True),
        ],
    )
    def test_default_comfort(self, low, high, expected):
        assert outdoor_air_helps(low, high, HEAT, COOL) is expected

    def test_uses_configured_ceiling_not_the_default(self):
        # Ceiling 78 -> low may be up to 75. With the old DEFAULT ceiling (74) a 73 low failed.
        assert outdoor_air_helps(73, 80, 68, 78) is True
        assert outdoor_air_helps(73, 80, 68, 74) is False

    def test_uses_configured_floor(self):
        assert outdoor_air_helps(50, 65, 64, 74) is True
        assert outdoor_air_helps(50, 65, 66, 74) is False

    def test_custom_delta(self):
        assert outdoor_air_helps(72, 80, HEAT, COOL, delta=2.0) is True
        assert outdoor_air_helps(72, 80, HEAT, COOL, delta=3.0) is False

    def test_monotone_in_high_and_low(self):
        highs = [60 + 0.5 * i for i in range(40)]
        results = [outdoor_air_helps(55, h, HEAT, COOL) for h in highs]
        assert results == sorted(results)  # once True, stays True as the high rises
        lows = [50 + 0.5 * i for i in range(40)]
        results = [outdoor_air_helps(lw, 80, HEAT, COOL) for lw in lows]
        assert results == sorted(results, reverse=True)  # once False, stays False as the low rises


class TestUnknownReadings:
    def test_missing_high_is_none(self):
        assert outdoor_air_helps(50, None, HEAT, COOL) is None

    def test_missing_low_is_none(self):
        assert outdoor_air_helps(None, 75, HEAT, COOL) is None

    def test_estimated_low_is_none_even_when_the_value_would_pass(self):
        assert outdoor_air_helps(40, 75, HEAT, COOL, low_is_estimated=True) is None


class TestHysteresis:
    def test_new_day_needs_the_plain_threshold(self):
        assert outdoor_air_helps(50, 67, HEAT, COOL, was_helping=False) is False

    def test_already_recommended_day_keeps_slack_on_the_high(self):
        assert outdoor_air_helps(50, 66.6, HEAT, COOL, was_helping=True) is True  # 68 - 1.5 = 66.5
        assert outdoor_air_helps(50, 66.4, HEAT, COOL, was_helping=True) is False

    def test_already_recommended_day_keeps_slack_on_the_low(self):
        assert outdoor_air_helps(72.4, 80, HEAT, COOL, was_helping=True) is True  # 71 + 1.5 = 72.5
        assert outdoor_air_helps(72.6, 80, HEAT, COOL, was_helping=True) is False

    def test_a_forecast_on_the_boundary_does_not_flap(self):
        state = False
        seen = []
        for high in (67.9, 68.0, 67.5, 68.4, 67.2, 67.9):
            state = bool(outdoor_air_helps(50, high, HEAT, COOL, was_helping=state))
            seen.append(state)
        assert seen == [False, True, True, True, True, True]


class TestDirectionStage:
    def test_cold_house_needs_warmer_outdoor(self):
        assert outdoor_direction_ok(60, 65, HEAT, COOL) is False
        assert outdoor_direction_ok(66, 65, HEAT, COOL) is True

    def test_hot_house_needs_cooler_outdoor(self):
        assert outdoor_direction_ok(80, 76, HEAT, COOL) is False
        assert outdoor_direction_ok(70, 76, HEAT, COOL) is True

    def test_in_band_does_not_veto(self):
        assert outdoor_direction_ok(50, 70, HEAT, COOL) is True
        assert outdoor_direction_ok(90, 70, HEAT, COOL) is True

    def test_band_edges_are_in_band(self):
        assert outdoor_direction_ok(50, 68.0, HEAT, COOL) is True
        assert outdoor_direction_ok(90, 74.0, HEAT, COOL) is True

    @pytest.mark.parametrize(("outdoor", "indoor"), [(None, 70), (60, None), (None, None)])
    def test_unknown_reading_does_not_veto(self, outdoor, indoor):
        assert outdoor_direction_ok(outdoor, indoor, HEAT, COOL) is True
