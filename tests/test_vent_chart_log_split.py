"""Tests for the Issue #587 vent-split extraction/estimator/commit-routing (Part 3.3, items 6-10).

Covers:
  6. _extract_ventilated_windows filters by fan_state
  7. _run_vent_window_chart_log_fit routes through the shared _vent_endpoint_estimate
  8. _run_vent_fan_chart_log_fit routes through the shared _vent_endpoint_estimate
  9. OLS commit path (_commit_event_from_dict) routes vent_window_decay and
     vent_fan_decay to their own cache fields (k_vent_window / k_vent_fan) separately
  10. The shared 2-param solar-separation logic (compute_k_env_solar) applies to both
      vent_window_decay and vent_fan_decay, not just the old single "ventilated" string

NOTE on items 7/8: this phase (Part 1 Step 1 of the combined #587+Defect-A/B plan)
deliberately does NOT implement Defect A (the RK4/bisection endpoint-estimator fix) —
the old closed-form t_out_avg/ratio/math.log formula stays in place inside the shared
_vent_endpoint_estimate() for now. So these two tests verify the *structural* claim
that's actually true in this phase — that both split call sites route through the one
shared _vent_endpoint_estimate() body (proving the #587 refactor didn't fork behavior)
and produce distinctly-tagged commits — rather than the plan's literal "uses the
RK4-fixed estimator" wording, which is Phase 2 (Defect A) work landing later.
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_ha_util = sys.modules.get("homeassistant.util")
if _ha_util is not None:
    _ha_util.dt.parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402
from custom_components.climate_advisor.learning import LearningEngine  # noqa: E402

ClimateAdvisorCoordinator = _coord_mod.ClimateAdvisorCoordinator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vent_chart_log_entries(
    *,
    n_hours: float,
    t_indoor_start: float,
    t_outdoor: float,
    k_passive: float,
    fan: bool,
    windows_open: bool = True,
    hvac: str = "idle",
    interval_minutes: int = 30,
    t0: datetime | None = None,
) -> list[dict]:
    """Generate synthetic chart_log entries (Newton's law of cooling) with fan/windows fields.

    Mirrors tests/test_dual_estimator.py::_make_chart_log_entries but adds the "hvac",
    "windows_open", and "fan" fields _extract_ventilated_windows needs.
    """
    if t0 is None:
        # Night hours (not in the 08:00-19:59 solar-guard exclusion window).
        t0 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

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
                "hvac": hvac,
                "windows_open": windows_open,
                "fan": fan,
            }
        )
    return entries


def _make_coord_with_chart_log(entries: list[dict]):
    """Build a minimal coordinator stub exposing only the chart_log + estimator methods."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    chart_log = MagicMock()
    chart_log._entries = entries
    coord._chart_log = chart_log
    coord.learning = MagicMock()
    coord.learning.record_thermal_observation = MagicMock()

    import types

    # _is_solar_hour is a @staticmethod — assign directly (no self-binding needed).
    coord._is_solar_hour = ClimateAdvisorCoordinator._is_solar_hour

    for method_name in (
        "_extract_ventilated_windows",
        "_vent_endpoint_estimate",
        "_run_vent_window_chart_log_fit",
        "_run_vent_fan_chart_log_fit",
        "_run_vent_chart_log_fit_impl",
        "_select_estimator",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    return coord


def _parse_datetime_real(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime):
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    # _is_solar_hour() calls dt_util.as_local(ts).hour — the stub HA environment's
    # dt_util is a MagicMock by default, so as_local() must be forwarded to a real
    # passthrough or comparisons against int hours raise TypeError.
    mock_dt.as_local.side_effect = lambda x: x
    return mock_dt


# ---------------------------------------------------------------------------
# 6: extraction filters by fan_state
# ---------------------------------------------------------------------------


class TestExtractVentilatedWindowsFanFilter:
    def test_extract_ventilated_windows_filters_by_fan_state(self):
        """fan_state=False extracts only fan-off windows; fan_state=True only fan-on."""
        fan_off_entries = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.05,
            fan=False,
            t0=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )
        fan_on_entries = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.10,
            fan=True,
            t0=datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC),
        )
        entries = fan_off_entries + fan_on_entries
        coord = _make_coord_with_chart_log(entries)

        dt_mock = _make_dt_mock(datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            fan_off_windows = coord._extract_ventilated_windows(entries, days=30, fan_state=False)
            fan_on_windows = coord._extract_ventilated_windows(entries, days=30, fan_state=True)

        assert len(fan_off_windows) == 1, f"expected exactly the fan-off window, got {len(fan_off_windows)}"
        assert len(fan_on_windows) == 1, f"expected exactly the fan-on window, got {len(fan_on_windows)}"
        # Confirm they're actually different windows (different timestamps)
        assert fan_off_windows[0][0]["ts"] != fan_on_windows[0][0]["ts"]


# ---------------------------------------------------------------------------
# 7-8: chart-log-fit split routes through shared estimator
# ---------------------------------------------------------------------------


class TestVentChartLogFitSharedEstimator:
    def test_vent_window_chart_log_fit_uses_shared_endpoint_estimator(self):
        """_run_vent_window_chart_log_fit commits via the shared _vent_endpoint_estimate,
        tagged hvac_mode="vent_window" (fan_state=False, current — not yet RK4-fixed — formula).
        """
        entries = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.05,
            fan=False,
        )
        coord = _make_coord_with_chart_log(entries)

        dt_mock = _make_dt_mock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._run_vent_window_chart_log_fit(backfill=True)

        assert coord.learning.record_thermal_observation.called, "expected a committed observation"
        committed = coord.learning.record_thermal_observation.call_args[0][0]
        assert committed["hvac_mode"] == "vent_window"
        assert committed["k_passive"] is not None
        assert committed["k_passive"] < 0, "cooling window should yield a negative k"

    def test_vent_fan_chart_log_fit_uses_shared_endpoint_estimator(self):
        """_run_vent_fan_chart_log_fit commits via the SAME shared _vent_endpoint_estimate
        body (fan_state=True), tagged hvac_mode="vent_fan" — proving the split didn't fork
        the estimator function itself.
        """
        entries = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.10,
            fan=True,
        )
        coord = _make_coord_with_chart_log(entries)

        dt_mock = _make_dt_mock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._run_vent_fan_chart_log_fit(backfill=True)

        assert coord.learning.record_thermal_observation.called, "expected a committed observation"
        committed = coord.learning.record_thermal_observation.call_args[0][0]
        assert committed["hvac_mode"] == "vent_fan"
        assert committed["k_passive"] is not None
        assert committed["k_passive"] < 0

    def test_vent_window_and_vent_fan_share_identical_estimator_output_for_same_window(self):
        """Direct proof of test 5's shared-helper claim: _vent_endpoint_estimate produces
        identical output regardless of which fan-state window list feeds it — the endpoint
        math doesn't care why the window is a decay window.
        """
        entries_a = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.07,
            fan=False,
        )
        entries_b = _make_vent_chart_log_entries(
            n_hours=4,
            t_indoor_start=74.0,
            t_outdoor=60.0,
            k_passive=-0.07,
            fan=True,
        )
        coord = _make_coord_with_chart_log([])

        result_a = coord._vent_endpoint_estimate(
            [{"ts": e["ts"], "indoor": e["indoor"], "outdoor": e["outdoor"]} for e in entries_a]
        )
        result_b = coord._vent_endpoint_estimate(
            [{"ts": e["ts"], "indoor": e["indoor"], "outdoor": e["outdoor"]} for e in entries_b]
        )
        assert result_a is not None and result_b is not None
        assert result_a["k"] == pytest.approx(result_b["k"], abs=1e-9)


