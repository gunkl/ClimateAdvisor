"""Coverage for the coordinator's chart-log thermal fits and observation sampler (Silver test-coverage, #1644).

What the person at home gets from this code: Climate Advisor learns how fast the house loses heat with everything off
(k_passive), with the windows open (k_vent_window / k_vent_fan) and when the sun peaks (solar phase), from the chart
log it already keeps. These tests drive the real coordinator methods on a partial coordinator and check what they
record: a learned rate tagged with its source and grade, a skipped window that is already recorded, the incremental
daily solar re-fit, and the AC-duty solar estimate counting each completed day once.
"""

from __future__ import annotations

import logging
import math
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402

ClimateAdvisorCoordinator = _coord_mod.ClimateAdvisorCoordinator
_LOG = "custom_components.climate_advisor.coordinator"

_NOW = datetime(2026, 1, 2, 6, 0, 0, tzinfo=UTC)


def _decay_rows(
    *,
    t0: datetime,
    hours: float,
    t_in0: float = 72.0,
    t_out: float = 40.0,
    k: float = -0.08,
    step_min: int = 30,
    windows_open: bool = False,
    fan: bool = False,
    hvac: str = "idle",
    naive: bool = False,
) -> list[dict]:
    rows = []
    for i in range(int(hours * 60 / step_min) + 1):
        h = i * step_min / 60.0
        ts = t0 + timedelta(minutes=i * step_min)
        if naive:
            ts = ts.replace(tzinfo=None)
        rows.append(
            {
                "ts": ts.isoformat(),
                "indoor": round(t_out + (t_in0 - t_out) * math.exp(k * h), 3),
                "outdoor": t_out,
                "hvac": hvac,
                "windows_open": windows_open,
                "fan": fan,
            }
        )
    return rows


def _fit_coord(entries: list[dict], *, record_returns: bool = True):
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._chart_log = SimpleNamespace(_entries=entries)
    coord.learning = MagicMock()
    coord.learning.record_thermal_observation = MagicMock(return_value=record_returns)
    for name in (
        "_extract_passive_windows",
        "_passive_endpoint_estimate",
        "_run_passive_chart_log_fit",
        "_extract_ventilated_windows",
        "_vent_endpoint_estimate",
        "_run_vent_chart_log_fit_impl",
        "_run_vent_window_chart_log_fit",
        "_run_vent_fan_chart_log_fit",
        "_select_estimator",
    ):
        setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
    return coord


@pytest.fixture
def night_now(frozen_now):
    return frozen_now(_NOW)


# ---------------------------------------------------------------------------
# Passive (closed house) chart-log fit
# ---------------------------------------------------------------------------


class TestPassiveChartLogFit:
    def test_short_night_window_commits_endpoint_rate(self, night_now, caplog):
        """A 4 h closed-house night with naive stamps: too short for block OLS, so the endpoint rate is recorded."""
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, naive=True)
        coord = _fit_coord(rows)
        with caplog.at_level(logging.INFO, logger=_LOG):
            coord._run_passive_chart_log_fit(backfill=True)
        coord.learning.record_thermal_observation.assert_called_once()
        obs = coord.learning.record_thermal_observation.call_args.args[0]
        assert obs["hvac_mode"] == "passive"
        assert obs["source"] == "endpoint"
        assert obs["confidence_grade"] == "low"
        assert obs["k_passive"] == pytest.approx(-0.08, abs=0.01)
        assert obs["elapsed_hours"] == 4.0
        assert obs["delta_t_f"] < 0
        assert obs["ratio"] == pytest.approx(math.exp(-0.08 * 4), rel=0.15)
        assert "obs_type=passive new_k=" in caplog.text
        assert "committed 1 observations (backfill)" in caplog.text

    def test_already_recorded_window_is_not_counted(self, night_now, caplog):
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4)
        coord = _fit_coord(rows, record_returns=False)
        with caplog.at_level(logging.INFO, logger=_LOG):
            coord._run_passive_chart_log_fit()
        coord.learning.record_thermal_observation.assert_called_once()
        assert "committed" not in caplog.text

    def test_long_night_uses_block_ols(self, night_now):
        """A 7 h clean decay gives block OLS a good fit that agrees with the endpoint: block_ols, medium grade."""
        rows = _decay_rows(t0=datetime(2025, 12, 31, 22, 0, tzinfo=UTC), hours=7, step_min=10)
        coord = _fit_coord(rows)
        coord._run_passive_chart_log_fit()
        obs = coord.learning.record_thermal_observation.call_args.args[0]
        assert obs["source"] == "block_ols"
        assert obs["confidence_grade"] == "medium"
        assert obs["r_squared"] >= 0.5

    def test_flat_window_yields_no_estimate(self, night_now):
        """Indoor that does not move by 1 °F gives neither estimator anything: nothing is recorded."""
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, k=-0.001)
        coord = _fit_coord(rows)
        coord._run_passive_chart_log_fit()
        coord.learning.record_thermal_observation.assert_not_called()

    def test_no_chart_log_or_empty_or_no_window(self, night_now):
        coord = _fit_coord([])
        coord._chart_log = None
        coord._run_passive_chart_log_fit()
        coord._chart_log = SimpleNamespace(_entries=[])
        coord._run_passive_chart_log_fit()
        # only daytime rows: the solar guard rejects them
        coord._chart_log = SimpleNamespace(
            _entries=_decay_rows(t0=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), hours=4),
        )
        coord._run_passive_chart_log_fit()
        coord.learning.record_thermal_observation.assert_not_called()


