"""Slice 2 tests: physics-based indoor temperature prediction (Issue #114).

Tests for _simulate_indoor_physics() ODE step function and the physics path
in _build_predicted_indoor_future() when a thermal model is available.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.coordinator import (
    _build_predicted_indoor_future,
    _simulate_indoor_physics,
)

# Reuse the helpers from test_prediction.py
_PRED_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}
_PRED_NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)


def _pred_entry(dt: datetime, temp: float) -> dict:
    return {"datetime": dt.isoformat(), "temperature": temp}


def _make_dt_util_mock(now: datetime):
    mock = MagicMock()
    mock.now.return_value = now
    mock.as_local.side_effect = lambda dt: dt
    mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
    return mock


# ---------------------------------------------------------------------------
# TestSimulateIndoorPhysics
# ---------------------------------------------------------------------------


class TestSimulateIndoorPhysics:
    """Unit tests for _simulate_indoor_physics() ODE step function."""

    def test_passive_decay_toward_outdoor(self):
        """With no HVAC (k_active=None), indoor decays toward outdoor over time."""
        t_in = 70.0
        t_out = 45.0
        k_p = -0.1  # hr^-1 moderate envelope

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, None, 1.0, None, comfort_heat=70, comfort_cool=75)
        assert t_next < t_in
        assert t_next > t_out  # won't fully reach outdoor in 1 hr

    def test_heating_raises_indoor(self):
        """With heating active (T < setpoint), indoor rises."""
        t_in = 65.0
        t_out = 40.0
        k_p = -0.05
        k_a = 3.0  # positive: heating contribution

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, k_a, 1.0, 70.0, comfort_heat=70, comfort_cool=75)
        assert t_next > t_in

    def test_heating_clamped_at_setpoint(self):
        """Heating never overshoots setpoint."""
        t_in = 69.5  # very close to setpoint
        t_out = 40.0
        k_p = -0.05
        k_a = 10.0  # aggressive

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, k_a, 5.0, 70.0, comfort_heat=70, comfort_cool=75)
        assert t_next <= 70.0

    def test_cooling_lowers_indoor(self):
        """With cooling active (T > setpoint), indoor drops."""
        t_in = 78.0
        t_out = 85.0
        k_p = -0.05
        k_a = -2.5  # negative: cooling contribution

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, k_a, 1.0, 75.0, comfort_heat=70, comfort_cool=75)
        assert t_next < t_in

    def test_cooling_clamped_at_setpoint(self):
        """Cooling never undershoots setpoint."""
        t_in = 75.5
        t_out = 85.0
        k_p = -0.05
        k_a = -15.0  # aggressive

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, k_a, 5.0, 75.0, comfort_heat=70, comfort_cool=75)
        assert t_next >= 75.0

    def test_hvac_off_no_k_active(self):
        """k_active=None → no HVAC contribution, only passive decay."""
        t_in = 72.0
        t_out = 50.0
        k_p = -0.1

        t_no_hvac = _simulate_indoor_physics(t_in, t_out, k_p, None, 1.0, None, comfort_heat=70, comfort_cool=75)
        # Should be same as k_active=0 with no setpoint
        exp_kp = math.exp(k_p * 1.0)
        expected = t_out + (t_in - t_out) * exp_kp
        assert t_no_hvac == pytest.approx(expected, abs=0.001)

    def test_ode_analytical_solution(self):
        """Result matches the analytical ODE formula directly."""
        t_in = 65.0
        t_out = 40.0
        k_p = -0.05
        q = 3.0
        dt = 1.0

        exp_kp = math.exp(k_p * dt)
        expected = t_out + (t_in - t_out) * exp_kp + (q / k_p) * (exp_kp - 1)

        # Setpoint high enough not to clamp
        result = _simulate_indoor_physics(t_in, t_out, k_p, q, dt, 80.0, comfort_heat=70, comfort_cool=75)
        assert result == pytest.approx(expected, abs=0.001)

    def test_heating_not_active_when_already_at_setpoint(self):
        """When T == setpoint already, HVAC doesn't need to heat → only passive decay."""
        t_in = 70.0  # already at setpoint
        t_out = 40.0
        k_p = -0.1
        k_a = 5.0

        t_next = _simulate_indoor_physics(t_in, t_out, k_p, k_a, 1.0, 70.0, comfort_heat=70, comfort_cool=75)
        # T == setpoint so no heating applied, only passive decay
        assert t_next < t_in  # passive decay pulls it down


