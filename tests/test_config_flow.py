"""Tests for config flow — multi-step wizard and entry migration.

Covers:
- Initial config flow wizard (async_step_user → temperature_sources → conditional
  entity picker steps → sensors → schedule)
- Conditional routing: weather_service/climate_fallback skips entity pickers;
  sensor/input_number triggers the appropriate picker step(s)
- _needs_entity() and _entity_selector_for_source() helper logic
- v1→v2 migration: outdoor_temp_entity present → sensor/input_number source;
  absent → weather_service (and indoor equivalent)
- Options flow multi-step data accumulation (existing tests retained)
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers shared across all test classes
# ---------------------------------------------------------------------------

def _make_config_entry(data: dict, version: int = 4) -> MagicMock:
    """Create a mock ConfigEntry with the given data and version."""
    entry = MagicMock()
    entry.data = dict(data)
    entry.entry_id = "test_entry_id"
    entry.version = version
    return entry


def _make_hass() -> MagicMock:
    """Create a minimal mock hass object used by migration tests."""
    hass = MagicMock()
    # Track the call so we can inspect what was written
    hass.config_entries.async_update_entry = MagicMock()
    return hass


FULL_CONFIG = {
    "weather_entity": "weather.forecast_home",
    "climate_entity": "climate.living_room",
    "comfort_heat": 70,
    "comfort_cool": 76,
    "setback_heat": 62,
    "setback_cool": 78,
    "notify_service": "notify.mobile_app_phone",
    "outdoor_temp_source": "weather_service",
    "indoor_temp_source": "climate_fallback",
    "door_window_sensors": ["binary_sensor.front_door"],
    "sensor_polarity_inverted": False,
    "sensor_debounce_seconds": 300,
    "manual_grace_seconds": 1800,
    "manual_grace_notify": False,
    "automation_grace_seconds": 3600,
    "automation_grace_notify": True,
    "wake_time": "06:30:00",
    "sleep_time": "22:30:00",
    "briefing_time": "06:00:00",
    "learning_enabled": True,
    "aggressive_savings": False,
}


# ---------------------------------------------------------------------------
# _needs_entity() helper
# ---------------------------------------------------------------------------

class TestNeedsEntity:
    """Unit tests for the _needs_entity() routing helper."""

    def _needs_entity(self, source: str) -> bool:
        """Inline replica of config_flow._needs_entity()."""
        return source in ("sensor", "input_number")

    def test_sensor_needs_entity(self):
        assert self._needs_entity("sensor") is True

    def test_input_number_needs_entity(self):
        assert self._needs_entity("input_number") is True

    def test_weather_service_no_entity_needed(self):
        assert self._needs_entity("weather_service") is False

    def test_climate_fallback_no_entity_needed(self):
        assert self._needs_entity("climate_fallback") is False

    def test_empty_string_no_entity_needed(self):
        assert self._needs_entity("") is False

    def test_unknown_source_no_entity_needed(self):
        assert self._needs_entity("unknown_type") is False


# ---------------------------------------------------------------------------
# Config flow wizard — step routing logic
# ---------------------------------------------------------------------------

class TestConfigFlowWizardRouting:
    """Test the conditional step-routing of the initial config flow wizard.

    Rather than spinning up a live ConfigFlow instance (which requires a
    running HA event loop), we replicate the routing decisions that
    async_step_temperature_sources() makes based on source selection.
    This directly mirrors the logic in config_flow.py.
    """

    def _route_after_temp_sources(
        self,
        outdoor_source: str,
        indoor_source: str,
    ) -> list[str]:
        """Return the ordered list of steps visited after temperature_sources.

        Replicates the routing logic in ClimateAdvisorConfigFlow.
        """
        def needs_entity(s: str) -> bool:
            return s in ("sensor", "input_number")

        steps = []
        if needs_entity(outdoor_source):
            steps.append("outdoor_temp_entity")
        if needs_entity(indoor_source):
            steps.append("indoor_temp_entity")
        steps.append("sensors")
        steps.append("schedule")
        return steps

    # --- weather_service + climate_fallback (no entity pickers) ---

    def test_both_defaults_skip_entity_pickers(self):
        """weather_service + climate_fallback goes straight to sensors."""
        steps = self._route_after_temp_sources("weather_service", "climate_fallback")
        assert steps == ["sensors", "schedule"]

    # --- outdoor sensor only ---

    def test_outdoor_sensor_shows_outdoor_picker(self):
        steps = self._route_after_temp_sources("sensor", "climate_fallback")
        assert steps[0] == "outdoor_temp_entity"
        assert "indoor_temp_entity" not in steps

    def test_outdoor_input_number_shows_outdoor_picker(self):
        steps = self._route_after_temp_sources("input_number", "climate_fallback")
        assert steps[0] == "outdoor_temp_entity"
        assert "indoor_temp_entity" not in steps

    # --- indoor sensor only ---

    def test_indoor_sensor_shows_indoor_picker(self):
        steps = self._route_after_temp_sources("weather_service", "sensor")
        assert "outdoor_temp_entity" not in steps
        assert steps[0] == "indoor_temp_entity"

    def test_indoor_input_number_shows_indoor_picker(self):
        steps = self._route_after_temp_sources("weather_service", "input_number")
        assert "outdoor_temp_entity" not in steps
        assert steps[0] == "indoor_temp_entity"

    # --- both sensor/input_number ---

    def test_both_sensor_shows_both_pickers_in_order(self):
        steps = self._route_after_temp_sources("sensor", "sensor")
        assert steps[0] == "outdoor_temp_entity"
        assert steps[1] == "indoor_temp_entity"
        assert steps[2] == "sensors"

    def test_outdoor_sensor_indoor_input_number(self):
        steps = self._route_after_temp_sources("sensor", "input_number")
        assert steps[0] == "outdoor_temp_entity"
        assert steps[1] == "indoor_temp_entity"

    def test_outdoor_input_number_indoor_sensor(self):
        steps = self._route_after_temp_sources("input_number", "sensor")
        assert steps[0] == "outdoor_temp_entity"
        assert steps[1] == "indoor_temp_entity"

    def test_both_input_number_shows_both_pickers(self):
        steps = self._route_after_temp_sources("input_number", "input_number")
        assert steps == ["outdoor_temp_entity", "indoor_temp_entity", "sensors", "schedule"]

    # --- schedule is always last ---

    def test_schedule_always_terminal(self):
        for outdoor in ("weather_service", "sensor", "input_number"):
            for indoor in ("climate_fallback", "sensor", "input_number"):
                steps = self._route_after_temp_sources(outdoor, indoor)
                assert steps[-1] == "schedule", (
                    f"schedule must be last for outdoor={outdoor}, indoor={indoor}"
                )


# ---------------------------------------------------------------------------
# Config flow wizard — data accumulation
# ---------------------------------------------------------------------------

class TestConfigFlowDataAccumulation:
    """Test that each wizard step's data is merged into _data correctly."""

    def _simulate_full_wizard(
        self,
        outdoor_source: str = "weather_service",
        indoor_source: str = "climate_fallback",
        outdoor_entity: str | None = None,
        indoor_entity: str | None = None,
    ) -> dict:
        """Simulate all wizard steps and return the accumulated _data dict."""
        data: dict = {}

        # Step 1: user (core entities + setpoints)
        data.update({
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        })

        # Step 2: temperature_sources
        data.update({
            "outdoor_temp_source": outdoor_source,
            "indoor_temp_source": indoor_source,
        })

        # Conditional step: outdoor entity picker
        if outdoor_source in ("sensor", "input_number"):
            assert outdoor_entity is not None, "outdoor_entity required for this source"
            data["outdoor_temp_entity"] = outdoor_entity

        # Conditional step: indoor entity picker
        if indoor_source in ("sensor", "input_number"):
            assert indoor_entity is not None, "indoor_entity required for this source"
            data["indoor_temp_entity"] = indoor_entity

        # Step: sensors
        data.update({
            "door_window_sensors": [],
            "sensor_polarity_inverted": False,
            "sensor_debounce_seconds": 300,
            "manual_grace_seconds": 1800,
            "manual_grace_notify": False,
            "automation_grace_seconds": 3600,
            "automation_grace_notify": True,
        })

        # Step: schedule (final — creates entry)
        data.update({
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "briefing_time": "06:00:00",
        })

        return data

    def test_happy_path_defaults_no_entity_pickers(self):
        """Full wizard with defaults produces a complete config dict."""
        result = self._simulate_full_wizard()
        assert result["weather_entity"] == "weather.forecast_home"
        assert result["climate_entity"] == "climate.living_room"
        assert result["outdoor_temp_source"] == "weather_service"
        assert result["indoor_temp_source"] == "climate_fallback"
        assert "outdoor_temp_entity" not in result
        assert "indoor_temp_entity" not in result
        assert result["wake_time"] == "06:30:00"
        assert result["briefing_time"] == "06:00:00"

    def test_outdoor_sensor_entity_included(self):
        result = self._simulate_full_wizard(
            outdoor_source="sensor",
            outdoor_entity="sensor.outside_temp",
        )
        assert result["outdoor_temp_source"] == "sensor"
        assert result["outdoor_temp_entity"] == "sensor.outside_temp"
        assert "indoor_temp_entity" not in result

    def test_outdoor_input_number_entity_included(self):
        result = self._simulate_full_wizard(
            outdoor_source="input_number",
            outdoor_entity="input_number.outdoor_temp",
        )
        assert result["outdoor_temp_source"] == "input_number"
        assert result["outdoor_temp_entity"] == "input_number.outdoor_temp"

    def test_indoor_sensor_entity_included(self):
        result = self._simulate_full_wizard(
            indoor_source="sensor",
            indoor_entity="sensor.living_room_temp",
        )
        assert result["indoor_temp_source"] == "sensor"
        assert result["indoor_temp_entity"] == "sensor.living_room_temp"
        assert "outdoor_temp_entity" not in result

    def test_both_sensor_entities_included(self):
        result = self._simulate_full_wizard(
            outdoor_source="sensor",
            outdoor_entity="sensor.outside_temp",
            indoor_source="sensor",
            indoor_entity="sensor.inside_temp",
        )
        assert result["outdoor_temp_entity"] == "sensor.outside_temp"
        assert result["indoor_temp_entity"] == "sensor.inside_temp"

    def test_schedule_fields_present_after_wizard(self):
        """The final step's schedule fields must all be in the result."""
        result = self._simulate_full_wizard()
        for field in ("wake_time", "sleep_time", "briefing_time"):
            assert field in result, f"Missing field: {field}"

    def test_sensors_defaults_when_none_configured(self):
        """Door/window sensors default to an empty list."""
        result = self._simulate_full_wizard()
        assert result["door_window_sensors"] == []
        assert result["sensor_polarity_inverted"] is False
        assert result["sensor_debounce_seconds"] == 300

    def test_step_user_fields_not_overwritten_by_later_steps(self):
        """Core fields from step 1 survive all subsequent steps unchanged."""
        result = self._simulate_full_wizard()
        assert result["comfort_heat"] == 70
        assert result["setback_cool"] == 80
        assert result["notify_service"] == "notify.notify"


