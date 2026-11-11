"""Issue #1512 / #1516, coordinator side: an HVAC run is learned from the run itself, at the off edge.

Occupant outcome: a furnace that keeps warming the room after it stops, and an AC after which the room warms, both
produce a learned rate (committed or rejected with a code) the moment the equipment stops, instead of being thrown
away by a post-run window judged on what the room did afterward.

Real coordinator methods bound onto stubs (``tests/test_thermal_event.py::_make_v3_coord``); ``self.learning`` is a
mock, so what is asserted is what the coordinator hands to ``record_hvac_run`` and records when it rejects.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402
from custom_components.climate_advisor import hvac_rate_fit  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    REJECT_HVAC_MODE_SWITCH,
    REJECT_NEW_SESSION,
    REJECT_NO_K_PASSIVE,
    THERMAL_COAST_MAX_S,
)
from tests.test_thermal_event import _make_dt_mock, _make_v3_coord  # noqa: E402

ClimateAdvisorCoordinator = _coord_mod.ClimateAdvisorCoordinator
_COORD_MOD = "custom_components.climate_advisor.coordinator"
_T0 = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _set_indoor(coord, value: float) -> None:
    coord.hass.states.get("climate.test").attributes["current_temperature"] = value


def _start(coord, mode: str, at: datetime) -> None:
    with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock(at)):
        asyncio.run(coord._start_hvac_observation(mode))


def _off_edge(coord, obs_type: str, at: datetime) -> None:
    with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock(at)):
        coord._end_hvac_active_phase(obs_type)


def _coord(**kw):
    coord = _make_v3_coord(**kw)
    coord._rejection_log = {}
    return coord


class TestOffEdgeCommit:
    def test_heat_run_with_post_off_coast_commits_with_the_off_reading(self):
        """A furnace that keeps warming the room after it stops: the rate uses the reading AT the off edge.

        68.0 at the start, 70.0 when the furnace stops, coasting to 71.5 afterward: committed with off_f=70.0.
        """
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=30))

        coord.learning.record_hvac_run.assert_called_once()
        run = coord.learning.record_hvac_run.call_args[0][0]
        assert run["mode"] == "heat"
        assert run["start_f"] == 68.0
        assert run["off_f"] == 70.0
        assert run["peak_f"] == 70.0
        assert run["minutes"] == pytest.approx(30.0)
        assert run["n_active"] == 2
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations

        _set_indoor(coord, 71.5)  # coast: nothing pending, nothing more committed
        coord.learning.record_hvac_run.assert_called_once()
        assert coord._rejection_log == {}

    def test_cool_run_commits_at_the_off_edge_regardless_of_the_room_warming_after(self):
        """Mirror, Issue #1516: an AC run commits at the off edge; the room warming afterward is irrelevant."""
        coord = _coord(indoor_temp=76.0)
        coord.learning.record_hvac_run.return_value = ({"hvac_mode": "cool", "k_active": -3.0}, None)
        _start(coord, "cool", _T0)
        _set_indoor(coord, 74.0)
        _off_edge(coord, OBS_TYPE_HVAC_COOL, _T0 + timedelta(minutes=40))
        _set_indoor(coord, 77.0)

        coord.learning.record_hvac_run.assert_called_once()
        run = coord.learning.record_hvac_run.call_args[0][0]
        assert run["mode"] == "cool"
        assert run["start_f"] == 76.0
        assert run["off_f"] == 74.0
        assert run["peak_f"] == 74.0  # the cool extremum is the trough
        assert coord._today_record.thermal_session_count == 1
        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations

    def test_new_session_no_longer_abandons_a_finished_run(self):
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=20))
        _start(coord, "heat", _T0 + timedelta(minutes=25))

        coord.learning.record_hvac_run.assert_called_once()
        assert coord._rejection_log == {}
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations

    def test_new_session_over_a_run_that_never_ended_is_rejected_with_its_code(self):
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _start(coord, "heat", _T0 + timedelta(minutes=10))
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == REJECT_NEW_SESSION
        assert event["minutes"] == 10.0
        coord.learning.record_hvac_run.assert_not_called()

    def test_abandon_carries_code_signal_and_minutes(self):
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 69.0)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock(_T0 + timedelta(minutes=6))):
            coord._pending_observations[OBS_TYPE_HVAC_HEAT]["active_samples"].append(coord._get_current_sample(6.0))
            coord._abandon_observation(
                OBS_TYPE_HVAC_HEAT, "heat_cool mode switch mid-session", reason_code=REJECT_HVAC_MODE_SWITCH
            )
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == REJECT_HVAC_MODE_SWITCH
        assert event["signal_f"] == pytest.approx(1.0)
        assert event["minutes"] == 6.0
        assert event["k_passive_source"] is None  # key always present on HVAC events, None when unknown

    def test_learning_reject_is_recorded_with_code_and_values(self):
        coord = _coord(indoor_temp=68.0)
        coord.learning.record_hvac_run.return_value = (None, "hvac_run_too_short")
        _start(coord, "heat", _T0)
        _set_indoor(coord, 68.3)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=3))
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == "hvac_run_too_short"
        assert event["signal_f"] == pytest.approx(0.3)
        assert event["minutes"] == 3.0
        assert event["k_passive_source"] == "none"
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations
        assert coord._today_record.thermal_session_count == 0

    def test_learning_health_lists_the_new_codes(self):
        coord = _coord()
        coord.learning._state.thermal_observations = []
        coord._rejection_log = {OBS_TYPE_HVAC_HEAT: [{"reason_code": "hvac_signal_below_resolution"}]}
        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)
        counts = coord._build_learning_health()[OBS_TYPE_HVAC_HEAT]["rejections"]
        for code in (
            "hvac_run_too_short",
            "hvac_signal_below_resolution",
            "new_session_started",
            "fan_activated",
            "hvac_mode_switch",
            "no_k_passive",
            "ols_bounds",
        ):
            assert code in counts
        assert counts["hvac_signal_below_resolution"] == 1


