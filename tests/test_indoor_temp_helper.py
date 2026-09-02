"""Tests for Issue #796 Step 10 — indoor temp read/validate dedup.

``AutomationEngine._get_indoor_temp_f()`` (automation.py) and
``ClimateAdvisorCoordinator._get_indoor_temp()`` (coordinator.py) previously
re-implemented the same source-resolution logic independently and had drifted:
the coordinator rejected physically implausible readings via a plausible-range
guard ([40, 110] °F); automation.py had no such guard on either source path, and
its climate_fallback path had no exception handling around the numeric
conversion (a non-numeric ``current_temperature`` would raise uncaught).

Both now delegate to the shared ``indoor_temp.resolve_indoor_temp_f()`` helper.
These tests exercise the plausibility guard and non-numeric handling through
BOTH call paths (the real bound AutomationEngine method and the real bound
coordinator method) to confirm they now behave identically, plus the shared
helper directly for the source-type matrix (sensor/input_number vs
climate_fallback).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_SENSOR,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import (  # noqa: E402
    MAX_PLAUSIBLE_INDOOR_F,
    MIN_PLAUSIBLE_INDOOR_F,
    resolve_indoor_temp_f,
)


def _make_state(state_value, attributes: dict | None = None) -> MagicMock:
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _make_engine(*, config_overrides: dict | None = None, states_get_return=None) -> AutomationEngine:
    """Build a minimal AutomationEngine stub with the real _get_indoor_temp_f bound."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=states_get_return)

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = "climate.thermostat"
    ae.config = {
        "climate_entity": "climate.thermostat",
        "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        ae.config.update(config_overrides)
    ae._get_indoor_temp_f = types.MethodType(AutomationEngine._get_indoor_temp_f, ae)
    return ae


def _make_coord(*, config_overrides: dict | None = None, states_get_return=None) -> ClimateAdvisorCoordinator:
    """Build a minimal coordinator stub with the real _get_indoor_temp bound."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=states_get_return)

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": "climate.thermostat",
        "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        coord.config.update(config_overrides)
    coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
    return coord


# ---------------------------------------------------------------------------
# Plausibility guard applies identically through both call paths
# ---------------------------------------------------------------------------


class TestPlausibilityGuardBothPaths:
    """The [40, 110] °F guard must reject the same values via either class."""

    def test_engine_rejects_extreme_low_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 25})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_low_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 25})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_rejects_extreme_high_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 120})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_high_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 120})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_accepts_normal_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 72})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() == 72.0

    def test_coord_accepts_normal_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 72})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() == 72.0

    def test_engine_rejects_extreme_low_sensor_source(self):
        state = _make_state("25")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_low_sensor_source(self):
        state = _make_state("25")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() is None

    def test_engine_accepts_normal_sensor_source(self):
        state = _make_state("72")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() == 72.0

    def test_coord_accepts_normal_sensor_source(self):
        state = _make_state("72")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() == 72.0


# ---------------------------------------------------------------------------
# Non-numeric handling — previously a real behavioral gap in automation.py
# ---------------------------------------------------------------------------


class TestNonNumericHandledBothPaths:
    """A non-numeric current_temperature must be treated as unavailable, not raise.

    Before this fix, AutomationEngine._get_indoor_temp_f()'s climate_fallback
    path had no try/except around float(temp) and would raise ValueError
    uncaught. The shared helper now catches it on both call paths.
    """

    def test_engine_climate_fallback_non_numeric_returns_none_not_raises(self):
        state = _make_state("heat", {"current_temperature": "unavailable"})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_climate_fallback_non_numeric_returns_none_not_raises(self):
        state = _make_state("heat", {"current_temperature": "unavailable"})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_sensor_source_non_numeric_returns_none(self):
        state = _make_state("unavailable")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() is None

    def test_coord_sensor_source_non_numeric_returns_none(self):
        state = _make_state("unavailable")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() is None


# ---------------------------------------------------------------------------
# Shared helper — direct source-type matrix
# ---------------------------------------------------------------------------


class TestResolveIndoorTempFDirect:
    """Direct tests of the shared helper across both source types."""

    def test_sensor_source_celsius_conversion(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("20")
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="celsius",
            indoor_temp_entity="sensor.indoor_temp",
            climate_entity="climate.thermostat",
        )
        assert result is not None
        assert abs(result - 68.0) < 0.01

    def test_climate_fallback_celsius_conversion(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {"current_temperature": 22})
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="celsius",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is not None
        assert abs(result - 71.6) < 0.01

    def test_sensor_source_no_entity_configured_returns_none(self):
        hass = MagicMock()
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_sensor_source_state_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="fahrenheit",
            indoor_temp_entity="sensor.indoor_temp",
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_climate_fallback_state_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_climate_fallback_current_temperature_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {})
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_boundary_values_are_inclusive(self):
        """MIN/MAX_PLAUSIBLE_INDOOR_F themselves are accepted (inclusive bounds)."""
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {"current_temperature": MIN_PLAUSIBLE_INDOOR_F})
        assert (
            resolve_indoor_temp_f(
                hass=hass,
                source=TEMP_SOURCE_CLIMATE_FALLBACK,
                unit="fahrenheit",
                indoor_temp_entity=None,
                climate_entity="climate.thermostat",
            )
            == MIN_PLAUSIBLE_INDOOR_F
        )

        hass.states.get.return_value = _make_state("heat", {"current_temperature": MAX_PLAUSIBLE_INDOOR_F})
        assert (
            resolve_indoor_temp_f(
                hass=hass,
                source=TEMP_SOURCE_CLIMATE_FALLBACK,
                unit="fahrenheit",
                indoor_temp_entity=None,
                climate_entity="climate.thermostat",
            )
            == MAX_PLAUSIBLE_INDOOR_F
        )