class TestExtractPassiveWindows:
    def test_skips_bad_rows_and_splits_on_missing_reading(self, night_now):
        coord = _fit_coord([])
        good = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, naive=True)
        rows = (
            [{"ts": ""}, {"ts": "not-a-date"}, {"ts": "2025-11-01T00:00:00+00:00", "indoor": 70, "outdoor": 40}]
            + good[:2]
            + [{"ts": good[2]["ts"], "indoor": None, "outdoor": 40}]  # flush a 2-row (too short) window
            + good[3:]
        )
        windows = coord._extract_passive_windows(rows, 2)
        assert len(windows) == 1
        assert windows[0][0]["ts"] == good[3]["ts"]
        assert windows[0][-1]["ts"] == good[-1]["ts"]

    def test_heating_row_breaks_window(self, night_now):
        coord = _fit_coord([])
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4)
        rows[4] = {**rows[4], "hvac": "heating"}
        windows = coord._extract_passive_windows(rows, 2)
        # after the heating row the house is still coasting, so the remainder is too short to count
        assert all(len(w) < len(rows) for w in windows)


class TestPassiveEndpointEstimate:
    def test_rejects_short_flat_and_bad_windows(self):
        coord = _fit_coord([])
        t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        bad_ts = [{"ts": "x", "indoor": 70.0, "outdoor": 40.0}, {"ts": "y", "indoor": 66.0, "outdoor": 40.0}]
        assert coord._passive_endpoint_estimate(bad_ts) is None
        flat = _decay_rows(t0=t0, hours=4, k=-0.001)
        assert coord._passive_endpoint_estimate(flat) is None
        short = _decay_rows(t0=t0, hours=1, k=-0.5)
        assert coord._passive_endpoint_estimate(short) is None

    def test_naive_window_estimates(self):
        coord = _fit_coord([])
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, naive=True)
        window = [{"ts": r["ts"], "indoor": r["indoor"], "outdoor": r["outdoor"]} for r in rows]
        est = coord._passive_endpoint_estimate(window)
        assert est["source"] == "endpoint"
        assert est["k"] == pytest.approx(-0.08, abs=0.01)

    def test_no_root_found_returns_none(self):
        coord = _fit_coord([])
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4)
        with patch.object(_coord_mod, "compute_k_passive_endpoint", return_value=None):
            assert coord._passive_endpoint_estimate(rows) is None


class TestSolarHour:
    def test_naive_day_and_bad_input(self):
        assert ClimateAdvisorCoordinator._is_solar_hour("2026-01-02T12:00:00") is True
        assert ClimateAdvisorCoordinator._is_solar_hour("2026-01-02T03:00:00") is False
        assert ClimateAdvisorCoordinator._is_solar_hour("garbage") is False


# ---------------------------------------------------------------------------
# Ventilated (windows open) chart-log fit
# ---------------------------------------------------------------------------