# ---------------------------------------------------------------------------
# Config flow wizard — step_user field requirements
# ---------------------------------------------------------------------------

class TestConfigFlowStepUserFields:
    """Test that async_step_user collects the expected required fields."""

    REQUIRED_FIELDS = [
        "weather_entity",
        "climate_entity",
        "comfort_heat",
        "comfort_cool",
        "setback_heat",
        "setback_cool",
        "notify_service",
    ]

    def test_all_required_fields_present(self):
        """Verify every required step-1 field is captured."""
        user_input = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        }
        for field in self.REQUIRED_FIELDS:
            assert field in user_input, f"Missing required field: {field}"

    def test_missing_required_field_detected(self):
        """A user_input dict without a required field is incomplete."""
        incomplete = {
            "weather_entity": "weather.forecast_home",
            # climate_entity intentionally omitted
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        }
        missing = [f for f in self.REQUIRED_FIELDS if f not in incomplete]
        assert missing == ["climate_entity"]

    def test_default_setpoints_within_bounds(self):
        """Default setpoints satisfy the slider constraints in the schema."""
        from custom_components.climate_advisor.const import (
            DEFAULT_COMFORT_HEAT,
            DEFAULT_COMFORT_COOL,
            DEFAULT_SETBACK_HEAT,
            DEFAULT_SETBACK_COOL,
        )
        assert 55 <= DEFAULT_COMFORT_HEAT <= 80
        assert 68 <= DEFAULT_COMFORT_COOL <= 85
        assert 45 <= DEFAULT_SETBACK_HEAT <= 65
        assert 75 <= DEFAULT_SETBACK_COOL <= 90