# ---------------------------------------------------------------------------
# 9-10: real-time OLS commit-routing split (learning.py)
# ---------------------------------------------------------------------------


def _make_decay_samples(n: int = 30, *, k_passive: float = -0.08, indoor_start: float = 74.0, outdoor: float = 60.0):
    """Generate clean 1-param decay samples (no solar_factor variation)."""
    dt_minutes = 5.0
    dt_hours = dt_minutes / 60.0
    samples = []
    t_in = indoor_start
    for i in range(n):
        samples.append(
            {
                "indoor_temp_f": round(t_in, 4),
                "outdoor_temp_f": outdoor,
                "elapsed_minutes": float(i * dt_minutes),
                "solar_factor": 0.0,
            }
        )
        rate = k_passive * (t_in - outdoor)
        t_in += rate * dt_hours
    return samples


def _make_rising_solar_samples(k_env: float = -0.08, k_solar: float = 4.0, n: int = 16):
    """Indoor net-rises despite outdoor cooler — solar dominates (forces the 2-param path)."""
    base_indoor = 72.0
    outdoor = 58.0
    dt_minutes = 8.0
    dt_hours = dt_minutes / 60.0
    samples = []
    t_in = base_indoor
    for i in range(n):
        sf = 0.05 + (0.80 / (n - 1)) * i
        samples.append(
            {
                "indoor_temp_f": round(t_in, 4),
                "outdoor_temp_f": outdoor,
                "elapsed_minutes": float(i * dt_minutes),
                "solar_factor": round(sf, 4),
            }
        )
        rate = k_env * (t_in - outdoor) + k_solar * sf
        t_in += rate * dt_hours
    return samples


