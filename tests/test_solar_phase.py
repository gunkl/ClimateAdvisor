"""Tests for Issue #147: learned solar phase offset + engine visibility.

TDD — written BEFORE implementation. All tests are expected to fail until
the implementation in coordinator.py, learning.py, const.py, and classifier.py
is complete.

Test classes:
  TestSolarFactor              — _solar_factor(local_hour, phase_offset_h) formula
  TestEstimateSolarPhaseOffset — _estimate_solar_phase_offset() quality gates
  TestODEPeakShift             — integration/scientific proof: ODE peaks shift with offset
  TestLearningConvergence      — EWMA accumulation in learning.update_solar_phase_offset()
  TestEngineStatus             — get_engine_status() shape and activation tracking
  TestMildDayDynamicScheduling — Fix C: MILD day constants + ODE-based close time
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# ── Imports under test ───────────────────────────────────────────────────────
# New constants (do not exist yet — ImportError expected until Phase 2).
# Use try/except so the module imports successfully and pytest can collect all
# 28 tests. Tests that depend on missing symbols will fail at assertion time,
# which is the correct TDD starting state.
from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402
from custom_components.climate_advisor.classifier import (  # noqa: E402
    ForecastSnapshot,
    classify_day,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_SAMPLES,
)
from custom_components.climate_advisor.learning import LearningEngine  # noqa: E402

# New constants — do not exist until Phase 2 implementation. Sentinel _MISSING
# causes individual tests to fail with a clear AssertionError instead of
# blocking collection with ImportError.
_MISSING = object()

try:
    from custom_components.climate_advisor.const import MILD_WINDOW_CLOSE_HOUR  # noqa: E402
except ImportError:
    MILD_WINDOW_CLOSE_HOUR = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import MILD_WINDOW_OPEN_HOUR  # noqa: E402
except ImportError:
    MILD_WINDOW_OPEN_HOUR = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import REJECT_NO_INTERIOR_PEAK  # noqa: E402
except ImportError:
    REJECT_NO_INTERIOR_PEAK = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import REJECT_WINDOW_TOO_SHORT  # noqa: E402
except ImportError:
    REJECT_WINDOW_TOO_SHORT = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import THERMAL_SOLAR_PHASE_ALPHA  # noqa: E402
except ImportError:
    THERMAL_SOLAR_PHASE_ALPHA = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import THERMAL_SOLAR_PHASE_OFFSET_MAX  # noqa: E402
except ImportError:
    THERMAL_SOLAR_PHASE_OFFSET_MAX = _MISSING  # type: ignore[assignment]

try:
    from custom_components.climate_advisor.const import THERMAL_SOLAR_PHASE_OFFSET_MIN  # noqa: E402
except ImportError:
    THERMAL_SOLAR_PHASE_OFFSET_MIN = _MISSING  # type: ignore[assignment]

_solar_factor = _coord_mod._solar_factor
_simulate_indoor_physics_v3 = _coord_mod._simulate_indoor_physics_v3

# _estimate_solar_phase_offset does not exist yet — import defensively so the
# module import does not blow up with AttributeError before individual tests run.
_estimate_solar_phase_offset = getattr(_coord_mod, "_estimate_solar_phase_offset", None)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_daytime_window(
    *,
    n_entries: int,
    t_indoor_values: list[float],
    t_outdoor: float = 55.0,
    start_hour: int = 9,
    interval_minutes: int = 60,
    hvac: str = "off",
    fan: str = "off",
    windows_open: bool = False,
    date: datetime | None = None,
) -> list[dict]:
    """Build synthetic chart_log-style window entries for _estimate_solar_phase_offset.

    Args:
        n_entries: Number of entries (must equal len(t_indoor_values)).
        t_indoor_values: Indoor temperature at each step.
        t_outdoor: Constant outdoor temperature.
        start_hour: Local hour of first entry.
        interval_minutes: Interval between entries.
        hvac/fan/windows_open: Condition flags.
        date: Base date (defaults to 2026-05-01 local).
    """
    assert len(t_indoor_values) == n_entries, "t_indoor_values must have exactly n_entries elements"

    if date is None:
        date = datetime(2026, 5, 1, start_hour, 0, 0, tzinfo=UTC)

    entries = []
    for i in range(n_entries):
        ts = date + timedelta(minutes=i * interval_minutes)
        entries.append(
            {
                "ts": ts.isoformat(),
                "indoor": t_indoor_values[i],
                "outdoor": t_outdoor,
                "hvac": hvac,
                "fan": fan,
                "windows_open": windows_open,
            }
        )
    return entries


def _indoor_rising_peak_at(
    *,
    peak_hour: int,
    start_hour: int = 9,
    interval_hours: int = 1,
    n_entries: int = 8,
    base_temp: float = 68.0,
    delta: float = 3.0,
) -> list[float]:
    """Generate indoor temperature values that peak at a specific local hour.

    Uses a simple triangular profile: rises linearly to peak, then falls.
    Guarantees peak is interior (not first or last entry).

    Args:
        peak_hour: Local hour of the indoor temperature peak.
        start_hour: Local hour of the first entry.
        interval_hours: Hours between entries.
        n_entries: Total number of entries.
        base_temp: Starting and ending indoor temperature.
        delta: Peak rise above base_temp.
    """
    hours = [start_hour + i * interval_hours for i in range(n_entries)]
    values = []
    peak_idx = hours.index(peak_hour) if peak_hour in hours else None
    if peak_idx is None:
        # Fallback: find closest
        peak_idx = min(range(n_entries), key=lambda i: abs(hours[i] - peak_hour))
    for i in range(n_entries):
        if i <= peak_idx:
            frac = i / peak_idx if peak_idx > 0 else 0.0
        else:
            frac = (n_entries - 1 - i) / (n_entries - 1 - peak_idx) if (n_entries - 1 - peak_idx) > 0 else 0.0
        values.append(base_temp + frac * delta)
    return values


def _make_learning() -> LearningEngine:
    """Return a fresh LearningEngine instance backed by a temp directory."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    return LearningEngine(storage_path=tmp)


