"""ha_stubs — idempotent HA sys.modules stub installer.

This module is the single source of truth for the homeassistant.* mock layer.
Both ``tests/conftest.py`` and the sim_harness runtime call ``install_ha_stubs()``
so the two environments stay in sync automatically.

The function is idempotent: calling it multiple times is safe (each module is
only injected if it is not already present in sys.modules).
"""

from __future__ import annotations

import enum as _enum
import os
import sys
from unittest.mock import MagicMock


def _make_mock_module(name: str) -> MagicMock:
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


# ---------------------------------------------------------------------------
# Real minimal base classes needed so HA-subclassing modules don't hit
# the metaclass conflict (MagicMock instances cannot be base classes).
# ---------------------------------------------------------------------------


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


class _MockDataUpdateCoordinator:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.DataUpdateCoordinator."""

    def __init__(self, *args, **kwargs):
        # Real DataUpdateCoordinator.__init__(self, hass, logger, *, name, ...) — first
        # positional arg is hass. Never previously captured here (pre-#474 gap): any
        # coordinator method reading self.hass after full ClimateAdvisorCoordinator(hass,
        # config) construction would hit AttributeError. Existing tests that use this
        # pattern (test_occupancy.py, test_weather_bias.py, test_learning_toggle.py)
        # happened not to exercise a method needing self.hass before this fix.
        self.hass = args[0] if args else kwargs.get("hass")
        self.data = None
        self.last_update_success = False

    async def async_request_refresh(self):
        """Stub for triggering a data refresh."""
        await self.async_config_entry_first_refresh()

    async def async_config_entry_first_refresh(self):
        """Run the first data fetch (Issue #474 — coordinator-level Tier A coverage).

        Real HA's DataUpdateCoordinator calls ``_async_update_data()`` and
        raises ConfigEntryNotReady on failure; the harness only needs the
        success path since scenarios drive a synthetic, always-ready
        environment (real weather/forecast entities are seeded before this
        runs).
        """
        self.data = await self._async_update_data()
        self.last_update_success = True


class _MockCoordinatorEntity:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator


class _MockSensorEntity:
    """Minimal stand-in for homeassistant.components.sensor.SensorEntity."""


class _MockJsonResponse:
    """Minimal stand-in for the aiohttp.web.Response a real HomeAssistantView.json() returns.

    Exposes ``status`` and ``json_data`` so tests can assert on both without
    round-tripping through a real aiohttp response body.
    """

    def __init__(self, data, status_code: int = 200) -> None:
        self.status = status_code
        self.json_data = data


class _MockHomeAssistantView:
    """Minimal stand-in for homeassistant.components.http.HomeAssistantView.

    Real subclasses in api.py set ``url``/``name``/``requires_auth`` as class
    attributes and call ``self.json(data, status_code=...)`` from their
    ``get``/``post`` handlers — this provides both without pulling in aiohttp.
    """

    requires_auth = True
    cors_allowed = False

    def json(self, result, status_code: int = 200, headers=None):
        return _MockJsonResponse(result, status_code)

    def json_message(self, message, status_code: int = 200, message_code=None, headers=None):
        return _MockJsonResponse({"message": message}, status_code)


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


class _UnitOfTemperature(_enum.StrEnum):
    FAHRENHEIT = "°F"
    CELSIUS = "°C"
    KELVIN = "K"


def install_ha_stubs() -> None:
    """Install homeassistant.* mock modules into sys.modules (idempotent).

    Safe to call multiple times — each module is only injected once.
    Also ensures the project root is on sys.path so
    ``custom_components.climate_advisor`` resolves.
    """
    # Ensure project root on path so custom_components imports work.
    # The project root is two directories above this file:
    #   tools/sim_harness/ha_stubs.py → tools/ → <project root>
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    # Inject all HA mock modules (idempotent guard)
    for mod_name in _HA_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = _make_mock_module(mod_name)

    # Attach real base classes to the already-installed mock modules.
    # These assignments are idempotent — re-assigning the same class is harmless.
    repairs = sys.modules["homeassistant.components.repairs"]
    repairs.RepairsFlow = _MockRepairsFlow
    repairs.ConfirmRepairFlow = _MockConfirmRepairFlow

    duc = sys.modules["homeassistant.helpers.update_coordinator"]
    duc.DataUpdateCoordinator = _MockDataUpdateCoordinator
    duc.CoordinatorEntity = _MockCoordinatorEntity

    sensor = sys.modules["homeassistant.components.sensor"]
    sensor.SensorEntity = _MockSensorEntity
    sensor.SensorStateClass = _SensorStateClass
    sensor.SensorDeviceClass = _SensorDeviceClass

    http = sys.modules["homeassistant.components.http"]
    http.HomeAssistantView = _MockHomeAssistantView

    const = sys.modules["homeassistant.const"]
    const.UnitOfTemperature = _UnitOfTemperature

    # voluptuous — use real package if available, mock otherwise
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous as _vol_check  # noqa: F401
            import voluptuous.error  # noqa: F401
        except ImportError:
            sys.modules["voluptuous"] = _make_mock_module("voluptuous")
            sys.modules["voluptuous.error"] = _make_mock_module("voluptuous.error")