class TestClosedHouseRateOrder:
    @staticmethod
    def _coord_with(model_k, cache_k, *, k_vent=None):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = {}
        coord._thermal_model = {}
        coord.learning = MagicMock()
        coord.learning._state.thermal_model_cache = {"k_passive": cache_k, "k_vent_window": k_vent}
        coord.control_thermal_model = lambda base: {"k_passive": model_k}
        coord._closed_house_rate = types.MethodType(ClimateAdvisorCoordinator._closed_house_rate, coord)
        return coord

    def test_control_model_value_wins_and_names_the_primary_engine(self):
        coord = self._coord_with(-0.031, -0.05)
        k, source = coord._closed_house_rate()
        assert k == -0.031
        # old expectation read the engine setting only to label the source (the v4 shadow structural test forbids
        # that in the coordinator); the label is now the engine-agnostic "primary"
        assert source == "primary"

    def test_v3_cache_is_the_fallback(self):
        assert self._coord_with(None, -0.05)._closed_house_rate() == (-0.05, "v3")

    def test_non_leak_values_are_ignored(self):
        assert self._coord_with(0.02, 0.0)._closed_house_rate() == (None, "none")

    def test_window_open_rate_is_never_a_closed_house_rate(self):
        """k_vent_window must not stand in for k_passive (D4: no bridge)."""
        assert self._coord_with(None, None, k_vent=-0.3)._closed_house_rate() == (None, "none")

    def test_no_rate_hands_none_to_learning_and_records_the_code(self):
        coord = _coord(indoor_temp=68.0)
        coord.learning.record_hvac_run.return_value = (None, REJECT_NO_K_PASSIVE)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=30))
        run = coord.learning.record_hvac_run.call_args[0][0]
        assert run["k_passive"] is None
        assert run["k_passive_source"] == "none"
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == REJECT_NO_K_PASSIVE

    def test_control_model_rate_reaches_learning(self):
        coord = _coord(indoor_temp=68.0)
        coord.control_thermal_model = lambda base: {"k_passive": -0.04}
        coord._closed_house_rate = types.MethodType(ClimateAdvisorCoordinator._closed_house_rate, coord)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=30))
        run = coord.learning.record_hvac_run.call_args[0][0]
        assert run["k_passive"] == -0.04
        assert run["k_passive_source"] == "primary"  # old: the engine id read from the setting (see above)


