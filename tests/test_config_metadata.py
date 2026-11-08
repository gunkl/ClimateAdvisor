"""Tests for CONFIG_METADATA in const.py."""

from __future__ import annotations

from custom_components.climate_advisor.const import CONFIG_METADATA

# All config keys that appear across config_flow.py steps.
EXPECTED_KEYS = {
    "weather_entity",
    "climate_entity",
    "comfort_heat",
    "comfort_cool",
    "setback_heat",
    "setback_cool",
    "vacation_heat",  # Issue #1347
    "vacation_cool",  # Issue #1347
    "notify_service",
    "briefing_notifications_enabled",
    "push_briefing",
    "push_door_window_pause",
    "push_occupancy_home",
    "push_entity_health",
    "email_briefing",
    "email_door_window_pause",
    "email_grace_expired",
    "email_grace_repause",
    "email_occupancy_home",
    "email_entity_health",
    "outdoor_temp_source",
    "indoor_temp_source",
    "sleep_indoor_temp_entity",
    "indoor_humidity_entity",  # Issue #1414
    "sleep_indoor_humidity_entity",
    "outdoor_humidity_entity",
    "thermostat_companion_sensor",  # Issue #1230 R1b
    "energy_rate",  # #1230 R5c (DP-10)
    "heat_power_kw",
    "cool_power_kw",
    "door_window_sensors",
    "sensor_polarity_inverted",
    "sensor_debounce_seconds",
    "manual_grace_seconds",
    "manual_grace_notify",
    "automation_grace_seconds",
    "automation_grace_notify",
    "fan_mode",
    "hvac_fan_restrict_mode",
    "fan_entity",
    "fan_state_entity",
    "fan_state_feedback",
    "fan_remote_entity",
    "fan_min_runtime_per_hour",
    "nat_vent_soft_start_enabled",
    "wake_time",
    "sleep_time",
    "sleep_heat",
    "sleep_cool",
    "briefing_time",
    "learning_enabled",
    "adaptive_preheat_enabled",
    "adaptive_setback_enabled",
    "weather_bias_enabled",
    "aggressive_savings",
    "home_toggle_entity",
    "home_toggle_invert",
    "vacation_toggle_entity",
    "vacation_toggle_invert",
    "guest_toggle_entity",
    "guest_toggle_invert",
    "temp_unit",
    "welcome_home_debounce_seconds",
    "min_preheat_minutes",
    "max_preheat_minutes",
    "default_preheat_minutes",
    "default_tou_lead_minutes",
    "preheat_safety_margin",
    "max_setback_depth_f",
    "comfort_mode_switch_min_interval_s",
    "comfort_deadband_hot_f",
    "comfort_deadband_warm_f",
    "comfort_deadband_mild_f",
    "comfort_deadband_cool_f",
    "comfort_deadband_cold_f",
    "comfort_family_recency_window_min",
    "ai_enabled",
    "ai_api_key",
    "ai_model",
    "ai_reasoning_effort",
    "ai_max_tokens",
    "ai_temperature",
    "ai_monthly_budget",
    "ai_auto_requests_per_day",
    "ai_manual_requests_per_day",
    "ai_investigator_enabled",
    "ai_investigator_requests_per_day",
    "override_confirm_seconds",
    "threshold_hot",
    "threshold_warm",
    "threshold_mild",
    "threshold_cool",
    "primary_engine",
    "shadow_engine",
}

VALID_CATEGORIES = {
    "core",
    "setpoints",
    "sensors",
    "fan",
    "schedule",
    "advanced",
    "occupancy",
    "notifications",
    "ai_settings",
}


class TestConfigMetadataHasAllKeys:
    """Verify CONFIG_METADATA contains entries for all expected config keys."""

    def test_all_expected_keys_present(self):
        missing = EXPECTED_KEYS - set(CONFIG_METADATA.keys())
        assert not missing, f"CONFIG_METADATA is missing keys: {missing}"

    def test_no_unexpected_keys(self):
        """Every key in CONFIG_METADATA should be a known config key."""
        extra = set(CONFIG_METADATA.keys()) - EXPECTED_KEYS
        assert not extra, f"CONFIG_METADATA has unexpected keys: {extra}"


# Form fields that are not settings a user "sets" (schedule-editor scratch fields, the zone's display name,
# the GitHub section which settings_extra reports separately) or are entity picks with no default.
_NOT_SETTINGS = {"?key", "manage", "delete_schedule", "days", "start", "end", "name", "zone_name"}
_UNREGISTERED_NO_DEFAULT = {"github_repo", "github_token", "indoor_temp_entity", "outdoor_temp_entity"}


def _options_flow_keys() -> set[str]:
    """Every config key an options-flow step can write, read from the real step source (#1230 R5c)."""
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from custom_components.climate_advisor import config_flow, const  # noqa: PLC0415

    src = Path(config_flow.__file__).read_text(encoding="utf-8")
    body = src[src.index("class ClimateAdvisorOptionsFlow") :]
    keys: set[str] = set()
    for m in re.finditer(r"vol\.(?:Required|Optional)\(\s*([A-Za-z_\"']+)", body):
        token = m.group(1)
        keys.add(token.strip("\"'") if token[0] in "\"'" else getattr(const, token, "?" + token))
    # Built in loops over key tuples rather than literal vol.Required(...) calls.
    keys |= set(config_flow._SETPOINT_KEYS) | set(config_flow._VACATION_KEYS)
    return keys - _NOT_SETTINGS