class TestVentChartLogFit:
    def test_windows_only_night_commits_endpoint(self, night_now, caplog):
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, windows_open=True, k=-0.3, naive=True)
        coord = _fit_coord(rows)
        with caplog.at_level(logging.INFO, logger=_LOG):
            coord._run_vent_window_chart_log_fit(backfill=True)
        obs = coord.learning.record_thermal_observation.call_args.args[0]
        assert obs["hvac_mode"] == "vent_window"
        assert obs["source"] == "endpoint"
        assert obs["k_passive"] == pytest.approx(-0.3, abs=0.03)
        assert "obs_type=vent_window new_k=" in caplog.text
        assert "chart_log vent_window: committed 1 observations (backfill)" in caplog.text

    def test_fan_night_already_recorded(self, night_now, caplog):
        rows = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, windows_open=True, fan=True, k=-0.5)
        coord = _fit_coord(rows, record_returns=False)
        with caplog.at_level(logging.INFO, logger=_LOG):
            coord._run_vent_fan_chart_log_fit()
        obs = coord.learning.record_thermal_observation.call_args.args[0]
        assert obs["hvac_mode"] == "vent_fan"
        assert "committed" not in caplog.text

    def test_long_vent_night_uses_block_ols(self, night_now):
        rows = _decay_rows(
            t0=datetime(2025, 12, 31, 22, 0, tzinfo=UTC), hours=7, step_min=10, windows_open=True, k=-0.2
        )
        coord = _fit_coord(rows)
        coord._run_vent_window_chart_log_fit(days=5)
        obs = coord.learning.record_thermal_observation.call_args.args[0]
        assert obs["source"] == "block_ols"

    def test_nothing_to_fit(self, night_now):
        coord = _fit_coord([])
        coord._chart_log = None
        coord._run_vent_window_chart_log_fit()
        coord._chart_log = SimpleNamespace(_entries=[])
        coord._run_vent_window_chart_log_fit()
        # closed windows: no ventilated window
        coord._chart_log = SimpleNamespace(_entries=_decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4))
        coord._run_vent_window_chart_log_fit()
        # flat open-window night: a window, but no estimate
        coord._chart_log = SimpleNamespace(
            _entries=_decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, windows_open=True, k=-0.001)
        )
        coord._run_vent_window_chart_log_fit()
        coord.learning.record_thermal_observation.assert_not_called()

    def test_extract_skips_bad_rows(self, night_now):
        coord = _fit_coord([])
        good = _decay_rows(t0=datetime(2026, 1, 2, 0, 0, tzinfo=UTC), hours=4, windows_open=True, naive=True)
        rows = (
            [{"ts": ""}, {"ts": "nope"}, {"ts": "2025-10-01T00:00:00+00:00", "indoor": 70, "outdoor": 40}]
            + [{"ts": good[0]["ts"], "indoor": None, "outdoor": 40, "windows_open": True}]
            + good
        )
        windows = coord._extract_ventilated_windows(rows, 2, fan_state=False)
        assert len(windows) == 1 and len(windows[0]) == len(good)

    def test_vent_endpoint_rejections(self):
        coord = _fit_coord([])
        t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        assert coord._vent_endpoint_estimate([{"ts": "a", "indoor": 70.0}, {"ts": "b", "indoor": 60.0}]) is None
        assert coord._vent_endpoint_estimate(_decay_rows(t0=t0, hours=4, k=-0.001)) is None
        assert coord._vent_endpoint_estimate(_decay_rows(t0=t0, hours=1, k=-0.5)) is None
        naive = _decay_rows(t0=t0, hours=4, k=-0.3, naive=True)
        assert coord._vent_endpoint_estimate(naive)["k"] == pytest.approx(-0.3, abs=0.03)
        with patch.object(_coord_mod, "compute_k_passive_endpoint", return_value=None):
            assert coord._vent_endpoint_estimate(naive) is None


# ---------------------------------------------------------------------------
# Solar phase fits
# ---------------------------------------------------------------------------


