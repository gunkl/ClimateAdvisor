"""Shared pytest fixtures for Climate Advisor tests."""
from __future__ import annotations

import sys
import os
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

# voluptuous is used by config_flow — mock it if not installed
if "voluptuous" not in sys.modules:
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
