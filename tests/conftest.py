"""Shared pytest fixtures for Climate Advisor tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Ensure the project root is on sys.path so imports from
# custom_components.climate_advisor resolve correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Mock the homeassistant package and its submodules so tests can import
# Climate Advisor modules without a running HA instance. This must happen
# BEFORE any custom_components imports.
def _make_mock_module(name):
    """Create a MagicMock that works as a module for 'from X import Y' statements."""
    mod = MagicMock()
    mod.__name__ = name
    mod.__path__ = []
    mod.__file__ = None
    mod.__spec__ = None
    mod.__loader__ = None
    mod.__package__ = name
    return mod


_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.weather",
    "homeassistant.components.climate",
    "homeassistant.data_entry_flow",
    "homeassistant.exceptions",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.components.http",
    "homeassistant.components.repairs",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.config_validation",
    "aiohttp",
    "aiohttp.web",
]

for mod_name in _HA_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _make_mock_module(mod_name)


# RepairsFlow needs to be a real class so repairs.py can subclass it
class _MockRepairsFlow:
    """Minimal stand-in for homeassistant.components.repairs.RepairsFlow."""

    hass = None

    def async_show_form(self, *, step_id, data_schema, errors=None):
        result = {"type": "form", "step_id": step_id, "data_schema": data_schema}
        if errors:
            result["errors"] = errors
        return result

    def async_create_entry(self, *, title="", data):
        return {"type": "create_entry", "title": title, "data": data}


class _MockConfirmRepairFlow(_MockRepairsFlow):
    """Minimal stand-in for homeassistant.components.repairs.ConfirmRepairFlow."""


sys.modules["homeassistant.components.repairs"].RepairsFlow = _MockRepairsFlow
sys.modules["homeassistant.components.repairs"].ConfirmRepairFlow = _MockConfirmRepairFlow


# DataUpdateCoordinator needs to be a real class so coordinator.py can subclass it
class _MockDataUpdateCoordinator:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.DataUpdateCoordinator."""

    def __init__(self, *args, **kwargs):
        pass

    async def async_request_refresh(self):
        """Stub for triggering a data refresh."""


sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = _MockDataUpdateCoordinator


# CoordinatorEntity and SensorEntity need to be real classes so sensor.py can subclass them
# (MagicMock instances cannot be used as base classes — metaclass conflict)
class _MockCoordinatorEntity:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator


class _MockSensorEntity:
    """Minimal stand-in for homeassistant.components.sensor.SensorEntity."""


sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _MockCoordinatorEntity
sys.modules["homeassistant.components.sensor"].SensorEntity = _MockSensorEntity

# Add SensorStateClass and SensorDeviceClass as real objects so tests can compare them
import enum as _enum  # noqa: E402


class _SensorStateClass(_enum.StrEnum):
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class _SensorDeviceClass(_enum.StrEnum):
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    PRESSURE = "pressure"
    POWER = "power"
    ENERGY = "energy"


sys.modules["homeassistant.components.sensor"].SensorStateClass = _SensorStateClass
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = _SensorDeviceClass

# Add UnitOfTemperature to homeassistant.const
import enum as _enum2  # noqa: E402, F811


class _UnitOfTemperature(_enum2.StrEnum):
    FAHRENHEIT = "°F"
    CELSIUS = "°C"
    KELVIN = "K"


sys.modules["homeassistant.const"].UnitOfTemperature = _UnitOfTemperature


def _install_ha_stubs():
    """No-op: stubs are installed at module load time in conftest."""


# voluptuous is used by config_flow — mock it only if not installed
try:
    import voluptuous as _vol_check  # noqa: F401

    # Real voluptuous is available — also ensure its error submodule is importable
    import voluptuous.error  # noqa: F401
except ImportError:
    sys.modules["voluptuous"] = _make_mock_module("voluptuous")
    sys.modules["voluptuous.error"] = _make_mock_module("voluptuous.error")

# Now safe to import Climate Advisor modules
import pytest  # noqa: E402

from custom_components.climate_advisor.classifier import ForecastSnapshot  # noqa: E402


@pytest.fixture
def basic_forecast() -> ForecastSnapshot:
    """A typical mid-season ForecastSnapshot with stable trend."""
    return ForecastSnapshot(
        today_high=72.0,
        today_low=55.0,
        tomorrow_high=73.0,
        tomorrow_low=56.0,
        current_outdoor_temp=65.0,
        current_indoor_temp=70.0,
        current_humidity=45.0,
    )
