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
    "notify_service",
    "push_briefing",
    "push_door_window_pause",
    "push_occupancy_home",
    "email_briefing",
    "email_door_window_pause",
    "email_grace_expired",
    "email_grace_repause",
    "email_occupancy_home",
    "outdoor_temp_source",
    "indoor_temp_source",
    "door_window_sensors",
    "sensor_polarity_inverted",
    "sensor_debounce_seconds",
    "manual_grace_seconds",
    "manual_grace_notify",
    "automation_grace_seconds",
    "automation_grace_notify",
    "fan_mode",
    "fan_entity",
    "fan_min_runtime_per_hour",
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
    "preheat_safety_margin",
    "max_setback_depth_f",
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
    "ai_investigator_model",
    "ai_investigator_reasoning_effort",
    "ai_investigator_max_tokens",
    "ai_investigator_requests_per_day",
    "override_confirm_seconds",
}

VALID_CATEGORIES = {"core", "sensors", "fan", "schedule", "advanced", "occupancy", "notifications", "ai_settings"}


class TestConfigMetadataHasAllKeys:
    """Verify CONFIG_METADATA contains entries for all expected config keys."""

    def test_all_expected_keys_present(self):
        missing = EXPECTED_KEYS - set(CONFIG_METADATA.keys())
        assert not missing, f"CONFIG_METADATA is missing keys: {missing}"

    def test_no_unexpected_keys(self):
        """Every key in CONFIG_METADATA should be a known config key."""
        extra = set(CONFIG_METADATA.keys()) - EXPECTED_KEYS
        assert not extra, f"CONFIG_METADATA has unexpected keys: {extra}"


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
        allowed = {"label", "description", "category", "display_transform", "sensitive", "default"}
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