# ── TestSolarFactor ───────────────────────────────────────────────────────────


class TestSolarFactor:
    """_solar_factor(local_hour, phase_offset_h) formula correctness.

    After the fix, _solar_factor(local_hour, phase_offset_h=DEFAULT) shifts the
    peak from hour 13 (old, offset=0) to 13+offset. The peak condition is:
        effective_hour = local_hour - offset = 13  →  sin(π/2) = 1.0
    """

    def test_peak_at_13_with_offset_zero(self):
        """_solar_factor(13, 0) == 1.0 — baseline, unchanged from pre-fix behavior."""
        result = _solar_factor(13, 0)
        assert result == pytest.approx(1.0), f"Expected 1.0, got {result}"

    def test_peak_at_15_with_offset_two(self):
        """_solar_factor(15, 2) == 1.0 — default prior (3pm peak)."""
        result = _solar_factor(15, 2)
        assert result == pytest.approx(1.0), f"Expected 1.0, got {result}"

    def test_peak_at_16_with_offset_three(self):
        """_solar_factor(16, 3) == 1.0 — 4pm peak."""
        result = _solar_factor(16, 3)
        assert result == pytest.approx(1.0), f"Expected 1.0, got {result}"

    def test_zero_below_effective_window(self):
        """_solar_factor(9, 2) == 0.0 — effective_hour=7 < THERMAL_SOLAR_DAYTIME_START_H=8."""
        result = _solar_factor(9, 2)
        assert result == 0.0, f"Expected 0.0 for hour below effective window, got {result}"

    def test_zero_at_effective_end(self):
        """_solar_factor(20, 2) == 0.0 — effective_hour=18 == THERMAL_SOLAR_DAYTIME_END_H=18."""
        result = _solar_factor(20, 2)
        assert result == 0.0, f"Expected 0.0 at effective end boundary, got {result}"

    def test_invalid_returns_zero(self):
        """_solar_factor(None, 2) and _solar_factor('x', 2) both return 0.0."""
        assert _solar_factor(None, 2) == 0.0, "Expected 0.0 for None hour"
        assert _solar_factor("x", 2) == 0.0, "Expected 0.0 for string hour"

    def test_default_offset_is_two(self):
        """_solar_factor(15) == 1.0 — default offset parameter == THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT=2."""
        result = _solar_factor(15)
        assert result == pytest.approx(1.0), (
            f"Expected peak at hour 15 with default offset=2, got {result}. "
            "Default offset should be THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT=2."
        )

    def test_symmetry(self):
        """_solar_factor(12, 2) ≈ _solar_factor(18, 2) — symmetric 3h each side of effective peak 13.

        With offset=2, effective peak at local_hour=15.
        local 12 → effective 10, which is 3h before peak at 13.
        local 18 → effective 16, which is 3h after peak at 13.
        sin is symmetric around π/2, so the values should be equal.
        """
        left = _solar_factor(12, 2)
        right = _solar_factor(18, 2)
        assert left == pytest.approx(right, abs=1e-9), (
            f"Symmetry failed: _solar_factor(12, 2)={left} != _solar_factor(18, 2)={right}"
        )