class TestDefaultsRegistry:
    """#1230 R5c: one source of truth for defaults, complete over every options-flow key."""

    def test_every_options_flow_key_has_a_default_or_is_no_default(self):
        missing = []
        for key in sorted(_options_flow_keys()):
            if key in _UNREGISTERED_NO_DEFAULT:
                continue
            meta = CONFIG_METADATA.get(key)
            if meta is None or not ("default" in meta or meta.get("no_default") is True):
                missing.append(key)
        assert not missing, f"options-flow keys with neither a CONFIG_METADATA default nor no_default: {missing}"

    def test_every_metadata_entry_is_classified_exactly_once(self):
        for key, meta in CONFIG_METADATA.items():
            assert ("default" in meta) != (meta.get("no_default") is True), (
                f"{key}: needs exactly one of default / no_default"
            )

    def test_unknown_keys_do_not_read_as_having_a_default(self):
        from custom_components.climate_advisor.dashboard_api import default_for  # noqa: PLC0415

        assert default_for("energy_rate", CONFIG_METADATA["energy_rate"]) is None
        assert default_for("heat_power_kw", CONFIG_METADATA["heat_power_kw"]) is None

    def test_defaults_match_the_options_forms(self):
        """The few defaults that live as literals in the form must equal the registry value."""
        from custom_components.climate_advisor import config_flow  # noqa: PLC0415

        assert (
            CONFIG_METADATA["max_setback_depth_f"]["default"] == config_flow._DELTA_FIELDS_F["max_setback_depth_f"][2]
        )
        for key in (
            "comfort_deadband_hot_f",
            "comfort_deadband_warm_f",
            "comfort_deadband_mild_f",
            "comfort_deadband_cool_f",
            "comfort_deadband_cold_f",
        ):
            assert CONFIG_METADATA[key]["default"] == config_flow._DELTA_FIELDS_F[key][2], key


class TestConfigMetadataStructure:
    """Each entry must have the required fields."""

    def test_each_entry_has_label(self):
        for key, meta in CONFIG_METADATA.items():
            assert "label" in meta, f"Entry '{key}' is missing 'label'"
            assert isinstance(meta["label"], str), f"Entry '{key}' label must be a str"
            assert meta["label"], f"Entry '{key}' label must not be empty"

    def test_each_entry_has_description(self):
        for key, meta in CONFIG_METADATA.items():
            assert "description" in meta, f"Entry '{key}' is missing 'description'"
            assert isinstance(meta["description"], str), f"Entry '{key}' description must be a str"
            assert meta["description"], f"Entry '{key}' description must not be empty"

    def test_each_entry_has_category(self):
        for key, meta in CONFIG_METADATA.items():
            assert "category" in meta, f"Entry '{key}' is missing 'category'"

    def test_each_entry_has_only_known_fields(self):
        allowed = {"label", "description", "category", "display_transform", "sensitive", "default", "no_default"}
        for key, meta in CONFIG_METADATA.items():
            extra = set(meta.keys()) - allowed
            assert not extra, f"Entry '{key}' has unexpected fields: {extra}"


class TestConfigMetadataCategoriesValid:
    """All categories must be in the allowed set."""

    def test_all_categories_valid(self):
        for key, meta in CONFIG_METADATA.items():
            cat = meta.get("category")
            assert cat in VALID_CATEGORIES, (
                f"Entry '{key}' has invalid category '{cat}'. Must be one of: {VALID_CATEGORIES}"
            )

    def test_core_category_populated(self):
        core_keys = [k for k, m in CONFIG_METADATA.items() if m["category"] == "core"]
        assert len(core_keys) >= 4, "Expected at least 4 entries in 'core' category"

    def test_sensors_category_populated(self):
        sensor_keys = [k for k, m in CONFIG_METADATA.items() if m["category"] == "sensors"]
        assert len(sensor_keys) >= 4, "Expected at least 4 entries in 'sensors' category"

    def test_schedule_category_populated(self):
        schedule_keys = [k for k, m in CONFIG_METADATA.items() if m["category"] == "schedule"]
        assert len(schedule_keys) >= 3, "Expected at least 3 entries in 'schedule' category"


def test_changed_markers_cover_formerly_unknown_defaults():
    """#1230 R5c: keys that had no known default (wake time, notify toggles, ...) now get a marker."""
    from custom_components.climate_advisor.dashboard_api import build_settings_extra  # noqa: PLC0415

    changed = build_settings_extra(
        {"wake_time": "07:15:00", "push_briefing": False, "ai_investigator_requests_per_day": 9, "energy_rate": 0.3}
    )["changed"]
    assert changed["wake_time"]["default"] == "06:30:00"
    assert changed["push_briefing"]["default"] is True
    assert changed["ai_investigator_requests_per_day"]["default"] == 3
    assert "energy_rate" not in changed  # no default: set-or-not is not "changed"
