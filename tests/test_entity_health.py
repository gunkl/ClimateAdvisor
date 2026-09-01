"""Tests for the generic entity-health sweep (Issue #805).

Pure-function tests for ``run_entity_health_sweep()`` — no coordinator or HA
object needed beyond a minimal ``hass`` stub, same testability approach as
``invariant_watchdog.py``'s pure check functions.

Coordinator-level wiring (debounce, notification, status-card surfacing) is
covered separately once that phase lands (Phase 3/4).

See: GitHub Issue #805
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.entity_health import (
    ENTITY_HEALTH_REGISTRY,
    EntityHealthIssue,
    run_entity_health_sweep,
)


def _make_hass(states: dict[str, str | None] | None = None, services: set[tuple[str, str]] | None = None):
    """A minimal hass stub: states maps entity_id -> state string, or None for "removed".
    services is a set of (domain, service) pairs that "exist"."""
    hass = MagicMock()
    states = states or {}
    services = services or set()

    def _states_get(entity_id):
        if entity_id not in states or states[entity_id] is None:
            return None
        s = MagicMock()
        s.state = states[entity_id]
        return s

    hass.states.get.side_effect = _states_get
    hass.services.has_service.side_effect = lambda domain, service: (domain, service) in services
    return hass


BASE_CONFIG = {
    "climate_entity": "climate.thermostat",
    "weather_entity": "weather.home",
    "notify_service": "notify.mobile_app",
}


class TestBlankIsNeverFlagged:
    """A blank/unset config value must never produce an issue — this is how
    'optional by design' is enforced structurally."""

    def test_empty_door_window_sensors_list_is_fine(self):
        config = {**BASE_CONFIG, "door_window_sensors": []}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert issues == []

    def test_unset_optional_toggles_are_fine(self):
        config = {**BASE_CONFIG}  # no toggle/fan_remote keys at all
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert issues == []

    def test_unset_fan_entity_with_fan_mode_disabled_is_fine(self):
        config = {**BASE_CONFIG, "fan_mode": "disabled"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert issues == []


class TestCriticalEntities:
    def test_missing_climate_entity_flagged_critical(self):
        config = {**BASE_CONFIG}
        hass = _make_hass(states={"weather.home": "sunny"}, services={("notify", "mobile_app")})
        # climate.thermostat not in states dict -> states.get returns None -> missing
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "climate_entity"]
        assert len(matches) == 1
        assert matches[0].status == "missing"
        assert matches[0].criticality == "critical"
        assert matches[0].entity_id == "climate.thermostat"

    def test_unavailable_climate_entity_flagged(self):
        config = {**BASE_CONFIG}
        hass = _make_hass(
            states={"climate.thermostat": "unavailable", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "climate_entity"]
        assert len(matches) == 1
        assert matches[0].status == "unavailable"

    def test_missing_weather_entity_flagged_critical(self):
        config = {**BASE_CONFIG}
        hass = _make_hass(states={"climate.thermostat": "cool"}, services={("notify", "mobile_app")})
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "weather_entity"]
        assert len(matches) == 1
        assert matches[0].criticality == "critical"

    def test_missing_notify_service_flagged_critical(self):
        config = {**BASE_CONFIG}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services=set(),  # notify.mobile_app does not exist
        )
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "notify_service"]
        assert len(matches) == 1
        assert matches[0].criticality == "critical"
        assert matches[0].status == "missing"

    def test_notify_service_bare_name_without_domain_prefix(self):
        """notify_service may be stored without the 'notify.' prefix."""
        config = {**BASE_CONFIG, "notify_service": "mobile_app"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert [i for i in issues if i.config_key == "notify_service"] == []


class TestConditionalGating:
    def test_outdoor_temp_entity_skipped_when_source_is_weather_service(self):
        config = {
            **BASE_CONFIG,
            "outdoor_temp_source": "weather_service",
            "outdoor_temp_entity": "sensor.outdoor",  # configured but source doesn't use it
        }
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},  # sensor.outdoor absent
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert [i for i in issues if i.config_key == "outdoor_temp_entity"] == []

    def test_outdoor_temp_entity_checked_when_source_is_sensor(self):
        config = {
            **BASE_CONFIG,
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.outdoor",
        }
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "outdoor_temp_entity"]
        assert len(matches) == 1
        assert matches[0].criticality == "degraded"

    def test_indoor_temp_entity_checked_when_source_is_input_number(self):
        config = {
            **BASE_CONFIG,
            "indoor_temp_source": "input_number",
            "indoor_temp_entity": "input_number.indoor",
        }
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert len([i for i in issues if i.config_key == "indoor_temp_entity"]) == 1

    def test_fan_entity_skipped_when_fan_mode_is_hvac_fan(self):
        config = {**BASE_CONFIG, "fan_mode": "hvac_fan", "fan_entity": "switch.whf"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},  # switch.whf absent
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert [i for i in issues if i.config_key == "fan_entity"] == []

    def test_fan_entity_checked_when_fan_mode_is_whole_house_fan(self):
        config = {**BASE_CONFIG, "fan_mode": "whole_house_fan", "fan_entity": "switch.whf"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "fan_entity"]
        assert len(matches) == 1
        assert matches[0].criticality == "degraded"

    def test_fan_entity_checked_when_fan_mode_is_both(self):
        config = {**BASE_CONFIG, "fan_mode": "both", "fan_entity": "switch.whf"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert len([i for i in issues if i.config_key == "fan_entity"]) == 1


class TestOptionalTierEntities:
    def test_missing_fan_remote_entity_flagged_optional(self):
        config = {**BASE_CONFIG, "fan_remote_entity": "sensor.rf_remote"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        matches = [i for i in issues if i.config_key == "fan_remote_entity"]
        assert len(matches) == 1
        assert matches[0].criticality == "optional"

    def test_missing_vacation_toggle_flagged_optional(self):
        config = {**BASE_CONFIG, "vacation_toggle_entity": "input_boolean.vacation"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert [i for i in issues if i.config_key == "vacation_toggle_entity"][0].criticality == "optional"

    def test_missing_home_toggle_flagged_degraded(self):
        """home_toggle_entity is tiered degraded (broader blast radius than vacation/guest)."""
        config = {**BASE_CONFIG, "home_toggle_entity": "input_boolean.home"}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        assert [i for i in issues if i.config_key == "home_toggle_entity"][0].criticality == "degraded"


class TestDoorWindowSensorsList:
    def test_empty_list_produces_no_issues(self):
        config = {**BASE_CONFIG, "door_window_sensors": []}
        hass = _make_hass(
            states={"climate.thermostat": "cool", "weather.home": "sunny"},
            services={("notify", "mobile_app")},
        )
        assert run_entity_health_sweep(hass, config) == []

    def test_one_missing_member_flagged_individually(self):
        config = {**BASE_CONFIG, "door_window_sensors": ["binary_sensor.front_door", "binary_sensor.back_door"]}
        hass = _make_hass(
            states={
                "climate.thermostat": "cool",
                "weather.home": "sunny",
                "binary_sensor.front_door": "off",
                # binary_sensor.back_door absent -> missing
            },
            services={("notify", "mobile_app")},
        )
        issues = run_entity_health_sweep(hass, config)
        door_issues = [i for i in issues if i.config_key == "door_window_sensors"]
        assert len(door_issues) == 1
        assert door_issues[0].entity_id == "binary_sensor.back_door"
        assert door_issues[0].criticality == "degraded"

    def test_all_present_produces_no_door_issues(self):
        config = {**BASE_CONFIG, "door_window_sensors": ["binary_sensor.front_door"]}
        hass = _make_hass(
            states={
                "climate.thermostat": "cool",
                "weather.home": "sunny",
                "binary_sensor.front_door": "off",
            },
            services={("notify", "mobile_app")},
        )
        assert [i for i in run_entity_health_sweep(hass, config) if i.config_key == "door_window_sensors"] == []

    def test_unavailable_member_flagged(self):
        config = {**BASE_CONFIG, "door_window_sensors": ["binary_sensor.front_door"]}
        hass = _make_hass(
            states={
                "climate.thermostat": "cool",
                "weather.home": "sunny",
                "binary_sensor.front_door": "unavailable",
            },
            services={("notify", "mobile_app")},
        )
        issues = [i for i in run_entity_health_sweep(hass, config) if i.config_key == "door_window_sensors"]
        assert len(issues) == 1
        assert issues[0].status == "unavailable"


class TestRegistryShape:
    def test_registry_covers_all_11_documented_keys(self):
        """Guard against silently dropping a monitored entity during refactors."""
        expected_keys = {
            "climate_entity",
            "weather_entity",
            "notify_service",
            "outdoor_temp_entity",
            "indoor_temp_entity",
            "fan_entity",
            "fan_state_entity",
            "fan_remote_entity",
            "home_toggle_entity",
            "vacation_toggle_entity",
            "guest_toggle_entity",
        }
        assert set(ENTITY_HEALTH_REGISTRY.keys()) == expected_keys

    def test_every_registry_entry_has_required_fields(self):
        for config_key, meta in ENTITY_HEALTH_REGISTRY.items():
            assert "friendly_name" in meta, config_key
            assert meta["criticality"] in ("critical", "degraded", "optional"), config_key
            assert callable(meta["relevant"]), config_key
            assert callable(meta["checker"]), config_key


class TestEntityHealthIssueShape:
    def test_dataclass_has_exact_expected_fields(self):
        """This shape is what the coordinator will serialize into coordinator.data
        (mirroring the existing invariant_violations field) — a field rename here
        would silently break that contract."""
        issue = EntityHealthIssue(
            config_key="climate_entity",
            entity_id="climate.thermostat",
            friendly_name="Thermostat",
            criticality="critical",
            status="missing",
        )
        assert issue.config_key == "climate_entity"
        assert issue.entity_id == "climate.thermostat"
        assert issue.friendly_name == "Thermostat"
        assert issue.criticality == "critical"
        assert issue.status == "missing"
