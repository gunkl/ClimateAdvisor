"""Tests for thermostat capability detection (Issue #249, Phase 1).

The program-selection logic (later phases) chooses how to arm the comfort band based on what the
configured thermostat advertises: single-mode (``cool``/``heat``) vs a ``heat_cool`` dual-setpoint
band. This phase only detects capability; it changes no automation behavior.

`parse_thermostat_capabilities` is a pure function and is tested exhaustively here. The thin engine
method `_get_thermostat_capabilities` is tested via a minimal stub (no full __init__) to confirm it
reads entity attributes and degrades gracefully when the entity is missing/unavailable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.automation import (
    AutomationEngine,
    ThermostatCapabilities,
    parse_thermostat_capabilities,
)
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

# ---------------------------------------------------------------------------
# parse_thermostat_capabilities — pure function
# ---------------------------------------------------------------------------


class TestParseCapabilities:
    def test_full_featured_heat_cool_thermostat(self):
        """heat_cool in modes + TARGET_TEMPERATURE_RANGE bit → dual-setpoint capable."""
        caps = parse_thermostat_capabilities(
            ["off", "heat", "cool", "heat_cool"],
            CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,  # range + single-target bits
        )
        assert caps.supports_heat is True
        assert caps.supports_cool is True
        assert caps.supports_heat_cool is True
        assert caps.supports_dual_setpoint is True
        assert caps.modes == ("off", "heat", "cool", "heat_cool")

    def test_single_mode_thermostat_cool_only(self):
        """A cool-only thermostat (no band) → single-mode cooling, no dual setpoint."""
        caps = parse_thermostat_capabilities(["off", "cool"], 1)
        assert caps.supports_cool is True
        assert caps.supports_heat is False
        assert caps.supports_heat_cool is False
        assert caps.supports_dual_setpoint is False

    def test_heat_only_thermostat(self):
        caps = parse_thermostat_capabilities(["off", "heat"], 1)
        assert caps.supports_heat is True
        assert caps.supports_cool is False
        assert caps.supports_heat_cool is False

    def test_auto_counts_as_band_mode(self):
        """Some thermostats expose the band as 'auto' rather than 'heat_cool'."""
        caps = parse_thermostat_capabilities(["off", "auto"], CLIMATE_FEATURE_TARGET_TEMP_RANGE)
        assert caps.supports_heat_cool is True
        assert caps.supports_dual_setpoint is True

    def test_band_mode_without_range_feature_is_not_dual_setpoint(self):
        """heat_cool in modes but no TARGET_TEMPERATURE_RANGE bit → cannot set dual setpoints.

        HA only accepts target_temp_low/high when the feature bit is present, so we must NOT
        attempt a dual-setpoint program on such a thermostat.
        """
        caps = parse_thermostat_capabilities(["off", "heat", "cool", "heat_cool"], 1)  # single-target only
        assert caps.supports_heat_cool is True
        assert caps.supports_dual_setpoint is False

    def test_unknown_thermostat_none_inputs(self):
        """Missing modes/features → all-False; caller falls back to current behavior."""
        caps = parse_thermostat_capabilities(None, None)
        assert caps == ThermostatCapabilities(
            modes=(),
            supports_heat=False,
            supports_cool=False,
            supports_heat_cool=False,
            supports_dual_setpoint=False,
            raw_supported_features=0,
        )

    def test_malformed_inputs_degrade_safely(self):
        """Non-list modes and non-int features must not raise."""
        caps = parse_thermostat_capabilities("heat,cool", "lots")
        assert caps.modes == ()
        assert caps.raw_supported_features == 0
        assert caps.supports_cool is False

    def test_tuple_modes_accepted(self):
        caps = parse_thermostat_capabilities(("off", "cool"), CLIMATE_FEATURE_TARGET_TEMP_RANGE)
        assert caps.supports_cool is True

    def test_capabilities_are_frozen(self):
        caps = parse_thermostat_capabilities(["cool"], 1)
        try:
            caps.supports_cool = False  # type: ignore[misc]
        except Exception as exc:  # frozen dataclass raises FrozenInstanceError
            assert "frozen" in type(exc).__name__.lower() or "attribute" in str(exc).lower()
        else:
            raise AssertionError("ThermostatCapabilities should be immutable")


# ---------------------------------------------------------------------------
# AutomationEngine._get_thermostat_capabilities — thin state reader
# ---------------------------------------------------------------------------


def _engine_with_state(state) -> AutomationEngine:
    """Build a minimal engine (no __init__) wired only for capability reads."""
    eng = object.__new__(AutomationEngine)
    eng.hass = MagicMock()
    eng.climate_entity = "climate.test"
    eng.hass.states.get = MagicMock(return_value=state)
    return eng


class TestEngineCapabilityReader:
    def test_reads_attributes_from_state(self):
        state = MagicMock()
        state.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        }
        caps = _engine_with_state(state)._get_thermostat_capabilities()
        assert caps.supports_dual_setpoint is True
        assert caps.supports_cool is True

    def test_missing_entity_returns_all_false(self):
        caps = _engine_with_state(None)._get_thermostat_capabilities()
        assert caps.supports_cool is False
        assert caps.supports_heat is False
        assert caps.modes == ()

    def test_state_without_attributes_degrades(self):
        state = MagicMock()
        state.attributes = None  # unavailable / odd state
        caps = _engine_with_state(state)._get_thermostat_capabilities()
        assert caps.modes == ()
        assert caps.raw_supported_features == 0
