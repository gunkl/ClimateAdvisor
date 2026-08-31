"""Tests for the shared lead-time-from-rate helper (Issue #786).

Covers the formula shape shared by automation.py's adaptive pre-heat, ode_ceiling_guard.py's
escalation lead time, and briefing.py's warm-day pre-cool lead time — see
thermal_lead_time.py's module docstring for the consolidation rationale.
"""

from __future__ import annotations

from custom_components.climate_advisor.thermal_lead_time import compute_lead_minutes_from_rate


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