class TestPeriodicSolarPhaseFit:
    def _coord(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._run_solar_phase_chart_log_fit = MagicMock()
        coord._maybe_run_periodic_solar_phase_fit = types.MethodType(
            ClimateAdvisorCoordinator._maybe_run_periodic_solar_phase_fit, coord
        )
        return coord

    def test_waits_for_the_startup_backfill(self, night_now):
        coord = self._coord()
        coord._solar_phase_backfill = False
        coord._last_solar_phase_fit_date = None
        coord._maybe_run_periodic_solar_phase_fit()
        coord._run_solar_phase_chart_log_fit.assert_not_called()

    def test_runs_once_per_day(self, night_now, caplog):
        coord = self._coord()
        coord._solar_phase_backfill = True
        coord._last_solar_phase_fit_date = None
        with caplog.at_level(logging.INFO, logger=_LOG):
            coord._maybe_run_periodic_solar_phase_fit()
            coord._maybe_run_periodic_solar_phase_fit()
        coord._run_solar_phase_chart_log_fit.assert_called_once_with(backfill=False)
        assert coord._last_solar_phase_fit_date == _NOW.date()
        assert "daily incremental re-fit complete" in caplog.text


def _solar_rows(day: datetime, *, naive: bool = False) -> list[dict]:
    rows = []
    for i in range(12):  # 09:00 .. 14:30, closed and idle, warming with the sun
        ts = day.replace(hour=9) + timedelta(minutes=30 * i)
        if naive:
            ts = ts.replace(tzinfo=None)
        rows.append(
            {
                "ts": ts.isoformat(),
                "indoor": 68.0 + i * 0.4,
                "outdoor": 50.0 + i,
                "hvac": "idle",
                "fan": "off",
                "windows_open": False,
            }
        )
    return rows


class TestSolarPhaseChartLogFit:
    def _coord(self, entries, *, update_returns=True):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._chart_log = SimpleNamespace(_entries=entries)
        coord.learning = MagicMock()
        coord.learning._state.thermal_model_cache = {"solar_phase_offset_h": 1.5}
        coord.learning.update_solar_phase_offset = MagicMock(return_value=update_returns)
        return coord

    def test_skips_without_chart_log(self, frozen_now, caplog):
        frozen_now(datetime(2026, 1, 2, 18, 0, tzinfo=UTC))
        coord = self._coord([])
        coord._chart_log = None
        with caplog.at_level(logging.DEBUG, logger=_LOG):
            ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(coord)
            coord._chart_log = SimpleNamespace(_entries=[])
            ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(coord)
        assert "chart_log not initialized" in caplog.text
        assert "chart_log empty" in caplog.text

    def test_window_committed_with_ewma_log(self, frozen_now, caplog):
        frozen_now(datetime(2026, 1, 2, 18, 0, tzinfo=UTC))
        rows = (
            [{"ts": ""}, {"ts": "bad"}, {"ts": "2025-06-01T10:00:00+00:00", "indoor": 70, "outdoor": 60}]
            + _solar_rows(datetime(2026, 1, 2, tzinfo=UTC), naive=True)
            + [{"ts": "2026-01-02T15:00:00+00:00", "indoor": None, "outdoor": 60}]
        )
        coord = self._coord(rows)
        with (
            patch.object(_coord_mod, "_estimate_solar_phase_offset", return_value=(2.25, None)),
            caplog.at_level(logging.INFO, logger=_LOG),
        ):
            ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(coord, backfill=True)
        call = coord.learning.update_solar_phase_offset.call_args
        assert call.args[0] == 2.25
        assert call.kwargs["window_end"] == rows[-2]["ts"]
        assert "Solar phase EWMA: observed=2.25h" in caplog.text
        assert "1/1 windows committed (backfill) (0 rejected)" in caplog.text

    def test_rejected_and_already_counted_windows(self, frozen_now, caplog):
        frozen_now(datetime(2026, 1, 2, 18, 0, tzinfo=UTC))
        day1 = _solar_rows(datetime(2026, 1, 1, tzinfo=UTC))
        day2 = _solar_rows(datetime(2026, 1, 2, tzinfo=UTC))
        gap = [{"ts": "2026-01-01T16:00:00+00:00", "indoor": None, "outdoor": 50.0}]
        coord = self._coord(day1 + gap + day2, update_returns=False)
        with (
            patch.object(_coord_mod, "_estimate_solar_phase_offset", side_effect=[(None, "flat"), (2.0, None)]),
            caplog.at_level(logging.INFO, logger=_LOG),
        ):
            ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(coord, backfill=True)
        assert coord.learning.update_solar_phase_offset.call_count == 1
        assert "0/2 windows committed (backfill) (1 rejected)" in caplog.text

    def test_as_local_failure_ends_the_window(self, frozen_now, caplog):
        frozen_now(datetime(2026, 1, 2, 18, 0, tzinfo=UTC))
        coord = self._coord(_solar_rows(datetime(2026, 1, 2, tzinfo=UTC)))
        with (
            patch.object(_coord_mod.dt_util, "as_local", side_effect=RuntimeError("tz")),
            caplog.at_level(logging.INFO, logger=_LOG),
        ):
            ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(coord)
        assert "0 qualifying passive-daytime windows — offset unchanged at 1.50h" in caplog.text
        coord.learning.update_solar_phase_offset.assert_not_called()


class TestAcDutySolarPhaseFit:
    def test_skips_without_chart_log(self, caplog):
        stub = SimpleNamespace(_chart_log=None, learning=MagicMock(), _solar_phase_ac_backfill=False)
        with caplog.at_level(logging.DEBUG, logger=_LOG):
            ClimateAdvisorCoordinator._run_ac_duty_solar_phase_fit(stub)
            stub._chart_log = SimpleNamespace(_entries=[])
            ClimateAdvisorCoordinator._run_ac_duty_solar_phase_fit(stub)
        assert "AC duty solar phase fit: chart_log not initialized" in caplog.text
        assert "AC duty solar phase fit: chart_log empty" in caplog.text
        assert stub._solar_phase_ac_backfill is False

    def test_bad_rows_rejected_days_and_no_estimate(self, frozen_now, caplog):
        frozen_now(datetime(2026, 6, 20, 12, 0, tzinfo=UTC))
        learning = SimpleNamespace(
            _state=SimpleNamespace(thermal_model_cache={"solar_phase_offset_ac_h": 2.5}),
            update_ac_duty_solar_phase_offset=MagicMock(return_value=True),
        )
        rows = [
            {"ts": "garbage"},
            {"ts": "2026-06-10T12:00:00+00:00", "hvac": "cooling"},
            {"ts": "2026-06-11T12:00:00+00:00", "hvac": "cooling"},
            {"ts": "2026-06-12T12:00:00+00:00", "hvac": "cooling"},
        ]
        stub = SimpleNamespace(_chart_log=SimpleNamespace(_entries=rows), learning=learning, _solar_phase_ac_backfill=0)
        with (
            patch.object(_coord_mod, "_entry_hour", side_effect=[None, 12, 12, 12]),
            patch.object(_coord_mod, "_is_ac_duty_solar_day", side_effect=[(False, "few"), (True, ""), (True, "")]),
            patch.object(_coord_mod, "_estimate_ac_duty_solar_phase", side_effect=[None, 3.0]),
            caplog.at_level(logging.INFO, logger=_LOG),
        ):
            ClimateAdvisorCoordinator._run_ac_duty_solar_phase_fit(stub)
        learning.update_ac_duty_solar_phase_offset.assert_called_once_with(3.0, "2026-06-12")
        assert "committed=1 rejected=2 current_offset=2.50h" in caplog.text
        assert stub._solar_phase_ac_backfill is True

    def test_missing_ts_key_skipped_and_unreadable_newest(self, frozen_now, caplog):
        frozen_now(datetime(2026, 6, 20, 12, 0, tzinfo=UTC))
        learning = MagicMock(spec=["update_ac_duty_solar_phase_offset"])
        rows = [{"hvac": "cooling"}]
        stub = SimpleNamespace(_chart_log=SimpleNamespace(_entries=rows), learning=learning, _solar_phase_ac_backfill=0)
        with (
            patch.object(_coord_mod, "_entry_hour", return_value=12),
            caplog.at_level(logging.INFO, logger=_LOG),
        ):
            ClimateAdvisorCoordinator._run_ac_duty_solar_phase_fit(stub)
        assert "committed=0 rejected=0 current_offset=none" in caplog.text
        learning.update_ac_duty_solar_phase_offset.assert_not_called()
