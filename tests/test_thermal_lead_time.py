"""Tests for the shared lead-time-from-rate helper (Issue #786).

Covers the formula shape shared by automation.py's adaptive pre-heat, ode_ceiling_guard.py's
escalation lead time, and briefing.py's warm-day pre-cool lead time — see
thermal_lead_time.py's module docstring for the consolidation rationale.
"""

from __future__ import annotations

import math

import pytest

from custom_components.climate_advisor.thermal_lead_time import (
    LEAK_SAFETY_MULTIPLIER,
    compute_lead_minutes_from_rate,
    leak_aware_minutes,
)


class TestComputeLeadMinutesFromRate:
    def test_none_rate_returns_clamped_fallback(self):
        result = compute_lead_minutes_from_rate(
            delta_t=5.0,
            rate=None,
            min_minutes=10.0,
            max_minutes=200.0,
            safety_multiplier=1.3,
            fallback_minutes=120.0,
        )
        assert result == 120.0

    def test_zero_rate_returns_clamped_fallback(self):
        result = compute_lead_minutes_from_rate(
            delta_t=5.0,
            rate=0.0,
            min_minutes=10.0,
            max_minutes=200.0,
            safety_multiplier=1.3,
            fallback_minutes=120.0,
        )
        assert result == 120.0

    def test_fallback_is_clamped_too(self):
        # fallback_minutes (300) exceeds max_minutes (200) — must still be clamped.
        result = compute_lead_minutes_from_rate(
            delta_t=5.0,
            rate=None,
            min_minutes=10.0,
            max_minutes=200.0,
            safety_multiplier=1.3,
            fallback_minutes=300.0,
        )
        assert result == 200.0

    def test_positive_rate_uses_formula(self):
        # delta_t=4, rate=2 F/hr, safety=1.3 -> (4/2)*60*1.3 = 156 minutes.
        result = compute_lead_minutes_from_rate(
            delta_t=4.0,
            rate=2.0,
            min_minutes=10.0,
            max_minutes=300.0,
            safety_multiplier=1.3,
            fallback_minutes=120.0,
        )
        assert result == 156.0

    def test_negative_rate_uses_abs_value(self):
        # k_active_cool is conventionally negative; formula must use abs(rate).
        result = compute_lead_minutes_from_rate(
            delta_t=4.0,
            rate=-2.0,
            min_minutes=10.0,
            max_minutes=300.0,
            safety_multiplier=1.3,
            fallback_minutes=120.0,
        )
        assert result == 156.0

    def test_result_clamped_to_max(self):
        result = compute_lead_minutes_from_rate(
            delta_t=100.0,
            rate=1.0,
            min_minutes=10.0,
            max_minutes=240.0,
            safety_multiplier=1.0,
            fallback_minutes=120.0,
        )
        assert result == 240.0

    def test_result_clamped_to_min(self):
        result = compute_lead_minutes_from_rate(
            delta_t=0.1,
            rate=100.0,
            min_minutes=30.0,
            max_minutes=240.0,
            safety_multiplier=1.0,
            fallback_minutes=120.0,
        )
        assert result == 30.0


# --- Issue #1517: leak-aware lead -------------------------------------------------------------------------------

_KW = {"min_minutes": 30.0, "max_minutes": 240.0, "safety_multiplier": 1.3, "fallback_minutes": 45.0}


def _lead(delta, rate, **leak):
    return compute_lead_minutes_from_rate(delta_t=delta, rate=rate, **_KW, **leak)


class TestLeakAwareMinutes:
    def test_matches_the_closed_form(self):
        # Heating 3 F/h, k -0.1, outdoor 40: Ts = 40 + 30 = 70; 62 -> 66.
        t = leak_aware_minutes(62.0, 66.0, 3.0, -0.1, 40.0)
        ts = 70.0
        temp = ts + (62.0 - ts) * math.exp(-0.1 * t / 60.0)
        assert temp == pytest.approx(66.0)

    def test_cold_outdoor_lengthens_heating_beyond_the_plain_time(self):
        assert leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 30.0) > 4.0 / 3.0 * 60.0

    def test_outdoor_above_start_shortens_heating(self):
        # Outdoor warmer than the house: the leak helps the heater.
        assert leak_aware_minutes(62.0, 66.0, 3.0, -0.1, 80.0) < 4.0 / 3.0 * 60.0

    def test_cooling_mirrors_heating(self):
        heat = leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 30.0)
        cool = leak_aware_minutes(98.0, 94.0, -3.0, -0.03, 130.0, cooling=True)  # mirror about 80 F: outdoor 30 -> 130
        assert cool == pytest.approx(heat)

    def test_no_leak_limit_is_delta_over_rate(self):
        assert leak_aware_minutes(62.0, 66.0, 3.0, -1e-9, 40.0) == pytest.approx(80.0, rel=1e-4)

    def test_unreachable_target_returns_none(self):
        # Steady state with the heater on is 40 + 1/0.1 = 50 F: 66 F is never reached.
        assert leak_aware_minutes(45.0, 66.0, 1.0, -0.1, 40.0) is None
        # Cooling mirror: steady state 130 - 10 = 120 F, target 94 F never reached.
        assert leak_aware_minutes(125.0, 94.0, 1.0, -0.1, 130.0, cooling=True) is None

    def test_move_already_done_is_zero(self):
        assert leak_aware_minutes(66.0, 65.0, 3.0, -0.1, 40.0) == 0.0
        assert leak_aware_minutes(70.0, 71.0, 3.0, -0.1, 40.0, cooling=True) == 0.0

    def test_rate_sd_is_cautious_and_never_below_a_quarter_of_the_rate(self):
        base = leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 40.0)
        cautious = leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 40.0, rate_sd=1.0)  # plans on 2.5 F/h
        assert cautious > base
        assert cautious == pytest.approx(leak_aware_minutes(62.0, 66.0, 2.5, -0.03, 40.0))
        huge = leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 40.0, rate_sd=100.0)
        assert huge == pytest.approx(leak_aware_minutes(62.0, 66.0, 0.75, -0.03, 40.0))


