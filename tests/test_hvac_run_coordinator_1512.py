"""Issue #1512 / #1516, coordinator side: the off edge no longer commits a per-run rate.

Occupant outcome: a furnace that keeps warming the room after it stops, and an AC after which the room warms, are
learned by the pooled cycle model from the whole recorded cycle (tests/test_cycle_wiring_1512.py), so the off edge
only has to tidy the pending observation. Pending-observation bookkeeping, the rejection codes of abandoned
observations and the tick sampler's run extremum stay here.

Real coordinator methods bound onto stubs (``tests/test_thermal_event.py::_make_v3_coord``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    REJECT_HVAC_MODE_SWITCH,
    REJECT_NEW_SESSION,
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
    def test_heat_off_edge_commits_nothing_per_run(self):
        """A furnace run's off edge removes the pending observation and learns nothing by itself.

        Old: record_hvac_run was handed the off-edge reading (the per-run estimator is retired, the pooled cycle
        model fits the whole cycle including the coast)."""
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=30))
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations
        coord.learning.apply_cycle_fit.assert_not_called()
        assert coord._rejection_log == {}
        assert coord._today_record.thermal_session_count == 0

    def test_cool_off_edge_commits_nothing_per_run(self):
        """Mirror: an AC run's off edge only removes the pending observation."""
        coord = _coord(indoor_temp=76.0)
        _start(coord, "cool", _T0)
        _set_indoor(coord, 74.0)
        _off_edge(coord, OBS_TYPE_HVAC_COOL, _T0 + timedelta(minutes=40))
        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations
        coord.learning.apply_cycle_fit.assert_not_called()
        assert coord._rejection_log == {}

    def test_new_session_no_longer_abandons_a_finished_run(self):
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _set_indoor(coord, 70.0)
        _off_edge(coord, OBS_TYPE_HVAC_HEAT, _T0 + timedelta(minutes=20))
        _start(coord, "heat", _T0 + timedelta(minutes=25))

        assert coord._rejection_log == {}
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations

    def test_new_session_over_a_run_that_never_ended_is_rejected_with_its_code(self):
        coord = _coord(indoor_temp=68.0)
        _start(coord, "heat", _T0)
        _start(coord, "heat", _T0 + timedelta(minutes=10))
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == REJECT_NEW_SESSION
        assert event["minutes"] == 10.0

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

    def test_learning_health_lists_the_cycle_codes(self):
        coord = _coord()
        coord.learning._state.thermal_observations = []
        coord._rejection_log = {OBS_TYPE_HVAC_HEAT: [{"reason_code": "cycle_signal_below_resolution"}]}
        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)
        counts = coord._build_learning_health()[OBS_TYPE_HVAC_HEAT]["rejections"]
        for code in (
            "cycle_too_short",
            "cycle_signal_below_resolution",
            "cycle_excluded_window_fan",
            "cycle_setpoint_change",
            "cycle_gap_restart",
            "cycle_thermostat_unavailable",
            "new_session_started",
            "fan_activated",
            "hvac_mode_switch",
            "no_k_passive",
            "ols_bounds",
        ):
            assert code in counts
        assert counts["cycle_signal_below_resolution"] == 1


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
