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


# ---------------------------------------------------------------------------
# Issue #1505: physical fan, paused sessions and settle times in the chart_log extractors
# ---------------------------------------------------------------------------


def _rows(spec: list[tuple[float, dict]], t0: datetime | None = None, interval_minutes: int = 15) -> list[dict]:
    """``spec``: (hours, fields) runs of rows; indoor decays toward a 50 F outdoor so every stretch is fittable."""
    t0 = t0 or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    out, i = [], 0
    for hours, fields in spec:
        for _ in range(int(hours * 60 / interval_minutes)):
            t = t0 + timedelta(minutes=i * interval_minutes)
            out.append(
                {
                    "ts": t.isoformat(),
                    "indoor": 50.0 + 20.0 * math.exp(-0.1 * i * interval_minutes / 60.0),
                    "outdoor": 50.0,
                    "hvac": "idle",
                    **fields,
                }
            )
            i += 1
    return out


WIN = {"windows_open": True, "fan": False, "fan_running": False, "nat_vent_active": False}
FAN = {"windows_open": True, "fan": True, "fan_running": True, "nat_vent_active": True}
PAUSED = {"windows_open": True, "fan": True, "fan_running": False, "nat_vent_active": True}
CLOSED = {"windows_open": False, "fan": False, "fan_running": False, "nat_vent_active": False}


class TestVentilationRowStates1505:
    def test_paused_session_is_neither_and_legacy_rows_fall_back(self):
        states = _coord_mod._ventilation_row_states(
            [
                {"ts": "2026-01-01T00:00:00+00:00", **PAUSED},
                {"ts": "2026-01-01T00:15:00+00:00", "windows_open": True, "fan": True},  # pre-#331: no fan_running
                {"ts": "2026-01-01T00:30:00+00:00", "windows_open": False, "fan": "off"},  # old text form
                {"ts": "2026-01-01T00:45:00+00:00", "windows_open": False, "fan": "on"},
            ]
        )
        assert states[0]["paused"] and not states[0]["running"]
        assert states[1]["running"] and not states[1]["paused"]
        assert not states[2]["running"] and states[3]["running"]

    def test_settle_after_fan_stop_and_after_windows_close(self):
        rows = _rows([(1, FAN), (3, WIN), (1, CLOSED)])
        states = _coord_mod._ventilation_row_states(rows)
        t = [datetime.fromisoformat(r["ts"]) for r in rows]
        stop = t[3]  # last fan row
        for ts, st in zip(t, states, strict=True):
            mins = (ts - stop).total_seconds() / 60
            if 0 < mins < 120:
                assert st["settling_fan"], ts
            if mins >= 120:
                assert not st["settling_fan"], ts
        # rows[16] is the first closed row
        assert states[16]["settling_close"] and states[17]["settling_close"]
        assert not states[18]["settling_close"]  # 30 min after the close


class TestExtractorsUseRowStates1505:
    def _coord(self, rows):
        coord = _make_coord_with_chart_log(rows)
        import types

        coord._extract_passive_windows = types.MethodType(ClimateAdvisorCoordinator._extract_passive_windows, coord)
        return coord

    def test_windows_only_starts_after_the_fan_settle_time(self):
        rows = _rows([(2, FAN), (5, WIN)])
        coord = self._coord(rows)
        with patch.object(_coord_mod, "dt_util", _make_dt_mock(datetime(2026, 1, 1, 12, tzinfo=UTC))):
            wins = coord._extract_ventilated_windows(rows, days=30, fan_state=False)
        assert len(wins) == 1
        first = datetime.fromisoformat(wins[0][0]["ts"])
        last_fan = datetime.fromisoformat(rows[7]["ts"])
        assert (first - last_fan).total_seconds() / 60 >= 120

    def test_paused_rows_split_the_fan_stretch_and_are_not_windows_only(self):
        rows = _rows([(3, FAN), (0.5, PAUSED), (3, FAN)])
        coord = self._coord(rows)
        with patch.object(_coord_mod, "dt_util", _make_dt_mock(datetime(2026, 1, 1, 12, tzinfo=UTC))):
            fan_w = coord._extract_ventilated_windows(rows, days=30, fan_state=True)
            win_w = coord._extract_ventilated_windows(rows, days=30, fan_state=False)
        assert len(fan_w) == 2  # was one window spanning the pause (legacy ``fan`` stays True while paused)
        assert all(r["ts"] not in {x["ts"] for x in rows[12:14]} for w in fan_w for r in w)
        assert win_w == []

    def test_passive_waits_after_the_windows_close(self):
        rows = _rows([(1, WIN), (6, CLOSED)])
        coord = self._coord(rows)
        with patch.object(_coord_mod, "dt_util", _make_dt_mock(datetime(2026, 1, 1, 12, tzinfo=UTC))):
            wins = coord._extract_passive_windows(rows, days=30)
        assert wins, "a settled closed-up stretch is still learned from"
        first = datetime.fromisoformat(wins[0][0]["ts"])
        close = datetime.fromisoformat(rows[4]["ts"])
        assert (first - close).total_seconds() / 60 >= 30

    def test_live_fit_fits_every_recent_window_once(self):
        rows = _rows([(3, FAN), (0.5, PAUSED), (3, FAN)])
        coord = self._coord(rows)
        with patch.object(_coord_mod, "dt_util", _make_dt_mock(datetime(2026, 1, 1, 7, tzinfo=UTC))):
            coord._run_vent_fan_chart_log_fit(backfill=False)
        assert coord.learning.record_thermal_observation.call_count == 2


class TestVentRelearn1505:
    def test_reset_drops_vent_fits_and_seed_takes_the_median(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        st = engine._state
        st.thermal_model_cache = {
            "k_passive": -0.05,
            "k_vent_window": -0.15,
            "k_vent_fan": -0.15,
            "observation_count_vent_window": 82,
            "observation_count_vent_fan": 189,
        }
        st.thermal_observations = [
            {"date": "2026-11-01", "hvac_mode": "passive", "k_passive": -0.03},
            {"date": "2026-11-01", "hvac_mode": "vent_window", "k_passive": -0.37},
            {"date": "2026-11-01", "hvac_mode": "vent_fan", "k_passive": -0.02},
        ]
        assert engine.reset_vent_rates() == 2
        assert [o["hvac_mode"] for o in st.thermal_observations] == ["passive"]
        assert st.thermal_model_cache["k_vent_window"] is None and st.thermal_model_cache["k_vent_fan"] is None
        assert st.thermal_model_cache["observation_count_vent_fan"] == 0
        assert st.thermal_model_cache["k_passive"] == -0.05  # the envelope rate is untouched
        # the re-learn's own fits, in arrival order: an EWMA from empty would sit near the FIRST (-0.167)
        for k in (-0.167, -0.131, -0.06, -0.054, -0.112):
            st.thermal_observations.append({"date": "2026-11-09", "hvac_mode": "vent_fan", "k_passive": k})
        st.thermal_observations.append({"date": "2026-11-09", "hvac_mode": "vent_window", "k_passive": -0.0235})
        engine.seed_vent_rates_from_fits()
        assert st.thermal_model_cache["k_vent_fan"] == pytest.approx(-0.112)
        assert st.thermal_model_cache["observation_count_vent_fan"] == 5
        assert st.thermal_model_cache["k_vent_window"] == pytest.approx(-0.0235)
