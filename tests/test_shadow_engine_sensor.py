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

    def test_reports_nat_vent_fsm_state_when_present(self) -> None:
        """Issue #633: the independent nat-vent FSM's own tracked state."""
        sensor = _make_sensor(
            {
                "production_state": "active_full_gate",
                "shadow_state": "active_full_gate",
                "nat_vent_fsm_state": "inactive",
                "agrees": False,
                "checked_at": "2026-08-08T12:00:00",
            }
        )
        assert sensor.extra_state_attributes["nat_vent_fsm_state"] == "inactive"

    def test_nat_vent_fsm_state_absent_before_first_fsm_evaluation(self) -> None:
        """A diagnostic recomputed from a mirrored call other than
        check_natural_vent_conditions (v1 scope) never populated the FSM key."""
        sensor = _make_sensor(
            {"production_state": "active", "shadow_state": "idle", "agrees": False, "checked_at": "t"}
        )
        assert sensor.extra_state_attributes["nat_vent_fsm_state"] is None

    def test_reports_door_window_fields_when_present(self) -> None:
        """Issue #660: door/window's own production/shadow/FSM state fields were
        already computed in coordinator.shadow_engine_diagnostic but never exposed
        on this sensor -- an observability gap fixed alongside the door/window FSM
        authority extension."""
        sensor = _make_sensor(
            {
                "production_state": "active",
                "shadow_state": "active",
                "agrees": True,
                "checked_at": "t",
                "door_window_production_state": "paused_active",
                "door_window_shadow_state": "paused_active",
                "door_window_fsm_state": "paused_active",
                "door_window_mirror_agrees": True,
                "door_window_fsm_agrees": True,
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["door_window_production_state"] == "paused_active"
        assert attrs["door_window_shadow_state"] == "paused_active"
        assert attrs["door_window_fsm_state"] == "paused_active"
        assert attrs["door_window_mirror_agrees"] is True
        assert attrs["door_window_fsm_agrees"] is True

    def test_door_window_fields_absent_before_first_evaluation(self) -> None:
        sensor = _make_sensor(
            {"production_state": "active", "shadow_state": "idle", "agrees": False, "checked_at": "t"}
        )
        attrs = sensor.extra_state_attributes
        assert attrs["door_window_production_state"] is None
        assert attrs["door_window_fsm_agrees"] is None

    def test_reports_override_grace_fields_when_present(self) -> None:
        """Issue #661: override/grace's own production/shadow/FSM state fields were
        already computed in coordinator.shadow_engine_diagnostic but never exposed
        on this sensor -- the same observability gap #660 fixed for door/window,
        closed here for override/grace."""
        sensor = _make_sensor(
            {
                "production_state": "active",
                "shadow_state": "active",
                "agrees": True,
                "checked_at": "t",
                "override_grace_production_state": "idle/active_protecting_override",
                "override_grace_shadow_state": "idle/active_protecting_override",
                "override_grace_fsm_state": "idle/active_protecting_override",
                "override_grace_mirror_agrees": True,
                "override_grace_fsm_agrees": True,
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["override_grace_production_state"] == "idle/active_protecting_override"
        assert attrs["override_grace_shadow_state"] == "idle/active_protecting_override"
        assert attrs["override_grace_fsm_state"] == "idle/active_protecting_override"
        assert attrs["override_grace_mirror_agrees"] is True
        assert attrs["override_grace_fsm_agrees"] is True

    def test_override_grace_fields_absent_before_first_evaluation(self) -> None:
        sensor = _make_sensor(
            {"production_state": "active", "shadow_state": "idle", "agrees": False, "checked_at": "t"}
        )
        attrs = sensor.extra_state_attributes
        assert attrs["override_grace_production_state"] is None
        assert attrs["override_grace_fsm_agrees"] is None


class TestEntityCategory:
    def test_is_diagnostic(self) -> None:
        from homeassistant.helpers.entity import EntityCategory

        sensor = _make_sensor(None)
        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC
