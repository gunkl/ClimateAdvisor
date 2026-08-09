"""Tests for Issue #613 (Block 5, subtask Q): ClimateAdvisorShadowEngineStatusSensor.

Diagnostic-category sensor exposing production/shadow nat-vent lifecycle agreement.
Deliberately not tied to coordinator.data (unlike most sensors here) — instantiated
directly against a MagicMock coordinator exposing `.shadow_engine_diagnostic`,
mirroring the pattern already established for other sensors reading a coordinator
attribute directly (see test_status_sensors.py's compliance-sensor helper).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.sensor import ClimateAdvisorShadowEngineStatusSensor  # noqa: E402


def _make_sensor(diagnostic):
    coordinator = MagicMock()
    coordinator.shadow_engine_diagnostic = diagnostic
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return ClimateAdvisorShadowEngineStatusSensor(coordinator, entry)


class TestNativeValue:
    def test_inactive_before_first_diagnostic(self) -> None:
        sensor = _make_sensor(None)
        assert sensor.native_value == "inactive"

    def test_agree(self) -> None:
        sensor = _make_sensor(
            {"production_state": "active", "shadow_state": "active", "agrees": True, "checked_at": "t"}
        )
        assert sensor.native_value == "agree"

    def test_disagree(self) -> None:
        sensor = _make_sensor(
            {"production_state": "active", "shadow_state": "idle", "agrees": False, "checked_at": "t"}
        )
        assert sensor.native_value == "disagree"


class TestExtraStateAttributes:
    def test_empty_before_first_diagnostic(self) -> None:
        sensor = _make_sensor(None)
        assert sensor.extra_state_attributes == {}

    def test_reports_both_states_and_timestamp(self) -> None:
        sensor = _make_sensor(
            {
                "production_state": "active",
                "shadow_state": "idle",
                "agrees": False,
                "checked_at": "2026-08-08T12:00:00",
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["production_state"] == "active"
        assert attrs["shadow_state"] == "idle"
        assert attrs["checked_at"] == "2026-08-08T12:00:00"


class TestEntityCategory:
    def test_is_diagnostic(self) -> None:
        from homeassistant.helpers.entity import EntityCategory

        sensor = _make_sensor(None)
        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC
