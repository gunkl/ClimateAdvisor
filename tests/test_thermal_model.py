"""Tests for LearningEngine.get_thermal_model() and get_weather_bias() (Phase 5G / Issue #114).

v2 architecture: thermal model is EWMA-based, reading from thermal_model_cache.
Observations use k_passive / k_active fields; legacy heating_rate_f_per_hour compat preserved.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.learning import LearningEngine, LearningState

_TODAY = "2026-03-27"
_TODAY_DATE = date(2026, 3, 27)


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _inject_obs(engine: LearningEngine, obs: dict) -> None:
    """Inject a v2 observation via record_thermal_observation with dt_util patched."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = _TODAY_DATE
    mock_dt.now.return_value.isoformat.return_value = f"{_TODAY}T12:00:00"
    with patch("custom_components.climate_advisor.learning.dt_util", mock_dt):
        engine.record_thermal_observation(obs)


def _make_heat_obs(k_active: float = 2.0, grade: str = "low") -> dict:
    """Build a v2 heat observation dict."""
    return {
        "event_id": "test-heat",
        "timestamp": f"{_TODAY}T10:00:00",
        "date": _TODAY,
        "hvac_mode": "heat",
        "session_minutes": 30.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 66.0,
        "peak_indoor_f": 66.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 25.0,
        "k_passive": -0.05,
        "k_active": k_active,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.75,
        "r_squared_active": 0.72,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": grade,
        "schema_version": 2,
    }


def _make_cool_obs(k_active: float = -3.0, grade: str = "low") -> dict:
    """Build a v2 cool observation dict (k_active is negative for cooling)."""
    return {
        "event_id": "test-cool",
        "timestamp": f"{_TODAY}T14:00:00",
        "date": _TODAY,
        "hvac_mode": "cool",
        "session_minutes": 30.0,
        "start_indoor_f": 76.0,
        "end_indoor_f": 75.0,
        "peak_indoor_f": 76.0,
        "start_outdoor_f": 90.0,
        "avg_outdoor_f": 90.0,
        "delta_t_avg": -14.0,
        "k_passive": -0.05,
        "k_active": k_active,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.75,
        "r_squared_active": 0.72,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": grade,
        "schema_version": 2,
    }


def _make_daily_record(
    date_str: str,
    forecast_high: float | None = None,
    observed_high: float | None = None,
    forecast_low: float | None = None,
    observed_low: float | None = None,
) -> dict:
    return {
        "date": date_str,
        "day_type": "mild",
        "trend_direction": "stable",
        "forecast_high_f": forecast_high,
        "observed_high_f": observed_high,
        "forecast_low_f": forecast_low,
        "observed_low_f": observed_low,
    }


# ---------------------------------------------------------------------------
# TestThermalModelComputation
# ---------------------------------------------------------------------------


class TestThermalModelComputation:
    """Tests for get_thermal_model() — v2 EWMA cache-based model."""

    def test_none_confidence_when_no_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert model["confidence"] == "none"
        assert model["heating_rate_f_per_hour"] is None
        assert model["cooling_rate_f_per_hour"] is None

    def test_low_confidence_at_5_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(5):
            _inject_obs(engine, _make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "low"

    def test_medium_confidence_at_10_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(10):
            _inject_obs(engine, _make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "medium"

    def test_high_confidence_at_20_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(20):
            _inject_obs(engine, _make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "high"

    def test_heating_rate_reflects_k_active(self, tmp_path: Path):
        """Single heat observation: heating_rate_f_per_hour == k_active exactly."""
        engine = _make_engine(tmp_path)
        _inject_obs(engine, _make_heat_obs(k_active=3.5))
        model = engine.get_thermal_model()
        # First observation initialises the EWMA directly (no blending)
        assert model["heating_rate_f_per_hour"] == pytest.approx(3.5, abs=0.01)

    def test_cooling_rate_uses_cool_observations_only(self, tmp_path: Path):
        """Heat and cool observations accumulate into separate k_active fields."""
        engine = _make_engine(tmp_path)
        for _ in range(5):
            _inject_obs(engine, _make_heat_obs(k_active=2.0))
        for _ in range(5):
            _inject_obs(engine, _make_cool_obs(k_active=-5.0))
        model = engine.get_thermal_model()
        # cooling_rate_f_per_hour is abs(k_active_cool) — positive scalar
        assert model["cooling_rate_f_per_hour"] is not None
        assert model["cooling_rate_f_per_hour"] > 0
        assert model["heating_rate_f_per_hour"] is not None
        assert model["heating_rate_f_per_hour"] > 0
        # Verify they are independently tracked (not identical values)
        assert model["heating_rate_f_per_hour"] != model["cooling_rate_f_per_hour"]

    def test_none_rate_when_no_obs_for_mode(self, tmp_path: Path):
        """Only heat observations → cooling_rate is None."""
        engine = _make_engine(tmp_path)
        for _ in range(5):
            _inject_obs(engine, _make_heat_obs())
        model = engine.get_thermal_model()
        assert model["cooling_rate_f_per_hour"] is None

    def test_ewma_moves_toward_new_values(self, tmp_path: Path):
        """After seeding at 2.0 then repeatedly applying 10.0, model moves up."""
        engine = _make_engine(tmp_path)
        _inject_obs(engine, _make_heat_obs(k_active=2.0))
        initial = engine.get_thermal_model()["heating_rate_f_per_hour"]
        assert initial == pytest.approx(2.0, abs=0.01)

        # Apply 20 obs at 10.0 — EWMA with alpha=0.05 for "low" grade
        for _ in range(20):
            _inject_obs(engine, _make_heat_obs(k_active=10.0))
        updated = engine.get_thermal_model()["heating_rate_f_per_hour"]
        # Model should have moved meaningfully toward 10.0
        assert updated > initial

    def test_observation_counts_correct(self, tmp_path: Path):
        """observation_count_heat and observation_count_cool reflect injected counts."""
        engine = _make_engine(tmp_path)
        for _ in range(3):
            _inject_obs(engine, _make_heat_obs())
        for _ in range(2):
            _inject_obs(engine, _make_cool_obs())
        model = engine.get_thermal_model()
        assert model["observation_count_heat"] == 3
        assert model["observation_count_cool"] == 2
        assert model["observation_count_total"] == 5

    def test_k_passive_in_model(self, tmp_path: Path):
        """k_passive is present in the model after an observation."""
        engine = _make_engine(tmp_path)
        _inject_obs(engine, _make_heat_obs())
        model = engine.get_thermal_model()
        assert model["k_passive"] is not None
        assert model["k_passive"] < 0  # always negative


# ---------------------------------------------------------------------------
# TestWeatherBiasComputation
# ---------------------------------------------------------------------------


class TestWeatherBiasComputation:
    """Tests for get_weather_bias()."""

    def test_none_confidence_when_no_records_with_forecast_data(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Records without forecast fields
        for i in range(10):
            engine._state.records.append({"date": f"2026-03-{i + 1:02d}", "day_type": "mild"})
        bias = engine.get_weather_bias()
        assert bias["confidence"] == "none"

    def test_low_confidence_at_7_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for i in range(7):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 1:02d}",
                    forecast_high=75.0,
                    observed_high=77.0,
                    forecast_low=55.0,
                    observed_low=56.0,
                )
            )
        bias = engine.get_weather_bias()
        assert bias["confidence"] == "low"

    def test_high_bias_is_mean_of_high_errors(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # 5 records: observed - forecast = [2, 3, 4, 5, 6] → mean = 4.0
        errors = [2.0, 3.0, 4.0, 5.0, 6.0]
        for i, err in enumerate(errors):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=70.0 + err,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        # 5 records is below MIN_WEATHER_BIAS_OBSERVATIONS (7), so confidence is "none"
        # Add 2 more with same pattern
        for i, err in enumerate([2.0, 3.0]):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 6:02d}",
                    forecast_high=70.0,
                    observed_high=70.0 + err,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        bias = engine.get_weather_bias()
        expected_mean = sum([2.0, 3.0, 4.0, 5.0, 6.0, 2.0, 3.0]) / 7
        assert bias["high_bias"] == pytest.approx(expected_mean, abs=0.01)

    def test_bias_skips_records_missing_any_forecast_field(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Mix: some complete, some with None
        for i in range(7):
            if i < 3:
                # Missing observed_high_f
                engine._state.records.append(
                    _make_daily_record(
                        f"2026-03-{i + 1:02d}",
                        forecast_high=70.0,
                        observed_high=None,
                        forecast_low=50.0,
                        observed_low=51.0,
                    )
                )
            else:
                engine._state.records.append(
                    _make_daily_record(
                        f"2026-03-{i + 1:02d}",
                        forecast_high=70.0,
                        observed_high=72.0,
                        forecast_low=50.0,
                        observed_low=51.0,
                    )
                )
        bias = engine.get_weather_bias()
        # Only 4 complete records — below MIN_WEATHER_BIAS_OBSERVATIONS (7)
        assert bias["confidence"] == "none"

    def test_uses_only_last_30_records_for_bias(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # First 5: high error 20°F (outliers)
        for i in range(5):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-01-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=90.0,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        # Next 30: high error 2°F
        for i in range(30):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-02-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=72.0,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        bias = engine.get_weather_bias()
        # Only last WEATHER_BIAS_MAX_OBS (30) used — all have error=2.0
        assert bias["high_bias"] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# TestThermalModelBackwardCompat
# ---------------------------------------------------------------------------


class TestThermalModelBackwardCompat:
    """Backward compatibility when thermal_observations key is absent from state."""

    def test_load_state_without_thermal_observations_key(self, tmp_path: Path):
        """LearningState without thermal_observations → get_thermal_model confidence == 'none'."""
        engine = LearningEngine(tmp_path)
        # Manually set state without thermal_observations
        engine._state = LearningState(
            records=[],
            active_suggestions=[],
            dismissed_suggestions=[],
            settings_history=[],
            thermal_observations=[],
        )
        model = engine.get_thermal_model()
        assert model["confidence"] == "none"


# ---------------------------------------------------------------------------
# TestVentSplitCacheRegression (Issue #587, Part 3.4 items 11-13)
# ---------------------------------------------------------------------------


class TestVentSplitCacheRegression:
    """Cache/confidence/removal regression tests for the k_vent/fan_only_decay retirement."""

    def test_k_vent_fully_removed_from_cache_and_status(self, tmp_path: Path):
        """ "k_vent" must be genuinely ABSENT from get_thermal_model()'s dict, not lingering None.

        Distinguishes "removed" from "present but null" — a caller doing
        ``"k_vent" in model`` must see False, not True-with-None-value.
        """
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert "k_vent" not in model, "k_vent key must be fully removed from get_thermal_model()"
        assert "observation_count_fan_only" not in model, "observation_count_fan_only key must be fully removed"
        assert "observation_count_vent" not in model, (
            "observation_count_vent (pre-#587 name) must be fully removed — replaced by "
            "observation_count_vent_window/observation_count_vent_fan"
        )
        # The replacement fields must be present (even if None/0 on a fresh install).
        assert "k_vent_fan" in model
        assert "observation_count_vent_window" in model
        assert "observation_count_vent_fan" in model
        assert model["k_vent_fan"] is None
        assert model["observation_count_vent_window"] == 0
        assert model["observation_count_vent_fan"] == 0

        # Same check against the raw cache dict (not just the get_thermal_model() view) —
        # force cache creation via a real commit first (cache is None before any obs).
        _inject_obs(
            engine,
            {"date": _TODAY, "hvac_mode": "passive", "k_passive": -0.05, "confidence_grade": "high"},
        )
        cache = engine._state.thermal_model_cache
        assert cache is not None
        assert "k_vent" not in cache
        assert "observation_count_fan_only" not in cache

    def test_grade_passive_confidence_excludes_vent_observations(self, tmp_path: Path):
        """_grade_passive_confidence must NOT count vent_window/vent_fan observations.

        Issue #587 deliberately drops the old fan_only_decay confidence-credit
        approximation rather than replacing it with vent_window/vent_fan counts —
        those regimes are NOT close proxies for envelope-only decay (they measure a
        materially larger, different rate). Confirms confidence stays "none" even
        after many vent-only commits, with no passive/heat/cool observations at all.
        """
        from custom_components.climate_advisor.const import THERMAL_PASSIVE_CONF_LOW
        from custom_components.climate_advisor.learning import _grade_passive_confidence

        engine = _make_engine(tmp_path)
        for _i in range(THERMAL_PASSIVE_CONF_LOW + 10):
            _inject_obs(
                engine,
                {
                    "date": _TODAY,
                    "hvac_mode": "vent_window",
                    "k_passive": -0.1,
                    "confidence_grade": "high",
                },
            )
            _inject_obs(
                engine,
                {
                    "date": _TODAY,
                    "hvac_mode": "vent_fan",
                    "k_passive": -0.2,
                    "confidence_grade": "high",
                },
            )

        cache = engine._state.thermal_model_cache
        assert cache["observation_count_vent_window"] >= THERMAL_PASSIVE_CONF_LOW + 10
        assert cache["observation_count_vent_fan"] >= THERMAL_PASSIVE_CONF_LOW + 10
        # k_passive itself must remain untouched (envelope guard) ...
        assert cache["k_passive"] is None
        # ... and confidence must still read "none" — vent commits alone never count.
        assert _grade_passive_confidence(cache) == "none"

    def test_k_vent_window_ewma_not_reset_on_upgrade(self, tmp_path: Path):
        """A pre-existing k_vent_window value survives seeding — no hard reset on upgrade.

        Issue #587's design explicitly rejects zeroing k_vent_window's accumulated EWMA
        value on redefinition (no reset-on-redefinition precedent exists anywhere in this
        codebase) — new, correctly-scoped vent_window_decay observations blend in via the
        normal EWMA recency-weighting instead.
        """
        engine = _make_engine(tmp_path)
        # Seed a pre-upgrade-style k_vent_window value directly (simulates a value that
        # accumulated under the old, wider "ventilated" definition before this upgrade).
        _inject_obs(
            engine,
            {
                "date": _TODAY,
                "hvac_mode": "vent_window",
                "k_passive": -0.30,
                "confidence_grade": "low",
            },
        )
        pre_upgrade_value = engine._state.thermal_model_cache["k_vent_window"]
        assert pre_upgrade_value == pytest.approx(-0.30, abs=1e-9)

        # A new, correctly-scoped commit blends in via EWMA — it must NOT reset to None
        # or jump straight to the new value; it must be strictly between the old value
        # and the new one (a proper EWMA blend), proving no hard reset occurred.
        _inject_obs(
            engine,
            {
                "date": _TODAY,
                "hvac_mode": "vent_window",
                "k_passive": -0.10,
                "confidence_grade": "low",
            },
        )
        post_value = engine._state.thermal_model_cache["k_vent_window"]
        assert post_value is not None, "k_vent_window must not be reset to None on a new commit"
        assert -0.30 < post_value < -0.10, (
            f"expected an EWMA blend strictly between -0.30 and -0.10 (no hard reset); got {post_value}"
        )
