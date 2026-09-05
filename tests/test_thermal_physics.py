"""Pure-math tests for the thermal physics engine (Issue #114).

Tests compute_k_passive(), compute_k_active(), _smooth_temps(), and the
EWMA accumulation in LearningEngine — no HA dependencies.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from custom_components.climate_advisor.learning import (
    LearningEngine,
    _smooth_temps,
    compute_k_active,
    compute_k_passive,
    compute_k_solar,
)

_FAKE_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_FAKE_NOW_PATCH = "custom_components.climate_advisor.learning.dt_util"

# ---------------------------------------------------------------------------
# Helpers — synthetic sample generation
# ---------------------------------------------------------------------------


def _make_decay_samples(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    n: int,
    dt_minutes: float = 1.0,
    round_to_int: bool = False,
    elapsed_offset: float = 0.0,
) -> list[dict]:
    """Generate synthetic post-heat exponential decay samples.

    Uses the ODE solution: T(t) = t_outdoor + (T0 - t_outdoor) * exp(k_passive * t_hr)
    """
    samples = []
    for i in range(n):
        t_hr = i * dt_minutes / 60.0
        indoor = t_outdoor + (t_start - t_outdoor) * math.exp(k_passive * t_hr)
        if round_to_int:
            indoor = float(round(indoor))
        samples.append(
            {
                "timestamp": f"2026-04-01T12:{i:02d}:00",
                "indoor_temp_f": indoor,
                "outdoor_temp_f": t_outdoor,
                "elapsed_minutes": elapsed_offset + i * dt_minutes,
            }
        )
    return samples


def _make_active_samples(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float,
    n: int,
    dt_minutes: float = 1.0,
) -> list[dict]:
    """Generate active heating/cooling samples using the full ODE solution.

    T(t+dt) = T_out + (T - T_out)*exp(k_p*dt) + (k_a/k_p)*(exp(k_p*dt) - 1)
    """
    samples = []
    T = t_start
    dt_hr = dt_minutes / 60.0
    exp_kp = math.exp(k_passive * dt_hr)
    for i in range(n):
        samples.append(
            {
                "timestamp": f"2026-04-01T10:{i:02d}:00",
                "indoor_temp_f": T,
                "outdoor_temp_f": t_outdoor,
                "elapsed_minutes": float(i * dt_minutes),
            }
        )
        T = t_outdoor + (T - t_outdoor) * exp_kp + (k_active / k_passive) * (exp_kp - 1)
    return samples


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _record_obs(engine: LearningEngine, obs: dict) -> None:
    """Record observation with dt_util.now() patched to a fixed datetime."""
    mock_dt = type("MockDt", (), {"now": staticmethod(lambda: _FAKE_NOW)})()
    with patch(_FAKE_NOW_PATCH, mock_dt):
        engine.record_thermal_observation(obs)


# ---------------------------------------------------------------------------
# Tests: _smooth_temps
# ---------------------------------------------------------------------------


def test_smooth_temps_passthrough_short():
    """Less than 3 samples → returned unchanged."""
    result = _smooth_temps([70.0, 71.0])
    assert result == [70.0, 71.0]


def test_smooth_temps_centered_average():
    """Middle elements are averaged; edges are preserved."""
    result = _smooth_temps([70.0, 72.0, 74.0, 72.0, 70.0])
    assert result[0] == 70.0
    assert result[-1] == 70.0
    # Middle: (70+72+74)/3 = 72.0
    assert abs(result[1] - 72.0) < 0.01


# ---------------------------------------------------------------------------
# Tests: compute_k_passive
# ---------------------------------------------------------------------------


def test_k_passive_from_clean_decay():
    """Synthetic exponential decay with known k_p → recovered within ±0.01."""
    true_k_p = -0.05
    samples = _make_decay_samples(t_start=75.0, t_outdoor=40.0, k_passive=true_k_p, n=25)
    k_p, r2, _code = compute_k_passive(samples)
    assert k_p is not None, "k_passive should not be None for clean data"
    assert abs(k_p - true_k_p) < 0.01, f"Expected k_passive≈{true_k_p}, got {k_p}"
    assert r2 >= 0.9, f"Expected R²≥0.9 for clean data, got {r2}"


def test_k_passive_with_integer_noise():
    """1°F integer rounding still yields valid k_passive.

    Uses 5-minute sample intervals and faster decay (k_p=-0.2) so each interval
    moves ~1°F — enough for the regression to see meaningful signal despite rounding.
    """
    true_k_p = -0.20
    samples = _make_decay_samples(
        t_start=80.0, t_outdoor=45.0, k_passive=true_k_p, n=25, dt_minutes=5.0, round_to_int=True
    )
    k_p, r2, _code = compute_k_passive(samples)
    assert k_p is not None, f"k_passive should survive integer rounding noise (R²={r2})"
    assert r2 >= 0.3, f"Expected R²≥0.3 with integer noise, got {r2}"


def test_k_passive_too_few_samples():
    """Fewer than THERMAL_MIN_POST_HEAT_SAMPLES+1 pairs → returns None (Issue #130: threshold=4)."""
    # THERMAL_MIN_POST_HEAT_SAMPLES=4 → need >= 5 samples for 4 pairs.  Use 4 → only 3 pairs.
    samples = _make_decay_samples(t_start=75.0, t_outdoor=40.0, k_passive=-0.05, n=4)
    k_p, r2, _code = compute_k_passive(samples)
    assert k_p is None


def test_observation_rejected_low_r_squared():
    """Random noise samples → R²<0.2 → k_passive returns None."""
    import random

    random.seed(42)
    samples = [
        {
            "timestamp": f"2026-04-01T12:{i:02d}:00",
            "indoor_temp_f": 68.0 + random.uniform(-2, 2),
            "outdoor_temp_f": 45.0,
            "elapsed_minutes": float(i),
        }
        for i in range(20)
    ]
    k_p, r2, _code = compute_k_passive(samples)
    assert k_p is None, f"Expected k_passive=None for noisy data, but got {k_p} (R²={r2})"


def test_k_passive_must_be_negative():
    """Data that implies indoor rising post-heat → k_passive>0 → rejected."""
    # Simulate indoor temp rising (as if heating continued) — physically invalid post-heat
    samples = [
        {
            "timestamp": f"2026-04-01T12:{i:02d}:00",
            "indoor_temp_f": 65.0 + i * 0.3,  # rising
            "outdoor_temp_f": 40.0,
            "elapsed_minutes": float(i),
        }
        for i in range(20)
    ]
    k_p, r2, _code = compute_k_passive(samples)
    assert k_p is None, f"Rising indoor temp should yield k_passive>0, which must be rejected; got {k_p}"


def test_pre_heat_samples_included_in_regression():
    """Adding pre-heat samples should keep R² ≥ post-heat-only baseline."""
    true_k_p = -0.04
    post_samples = _make_decay_samples(t_start=75.0, t_outdoor=40.0, k_passive=true_k_p, n=12)
    pre_samples = _make_decay_samples(t_start=73.0, t_outdoor=40.0, k_passive=true_k_p, n=5, elapsed_offset=-5.0)
    k_p_post_only, r2_post_only, _code1 = compute_k_passive(post_samples)
    k_p_with_pre, r2_with_pre, _code2 = compute_k_passive(post_samples, pre_samples)

    # Both should succeed; with pre-heat R² should be at least as good
    assert k_p_post_only is not None
    assert k_p_with_pre is not None
    assert r2_with_pre >= r2_post_only - 0.05  # allow tiny tolerance


# ---------------------------------------------------------------------------
# Tests: compute_k_active
# ---------------------------------------------------------------------------


def test_k_active_heat_extracted_correctly():
    """Synthetic heating active phase → k_active_heat within ±0.5."""
    true_k_p = -0.05
    true_k_a = 3.0
    active = _make_active_samples(t_start=65.0, t_outdoor=40.0, k_passive=true_k_p, k_active=true_k_a, n=15)
    k_a, r2 = compute_k_active(active, k_passive=true_k_p, session_mode="heat")
    assert k_a is not None, "k_active should be extracted for heating session"
    assert abs(k_a - true_k_a) < 0.5, f"Expected k_active≈{true_k_a}, got {k_a}"


def test_k_active_cool_extracted_correctly():
    """Synthetic cooling active phase → k_active_cool negative, within ±0.5."""
    true_k_p = -0.05
    true_k_a = -2.5  # cooling
    active = _make_active_samples(t_start=78.0, t_outdoor=90.0, k_passive=true_k_p, k_active=true_k_a, n=15)
    k_a, r2 = compute_k_active(active, k_passive=true_k_p, session_mode="cool")
    assert k_a is not None, "k_active should be extracted for cooling session"
    assert k_a < 0, f"k_active_cool should be negative, got {k_a}"
    assert abs(k_a - true_k_a) < 0.5, f"Expected k_active≈{true_k_a}, got {k_a}"


def test_fan_only_k_active_is_none():
    """fan_only session → compute_k_active returns (None, 0.0)."""
    active = _make_decay_samples(t_start=70.0, t_outdoor=65.0, k_passive=-0.03, n=10)
    k_a, r2 = compute_k_active(active, k_passive=-0.03, session_mode="fan_only")
    assert k_a is None


def test_k_active_too_few_active_samples():
    """Single active sample → returns (None, 0.0)."""
    active = _make_active_samples(t_start=65.0, t_outdoor=40.0, k_passive=-0.05, k_active=3.0, n=1)
    k_a, r2 = compute_k_active(active, k_passive=-0.05, session_mode="heat")
    assert k_a is None


# ---------------------------------------------------------------------------
# Tests: compute_k_solar (Issue #587, Defect B)
# ---------------------------------------------------------------------------


def test_k_solar_subtracts_passive_contribution():
    """100%-conduction window (no solar physics at all) → compute_k_solar ≈ 0.

    Direct regression test for the reported bug: the original investigation
    found 21 solar_gain observations on a simulator with zero solar physics,
    all reporting 1.09-4.00°F/hr of pure-conduction warming as "solar gain"
    because the old formula (mean_rate = ΔT_indoor / Δt) attributed the entire
    observed warming rate to solar with no subtraction. These samples are
    generated from the pure decay-toward-outdoor ODE (dT/dt = k_passive*(T-T_out)),
    i.e. warming purely because outdoor is warmer than indoor — exactly the
    "warm simulated outdoor with zero solar physics" scenario from the
    investigation. The old raw mean_rate on this window is large and positive;
    the fixed compute_k_solar must report ≈0 once k_passive's conductive
    contribution is subtracted.
    """
    true_k_p = -0.08
    samples = _make_decay_samples(t_start=68.0, t_outdoor=95.0, k_passive=true_k_p, n=13, dt_minutes=15.0)

    # Old-style raw rate (what the pre-fix code committed as k_solar) — for
    # the report's numeric-proof comparison, not itself asserted here.
    indoor_temps = [s["indoor_temp_f"] for s in samples]
    elapsed = [s["elapsed_minutes"] for s in samples]
    total_dt_hours = (elapsed[-1] - elapsed[0]) / 60.0
    old_style_mean_rate = (indoor_temps[-1] - indoor_temps[0]) / total_dt_hours
    assert old_style_mean_rate > 1.0, "sanity check: old formula should report a large positive rate here"

    k_solar, r2 = compute_k_solar(samples, k_passive=true_k_p)
    assert k_solar is not None
    assert k_solar < 0.1, f"Expected compute_k_solar≈0 for pure-conduction window, got {k_solar}"


def test_k_solar_recovers_known_solar_component():
    """Conductive term + a known added constant solar term → recovers just the solar term.

    Samples are generated from dT/dt = k_passive*(T-T_out) + k_solar_true (the same
    general linear-ODE shape _make_active_samples already uses for k_active) — i.e.
    ordinary conduction plus a genuine additional constant warming contribution.
    compute_k_solar must recover close to k_solar_true, not the raw combined rate.
    """
    true_k_p = -0.06
    true_k_solar = 1.5
    samples = _make_active_samples(t_start=72.0, t_outdoor=78.0, k_passive=true_k_p, k_active=true_k_solar, n=13)

    k_solar, r2 = compute_k_solar(samples, k_passive=true_k_p)
    assert k_solar is not None
    assert abs(k_solar - true_k_solar) < 0.2, f"Expected compute_k_solar≈{true_k_solar}, got {k_solar}"


# ---------------------------------------------------------------------------
# Tests: LearningEngine EWMA accumulation
# ---------------------------------------------------------------------------


def test_ewma_accumulation_high_confidence(tmp_path):
    """3 high-confidence heat observations → k_active_heat converges toward true value."""
    engine = _make_engine(tmp_path)
    true_k_a = 3.0
    true_k_p = -0.05

    obs_base = {
        "event_id": "test-1",
        "timestamp": "2026-04-01T10:00:00",
        "date": "2026-04-01",
        "hvac_mode": "heat",
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": true_k_p,
        "k_active": true_k_a,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.85,
        "r_squared_active": 0.82,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "high",
        "schema_version": 2,
    }

    # Record 3 identical observations — EWMA should stay near true_k_a
    for i in range(3):
        obs = dict(obs_base, event_id=f"test-{i}")
        _record_obs(engine, obs)

    model = engine.get_thermal_model()
    assert model["k_active_heat"] is not None
    assert abs(model["k_active_heat"] - true_k_a) < 0.5


def test_ewma_heat_and_cool_independent(tmp_path):
    """Heat and cool observations accumulate k_active separately."""
    engine = _make_engine(tmp_path)

    heat_obs = {
        "event_id": "h1",
        "timestamp": "2026-04-01T08:00:00",
        "date": "2026-04-01",
        "hvac_mode": "heat",
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": -0.05,
        "k_active": 3.0,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.80,
        "r_squared_active": 0.78,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "medium",
        "schema_version": 2,
    }
    cool_obs = {
        "event_id": "c1",
        "timestamp": "2026-07-01T14:00:00",
        "date": "2026-07-01",
        "hvac_mode": "cool",
        "session_minutes": 10.0,
        "start_indoor_f": 78.0,
        "end_indoor_f": 75.0,
        "peak_indoor_f": 78.0,
        "start_outdoor_f": 90.0,
        "avg_outdoor_f": 90.0,
        "delta_t_avg": -13.0,
        "k_passive": -0.05,
        "k_active": -2.5,
        "passive_baseline_rate": 0.4,
        "r_squared_passive": 0.75,
        "r_squared_active": 0.72,
        "sample_count_pre": 5,
        "sample_count_active": 10,
        "sample_count_post": 12,
        "confidence_grade": "medium",
        "schema_version": 2,
    }

    for _ in range(3):
        _record_obs(engine, dict(heat_obs))
    for _ in range(3):
        _record_obs(engine, dict(cool_obs))

    model = engine.get_thermal_model()
    assert model["k_active_heat"] is not None
    assert model["k_active_cool"] is not None
    assert model["k_active_heat"] > 0, "k_active_heat should be positive (heating)"
    assert model["k_active_cool"] < 0, "k_active_cool should be negative (cooling)"
    assert model["observation_count_heat"] == 3
    assert model["observation_count_cool"] == 3


def test_vent_fan_obs_sets_k_vent_fan_only(tmp_path):
    """vent_fan observation sets k_vent_fan but must NOT write k_passive or k_active_heat/cool.

    Issue #587: fan_only_decay/k_vent are retired outright (the old fan-on/windows-
    closed regime this test used to exercise no longer exists — see
    tests/test_thermal_vent_split.py::test_fan_only_decay_no_longer_triggers for that
    retirement's regression coverage). This test now exercises vent_fan's replacement
    cache field: vent_fan k_p reflects the effective decay rate with windows open AND
    fan/WHF running — a different coefficient from the bare envelope rate. Writing it
    into k_passive would bias the envelope model and corrupt bedtime-setback physics
    predictions.
    """
    engine = _make_engine(tmp_path)

    vent_fan_obs = {
        "event_id": "f1",
        "timestamp": "2026-04-01T20:00:00",
        "date": "2026-04-01",
        "hvac_mode": "vent_fan",
        "session_minutes": 15.0,
        "start_indoor_f": 72.0,
        "end_indoor_f": 71.0,
        "peak_indoor_f": 72.0,
        "start_outdoor_f": 68.0,
        "avg_outdoor_f": 68.0,
        "delta_t_avg": 3.5,
        "k_passive": -0.04,
        "k_active": None,
        "passive_baseline_rate": -0.2,
        "r_squared_passive": 0.72,
        "r_squared_active": None,
        "sample_count_pre": 5,
        "sample_count_active": 10,
        "sample_count_post": 20,
        "confidence_grade": "medium",
        "schema_version": 2,
    }

    _record_obs(engine, vent_fan_obs)
    model = engine.get_thermal_model()
    # k_passive must stay None — vent_fan must not contaminate the envelope model
    assert model["k_passive"] is None, f"k_passive must be None after vent_fan obs; got {model['k_passive']}"
    # k_vent_fan must be set to the observed decay rate
    assert model["k_vent_fan"] is not None and abs(model["k_vent_fan"] - (-0.04)) < 1e-9, (
        f"k_vent_fan should be -0.04 from vent_fan obs; got {model['k_vent_fan']}"
    )
    assert model["k_active_heat"] is None
    assert model["k_active_cool"] is None


def test_legacy_compat_fields_present(tmp_path):
    """get_thermal_model() always returns legacy compat fields."""
    engine = _make_engine(tmp_path)

    obs = {
        "event_id": "x1",
        "timestamp": "2026-04-01T10:00:00",
        "date": "2026-04-01",
        "hvac_mode": "heat",
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": -0.05,
        "k_active": 3.2,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.85,
        "r_squared_active": 0.82,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "high",
        "schema_version": 2,
    }
    for _ in range(5):
        _record_obs(engine, dict(obs))

    model = engine.get_thermal_model()
    # v2 fields
    assert "k_active_heat" in model
    assert "k_active_cool" in model
    assert "k_passive" in model
    # legacy compat
    assert "heating_rate_f_per_hour" in model
    assert "cooling_rate_f_per_hour" in model
    assert "observation_count_heat" in model
    assert "observation_count_cool" in model
    assert "confidence" in model
    # Legacy heating_rate_f_per_hour == k_active_heat (rounded to 2 dp)
    assert abs(model["heating_rate_f_per_hour"] - model["k_active_heat"]) < 0.01


def test_get_thermal_model_empty_returns_none_confidence(tmp_path):
    """No observations → confidence='none', all rates None."""
    engine = _make_engine(tmp_path)
    model = engine.get_thermal_model()
    assert model["confidence"] == "none"
    assert model["k_active_heat"] is None
    assert model["k_active_cool"] is None
    assert model["k_passive"] is None
    assert model["heating_rate_f_per_hour"] is None
    assert model["cooling_rate_f_per_hour"] is None
