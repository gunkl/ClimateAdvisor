"""Issues #1434/#1438: the HA-stop write task saves what ``async_shutdown`` saves, without counting twice.

Occupant view: a heating or cooling run in progress at a real HA restart keeps its pre-restart minutes in today's
runtime (the real-HA proof is ``tests_ha/test_stop_persists_1434_1438.py``). A zone reload after the STOP (the unload
path runs too) must not add those minutes a second time, or the day's runtime and cost estimate would come out high.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import DailyRecord
from tests.test_restart_cause import _make_coord_stub

_DT = "custom_components.climate_advisor.coordinator.dt_util"
_START = datetime(2026, 1, 15, 8, 0, 0)


def _running_coord(mode: str = "heat"):
    coord = _make_coord_stub()
    coord._RUNTIME_TRACKING = ClimateAdvisorCoordinator._RUNTIME_TRACKING
    coord._RUNTIME_MODE_FIELD = ClimateAdvisorCoordinator._RUNTIME_MODE_FIELD
    coord._flush_runtime_for_mode = types.MethodType(ClimateAdvisorCoordinator._flush_runtime_for_mode, coord)
    coord._today_record = DailyRecord(date="2026-01-15", day_type="cold", trend_direction="stable")
    coord._hvac_session_mode = mode
    coord._hvac_on_since = _START
    coord._thermostat_fan_only_on_since = _START if mode == "fan_only" else None
    if mode == "fan_only":
        coord._hvac_on_since = None
    coord._shut_down = False
    coord._v4_cancel_fit = MagicMock()
    return coord


def _at(minutes: float, coro_fn):
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = _START + timedelta(minutes=minutes)
        asyncio.run(coro_fn())


@pytest.mark.parametrize(
    ("mode", "field"),
    [
        ("heat", "hvac_runtime_minutes"),
        ("cool", "hvac_runtime_minutes"),
        ("fan_only", "thermostat_fan_only_runtime_minutes"),
    ],
)
def test_stop_flushes_the_run_once_and_a_later_unload_adds_only_the_time_since(mode, field):
    coord = _running_coord(mode)
    _at(30, coord._persist_on_ha_stop)
    assert getattr(coord._today_record, field) == pytest.approx(30.0)
    coord._async_save_state.assert_awaited()  # the state file holds today's record with the run's minutes
    coord._chart_log.save.assert_called()
    coord._v4_flush_on_shutdown.assert_awaited()

    _at(30, coord.async_shutdown)  # a reload at the same instant: nothing more to add
    assert getattr(coord._today_record, field) == pytest.approx(30.0)


def test_a_second_stop_flush_never_recounts_the_run():
    coord = _running_coord()
    _at(30, coord._persist_on_ha_stop)
    _at(31, coord._persist_on_ha_stop)  # 1 more minute of real running: only that is added
    assert coord._today_record.hvac_runtime_minutes == pytest.approx(31.0)


def test_unload_alone_still_counts_the_run():
    coord = _running_coord()
    _at(30, coord.async_shutdown)
    assert coord._today_record.hvac_runtime_minutes == pytest.approx(30.0)
    coord._chart_log.save.assert_called()  # Issue #1438: a reload no longer drops unsaved chart-log event rows


def test_one_failing_save_does_not_stop_the_others():
    coord = _running_coord()
    coord._chart_log.save = MagicMock(side_effect=OSError("disk full"))
    _at(30, coord._persist_on_ha_stop)
    assert coord._today_record.hvac_runtime_minutes == pytest.approx(30.0)
    coord._v4_flush_on_shutdown.assert_awaited()
    assert coord.learning._state.last_shutdown_kind == "ha_stop"
