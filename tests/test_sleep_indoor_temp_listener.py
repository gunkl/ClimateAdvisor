"""Regression test for Issue #964: the sleep-window indoor sensor
(``sleep_indoor_temp_entity``) needs a state-change listener of its own.

Before this fix, the only per-tick listener for indoor temperature was on the
climate entity (``_async_thermostat_changed``). When ``sleep_indoor_temp_entity`` is
configured, ``_get_indoor_temp()`` swaps to that sensor during the sleep window, but
nothing woke the reactive checks when *that* sensor changed — they only fired on the
climate entity's own tick cadence, which is a different physical sensor with its own,
independent update pattern. ``_on_sleep_indoor_temp_changed()``
(``coordinator.py``) is the new listener that closes this gap, mirroring the existing
``_indoor_temp_entity``/``_async_indoor_temp_changed`` pattern.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_coord(*, sleep_time: str = "22:30", wake_time: str = "06:30") -> MagicMock:
    """Coordinator stub with _on_sleep_indoor_temp_changed + its dependency bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    coord.config = {
        "climate_entity": "climate.thermostat",
        "sleep_indoor_temp_entity": "sensor.bedroom_temp",
        "sleep_time": sleep_time,
        "wake_time": wake_time,
    }

    ae = MagicMock()
    ae._natural_vent_active = True
    ae._fan_active = False
    ae.nat_vent_temperature_check = AsyncMock()
    ae.fan_thermostat_check = AsyncMock()
    ae.comfort_family_temperature_check = AsyncMock()
    coord.automation_engine = ae

    coord._get_indoor_temp = MagicMock(return_value=66.0)
    coord._last_outdoor_temp = 65.0
    coord._last_predicted_indoor = None

    coord._async_run_temp_reactive_checks = types.MethodType(
        ClimateAdvisorCoordinator._async_run_temp_reactive_checks, coord
    )
    coord._on_sleep_indoor_temp_changed = types.MethodType(
        ClimateAdvisorCoordinator._on_sleep_indoor_temp_changed, coord
    )
    return coord


class TestSleepIndoorTempListener:
    """_on_sleep_indoor_temp_changed() must trigger the reactive checks during
    the sleep window, and stay a no-op outside it."""

    def test_fires_reactive_checks_during_sleep_window(self):
        coord = _make_coord()
        _during_sleep = datetime(2026, 9, 22, 2, 0)  # 2:00 AM — well inside 22:30->06:30

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_during_sleep):
            asyncio.run(coord._on_sleep_indoor_temp_changed(MagicMock()))

        coord.automation_engine.nat_vent_temperature_check.assert_called_once()
        passed_temp = coord.automation_engine.nat_vent_temperature_check.call_args[0][0]
        assert passed_temp == 66.0

    def test_no_op_outside_sleep_window(self):
        """A bedroom-sensor tick during the day must not trigger a re-check —
        the primary (hallway) listener already covers the awake/daytime case,
        and _get_indoor_temp() wouldn't be reading the bedroom sensor anyway."""
        coord = _make_coord()
        _during_day = datetime(2026, 9, 22, 14, 0)  # 2:00 PM — well outside the sleep window

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_during_day):
            asyncio.run(coord._on_sleep_indoor_temp_changed(MagicMock()))

        coord.automation_engine.nat_vent_temperature_check.assert_not_called()

    def test_reactive_checks_share_single_resolved_indoor_value(self):
        """_async_run_temp_reactive_checks() must resolve indoor exactly once and
        reuse it for all three checks (Issue #964 DRY requirement — previously two
        of the three checks independently re-derived their own, inconsistent value)."""
        coord = _make_coord()
        coord.automation_engine._fan_active = True
        _during_sleep = datetime(2026, 9, 22, 2, 0)

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_during_sleep):
            asyncio.run(coord._on_sleep_indoor_temp_changed(MagicMock()))

        assert coord._get_indoor_temp.call_count == 1
        assert coord.automation_engine.nat_vent_temperature_check.call_args[0][0] == 66.0
        assert coord.automation_engine.fan_thermostat_check.call_args[1]["indoor"] == 66.0
        assert coord.automation_engine.comfort_family_temperature_check.call_args[0][0] == 66.0
