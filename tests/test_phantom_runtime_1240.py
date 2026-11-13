"""Issue #1240: a mode change while the thermostat reports idle is not HVAC runtime.

What the occupant saw: the thermostat sat in heat (or cool) mode without ever running, was then
switched off, and the dashboard reported hours of "HVAC Runtime" that never happened. Those false
minutes also reached the compliance average, the ``high_runtime_mild_days`` suggestion, the AI
context, the TOU summary and the thermal-learning watchdog.

Cause (regression from ebbf5ab, Fix #93): the hvac_mode fallback in ``_async_thermostat_changed``
was widened from "hvac_action missing" to "hvac_action is not heating/cooling" so a thermostat that
reports ``"fan"`` persistently would still get session edges. That also swept in ``"idle"``/``"off"``
— a positive "not running" report — so off->heat with hvac_action idle armed the runtime timer, and
heat->off (or the day-rollover flush) credited the idle window.

These tests drive the REAL ``_async_thermostat_changed`` (via the shared stub in
``test_hvac_session_detection``) and assert the persisted counters.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest

from tests.test_hvac_session_detection import (
    _make_state,
    _make_thermostat_coord,
    _make_thermostat_event,
)

_DT = "custom_components.climate_advisor.coordinator.dt_util"


def _fire(coord, when: datetime, old: tuple[str, str], new: tuple[str, str]) -> None:
    """Deliver one thermostat state change (mode, hvac_action) -> (mode, hvac_action) at ``when``."""
    event = _make_thermostat_event(_make_state(old[0], old[1]), _make_state(new[0], new[1]))
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = when
        asyncio.run(coord._async_thermostat_changed(event))


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 8, hour, minute, 0)


class TestIdleModeChangeIsNotRuntime:
    """A mode selection the thermostat reports as idle/off must never accrue runtime."""

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("idle_action", ["idle", "off"])
    def test_idle_mode_window_then_off_gives_zero_runtime(self, mode, idle_action):
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", idle_action), (mode, idle_action))
        assert coord._hvac_on_since is None, "an idle mode selection must not arm the runtime timer"
        _fire(coord, _t(11), (mode, idle_action), ("off", idle_action))  # 1 h later

        assert coord._today_record.hvac_runtime_minutes == 0.0
        assert coord._hvac_session_mode is None
        coord._start_hvac_observation.assert_not_called()

    def test_idle_heat_window_contributes_nothing_to_live_or_rollover_total(self):
        """The day-rollover flush and the live getter both read the timer: nothing may be armed."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", "idle"), ("heat", "idle"))
        with patch(_DT) as mock_dt:
            mock_dt.now.return_value = _t(23, 59)
            coord._flush_runtime_for_mode(coord._hvac_session_mode)  # what the rollover does
            assert coord._runtime_today_for_mode("heat") == 0.0
        assert coord._today_record.hvac_runtime_minutes == 0.0

    def test_real_heating_after_idle_window_counts_only_the_heating(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", "idle"), ("heat", "idle"))
        _fire(coord, _t(11), ("heat", "idle"), ("heat", "heating"))  # burner on
        _fire(coord, _t(11, 20), ("heat", "heating"), ("heat", "idle"))  # 20 min of heat
        _fire(coord, _t(12), ("heat", "idle"), ("off", "idle"))

        assert coord._today_record.hvac_runtime_minutes == pytest.approx(20.0)
        coord._start_hvac_observation.assert_called_once_with("heat")

    def test_real_cooling_after_idle_window_counts_only_the_cooling(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(13), ("off", "idle"), ("cool", "idle"))
        _fire(coord, _t(14), ("cool", "idle"), ("cool", "cooling"))
        _fire(coord, _t(14, 35), ("cool", "cooling"), ("cool", "idle"))
        _fire(coord, _t(16), ("cool", "idle"), ("off", "idle"))

        assert coord._today_record.hvac_runtime_minutes == pytest.approx(35.0)

    def test_idle_heat_to_idle_cool_mode_switch_starts_nothing(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", "idle"), ("heat", "idle"))
        _fire(coord, _t(10, 30), ("heat", "idle"), ("cool", "idle"))
        _fire(coord, _t(11), ("cool", "idle"), ("off", "idle"))

        assert coord._today_record.hvac_runtime_minutes == 0.0
        assert coord._today_record.thermostat_fan_only_runtime_minutes == 0.0


class TestSymmetryFanOnlyAndGlide:
    """The same rule on the fan_only counter and on the glide/stop paths."""

    def test_idle_fan_only_window_gives_zero_fan_only_runtime(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", "idle"), ("fan_only", "idle"))
        _fire(coord, _t(11), ("fan_only", "idle"), ("off", "idle"))

        assert coord._today_record.thermostat_fan_only_runtime_minutes == 0.0
        assert coord._thermostat_fan_only_on_since is None

    def test_glide_from_running_fan_only_into_idle_heat_ends_the_session(self):
        """fan_only (blower running) -> heat while idle: the fan run ends, no heat timer starts."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", ""), ("fan_only", "fan"))
        _fire(coord, _t(10, 30), ("fan_only", "fan"), ("heat", "idle"))  # 30 min of fan
        _fire(coord, _t(12), ("heat", "idle"), ("off", "idle"))

        assert coord._today_record.thermostat_fan_only_runtime_minutes == pytest.approx(30.0)
        assert coord._today_record.hvac_runtime_minutes == 0.0
        assert coord._hvac_on_since is None
        coord._start_hvac_observation.assert_not_called()

    def test_idle_heat_then_running_fan_only_tracks_only_the_fan(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", "idle"), ("heat", "idle"))
        _fire(coord, _t(11), ("heat", "idle"), ("fan_only", "fan"))
        _fire(coord, _t(11, 45), ("fan_only", "fan"), ("off", "idle"))

        assert coord._today_record.hvac_runtime_minutes == 0.0
        assert coord._today_record.thermostat_fan_only_runtime_minutes == pytest.approx(45.0)

    def test_session_ends_when_thermostat_turns_idle_without_a_mode_change(self):
        """Stop-side mirror: a reported heat run ends when the thermostat reports idle, not when the mode is switched
        off. (Was a no-signal "fan" session; owner decision 2026-11-13 removed the fallback that started it, so the
        run is now reported as 'heating'.)"""
        coord = _make_thermostat_coord()
        _fire(coord, _t(10), ("off", ""), ("heat", "heating"))
        _fire(coord, _t(10, 40), ("heat", "heating"), ("heat", "idle"))  # 40 min, then idle
        _fire(coord, _t(13), ("heat", "idle"), ("off", "idle"))  # idle for 2 h 20 min

        assert coord._today_record.hvac_runtime_minutes == pytest.approx(40.0)
        assert coord._hvac_on_since is None


class TestNoSignalFallbackRemoved:
    """Owner decision 2026-11-13 ("Never, remove it."): the #93 mode fallback these guards protected is gone. A
    heating or cooling run exists only when the thermostat reports heating/cooling; a missing or 'fan' hvac_action
    with a mode selected is no run, no runtime and no observation. (Was ``TestNoSignalFallbackKept``, which asserted
    50 min of runtime from the selected mode; that expectation is now wrong by the owner's decision.)"""

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("action", ["", "fan"])
    def test_no_run_when_hvac_action_missing_or_fan(self, mode, action):
        coord = _make_thermostat_coord()
        _fire(coord, _t(6), ("off", action), (mode, action))
        _fire(coord, _t(6, 50), (mode, action), ("off", action))

        assert coord._today_record.hvac_runtime_minutes == 0.0
        coord._start_hvac_observation.assert_not_called()
