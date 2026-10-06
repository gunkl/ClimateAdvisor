"""Tests for the HA-system-unit (#1021) and sensor-entity (#1020) unit checks.

Exercises the real ``ClimateAdvisorCoordinator._check_unit_sources`` via a partial
instance (object.__new__ + MethodType).
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_COORD_LOGGER = "custom_components.climate_advisor.coordinator"

OUT_ID = "sensor.secret_outdoor_probe"
IN_ID = "sensor.secret_indoor_probe"
SLEEP_ID = "input_number.secret_sleep_probe"


def _cls():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _st(unit="__absent__", state="70"):
    s = MagicMock()
    s.state = state
    s.attributes = {} if unit == "__absent__" else {"unit_of_measurement": unit}
    return s


def _make(configured="fahrenheit", *, ha_unit="__mock__", states=None, config=None, init_attrs=True):
    cls = _cls()
    coord = object.__new__(cls)
    coord.config = {"temp_unit": configured}
    if config:
        coord.config.update(config)
    coord.hass = MagicMock()
    if ha_unit != "__mock__":
        coord.hass.config.units.temperature_unit = ha_unit
    table = states if states is not None else {}
    coord.hass.states.get = lambda eid: table.get(eid)
    coord._table = table
    if init_attrs:
        coord._ha_unit_checked = False
        coord._sensor_unit_checked = set()
        coord._unit_check_info = {}
    coord._check = types.MethodType(cls._check_unit_sources, coord)
    return coord


def _warns(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == _COORD_LOGGER]


class TestHaSystemUnit:
    def test_matching_is_silent_but_recorded(self, caplog):
        c = _make("fahrenheit", ha_unit="°F")
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert _warns(caplog) == []
        assert c._unit_check_info["ha_system_unit"] == "fahrenheit"
        assert c._unit_check_info["ha_unit_mismatch"] is False

    @pytest.mark.parametrize(
        ("configured", "ha_unit", "ha_key"),
        [("celsius", "°F", "fahrenheit"), ("fahrenheit", "°C", "celsius")],
    )
    def test_mismatch_warns_once_and_latches(self, caplog, configured, ha_unit, ha_key):
        c = _make(configured, ha_unit=ha_unit)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
            c._check()
        msgs = _warns(caplog)
        assert len(msgs) == 1
        assert "Home Assistant" in msgs[0]
        assert f"ha_unit={ha_key}" in msgs[0]
        assert c._unit_check_info["ha_unit_mismatch"] is True
        assert c._ha_unit_checked is True

    def test_unrecognised_mock_units_not_latched(self, caplog):
        c = _make("celsius")  # hass.config.units.temperature_unit is a MagicMock
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert _warns(caplog) == []
        assert c._ha_unit_checked is False
        assert "ha_system_unit" not in c._unit_check_info

    def test_stub_without_new_attributes(self, caplog):
        c = _make("celsius", ha_unit="°F", init_attrs=False)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
            c._check()
        assert len(_warns(caplog)) == 1
        assert c._unit_check_info["ha_unit_mismatch"] is True


_SENSOR_CFG = {
    "outdoor_temp_source": "sensor",
    "outdoor_temp_entity": OUT_ID,
    "indoor_temp_source": "sensor",
    "indoor_temp_entity": IN_ID,
    "sleep_indoor_temp_entity": SLEEP_ID,
}


class TestSensorUnits:
    @pytest.mark.parametrize(
        ("role", "eid"),
        [("outdoor_temp", OUT_ID), ("indoor_temp", IN_ID), ("sleep_indoor_temp", SLEEP_ID)],
    )
    def test_each_role_mismatch_warns_once_with_role_not_entity(self, caplog, role, eid):
        c = _make("celsius", states={eid: _st("°F")}, config=_SENSOR_CFG)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
            c._check()
        msgs = _warns(caplog)
        assert len(msgs) == 1
        assert f"role={role}" in msgs[0]
        assert "sensor_unit=fahrenheit" in msgs[0]
        assert eid not in caplog.text
        assert c._unit_check_info["sensor_unit_mismatches"] == {role: "fahrenheit"}

    def test_other_direction(self, caplog):
        c = _make("fahrenheit", states={OUT_ID: _st("°C")}, config=_SENSOR_CFG)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert len(_warns(caplog)) == 1

    @pytest.mark.parametrize("unit", ["°F", "__absent__", "ppm", None])
    def test_matching_absent_unrecognised_silent(self, caplog, unit):
        states = {OUT_ID: _st(unit), IN_ID: _st(unit), SLEEP_ID: _st(unit)}
        c = _make("fahrenheit", states=states, config=_SENSOR_CFG)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert _warns(caplog) == []
        assert "sensor_unit_mismatches" not in c._unit_check_info
        assert c._sensor_unit_checked == {"outdoor_temp", "indoor_temp", "sleep_indoor_temp"}

    def test_source_gating(self, caplog):
        cfg = dict(_SENSOR_CFG, outdoor_temp_source="weather_service", indoor_temp_source="climate_fallback")
        states = {OUT_ID: _st("°F"), IN_ID: _st("°F"), SLEEP_ID: _st("°F")}
        c = _make("celsius", states=states, config=cfg)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        msgs = _warns(caplog)
        assert len(msgs) == 1
        assert "role=sleep_indoor_temp" in msgs[0]
        assert c._sensor_unit_checked == {"sleep_indoor_temp"}

    def test_default_sources_skip_outdoor_and_indoor(self, caplog):
        cfg = {k: v for k, v in _SENSOR_CFG.items() if not k.endswith("_source")}
        c = _make("celsius", states={OUT_ID: _st("°F"), IN_ID: _st("°F")}, config=cfg)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert _warns(caplog) == []

    def test_input_number_source_is_checked(self, caplog):
        cfg = dict(_SENSOR_CFG, outdoor_temp_source="input_number")
        c = _make("celsius", states={OUT_ID: _st("°F")}, config=cfg)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
        assert len(_warns(caplog)) == 1

    @pytest.mark.parametrize("first", [None, "unavailable", "unknown"])
    def test_not_ready_does_not_latch_then_checked(self, caplog, first):
        c = _make("celsius", states={}, config=_SENSOR_CFG)
        if first is not None:
            c._table[OUT_ID] = _st("°F", state=first)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
            assert _warns(caplog) == []
            assert "outdoor_temp" not in c._sensor_unit_checked
            c._table[OUT_ID] = _st("°F")
            c._check()
        assert len(_warns(caplog)) == 1
        assert "outdoor_temp" in c._sensor_unit_checked

    def test_stub_without_new_attributes(self, caplog):
        c = _make("celsius", states={OUT_ID: _st("°F")}, config=_SENSOR_CFG, init_attrs=False)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            c._check()
            c._check()
        assert len(_warns(caplog)) == 1
        assert c._unit_check_info["sensor_unit_mismatches"] == {"outdoor_temp": "fahrenheit"}


class TestWiring:
    """The checks are only useful if the update cycle calls them. Every other test calls
    the methods directly, so deleting the call sites would otherwise leave the suite green."""

    def test_update_cycle_calls_both_unit_checks(self):
        import inspect

        src = inspect.getsource(_cls()._async_update_data_impl)
        assert "self._check_climate_unit()" in src
        assert "self._check_unit_sources()" in src
