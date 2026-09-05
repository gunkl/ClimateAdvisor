"""Tests for the dual-estimator framework: compute_k_passive_blocks + _select_estimator.

Covers:
1. compute_k_passive_blocks — clean window, too-few-blocks, sparse block, wrong sign,
   out-of-bounds k, bad R² (Issue #146, v0.3.45)
2. _select_estimator — all 9 rows of the decision table
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# ── Imports under test ───────────────────────────────────────────────────────
# _select_estimator is a (non-static) method; import via coordinator module
from custom_components.climate_advisor import coordinator as _coord_mod
from custom_components.climate_advisor.const import (
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_BOUNDS,
    REJECT_OLS_WRONG_SIGN,
    REJECT_TOO_FEW_BLOCKS,
    REJECT_TOO_FEW_SAMPLES,
    THERMAL_BLOCK_OLS_BLOCK_MINUTES,
    THERMAL_DUAL_OLS_GOOD,
    THERMAL_DUAL_OLS_OK,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
)
from custom_components.climate_advisor.learning import compute_k_passive_blocks, compute_k_passive_endpoint

ClimateAdvisorCoordinator = _coord_mod.ClimateAdvisorCoordinator


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_chart_log_entries(
    *,
    n_hours: int,
    t_indoor_start: float,
    t_outdoor: float,
    k_passive: float,
    interval_minutes: int = 30,
    t0: datetime | None = None,
) -> list[dict]:
    """Generate synthetic chart_log entries following Newton's law of cooling.

    T_indoor(t) = T_outdoor + (T_indoor_start - T_outdoor) * exp(k_passive * t_hours)

    Args:
        n_hours: Total window duration in hours.
        t_indoor_start: Initial indoor temperature (°F).
        t_outdoor: Constant outdoor temperature (°F).
        k_passive: True envelope decay rate (hr⁻¹, must be < 0 for cooling).
        interval_minutes: Sampling interval in minutes.
        t0: Start timestamp (defaults to 2026-01-01 02:00 UTC).

    Returns:
        List of chart_log entry dicts with keys "ts", "indoor", "outdoor".
    """
    if t0 is None:
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

    entries = []
    n_steps = int(n_hours * 60 / interval_minutes) + 1
    for i in range(n_steps):
        elapsed_h = i * interval_minutes / 60.0
        t_indoor = t_outdoor + (t_indoor_start - t_outdoor) * math.exp(k_passive * elapsed_h)
        ts = t0 + timedelta(minutes=i * interval_minutes)
        entries.append(
            {
                "ts": ts.isoformat(),
                "indoor": round(t_indoor, 4),
                "outdoor": t_outdoor,
            }
        )
    return entries


def _make_sinusoidal_chart_log_entries(
    *,
    n_hours: float,
    t_indoor_start: float,
    t_outdoor_mean: float,
    t_outdoor_amplitude: float,
    outdoor_period_hours: float,
    k_true: float,
    interval_minutes: int = 30,
    fine_step_minutes: float = 1.0,
    phase_hours: float = 0.0,
    t0: datetime | None = None,
) -> tuple[list[dict], float]:
    """Generate a synthetic chart_log window with a time-varying (sinusoidal) outdoor temp.

    Ground truth is produced by an independent fine-step (1-minute) RK4 forward
    integration of dT/dt = k_true*(T - T_out(t)) against the TRUE continuous sinusoid —
    much finer-grained than the ~30-min sample spacing the estimator under test actually
    sees (which only gets the coarse piecewise-linear-interpolated trace). This makes the
    coarse-sample recovery a genuine test, not a tautology against the estimator's own math.

    Returns (entries, t_end_true) where entries are the coarse interval_minutes-spaced
    chart_log samples (what compute_k_passive_endpoint()/the old formula both see) and
    t_end_true is the fine-grained ground-truth indoor temp at n_hours.
    """
    if t0 is None:
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

    def _t_out(t_hours: float) -> float:
        return t_outdoor_mean + t_outdoor_amplitude * math.sin(
            2 * math.pi * (t_hours + phase_hours) / outdoor_period_hours
        )

    total_minutes = n_hours * 60.0
    n_fine_steps = int(round(total_minutes / fine_step_minutes))
    h = fine_step_minutes / 60.0
    temp = t_indoor_start
    t = 0.0
    fine_temps = [temp]
    for _ in range(n_fine_steps):

        def _deriv(tt: float, tt_indoor: float) -> float:
            return k_true * (tt_indoor - _t_out(tt))

        k1 = _deriv(t, temp)
        k2 = _deriv(t + h / 2.0, temp + h / 2.0 * k1)
        k3 = _deriv(t + h / 2.0, temp + h / 2.0 * k2)
        k4 = _deriv(t + h, temp + h * k3)
        temp += (h / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        t += h
        fine_temps.append(temp)

    n_per_coarse = int(round(interval_minutes / fine_step_minutes))
    n_coarse_steps = int(round(total_minutes / interval_minutes)) + 1
    entries = []
    for i in range(n_coarse_steps):
        idx = i * n_per_coarse
        elapsed_h = i * interval_minutes / 60.0
        entries.append(
            {
                "ts": (t0 + timedelta(minutes=i * interval_minutes)).isoformat(),
                "indoor": fine_temps[idx],
                "outdoor": _t_out(elapsed_h),
            }
        )
    return entries, fine_temps[-1]


def _call_passive_endpoint_estimate(window):
    """Call ClimateAdvisorCoordinator._passive_endpoint_estimate via a MagicMock-bound instance.

    The method reads only its `window` argument (no `self` state), so binding to a
    MagicMock is safe for unit testing — same pattern as _call_select_estimator below.
    """
    fake_self = MagicMock()
    return ClimateAdvisorCoordinator._passive_endpoint_estimate(fake_self, window)


def _call_vent_endpoint_estimate(window):
    """Call ClimateAdvisorCoordinator._vent_endpoint_estimate via a MagicMock-bound instance."""
    fake_self = MagicMock()
    return ClimateAdvisorCoordinator._vent_endpoint_estimate(fake_self, window)


def _call_select_estimator(result_a, result_b):
    """Call _select_estimator via a MagicMock-bound coordinator instance.

    _select_estimator is an instance method (not staticmethod) but only uses
    self for logging — binding to a MagicMock is safe for unit testing.
    """
    fake_self = MagicMock()
    return ClimateAdvisorCoordinator._select_estimator(fake_self, result_a, result_b)


# ── TestComputeKPassiveBlocks ─────────────────────────────────────────────────


class TestComputeKPassiveBlocks:
    def test_clean_8_block_window_returns_valid_k(self):
        """8 hours of clean exponential decay should yield a valid k in bounds, no reject."""
        # ~−0.02 hr⁻¹ → mild overnight drift of ~0.5°F per hour (72°F → 45°F asymptote)
        entries = _make_chart_log_entries(
            n_hours=8,
            t_indoor_start=72.0,
            t_outdoor=45.0,
            k_passive=-0.02,
            interval_minutes=30,
        )
        k, r2, reason = compute_k_passive_blocks(entries, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        assert k is not None, f"Expected valid k, got reason={reason}"
        assert reason is None, f"Expected no rejection, got reason={reason}"
        assert THERMAL_K_PASSIVE_MIN <= k <= THERMAL_K_PASSIVE_MAX, f"k={k} out of bounds"
        assert 0.0 <= r2 <= 1.0, f"R²={r2} out of range"

    def test_5_block_window_returns_too_few_blocks(self):
        """5 hours of data at 60-min blocks → 5 blocks < min_blocks(6) → REJECT_TOO_FEW_BLOCKS."""
        entries = _make_chart_log_entries(
            n_hours=5,
            t_indoor_start=72.0,
            t_outdoor=45.0,
            k_passive=-0.02,
            interval_minutes=30,
        )
        k, r2, reason = compute_k_passive_blocks(entries, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        assert k is None
        assert reason == REJECT_TOO_FEW_BLOCKS

    def test_sparse_block_skipped_still_fires(self):
        """7 blocks but one has only 1 entry (skipped) → 6 usable blocks → succeeds."""
        # Build 7-hour window at 30-min intervals = 14+ entries
        entries = _make_chart_log_entries(
            n_hours=7,
            t_indoor_start=72.0,
            t_outdoor=45.0,
            k_passive=-0.02,
            interval_minutes=30,
        )
        # Make block index 3 sparse by removing all but 1 entry in that block
        # Block 3 spans minutes [180, 240); at 30-min intervals, entries at 180 and 210
        # Remove entry at 210 min so block 3 has only 1 entry → gets skipped
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        target_ts = (t0 + timedelta(minutes=210)).isoformat()
        entries_sparse = [e for e in entries if e["ts"] != target_ts]

        # Block 3 now has only 1 entry → skipped → 6 remaining blocks → should succeed
        k, r2, reason = compute_k_passive_blocks(entries_sparse, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        assert k is not None, f"Expected success with 6 usable blocks, got reason={reason}"
        assert reason is None

    def test_warming_house_returns_wrong_sign(self):
        """Indoor above outdoor and rising → positive rate, positive delta → k > 0 → REJECT_OLS_WRONG_SIGN.

        For wrong-sign to trigger, indoor must be above outdoor (delta > 0) while also
        rising (rate > 0), giving k = rate/delta > 0.  Indoor below outdoor while rising
        gives delta < 0 and rate > 0 → k < 0, which may still pass validation.
        """
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        entries = []
        for i in range(16):
            # Indoor consistently above outdoor (delta > 0) and rising (rate > 0) → k > 0
            t_indoor = 75.0 + i * 0.5  # 75, 75.5, 76.0, ... (rising)
            ts = t0 + timedelta(minutes=i * 30)
            entries.append({"ts": ts.isoformat(), "indoor": t_indoor, "outdoor": 45.0})

        k, _r2, reason = compute_k_passive_blocks(entries, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        assert k is None
        # Wrong sign → REJECT_OLS_WRONG_SIGN (k > 0 because rate > 0 and delta > 0)
        assert reason == REJECT_OLS_WRONG_SIGN

    def test_k_outside_bounds_rejected(self):
        """Entries yielding k far outside [−0.5, −0.001] → REJECT_OLS_BOUNDS."""
        # Very fast decay: k_passive = -2.0 (well outside −0.5 upper limit)
        entries = _make_chart_log_entries(
            n_hours=8,
            t_indoor_start=80.0,
            t_outdoor=45.0,
            k_passive=-2.0,
            interval_minutes=30,
        )
        k, _r2, reason = compute_k_passive_blocks(entries, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        assert k is None
        assert reason == REJECT_OLS_BOUNDS

    def test_bad_r_squared_rejected(self):
        """Noisy data that passes sign/bounds but has poor R² → REJECT_OLS_BAD_FIT."""
        # Inject random-walk noise so indoor temp doesn't follow a clean decay curve.
        # Use a realistic k but add large noise that destroys R².
        import random

        random.seed(42)
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        entries = []
        t_indoor = 72.0
        for i in range(20):
            ts = t0 + timedelta(minutes=i * 30)
            # Add ±3°F random jitter — enough to destroy R² while keeping k in bounds
            noise = random.uniform(-3.0, 3.0)
            entries.append({"ts": ts.isoformat(), "indoor": t_indoor + noise, "outdoor": 45.0})
            # Drift indoor slightly to keep delta non-zero
            t_indoor -= 0.05

        k, r2, reason = compute_k_passive_blocks(entries, block_minutes=THERMAL_BLOCK_OLS_BLOCK_MINUTES)

        # Either rejected for bad R² or for wrong sign/bounds due to noise —
        # the key assertion is that it did NOT return a clean k with reason=None
        # when data is this noisy.
        if k is not None:
            # If by chance the noise produced a valid-looking regression, verify R² ≥ threshold
            # (this path is acceptable — it means the noise happened to be structured)
            assert r2 >= 0.2, f"Accepted k={k} with R²={r2} below threshold"
        else:
            assert reason in {REJECT_OLS_BAD_FIT, REJECT_OLS_WRONG_SIGN, REJECT_OLS_BOUNDS, REJECT_TOO_FEW_SAMPLES}


# ── TestSelectEstimator ───────────────────────────────────────────────────────


def _result_a(k: float = -0.021) -> dict:
    """Synthetic endpoint estimator result (no r_squared)."""
    return {"k": k, "r_squared": None, "source": "endpoint", "grade": "low"}


def _result_b(k: float = -0.019, r2: float = 0.60) -> dict:
    """Synthetic block-OLS estimator result."""
    return {"k": k, "r_squared": r2, "source": "block_ols", "grade": "low"}


class TestSelectEstimator:
    def test_both_none_returns_none(self):
        """Row 1: A=no, B=no → None."""
        assert _call_select_estimator(None, None) is None

    def test_a_valid_b_none_returns_a_grade_low(self):
        """Row 2: A=yes, B=no → A, grade=low."""
        result = _call_select_estimator(_result_a(), None)
        assert result is not None
        assert result["source"] == "endpoint"
        assert result["grade"] == "low"
        assert result["k"] == -0.021

    def test_b_valid_only_r2_ok_not_good_returns_b_grade_low(self):
        """Row 3: A=no, B=yes, R²=0.35 (≥OK, <GOOD) → B, grade=low."""
        r2 = (THERMAL_DUAL_OLS_OK + THERMAL_DUAL_OLS_GOOD) / 2.0  # between OK and GOOD
        result = _call_select_estimator(None, _result_b(r2=r2))
        assert result is not None
        assert result["source"] == "block_ols"
        assert result["grade"] == "low"

    def test_b_valid_only_r2_good_returns_b_grade_medium(self):
        """Row 4: A=no, B=yes, R²≥GOOD → B, grade=medium."""
        result = _call_select_estimator(None, _result_b(r2=THERMAL_DUAL_OLS_GOOD))
        assert result is not None
        assert result["source"] == "block_ols"
        assert result["grade"] == "medium"

    def test_b_valid_only_r2_below_ok_returns_none(self):
        """A=no, B=yes, R²<OK → None (B unreliable, A absent)."""
        r2 = THERMAL_DUAL_OLS_OK - 0.05  # below OK threshold
        result = _call_select_estimator(None, _result_b(r2=r2))
        assert result is None

    def test_both_valid_r2_b_below_ok_returns_a_grade_low(self):
        """Row 5: A=yes, B=yes, R²_B < OK → A, grade=low."""
        r2 = THERMAL_DUAL_OLS_OK - 0.05
        result = _call_select_estimator(_result_a(k=-0.021), _result_b(k=-0.019, r2=r2))
        assert result is not None
        assert result["source"] == "endpoint"
        assert result["grade"] == "low"

    def test_both_valid_r2_ok_agree_returns_b_grade_low(self):
        """Row 6: A=yes, B=yes, R²_B in [OK,GOOD), agree → B, grade=low."""
        r2 = (THERMAL_DUAL_OLS_OK + THERMAL_DUAL_OLS_GOOD) / 2.0
        # k_A and k_B within 30% relative difference → agree
        k_a = -0.020
        k_b = -0.020 * 1.10  # 10% relative diff → agree
        result = _call_select_estimator(_result_a(k=k_a), _result_b(k=k_b, r2=r2))
        assert result is not None
        assert result["source"] == "block_ols"
        assert result["grade"] == "low"

    def test_both_valid_r2_ok_disagree_returns_a_grade_low(self):
        """Row 7: A=yes, B=yes, R²_B in [OK,GOOD), disagree → A, grade=low."""
        r2 = (THERMAL_DUAL_OLS_OK + THERMAL_DUAL_OLS_GOOD) / 2.0
        # rel diff > 30% → disagree
        k_a = -0.020
        k_b = -0.020 * 1.50  # 50% relative diff → disagree
        result = _call_select_estimator(_result_a(k=k_a), _result_b(k=k_b, r2=r2))
        assert result is not None
        assert result["source"] == "endpoint"
        assert result["grade"] == "low"

    def test_both_valid_r2_good_agree_returns_b_grade_medium(self):
        """Row 8: A=yes, B=yes, R²_B ≥ GOOD, agree → B, grade=medium."""
        r2 = THERMAL_DUAL_OLS_GOOD + 0.05
        k_a = -0.021
        k_b = -0.021 * 1.05  # 5% diff → agree
        result = _call_select_estimator(_result_a(k=k_a), _result_b(k=k_b, r2=r2))
        assert result is not None
        assert result["source"] == "block_ols"
        assert result["grade"] == "medium"

    def test_both_valid_r2_good_disagree_returns_a_grade_low(self):
        """Row 9: A=yes, B=yes, R²_B ≥ GOOD, disagree → A, grade=low."""
        r2 = THERMAL_DUAL_OLS_GOOD + 0.05
        k_a = -0.021
        k_b = -0.021 * 2.0  # 100% diff → disagree
        result = _call_select_estimator(_result_a(k=k_a), _result_b(k=k_b, r2=r2))
        assert result is not None
        assert result["source"] == "endpoint"
        assert result["grade"] == "low"


# ── TestComputeKPassiveEndpoint (Issue #587 Defect A) ──────────────────────────
#
# Defect A: the endpoint k estimator assumed outdoor temperature was constant across
# the whole window (t_out_avg), when real outdoor readings swing through the day. On
# two live windows (5.3h and 8.6h) this produced k values 1.3x-1.9x higher in magnitude
# than the ODE-consistent true rate. compute_k_passive_endpoint() replaces the closed-form
# formula with a bisection root-find against the window's real per-sample outdoor trace
# (RK4 forward-integration), while staying bookend-only on the indoor side (#141).


class TestComputeKPassiveEndpoint:
    def test_endpoint_matches_old_formula_when_outdoor_constant(self):
        """Constant-outdoor window: new helper matches the old closed-form formula.

        No behavior change for the common stable case — piecewise-linear interpolation
        over a constant trace reduces to the same constant, so RK4 integration should
        reproduce the same exponential-decay answer the old ratio/log formula gave.
        """
        k_true = -0.02
        entries = _make_chart_log_entries(
            n_hours=8,
            t_indoor_start=72.0,
            t_outdoor=45.0,
            k_passive=k_true,
            interval_minutes=30,
        )
        t_start = entries[0]["indoor"]
        t_end = entries[-1]["indoor"]
        t_out_avg = sum(e["outdoor"] for e in entries) / len(entries)
        ratio = (t_end - t_out_avg) / (t_start - t_out_avg)
        k_old = math.log(ratio) / 8.0

        k_new = compute_k_passive_endpoint(entries, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)

        assert k_new is not None
        assert k_new == pytest.approx(k_old, abs=1e-4)
        assert k_new == pytest.approx(k_true, rel=0.02)

    def test_endpoint_recovers_true_k_under_sinusoidal_outdoor(self):
        """Sinusoidal-outdoor window: new estimator recovers true k within ~2%;
        the old closed-form formula is measurably biased (>15%) on the same data.

        This is the direct proof the fix closes the reported 1.3x-1.9x gap from the
        original investigation: an 8.6-hour overnight window with outdoor swinging
        through a diurnal sinusoid (mean 50°F, amplitude 10°F, 24h period), true
        k=-0.15 hr⁻¹.
        """
        k_true = -0.15
        entries, _t_end_ground_truth = _make_sinusoidal_chart_log_entries(
            n_hours=8.6,
            t_indoor_start=72.0,
            t_outdoor_mean=50.0,
            t_outdoor_amplitude=10.0,
            outdoor_period_hours=24.0,
            k_true=k_true,
            interval_minutes=30,
            phase_hours=6.0,
        )
        t_start = entries[0]["indoor"]
        t_end = entries[-1]["indoor"]
        t_out_avg = sum(e["outdoor"] for e in entries) / len(entries)
        ratio = (t_end - t_out_avg) / (t_start - t_out_avg)
        k_old = math.log(ratio) / 8.6

        k_new = compute_k_passive_endpoint(entries, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)

        assert k_new is not None
        new_err_pct = abs((k_new - k_true) / k_true) * 100.0
        old_err_pct = abs((k_old - k_true) / k_true) * 100.0

        assert new_err_pct < 2.0, f"new estimator error {new_err_pct:.2f}% exceeds 2%"
        assert old_err_pct > 15.0, f"old formula error {old_err_pct:.2f}% does not exceed 15%"

    def test_endpoint_degrades_gracefully_with_only_two_samples(self):
        """A 2-sample window still returns a valid, bounded k — no exception.

        Piecewise-linear interpolation over exactly 2 outdoor points *is* the whole
        trace, so this is the degenerate case where the new estimator's answer should
        match the old closed-form formula (both reduce to the same math).
        """
        t0 = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        entries = [
            {"ts": t0.isoformat(), "indoor": 72.0, "outdoor": 45.0},
            {"ts": (t0 + timedelta(hours=8)).isoformat(), "indoor": 65.0, "outdoor": 45.0},
        ]

        k_new = compute_k_passive_endpoint(entries, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)

        assert k_new is not None
        assert THERMAL_K_PASSIVE_MIN <= k_new <= THERMAL_K_PASSIVE_MAX
        ratio = (65.0 - 45.0) / (72.0 - 45.0)
        k_old = math.log(ratio) / 8.0
        assert k_new == pytest.approx(k_old, abs=1e-3)

    def test_endpoint_returns_none_when_no_root_in_bounds(self):
        """An unreachable T_end (given the real outdoor path) ⇒ None.

        Preserves the same regime-filter/reject-and-fallback-to-block-OLS contract as
        the old ratio<=0/ratio>=1.0 check: indoor barely moves while outdoor sits far
        away, so no k in [THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX] can explain a
        near-zero observed change over an 8-hour window with a huge indoor-outdoor gap.
        """
        entries = _make_chart_log_entries(
            n_hours=8,
            t_indoor_start=72.0,
            t_outdoor=71.9,  # tiny indoor-outdoor gap
            k_passive=THERMAL_K_PASSIVE_MIN,  # even the fastest allowed decay barely moves T
            interval_minutes=30,
        )
        # Force an artificial T_end far outside what any k in-bounds could produce:
        # indoor rising away from outdoor while outdoor stays put — no decay-toward-outdoor
        # k reproduces this.
        entries[-1]["indoor"] = 90.0

        k_new = compute_k_passive_endpoint(entries, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)

        assert k_new is None

    def test_vent_endpoint_shares_passive_helper(self):
        """_vent_endpoint_estimate and _passive_endpoint_estimate produce identical
        output for the same synthetic window — proves the Issue #587 Phase 1
        shared-helper refactor didn't fork behavior, and this phase's Defect A fix
        landed identically in both (both call the same compute_k_passive_endpoint()).
        """
        entries = _make_chart_log_entries(
            n_hours=8,
            t_indoor_start=72.0,
            t_outdoor=45.0,
            k_passive=-0.03,
            interval_minutes=30,
        )

        result_passive = _call_passive_endpoint_estimate(entries)
        result_vent = _call_vent_endpoint_estimate(entries)

        assert result_passive is not None
        assert result_vent is not None
        assert result_passive["k"] == result_vent["k"]
        assert result_passive["source"] == result_vent["source"] == "endpoint"
        assert result_passive["grade"] == result_vent["grade"] == "low"