class TestTickSamplerPeak:
    @pytest.mark.parametrize(
        ("obs_type", "mode", "prior", "indoor", "expected"),
        [
            (OBS_TYPE_HVAC_HEAT, "heat", 70.0, 71.0, 71.0),
            (OBS_TYPE_HVAC_HEAT, "heat", 70.0, 69.0, 70.0),
            (OBS_TYPE_HVAC_COOL, "cool", 75.0, 74.0, 74.0),  # mirror: cool keeps the min
            (OBS_TYPE_HVAC_COOL, "cool", 75.0, 76.0, 75.0),
        ],
    )
    def test_peak_is_the_run_extremum(self, obs_type, mode, prior, indoor, expected):
        from tests.test_thermal_observations import _FAKE_NOW, _make_obs_coord

        coord = _make_obs_coord(hvac_action="heating" if mode == "heat" else "cooling", indoor_temp=indoor)
        coord._pending_observations[obs_type] = {
            "obs_type": obs_type,
            "obs_id": "tick-peak",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "_phase": "active",
            "active_samples": [],
            "peak_indoor_f": prior,
        }
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock(_FAKE_NOW)):
            coord._sample_all_observations()
        assert coord._pending_observations[obs_type]["peak_indoor_f"] == expected


def _rows(spec: list[tuple[int, str]], *, windows_open: bool = False, t0: datetime | None = None) -> list[dict]:
    t0 = t0 or datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    return [
        {
            "ts": (t0 + timedelta(minutes=m)).isoformat(),
            "indoor": 70.0 - 0.02 * m,
            "outdoor": 40.0,
            "hvac": hvac,
            "windows_open": windows_open,
            "fan": False,
            "fan_running": False,
        }
        for m, hvac in spec
    ]