# ---------------------------------------------------------------------------
# TestBuildPredictedIndoorFuturePhysics
# ---------------------------------------------------------------------------


class TestBuildPredictedIndoorFuturePhysics:
    """Tests for _build_predicted_indoor_future() physics simulation path."""

    def _good_model(self):
        return {
            "confidence": "low",
            "k_passive": -0.05,
            "k_active_heat": 3.0,
            "k_active_cool": -2.5,
        }

    def _call(self, entries, *, now=_PRED_NOW, indoor=68.0, model=None, config=None):
        cfg = config or _PRED_CONFIG
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            return _build_predicted_indoor_future(
                entries,
                cfg,
                now,
                current_indoor_temp=indoor,
                thermal_model=model or self._good_model(),
            )

    def test_physics_path_produces_results(self):
        """Valid model + indoor temp → physics path runs without error."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 5)]
        result = self._call(entries, now=now, indoor=65.0)
        assert len(result) == 7

    def test_physics_differs_from_setpoint_schedule(self):
        """Physics output is distinct from the step-function setpoint fallback."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 5)]

        physics = self._call(entries, now=now, indoor=65.0)

        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            setpoint = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)

        temps_physics = [r["temp"] for r in physics]
        temps_setpoint = [r["temp"] for r in setpoint]
        assert temps_physics != temps_setpoint

    def test_fallback_when_confidence_none(self):
        """confidence='none' → setpoint-schedule fallback, not physics."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 5)]
        model = {"confidence": "none"}
        result = self._call(entries, now=now, indoor=68.0, model=model)
        assert len(result) == 7

    def test_fallback_when_no_model(self):
        """thermal_model=None → setpoint fallback, no crash."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 5)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        assert len(result) == 7

    def test_fallback_when_no_indoor_temp(self):
        """current_indoor_temp=None → setpoint fallback (can't seed ODE)."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 5)]
        result = self._call(entries, now=now, indoor=None)
        assert len(result) == 7

    def test_fallback_when_k_passive_positive(self):
        """k_passive > 0 is physically invalid → falls back to setpoint schedule."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 4)]
        bad_model = {"confidence": "high", "k_passive": 0.05}
        result = self._call(entries, now=now, indoor=68.0, model=bad_model)
        assert len(result) == 5  # no crash, fallback used

    def test_physics_passive_decay_on_hvac_off_day(self):
        """HVAC-off day: indoor drifts passively toward outdoor with k_passive."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        # 60°F high → mild → HVAC off
        entries = [_pred_entry(now + timedelta(hours=i), 60.0) for i in range(1, 6)]
        model = {
            "confidence": "low",
            "k_passive": -0.1,
            "k_active_heat": 3.0,
            "k_active_cool": -2.5,
        }

        result = self._call(entries, now=now, indoor=72.0, model=model)
        assert len(result) == 9
        temps = [r["temp"] for r in result]
        # Indoor at 72°F, outdoor 60°F, HVAC off → drifts down toward 60°F
        assert temps[0] < 72.0
        assert temps[-1] < temps[0]

    def test_physics_produces_continuous_trajectory(self):
        """No single-step temperature jump > 10°F (physics is smooth)."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 10)]
        result = self._call(entries, now=now, indoor=65.0)
        temps = [r["temp"] for r in result]
        for i in range(len(temps) - 1):
            assert abs(temps[i + 1] - temps[i]) <= 10.0, f"Jump at step {i}: {temps[i]:.1f} → {temps[i + 1]:.1f}"

    def test_physics_high_confidence_also_activates(self):
        """confidence='high' with valid k_passive also activates physics path."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 4)]
        model = {
            "confidence": "high",
            "k_passive": -0.05,
            "k_active_heat": 3.0,
            "k_active_cool": -2.5,
        }
        result = self._call(entries, now=now, indoor=65.0, model=model)
        assert len(result) == 5