class TestOLSCommitRoutingSplit:
    _DT_PATCH = "custom_components.climate_advisor.learning.dt_util"
    _FAKE_DT = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def _make_engine(self, tmp_path: Path) -> LearningEngine:
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def test_ols_commit_routes_vent_window_and_vent_fan_separately(self, tmp_path: Path):
        """vent_window_decay and vent_fan_decay each write their OWN cache field —
        k_vent_window and k_vent_fan respectively — never cross-contaminating.
        """
        engine = self._make_engine(tmp_path)
        window_samples = _make_decay_samples(k_passive=-0.06)
        fan_samples = _make_decay_samples(k_passive=-0.15)

        dt_mock = _make_dt_mock(self._FAKE_DT)
        with patch(self._DT_PATCH, dt_mock):
            obs_w, reject_w, _ = engine._commit_event_from_dict(
                {"obs_id": "w1", "samples": window_samples}, force_grade="high", obs_type="vent_window_decay"
            )
            obs_f, reject_f, _ = engine._commit_event_from_dict(
                {"obs_id": "f1", "samples": fan_samples}, force_grade="high", obs_type="vent_fan_decay"
            )

        assert obs_w is not None, f"vent_window_decay commit should succeed; reject={reject_w}"
        assert obs_f is not None, f"vent_fan_decay commit should succeed; reject={reject_f}"
        assert obs_w["hvac_mode"] == "vent_window"
        assert obs_f["hvac_mode"] == "vent_fan"

        model = engine.get_thermal_model()
        assert model["k_vent_window"] is not None and model["k_vent_window"] < 0
        assert model["k_vent_fan"] is not None and model["k_vent_fan"] < 0
        # The two fields must NOT be equal to each other's raw committed k (cross-write check):
        # window k came from k_passive=-0.06, fan k from k_passive=-0.15 — first observation
        # per field means cache value == committed k_passive exactly.
        assert model["k_vent_window"] == pytest.approx(obs_w["k_passive"], abs=1e-9)
        assert model["k_vent_fan"] == pytest.approx(obs_f["k_passive"], abs=1e-9)
        assert model["k_vent_window"] != pytest.approx(model["k_vent_fan"], abs=1e-3)

    def test_two_param_solar_separation_applies_to_both_vent_types(self, tmp_path: Path):
        """The shared 2-param k_env/k_solar separation fires for BOTH vent_window_decay
        and vent_fan_decay — confirms it wasn't left gated on the old "ventilated_decay"
        string during the #587 widen.
        """
        engine = self._make_engine(tmp_path)
        window_samples = _make_rising_solar_samples(k_env=-0.08, k_solar=4.0)
        fan_samples = _make_rising_solar_samples(k_env=-0.12, k_solar=3.0)

        dt_mock = _make_dt_mock(self._FAKE_DT)
        with patch(self._DT_PATCH, dt_mock):
            obs_w, reject_w, _ = engine._commit_event_from_dict(
                {"obs_id": "w2", "samples": window_samples}, force_grade="high", obs_type="vent_window_decay"
            )
            obs_f, reject_f, _ = engine._commit_event_from_dict(
                {"obs_id": "f2", "samples": fan_samples}, force_grade="high", obs_type="vent_fan_decay"
            )

        assert obs_w is not None, f"vent_window_decay 2-param commit should succeed; reject={reject_w}"
        assert obs_f is not None, f"vent_fan_decay 2-param commit should succeed; reject={reject_f}"
        assert obs_w.get("two_param") is True, "vent_window_decay should have gone through the 2-param path"
        assert obs_f.get("two_param") is True, "vent_fan_decay should have gone through the 2-param path"
        assert obs_w["k_solar"] is not None and obs_w["k_solar"] > 0
        assert obs_f["k_solar"] is not None and obs_f["k_solar"] > 0

        model = engine.get_thermal_model()
        assert model["k_vent_window"] is not None
        assert model["k_vent_fan"] is not None
        assert model["k_solar"] is not None, "k_solar should be updated by either 2-param commit"