class TestSettlingHvac:
    @staticmethod
    def _spec():
        return [(0, "heating"), (10, "heating")] + [(m, "idle") for m in range(20, 320, 10)]

    def test_flag_is_true_only_in_the_window_after_the_off_edge(self):
        spec = self._spec()
        states = _coord_mod._ventilation_row_states(_rows(spec))
        by_min = {m: st["settling_hvac"] for (m, _), st in zip(spec, states, strict=True)}
        assert by_min[0] is False  # a heating row is never "settling"
        assert by_min[10] is False
        assert THERMAL_COAST_MAX_S == 1800
        # the window runs from the off edge (the first idle row, minute 20), not from the last heating row (minute 10):
        # the old expectation (minute 40 settled) shortened the window by the gap between the last active row and
        # the off edge
        assert by_min[20] is True
        assert by_min[30] is True
        assert by_min[40] is True
        assert by_min[50] is False  # exactly the window after the off edge: settled
        assert by_min[100] is False

    def test_cooling_is_the_mirror(self):
        spec = [(0, "cooling"), (10, "cooling")] + [(m, "idle") for m in range(20, 100, 10)]
        states = _coord_mod._ventilation_row_states(_rows(spec))
        assert [st["settling_hvac"] for st in states[:2]] == [False, False]
        assert states[2]["settling_hvac"] is True
        assert (
            states[4]["settling_hvac"] is True
        )  # old: False, window was timed from the last cooling row, not the off edge
        assert states[5]["settling_hvac"] is False

    def test_passive_windows_start_after_the_settle_window(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._is_solar_hour = ClimateAdvisorCoordinator._is_solar_hour
        windows = coord._extract_passive_windows(_rows(self._spec()), 3650)
        assert windows
        first = datetime.fromisoformat(windows[0][0]["ts"])
        # old: 01:40, ten minutes early (window timed from the last heating row instead of the off edge)
        assert first == datetime(2026, 1, 1, 1, 50, tzinfo=UTC)

    def test_vent_window_windows_exclude_the_settle_window(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._is_solar_hour = ClimateAdvisorCoordinator._is_solar_hour
        rows = _rows(self._spec(), windows_open=True)
        windows = coord._extract_ventilated_windows(rows, 3650, fan_state=False)
        assert windows
        first = datetime.fromisoformat(windows[0][0]["ts"])
        assert first >= datetime(2026, 1, 1, 1, 40, tzinfo=UTC)


class TestDutyBalanceWiring:
    @staticmethod
    def _coord_with_rows(rows):
        coord = object.__new__(ClimateAdvisorCoordinator)
        chart_log = MagicMock()
        chart_log._entries = rows
        coord._chart_log = chart_log
        coord.learning = MagicMock()
        coord.learning.record_duty_balance_fit = MagicMock(return_value=3)
        coord._is_solar_hour = ClimateAdvisorCoordinator._is_solar_hour
        for name in ("_run_duty_balance_chart_log_fit", "_maybe_run_periodic_duty_balance_fit"):
            setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
        return coord

    @staticmethod
    def _recent_rows(n: int, *, days_ago: float = 1.0) -> list[dict]:
        t0 = datetime.now(UTC) - timedelta(days=days_ago)
        return _rows([(10 * i, "idle") for i in range(n)], t0=t0)

    def test_fit_is_called_with_rows_and_states_and_recorded(self):
        rows = self._recent_rows(6)
        old = self._recent_rows(4, days_ago=45.0)  # outside the 30-day window
        coord = self._coord_with_rows(old + rows)
        fit = {"k_passive": -0.04, "k_active_heat": 3.0, "k_active_cool": None, "n_intervals": 30, "r_squared": 0.5}
        with patch.object(hvac_rate_fit, "fit_duty_balance", return_value=fit) as mocked:
            coord._run_duty_balance_chart_log_fit(days=30)
        mocked.assert_called_once()
        args, kwargs = mocked.call_args
        assert [r["ts"] for r in args[0]] == [r["ts"] for r in rows]
        assert len(args[1]) == len(args[0])
        assert all("settling_hvac" in st for st in args[1])
        assert kwargs["is_solar_hour"] == coord._is_solar_hour
        coord.learning.record_duty_balance_fit.assert_called_once_with(fit)

    def test_no_identifiable_fit_records_nothing(self):
        coord = self._coord_with_rows(self._recent_rows(6))
        with patch.object(hvac_rate_fit, "fit_duty_balance", return_value=None):
            coord._run_duty_balance_chart_log_fit()
        coord.learning.record_duty_balance_fit.assert_not_called()

    def test_fit_below_the_r2_floor_is_not_recorded_and_logs_at_info(self, caplog):
        coord = self._coord_with_rows(self._recent_rows(6))
        fit = {"k_passive": -0.04, "k_active_heat": 3.0, "k_active_cool": None, "n_intervals": 30, "r_squared": 0.05}
        with caplog.at_level("INFO"), patch.object(hvac_rate_fit, "fit_duty_balance", return_value=fit):
            coord._run_duty_balance_chart_log_fit()
        coord.learning.record_duty_balance_fit.assert_not_called()
        assert any("below the" in r.message and r.levelname == "INFO" for r in caplog.records)

    def test_failed_fit_logs_the_reason_at_info(self, caplog):
        coord = self._coord_with_rows(self._recent_rows(6))
        with caplog.at_level("INFO"):
            coord._run_duty_balance_chart_log_fit()
        assert any("too few settled night intervals" in r.message and r.levelname == "INFO" for r in caplog.records)

    def test_periodic_wrapper_runs_once_per_day(self):
        coord = self._coord_with_rows(self._recent_rows(6))
        with patch.object(hvac_rate_fit, "fit_duty_balance", return_value={"k_passive": -0.04}) as mocked:
            coord._maybe_run_periodic_duty_balance_fit()
            coord._maybe_run_periodic_duty_balance_fit()
        assert mocked.call_count == 1

    def test_startup_recovery_runs_the_fit(self):
        assert "_maybe_run_periodic_duty_balance_fit" in inspect.getsource(
            ClimateAdvisorCoordinator._run_first_cycle_recovery
        )