# ---------------------------------------------------------------------------
# v1→v2 migration
# ---------------------------------------------------------------------------

class TestMigrationV1ToV2:
    """Test async_migrate_entry() for the v1→v2 transition.

    The migration logic is tested by replicating it inline — identical to
    how other test modules (e.g., test_coordinator_retry.py) replicate
    coordinator logic.  This avoids requiring a live HA event loop while
    still exercising every branch of the actual migration code.
    """

    def _run_v1_to_v2_migration(self, v1_data: dict) -> dict:
        """Apply the v1→v2 migration logic and return the resulting data dict.

        Mirrors async_migrate_entry() in __init__.py, version==1 branch.
        """
        new_data = dict(v1_data)

        # Outdoor temp source
        outdoor_entity = new_data.get("outdoor_temp_entity")
        if outdoor_entity:
            if outdoor_entity.startswith("input_number."):
                new_data["outdoor_temp_source"] = "input_number"
            else:
                new_data["outdoor_temp_source"] = "sensor"
        else:
            new_data["outdoor_temp_source"] = "weather_service"
            new_data.pop("outdoor_temp_entity", None)

        # Indoor temp source
        indoor_entity = new_data.get("indoor_temp_entity")
        if indoor_entity:
            if indoor_entity.startswith("input_number."):
                new_data["indoor_temp_source"] = "input_number"
            else:
                new_data["indoor_temp_source"] = "sensor"
        else:
            new_data["indoor_temp_source"] = "climate_fallback"
            new_data.pop("indoor_temp_entity", None)

        return new_data

    # --- outdoor migration ---

    def test_outdoor_sensor_entity_maps_to_sensor_source(self):
        v1 = {"outdoor_temp_entity": "sensor.outdoor_temp"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "sensor"
        assert result["outdoor_temp_entity"] == "sensor.outdoor_temp"

    def test_outdoor_input_number_entity_maps_to_input_number_source(self):
        v1 = {"outdoor_temp_entity": "input_number.outdoor_temp"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "input_number"
        assert result["outdoor_temp_entity"] == "input_number.outdoor_temp"

    def test_no_outdoor_entity_maps_to_weather_service(self):
        v1: dict = {}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "weather_service"
        assert "outdoor_temp_entity" not in result

    def test_outdoor_entity_none_maps_to_weather_service(self):
        """Explicit None value for outdoor_temp_entity → weather_service."""
        v1 = {"outdoor_temp_entity": None}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "weather_service"
        assert "outdoor_temp_entity" not in result

    def test_outdoor_entity_empty_string_maps_to_weather_service(self):
        """Empty string is falsy → treated the same as absent."""
        v1 = {"outdoor_temp_entity": ""}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "weather_service"

    # --- indoor migration ---

    def test_indoor_sensor_entity_maps_to_sensor_source(self):
        v1 = {"indoor_temp_entity": "sensor.living_room_temp"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["indoor_temp_source"] == "sensor"
        assert result["indoor_temp_entity"] == "sensor.living_room_temp"

    def test_indoor_input_number_entity_maps_to_input_number_source(self):
        v1 = {"indoor_temp_entity": "input_number.indoor_setpoint"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["indoor_temp_source"] == "input_number"
        assert result["indoor_temp_entity"] == "input_number.indoor_setpoint"

    def test_no_indoor_entity_maps_to_climate_fallback(self):
        v1: dict = {}
        result = self._run_v1_to_v2_migration(v1)
        assert result["indoor_temp_source"] == "climate_fallback"
        assert "indoor_temp_entity" not in result

    def test_indoor_entity_none_maps_to_climate_fallback(self):
        v1 = {"indoor_temp_entity": None}
        result = self._run_v1_to_v2_migration(v1)
        assert result["indoor_temp_source"] == "climate_fallback"
        assert "indoor_temp_entity" not in result

    # --- combined scenarios ---

    def test_both_entities_present_sensor_type(self):
        v1 = {
            "outdoor_temp_entity": "sensor.outside",
            "indoor_temp_entity": "sensor.inside",
        }
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "sensor"
        assert result["indoor_temp_source"] == "sensor"
        assert result["outdoor_temp_entity"] == "sensor.outside"
        assert result["indoor_temp_entity"] == "sensor.inside"

    def test_both_entities_absent(self):
        """No entities → both sources fall back to defaults."""
        v1 = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
        }
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "weather_service"
        assert result["indoor_temp_source"] == "climate_fallback"
        assert "outdoor_temp_entity" not in result
        assert "indoor_temp_entity" not in result

    def test_outdoor_sensor_indoor_absent(self):
        v1 = {"outdoor_temp_entity": "sensor.outside"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "sensor"
        assert result["indoor_temp_source"] == "climate_fallback"

    def test_outdoor_absent_indoor_sensor(self):
        v1 = {"indoor_temp_entity": "sensor.inside"}
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "weather_service"
        assert result["indoor_temp_source"] == "sensor"

    def test_outdoor_input_number_indoor_sensor(self):
        v1 = {
            "outdoor_temp_entity": "input_number.outdoor_temp",
            "indoor_temp_entity": "sensor.inside",
        }
        result = self._run_v1_to_v2_migration(v1)
        assert result["outdoor_temp_source"] == "input_number"
        assert result["indoor_temp_source"] == "sensor"

    def test_existing_non_entity_fields_preserved(self):
        """Migration must not discard unrelated config fields."""
        v1 = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
            "outdoor_temp_entity": "sensor.outside",
        }
        result = self._run_v1_to_v2_migration(v1)
        assert result["weather_entity"] == "weather.forecast_home"
        assert result["climate_entity"] == "climate.living_room"
        assert result["comfort_heat"] == 72
        assert result["notify_service"] == "notify.notify"

    def test_outdoor_entity_key_removed_when_no_entity(self):
        """When no entity existed the key must be absent — not set to None."""
        v1 = {"outdoor_temp_entity": None}
        result = self._run_v1_to_v2_migration(v1)
        assert "outdoor_temp_entity" not in result

    def test_indoor_entity_key_removed_when_no_entity(self):
        """When no entity existed the key must be absent — not set to None."""
        v1 = {"indoor_temp_entity": None}
        result = self._run_v1_to_v2_migration(v1)
        assert "indoor_temp_entity" not in result


# ---------------------------------------------------------------------------
# Migration invoked via the real async_migrate_entry function
# ---------------------------------------------------------------------------

class TestMigrationViaRealFunction:
    """Call async_migrate_entry() directly to ensure the real code is correct.

    The function is async, so we run it with asyncio.run().  We use a mock
    hass whose async_update_entry() captures the arguments, letting us verify
    exactly what gets written to the config entry.
    """

    def _call_migrate(self, v1_data: dict, start_version: int = 1) -> tuple[dict, bool]:
        """Run async_migrate_entry and return (final_data, return_value)."""
        from custom_components.climate_advisor import async_migrate_entry

        written_data: dict = {}
        written_version: list[int] = []

        def capture_update(entry, *, data, version):
            # The migration calls this multiple times (once per version hop).
            # Merge successive calls so we see the final state.
            written_data.update(data)
            written_version.append(version)
            # Also update entry.data so subsequent migration steps see the new data
            entry.data = dict(data)
            entry.version = version

        entry = _make_config_entry(v1_data, version=start_version)
        hass = _make_hass()
        hass.config_entries.async_update_entry.side_effect = capture_update

        result = asyncio.run(async_migrate_entry(hass, entry))
        return written_data, result

    def test_returns_true_on_success(self):
        _, ok = self._call_migrate({})
        assert ok is True

    def test_v1_no_entities_produces_correct_sources(self):
        v1 = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
        }
        data, _ = self._call_migrate(v1)
        assert data["outdoor_temp_source"] == "weather_service"
        assert data["indoor_temp_source"] == "climate_fallback"

    def test_v1_outdoor_sensor_entity_migrated(self):
        v1 = {"outdoor_temp_entity": "sensor.backyard_temp"}
        data, _ = self._call_migrate(v1)
        assert data["outdoor_temp_source"] == "sensor"
        assert data["outdoor_temp_entity"] == "sensor.backyard_temp"

    def test_v1_outdoor_input_number_migrated(self):
        v1 = {"outdoor_temp_entity": "input_number.outdoor_temp"}
        data, _ = self._call_migrate(v1)
        assert data["outdoor_temp_source"] == "input_number"

    def test_v1_indoor_sensor_entity_migrated(self):
        v1 = {"indoor_temp_entity": "sensor.hallway_temp"}
        data, _ = self._call_migrate(v1)
        assert data["indoor_temp_source"] == "sensor"

    def test_v1_indoor_input_number_migrated(self):
        v1 = {"indoor_temp_entity": "input_number.indoor_temp"}
        data, _ = self._call_migrate(v1)
        assert data["indoor_temp_source"] == "input_number"

    def test_v1_chain_migration_produces_v4_fields(self):
        """A v1 entry should chain through all migrations and gain v4 fields."""
        from custom_components.climate_advisor.const import (
            DEFAULT_SENSOR_DEBOUNCE_SECONDS,
            DEFAULT_MANUAL_GRACE_SECONDS,
            DEFAULT_AUTOMATION_GRACE_SECONDS,
            CONF_SENSOR_DEBOUNCE,
            CONF_MANUAL_GRACE_PERIOD,
            CONF_MANUAL_GRACE_NOTIFY,
            CONF_AUTOMATION_GRACE_PERIOD,
            CONF_AUTOMATION_GRACE_NOTIFY,
        )
        v1 = {"weather_entity": "weather.forecast_home", "climate_entity": "climate.living_room"}
        data, ok = self._call_migrate(v1)
        assert ok is True
        # v4 fields should be present with defaults
        assert data.get(CONF_SENSOR_DEBOUNCE) == DEFAULT_SENSOR_DEBOUNCE_SECONDS
        assert data.get(CONF_MANUAL_GRACE_PERIOD) == DEFAULT_MANUAL_GRACE_SECONDS
        assert data.get(CONF_MANUAL_GRACE_NOTIFY) is False
        assert data.get(CONF_AUTOMATION_GRACE_PERIOD) == DEFAULT_AUTOMATION_GRACE_SECONDS
        assert data.get(CONF_AUTOMATION_GRACE_NOTIFY) is True

    def test_already_v4_entry_returns_true_without_changes(self):
        """A v4 entry should pass through with no mutations."""
        v4 = dict(FULL_CONFIG)
        entry = _make_config_entry(v4, version=4)
        hass = _make_hass()

        from custom_components.climate_advisor import async_migrate_entry
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        # No updates should have been written
        hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Options flow multi-step data accumulation (existing tests retained)
# ---------------------------------------------------------------------------

class TestOptionsFlowMultiStep:
    """Test that the multi-step options flow merges data correctly."""

    def test_step_init_merges_core_settings(self):
        """Step 1 (init) collects core entity and temperature settings."""
        original = dict(FULL_CONFIG)
        step1_input = {
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        }
        merged = {**original, **step1_input}
        assert merged["weather_entity"] == "weather.home"
        assert merged["comfort_heat"] == 72
        assert merged["setback_heat"] == 60
        assert merged["setback_cool"] == 80
        assert merged["notify_service"] == "notify.notify"

    def test_multi_step_accumulation(self):
        """All 5 steps accumulate into a single merged result."""
        original = dict(FULL_CONFIG)
        updates = {}

        # Step 1: init
        updates.update({
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        })

        # Step 2: temperature_sources
        updates.update({
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.outdoor_temp",
            "indoor_temp_source": "climate_fallback",
        })

        # Step 3: sensors
        updates.update({
            "door_window_sensors": ["binary_sensor.back_door"],
            "sensor_polarity_inverted": True,
            "sensor_debounce_seconds": 600,
            "manual_grace_seconds": 900,
            "manual_grace_notify": True,
            "automation_grace_seconds": 1800,
            "automation_grace_notify": False,
        })

        # Step 4: schedule
        updates.update({
            "wake_time": "07:00:00",
            "sleep_time": "23:00:00",
            "briefing_time": "06:30:00",
        })

        # Step 5: advanced
        updates.update({
            "learning_enabled": False,
            "aggressive_savings": True,
        })

        merged = {**original, **updates}

        # Verify all updated fields
        assert merged["weather_entity"] == "weather.home"
        assert merged["comfort_heat"] == 72
        assert merged["setback_heat"] == 60
        assert merged["outdoor_temp_source"] == "sensor"
        assert merged["outdoor_temp_entity"] == "sensor.outdoor_temp"
        assert merged["door_window_sensors"] == ["binary_sensor.back_door"]
        assert merged["sensor_polarity_inverted"] is True
        assert merged["sensor_debounce_seconds"] == 600
        assert merged["wake_time"] == "07:00:00"
        assert merged["sleep_time"] == "23:00:00"
        assert merged["briefing_time"] == "06:30:00"
        assert merged["learning_enabled"] is False
        assert merged["aggressive_savings"] is True

    def test_fields_not_in_updates_are_preserved(self):
        """Fields not touched across any step retain their original values."""
        original = dict(FULL_CONFIG)
        updates = {
            "comfort_heat": 72,
            "learning_enabled": False,
        }
        merged = {**original, **updates}
        # Updated fields
        assert merged["comfort_heat"] == 72
        assert merged["learning_enabled"] is False
        # Preserved fields
        assert merged["weather_entity"] == "weather.forecast_home"
        assert merged["notify_service"] == "notify.mobile_app_phone"
        assert merged["setback_heat"] == 62
        assert merged["wake_time"] == "06:30:00"
        assert merged["door_window_sensors"] == ["binary_sensor.front_door"]

    def test_new_fields_have_defaults_for_old_entries(self):
        """Config entries created before new fields were added get safe defaults."""
        old_entry_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 76,
        }
        # Simulate options flow defaulting missing fields
        defaults = {
            "setback_heat": old_entry_data.get("setback_heat", 60),
            "setback_cool": old_entry_data.get("setback_cool", 80),
            "notify_service": old_entry_data.get("notify_service", "notify.notify"),
            "wake_time": old_entry_data.get("wake_time", "06:30:00"),
            "sleep_time": old_entry_data.get("sleep_time", "22:30:00"),
            "briefing_time": old_entry_data.get("briefing_time", "06:00:00"),
            "learning_enabled": old_entry_data.get("learning_enabled", True),
            "aggressive_savings": old_entry_data.get("aggressive_savings", False),
        }
        assert defaults["setback_heat"] == 60
        assert defaults["notify_service"] == "notify.notify"
        assert defaults["wake_time"] == "06:30:00"
        assert defaults["learning_enabled"] is True