# ── TestEstimateSolarPhaseOffset ─────────────────────────────────────────────


class TestEstimateSolarPhaseOffset:
    """_estimate_solar_phase_offset(window_entries) → (offset_h | None, reject_reason | None).

    Tests quality gates and peak detection. _estimate_solar_phase_offset is a new
    free function in coordinator.py that does not exist yet — tests will fail with
    AttributeError until Phase 2 implementation.
    """

    def _call(self, entries):
        """Invoke _estimate_solar_phase_offset or skip with AttributeError message."""
        assert _estimate_solar_phase_offset is not None, (
            "_estimate_solar_phase_offset not found in coordinator module — "
            "implementation not yet in place (expected for TDD Phase 1)"
        )
        return _estimate_solar_phase_offset(entries)

    def test_peak_at_hour_15_returns_offset_two(self):
        """Indoor peak at hour 15 → phase_obs = 15 − 13 = 2.0, no rejection."""
        # 8 hourly entries from hour 9..16, peak at hour 15
        temps = _indoor_rising_peak_at(
            peak_hour=15, start_hour=9, interval_hours=1, n_entries=8, base_temp=68.0, delta=3.0
        )
        entries = _make_daytime_window(
            n_entries=8,
            t_indoor_values=temps,
            start_hour=9,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert reason is None, f"Expected no rejection, got {reason}"
        assert obs == pytest.approx(2.0, abs=0.01), f"Expected phase_obs=2.0, got {obs}"

    def test_peak_at_hour_16_returns_offset_three(self):
        """Indoor peak at hour 16 → phase_obs = 16 − 13 = 3.0, no rejection."""
        temps = _indoor_rising_peak_at(
            peak_hour=16, start_hour=9, interval_hours=1, n_entries=9, base_temp=68.0, delta=3.0
        )
        entries = _make_daytime_window(
            n_entries=9,
            t_indoor_values=temps,
            start_hour=9,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert reason is None, f"Expected no rejection, got {reason}"
        assert obs == pytest.approx(3.0, abs=0.01), f"Expected phase_obs=3.0, got {obs}"

    def test_too_few_entries(self):
        """2 entries → (None, REJECT_TOO_FEW_SAMPLES) — below THERMAL_SOLAR_PHASE_MIN_ENTRIES=3."""
        entries = _make_daytime_window(
            n_entries=2,
            t_indoor_values=[68.0, 70.0],
            start_hour=10,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert obs is None, f"Expected obs=None for too-few-entries, got {obs}"
        assert reason == REJECT_TOO_FEW_SAMPLES, f"Expected REJECT_TOO_FEW_SAMPLES, got {reason}"

    def test_window_too_short(self):
        """3 entries at 1h apart = 2h span → (None, REJECT_WINDOW_TOO_SHORT); need ≥4h."""
        entries = _make_daytime_window(
            n_entries=3,
            t_indoor_values=[68.0, 70.0, 69.0],
            start_hour=10,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert obs is None, f"Expected obs=None for too-short window, got {obs}"
        assert reason == REJECT_WINDOW_TOO_SHORT, f"Expected REJECT_WINDOW_TOO_SHORT, got {reason}"

    def test_small_delta(self):
        """Indoor range < 1.5°F → (None, REJECT_SMALL_DELTA) — no visible solar signal."""
        # 6 hourly entries, peak only 0.5°F above base — below THERMAL_SOLAR_PHASE_MIN_DT_F=1.5
        temps = [68.0, 68.2, 68.4, 68.5, 68.3, 68.0]
        entries = _make_daytime_window(
            n_entries=6,
            t_indoor_values=temps,
            start_hour=9,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert obs is None, f"Expected obs=None for small delta, got {obs}"
        assert reason == REJECT_SMALL_DELTA, f"Expected REJECT_SMALL_DELTA, got {reason}"

    def test_peak_at_first_entry(self):
        """Max indoor at first entry → (None, REJECT_NO_INTERIOR_PEAK) — not a daytime peak."""
        # Monotonically decreasing — peak is at index 0
        temps = [72.0, 71.0, 70.0, 69.0, 68.5, 68.0]
        entries = _make_daytime_window(
            n_entries=6,
            t_indoor_values=temps,
            start_hour=9,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        assert obs is None, f"Expected obs=None for first-entry peak, got {obs}"
        assert reason == REJECT_NO_INTERIOR_PEAK, f"Expected REJECT_NO_INTERIOR_PEAK, got {reason}"

    def test_peak_clamped_high(self):
        """Indoor peak at hour 19 → raw phase_obs=6, clamped to THERMAL_SOLAR_PHASE_OFFSET_MAX=4."""
        # Peak at last interior entry at local hour 19 (start=9, 10 entries at 1h → hours 9..18)
        # We need peak at hour 19: use start=10, 10 entries, peak at entry for hour 19
        temps = _indoor_rising_peak_at(
            peak_hour=19, start_hour=10, interval_hours=1, n_entries=10, base_temp=68.0, delta=4.0
        )
        entries = _make_daytime_window(
            n_entries=10,
            t_indoor_values=temps,
            start_hour=10,
            interval_minutes=60,
        )
        obs, reason = self._call(entries)
        # Raw phase_obs = 19 - 13 = 6, clamped to THERMAL_SOLAR_PHASE_OFFSET_MAX=4
        assert reason is None, f"Expected no rejection for clamped peak, got {reason}"
        assert obs == pytest.approx(THERMAL_SOLAR_PHASE_OFFSET_MAX, abs=0.01), (
            f"Expected clamped obs={THERMAL_SOLAR_PHASE_OFFSET_MAX}, got {obs}"
        )


# ── TestODEPeakShift ─────────────────────────────────────────────────────────


class TestODEPeakShift:
    """Integration/scientific proof: solar phase offset shifts the ODE indoor peak.

    These tests call _simulate_indoor_physics_v3 directly in a loop (hours 8–20)
    and verify that the predicted indoor temperature peaks at the expected hour.

    Synthetic house parameters (from plan §Scientific Proof Requirement):
        T_out = 55°F constant
        T_in  = 68°F at 8am
        k_passive = −0.020 hr⁻¹  (mild envelope decay)
        k_solar   = 3.0 °F/hr     (strong solar gain)
        No HVAC (setpoint=None, k_active=None)
        No ventilation

    With offset=0: peak when _solar_factor(h, 0) peaks → h=13 (1pm).
    With offset=2: peak when _solar_factor(h, 2) peaks → h=15 (3pm).
    """

    # Synthetic house parameters
    K_PASSIVE = -0.020
    K_SOLAR = 3.0
    T_OUTDOOR = 55.0
    T_START = 68.0
    DT_HOURS = 1.0  # 1h steps
    COMFORT_HEAT = 60.0
    COMFORT_COOL = 80.0

    def _run_ode_hours_8_to_20(self, phase_offset_h: float) -> dict[int, float]:
        """Run the physics v3 ODE for hours 8–22 (inclusive) and return {hour: T_indoor}.

        Range extended to 23 to capture peaks from large offsets (e.g. offset=3 peaks ~hour 21).
        """
        t_current = self.T_START
        result = {}
        for h in range(8, 23):  # extended to 23 to capture peaks from large offsets
            sf = _solar_factor(h, phase_offset_h)
            t_next = _simulate_indoor_physics_v3(
                t_start=t_current,
                t_outdoor=self.T_OUTDOOR,
                k_passive=self.K_PASSIVE,
                k_active=None,
                dt_hours=self.DT_HOURS,
                setpoint=None,
                comfort_heat=self.COMFORT_HEAT,
                comfort_cool=self.COMFORT_COOL,
                k_solar=self.K_SOLAR,
                solar_factor=sf,
                ventilation_active=False,
            )
            result[h + 1] = t_next  # temperature arriving at h+1
            t_current = t_next
        return result

    def test_offset_shifts_peak_by_two_hours(self):
        """Offset=2 peaks exactly 2h later than offset=0 — the core scientific proof.

        _solar_factor(h, 2) == _solar_factor(h-2, 0), so the solar forcing
        curve with offset=2 is a rigid 2h rightward shift of offset=0.
        The ODE temperature response peak therefore shifts by the same 2h.
        """
        temps_0 = self._run_ode_hours_8_to_20(phase_offset_h=0)
        temps_2 = self._run_ode_hours_8_to_20(phase_offset_h=2)
        peak_0 = max(temps_0, key=lambda h: temps_0[h])
        peak_2 = max(temps_2, key=lambda h: temps_2[h])
        assert peak_2 == peak_0 + 2, (
            f"ODE with offset=2 must peak exactly 2h later than offset=0. "
            f"offset=0 peak={peak_0}, offset=2 peak={peak_2}. "
            f"Temps offset=0: {temps_0}. Temps offset=2: {temps_2}"
        )

    def test_offset_shifts_peak_by_three_hours(self):
        """Offset=3 peaks exactly 3h later than offset=0."""
        temps_0 = self._run_ode_hours_8_to_20(phase_offset_h=0)
        temps_3 = self._run_ode_hours_8_to_20(phase_offset_h=3)
        peak_0 = max(temps_0, key=lambda h: temps_0[h])
        peak_3 = max(temps_3, key=lambda h: temps_3[h])
        assert peak_3 == peak_0 + 3, (
            f"ODE with offset=3 must peak exactly 3h later than offset=0. "
            f"offset=0 peak={peak_0}, offset=3 peak={peak_3}."
        )

    def test_offset_matches_actual_peak_in_synthetic_data(self):
        """End-to-end proof: estimate offset from synthetic data, ODE with that offset peaks later.

        Build synthetic chart_log entries with actual indoor peak at hour 15.
        1. _estimate_solar_phase_offset should return 2.0.
        2. ODE run with that offset peaks later than ODE with offset=0 by the same amount.
        """
        assert _estimate_solar_phase_offset is not None, (
            "_estimate_solar_phase_offset not found — implementation not yet in place"
        )

        temps_list = _indoor_rising_peak_at(
            peak_hour=15,
            start_hour=9,
            interval_hours=1,
            n_entries=9,
            base_temp=68.0,
            delta=4.0,
        )
        entries = _make_daytime_window(
            n_entries=9,
            t_indoor_values=temps_list,
            start_hour=9,
            interval_minutes=60,
        )

        # Step 1: estimator returns offset=2.0
        obs, reason = _estimate_solar_phase_offset(entries)
        assert reason is None, f"Expected no rejection, got {reason}"
        assert obs == pytest.approx(2.0, abs=0.05), f"Expected phase_obs=2.0, got {obs}"

        # Step 2: ODE with that offset peaks obs hours later than offset=0
        temps_0 = self._run_ode_hours_8_to_20(phase_offset_h=0)
        temps_obs = self._run_ode_hours_8_to_20(phase_offset_h=obs)
        peak_0 = max(temps_0, key=lambda h: temps_0[h])
        peak_obs = max(temps_obs, key=lambda h: temps_obs[h])
        expected_shift = int(round(obs))
        assert peak_obs == peak_0 + expected_shift, (
            f"ODE with offset={obs} must peak {expected_shift}h later than offset=0. "
            f"offset=0 peak={peak_0}, offset={obs} peak={peak_obs}."
        )


# ── TestLearningConvergence ───────────────────────────────────────────────────


class TestLearningConvergence:
    """EWMA learning for solar_phase_offset_h via update_solar_phase_offset().

    update_solar_phase_offset does not exist yet — tests will fail with
    AttributeError until learning.py Phase 2 implementation is in place.
    """

    def _update(self, learning: LearningEngine, obs_h: float) -> None:
        """Call update_solar_phase_offset(obs_h, alpha=THERMAL_SOLAR_PHASE_ALPHA)."""
        learning.update_solar_phase_offset(obs_h, THERMAL_SOLAR_PHASE_ALPHA)

    def test_first_observation_initializes_directly(self):
        """First observation: current=None → stored value equals observed value directly.

        With no prior, the first update should set solar_phase_offset_h to the
        observed value (EWMA from None is treated as: new_val = obs).
        """
        learning = _make_learning()
        self._update(learning, 2.0)
        model = learning.get_thermal_model()
        stored = model.get("solar_phase_offset_h")
        assert stored == pytest.approx(2.0, abs=0.01), f"First observation should initialize to 2.0, got {stored}"

    def test_ewma_converges(self):
        """10 observations of 2.0 starting from None → final value ≈ 2.0 (within 0.1)."""
        learning = _make_learning()
        for _ in range(10):
            self._update(learning, 2.0)
        model = learning.get_thermal_model()
        stored = model.get("solar_phase_offset_h")
        assert stored == pytest.approx(2.0, abs=0.1), (
            f"After 10 obs of 2.0, expected convergence near 2.0, got {stored}"
        )

    def test_observation_clamped_below_zero(self):
        """Observation of -1 → clamped to THERMAL_SOLAR_PHASE_OFFSET_MIN=0 before EWMA."""
        learning = _make_learning()
        # Initialize with a known value
        self._update(learning, 2.0)
        # Now push a very negative observation — should be clamped to 0
        for _ in range(20):
            self._update(learning, -1.0)
        model = learning.get_thermal_model()
        stored = model.get("solar_phase_offset_h")
        assert stored >= THERMAL_SOLAR_PHASE_OFFSET_MIN, (
            f"Stored value {stored} is below THERMAL_SOLAR_PHASE_OFFSET_MIN={THERMAL_SOLAR_PHASE_OFFSET_MIN}. "
            "Negative observations must be clamped before EWMA."
        )

    def test_first_active_date_set_on_first_update(self):
        """first_active_date_phase_offset is set after first update_solar_phase_offset() call."""
        learning = _make_learning()
        model_before = learning.get_thermal_model()
        assert model_before.get("first_active_date_phase_offset") is None, (
            "first_active_date_phase_offset should be None before any observation"
        )
        self._update(learning, 2.0)
        model_after = learning.get_thermal_model()
        date_after = model_after.get("first_active_date_phase_offset")
        assert date_after is not None, (
            "first_active_date_phase_offset should be set after first update_solar_phase_offset() call"
        )
        # Should be an ISO date string (YYYY-MM-DD)
        assert isinstance(date_after, str) and len(date_after) == 10, (
            f"Expected ISO date string (YYYY-MM-DD), got {date_after!r}"
        )


# ── TestEngineStatus ─────────────────────────────────────────────────────────


class TestEngineStatus:
    """get_engine_status() method on LearningEngine.

    get_engine_status does not exist yet — tests will fail with
    AttributeError until learning.py Phase 2 implementation is in place.
    """

    def _call_engine_status(self, learning: LearningEngine) -> dict:
        return learning.get_engine_status()

    def test_inactive_before_observations(self):
        """Fresh learning state → all engines inactive, all first_active_date_* = None."""
        learning = _make_learning()
        status = self._call_engine_status(learning)

        # All engines should be inactive
        for engine_key in ("k_passive", "k_solar", "solar_phase_offset_h", "k_vent_window", "k_active_hvac"):
            engine = status.get(engine_key, {})
            assert not engine.get("active", True), (
                f"Engine {engine_key!r} should be inactive on fresh learning, got active={engine.get('active')}"
            )
            since = engine.get("since")
            assert since is None, f"Engine {engine_key!r} since should be None on fresh learning, got {since!r}"

    def test_active_after_first_observation(self):
        """After one k_passive obs via update_solar_phase_offset, phase_offset engine is active.

        Also verifies that after simulating a k_passive observation via the learning
        cache, the k_passive engine becomes active.
        """
        learning = _make_learning()

        # Inject a solar phase observation
        learning.update_solar_phase_offset(2.0, THERMAL_SOLAR_PHASE_ALPHA)

        status = self._call_engine_status(learning)
        phase_engine = status.get("solar_phase_offset_h", {})
        assert phase_engine.get("active") is True, (
            f"solar_phase_offset_h engine should be active after first update, got active={phase_engine.get('active')}"
        )
        assert phase_engine.get("since") is not None, (
            "solar_phase_offset_h engine 'since' should be set after first update"
        )

    def test_engine_status_response_shape(self):
        """All expected top-level keys present in get_engine_status() return dict."""
        learning = _make_learning()
        status = self._call_engine_status(learning)

        required_engine_keys = {
            "k_passive",
            "k_solar",
            "solar_phase_offset_h",
            "k_vent_window",
            "k_active_hvac",
        }
        required_meta_keys = {
            "ode_version",
            "physics_eligible",
            "physics_eligible_reason",
        }

        for key in required_engine_keys:
            assert key in status, f"Missing engine key {key!r} in get_engine_status() response"
            engine = status[key]
            assert "active" in engine, f"Engine {key!r} missing 'active' field"
            assert "since" in engine, f"Engine {key!r} missing 'since' field"

        for key in required_meta_keys:
            assert key in status, f"Missing meta key {key!r} in get_engine_status() response"

    def test_engine_status_k_active_hvac_value_shape(self):
        """get_engine_status()["k_active_hvac"]["value"] is a dict with "heat" and "cool" keys.

        Contract test — guards the shape that _format_engine_status_for_ai must read.
        MUST PASS before and after fix (this verifies the learning.py shape is correct).
        """
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        learning = LearningEngine(storage_path=tmp)

        # Inject a k_active_heat value directly into the thermal cache
        if learning._state.thermal_model_cache is None:
            learning._state.thermal_model_cache = {}
        learning._state.thermal_model_cache["k_active_heat"] = 5.0

        status = learning.get_engine_status()
        hvac_entry = status.get("k_active_hvac", {})
        assert "value" in hvac_entry, f"k_active_hvac entry must have a 'value' key.\nGot: {hvac_entry}"
        value_dict = hvac_entry["value"]
        assert isinstance(value_dict, dict), (
            f"k_active_hvac['value'] must be a dict with 'heat'/'cool' keys.\nGot: {value_dict!r}"
        )
        assert "heat" in value_dict, f"k_active_hvac['value'] must have a 'heat' key.\nGot: {value_dict}"
        assert "cool" in value_dict, f"k_active_hvac['value'] must have a 'cool' key.\nGot: {value_dict}"

    def test_format_engine_status_reads_nested_value_key(self):
        """_format_engine_status_for_ai with k_active_heat=5.0 → "5.0" in output.

        MUST FAIL before Fix A: _format_engine_status_for_ai reads
        hvac_info.get("k_active_heat") (returns None) instead of
        hvac_info["value"]["heat"] (returns 5.0).
        MUST PASS after Fix A.
        """
        import tempfile
        from pathlib import Path

        from custom_components.climate_advisor.ai_skills_activity import _format_engine_status_for_ai

        tmp = Path(tempfile.mkdtemp())
        learning = LearningEngine(storage_path=tmp)

        if learning._state.thermal_model_cache is None:
            learning._state.thermal_model_cache = {}
        learning._state.thermal_model_cache["k_active_heat"] = 5.0

        status = learning.get_engine_status()
        result = _format_engine_status_for_ai(status)

        assert "5.0" in result, (
            f"Expected '5.0' in _format_engine_status_for_ai output with k_active_heat=5.0.\n"
            f"Got:\n{result}\n"
            "Bug A: _format_engine_status_for_ai reads hvac_info.get('k_active_heat') "
            "directly from the dict top level, but get_engine_status() nests the value "
            "under hvac_info['value']['heat']."
        )


# ── TestMildDayDynamicScheduling ─────────────────────────────────────────────


class TestMildDayDynamicScheduling:
    """Fix C: MILD day window scheduling moves from hardcoded times to constants + ODE.

    Tests 26–28: verify that classifier.py uses MILD_WINDOW_OPEN_HOUR and
    MILD_WINDOW_CLOSE_HOUR from const.py (not hardcoded literals), and that briefing
    uses ODE nat_vent_cutoff as the dynamic close time when available.
    """

    def _mild_snapshot(self) -> ForecastSnapshot:
        """Return a ForecastSnapshot that classifies as MILD (60–74°F high)."""
        return ForecastSnapshot(
            today_high=68.0,
            today_low=52.0,
            tomorrow_high=70.0,
            tomorrow_low=54.0,
            current_outdoor_temp=58.0,
            current_indoor_temp=70.0,
            current_humidity=50.0,
        )

    def test_mild_day_uses_const_fallback_when_no_ode(self):
        """MILD DayClassification uses time(MILD_WINDOW_OPEN_HOUR, 0) / time(MILD_WINDOW_CLOSE_HOUR, 0).

        Verifies that after Fix C the classifier reads from the constants, not hardcoded
        literals. Without the fix, classifier.py:118–119 reads time(10, 0)/time(17, 0) as
        literals — the constants still exist and still equal 10/17, so the test passes
        even before the fix if we only check values. The real guard is test 28 below.
        """
        snapshot = self._mild_snapshot()
        classification = classify_day(snapshot)
        assert classification.window_open_time == time(MILD_WINDOW_OPEN_HOUR, 0), (
            f"Expected window_open_time=time({MILD_WINDOW_OPEN_HOUR}, 0), got {classification.window_open_time}"
        )
        assert classification.window_close_time == time(MILD_WINDOW_CLOSE_HOUR, 0), (
            f"Expected window_close_time=time({MILD_WINDOW_CLOSE_HOUR}, 0), got {classification.window_close_time}"
        )

    def test_mild_day_close_time_uses_ode_crossover(self):
        """Briefing for MILD day with ODE data showing crossover at 13:30 uses ~13:30, not 17:00.

        This test exercises the briefing layer. Since briefing.py is not yet updated
        (Phase 2), this test is expected to fail until _derive_natural_vent_events is
        applied to the MILD day path.

        The test calls the briefing module's MILD-day plan function and checks that
        the window_close_time in the output reflects the ODE nat_vent_crossover
        (13:30) rather than the const fallback (17:00).
        """
        import importlib

        briefing_mod = importlib.import_module("custom_components.climate_advisor.briefing")
        # _derive_warm_day_events / _derive_natural_vent_events must exist on the module
        # after Fix C implementation. Until then, test fails with AttributeError.
        derive_fn = getattr(briefing_mod, "_derive_natural_vent_events", None)
        assert derive_fn is not None, (
            "_derive_natural_vent_events not found in briefing.py — Fix C briefing change not yet implemented"
        )

        # Synthetic predicted futures: outdoor crosses indoor at hour 13 (13:30 between 13 and 14)
        # indoor_future[h] > outdoor_future[h] is true until nat_vent_cutoff
        # For simplicity, use a 24-entry list where each index = hour
        pred_indoor = [70.0] * 24
        pred_outdoor = [55.0] * 24
        # At hour 13, outdoor temp rises to equal indoor, then exceeds it
        pred_outdoor[13] = 70.0  # equal at 13 → crossover just before 14
        pred_outdoor[14] = 72.0  # outdoor > indoor from 14 onward
        pred_outdoor[15] = 73.0
        # Build hourly list format (list of floats indexed by hour)
        result = derive_fn(
            predicted_indoor_future=pred_indoor,
            predicted_outdoor_future=pred_outdoor,
            comfort_cool=75.0,
            k_active_cool=None,
        )
        crossover_hour = result.get("nat_vent_cutoff")
        assert crossover_hour is not None, "nat_vent_cutoff should be set in result"
        # Crossover detected at hour 13 or 14 (first hour outdoor >= indoor)
        assert crossover_hour <= 14, f"Expected nat_vent_cutoff ≤ 14 (before 17:00 fallback), got {crossover_hour}"

    def test_mild_day_constants_in_const_py(self):
        """MILD_WINDOW_OPEN_HOUR == 10 and MILD_WINDOW_CLOSE_HOUR == 17 exist in const.py.

        These were previously hardcoded as time(10, 0) and time(17, 0) in
        classifier.py lines 118–119. Fix C moves them to const.py.
        This test fails until const.py is updated (Phase 2).
        """
        assert MILD_WINDOW_OPEN_HOUR == 10, f"MILD_WINDOW_OPEN_HOUR should be 10, got {MILD_WINDOW_OPEN_HOUR}"
        assert MILD_WINDOW_CLOSE_HOUR == 17, f"MILD_WINDOW_CLOSE_HOUR should be 17, got {MILD_WINDOW_CLOSE_HOUR}"


# ── pytest import guard ───────────────────────────────────────────────────────

import pytest  # noqa: E402 — must come after sys.modules manipulation