class TestComputeLeadLeakAware:
    _LEAK = {"start_temp": 62.0, "target_temp": 66.0, "outdoor_temp": 30.0, "k_passive": -0.03, "confidence": "high"}

    def test_uses_the_leak_model_and_the_smaller_safety_factor(self):
        raw = leak_aware_minutes(62.0, 66.0, 3.0, -0.03, 30.0)
        assert _lead(4.0, 3.0, **self._LEAK) == pytest.approx(raw * LEAK_SAFETY_MULTIPLIER)
        assert LEAK_SAFETY_MULTIPLIER == 1.15

    def test_unreachable_target_returns_the_ceiling(self):
        assert (
            _lead(21.0, 1.0, start_temp=45.0, target_temp=66.0, outdoor_temp=40.0, k_passive=-0.1, confidence="high")
            == 240.0
        )

    def test_floor_applies(self):
        assert (
            _lead(0.5, 3.0, start_temp=65.5, target_temp=66.0, outdoor_temp=70.0, k_passive=-0.1, confidence="high")
            == 30.0
        )

    @pytest.mark.parametrize("missing", ["start_temp", "target_temp", "outdoor_temp", "k_passive"])
    def test_a_missing_input_falls_back_exactly_to_the_plain_formula(self, missing):
        leak = {**self._LEAK, missing: None}
        assert _lead(4.0, 3.0, **leak) == _lead(4.0, 3.0) == pytest.approx(4.0 / 3.0 * 60.0 * 1.3)

    def test_non_negative_k_falls_back(self):
        assert _lead(4.0, 3.0, **{**self._LEAK, "k_passive": 0.0}) == _lead(4.0, 3.0)

    def test_no_rate_still_returns_the_fallback(self):
        assert _lead(4.0, None, **self._LEAK) == 45.0

    def test_cooling_flag_selects_the_direction(self):
        leak = {
            "start_temp": 98.0,
            "target_temp": 94.0,
            "outdoor_temp": 130.0,
            "k_passive": -0.03,
            "cooling": True,
            "confidence": "high",
        }
        assert _lead(4.0, -3.0, **leak) == pytest.approx(_lead(4.0, 3.0, **self._LEAK))


class TestSimulatedRecoveries:
    """Issue #1517 outcome check: known house (q, k), recoveries of 2-4 F across outdoor temperatures.

    A recovery is "late" when the lead is more than 5 minutes shorter than the time the house really takes.
    """

    @staticmethod
    def _late_fractions(q, k):
        late_plain = late_leak = n = 0
        for outdoor in range(15, 60, 5):
            for start in (60.0, 63.0, 66.0):
                for delta in (2.0, 3.0, 4.0):
                    true = leak_aware_minutes(start, start + delta, q, k, float(outdoor))
                    if true is None or true > 235.0:
                        continue  # unreachable / past the 240 min ceiling: no formula can be on time
                    n += 1
                    late_plain += _lead(delta, q) < true - 5.0
                    late_leak += (
                        _lead(
                            delta,
                            q,
                            start_temp=start,
                            target_temp=start + delta,
                            outdoor_temp=float(outdoor),
                            k_passive=k,
                            confidence="high",
                        )
                        < true - 5.0
                    )
        return late_plain / n, late_leak / n

    @pytest.mark.parametrize(("q", "k"), [(3.0, -0.08), (2.0, -0.05), (4.0, -0.12)])
    def test_leak_aware_is_not_late_where_today_is(self, q, k):
        plain, leak = self._late_fractions(q, k)
        assert leak == 0.0
        assert plain > 0.15  # the plain formula (delta / rate * 1.3) is late in a large share of these recoveries
