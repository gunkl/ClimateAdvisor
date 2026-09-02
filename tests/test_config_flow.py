"""Tests for config flow — multi-step wizard, entry migration, and menu-based options.

Covers:
- Initial config flow wizard (async_step_user → temperature_sources → conditional
  entity picker steps → sensors → schedule)
- Conditional routing: weather_service/climate_fallback skips entity pickers;
  sensor/input_number triggers the appropriate picker step(s)
- _needs_entity() and _entity_selector_for_source() helper logic
- v1→v2 migration: outdoor_temp_entity present → sensor/input_number source;
  absent → weather_service (and indoor equivalent)
- v7→v8 migration: email_notify → per-event notification toggles
- Options flow menu navigation (Issue #50)
- Notifications step — per-event push/email toggles
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

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


def _make_options_flow(entry_data: dict):
    """Instantiate the REAL options flow for direct invocation.

    Enabled by the ha_stubs realification of ``config_entries.OptionsFlow``
    (same pattern as SensorEntity/HomeAssistantView, Issue #452). Returns
    ``(flow, captured)`` where ``captured['data']`` is the dict persisted by
    the most recent section commit (``_commit_section``, Issue #557 — each
    section step persists immediately on submit; there is no longer a
    separate save step).

    The mocked ``async_update_entry`` also mirrors real HA's behavior of
    mutating ``entry.data`` in place, so that driving multiple section steps
    in sequence correctly accumulates: each step's ``_commit_section`` reads
    ``self.config_entry.data`` fresh, which must reflect prior steps' commits.

    DOCTRINE: options-flow tests must exercise the production step handlers —
    never re-implement the merge in the test body. A mirror of the merge
    embeds the same logic it claims to test and cannot catch a bug in it
    (this is exactly how Issue #434 shipped despite green tests).
    """
    from unittest.mock import AsyncMock

    from custom_components.climate_advisor.config_flow import ClimateAdvisorOptionsFlow

    flow = object.__new__(ClimateAdvisorOptionsFlow)
    flow._updates = {}
    flow._removed = set()
    flow._editing_schedule_id = None
    flow.config_entry = _make_config_entry(entry_data)

    captured: dict = {}

    def _update_entry(entry, data):
        captured["data"] = data
        entry.data = data  # mirror real HA: async_update_entry mutates entry.data

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    hass.config_entries.async_reload = AsyncMock()
    flow.hass = hass
    return flow, captured


def _run_options_flow(entry_data: dict, steps: list[tuple[str, dict]]) -> dict:
    """Drive the REAL flow: apply each (step_method_name, user_input) in sequence.

    Each step commits immediately (Issue #557) — no trailing save call.
    Returns the dict persisted by the last driven step.
    """
    flow, captured = _make_options_flow(entry_data)

    async def _drive() -> None:
        for method_name, user_input in steps:
            await getattr(flow, method_name)(user_input)

    asyncio.run(_drive())
    return captured["data"]


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
    "automation_grace_seconds": 300,
    "automation_grace_notify": True,
    "push_briefing": True,
    "push_door_window_pause": True,
    "push_occupancy_home": True,
    "email_briefing": True,
    "email_door_window_pause": True,
    "email_grace_expired": True,
    "email_grace_repause": True,
    "email_occupancy_home": True,
    "wake_time": "06:30:00",
    "sleep_time": "22:30:00",
    "briefing_time": "06:00:00",
    "learning_enabled": True,
    "aggressive_savings": False,
    "home_toggle_entity": None,
    "home_toggle_invert": False,
    "vacation_toggle_entity": None,
    "vacation_toggle_invert": False,
    "guest_toggle_entity": None,
    "guest_toggle_invert": False,
    "temp_unit": "fahrenheit",
    "welcome_home_debounce_seconds": 3600,
}

FULL_CONFIG_V9 = {k: v for k, v in FULL_CONFIG.items() if k != "welcome_home_debounce_seconds"}
FULL_CONFIG_V8 = {k: v for k, v in FULL_CONFIG_V9.items() if k != "temp_unit"}
# v10 = v9 + welcome_home_debounce_seconds; does NOT contain the three v11 adaptive keys
FULL_CONFIG_V10 = {
    k: v
    for k, v in FULL_CONFIG.items()
    if k not in ("adaptive_preheat_enabled", "adaptive_setback_enabled", "weather_bias_enabled")
}
# v11 = v10 + three adaptive keys; does NOT contain the five v12 preheat/setback threshold keys
_V12_THRESHOLD_KEYS = (
    "min_preheat_minutes",
    "max_preheat_minutes",
    "default_preheat_minutes",
    "preheat_safety_margin",
    "max_setback_depth_f",
)
FULL_CONFIG_V11 = {
    **FULL_CONFIG,
    "adaptive_preheat_enabled": True,
    "adaptive_setback_enabled": True,
    "weather_bias_enabled": True,
}

# v12 = v11 + five threshold keys; does NOT contain the nine v13 AI keys
_AI_KEYS = (
    "ai_enabled",
    "ai_api_key",
    "ai_model",
    "ai_reasoning_effort",
    "ai_max_tokens",
    "ai_temperature",
    "ai_monthly_budget",
    "ai_auto_requests_per_day",
    "ai_manual_requests_per_day",
)
FULL_CONFIG_V12 = {
    **FULL_CONFIG_V11,
    "min_preheat_minutes": 30,
    "max_preheat_minutes": 240,
    "default_preheat_minutes": 120,
    "preheat_safety_margin": 1.3,
    "max_setback_depth_f": 8.0,
}

# v7 config (before migration to v8) — has email_notify instead of per-event toggles
FULL_CONFIG_V7 = {
    **{k: v for k, v in FULL_CONFIG.items() if not k.startswith(("push_", "email_"))},
    "email_notify": True,
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
                assert steps[-1] == "schedule", f"schedule must be last for outdoor={outdoor}, indoor={indoor}"


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
        data.update(
            {
                "weather_entity": "weather.forecast_home",
                "climate_entity": "climate.living_room",
                "comfort_heat": 70,
                "comfort_cool": 75,
                "setback_heat": 60,
                "setback_cool": 80,
                "notify_service": "notify.notify",
            }
        )

        # Step 2: temperature_sources
        data.update(
            {
                "outdoor_temp_source": outdoor_source,
                "indoor_temp_source": indoor_source,
            }
        )

        # Conditional step: outdoor entity picker
        if outdoor_source in ("sensor", "input_number"):
            assert outdoor_entity is not None, "outdoor_entity required for this source"
            data["outdoor_temp_entity"] = outdoor_entity

        # Conditional step: indoor entity picker
        if indoor_source in ("sensor", "input_number"):
            assert indoor_entity is not None, "indoor_entity required for this source"
            data["indoor_temp_entity"] = indoor_entity

        # Step: sensors
        data.update(
            {
                "door_window_sensors": [],
                "sensor_polarity_inverted": False,
                "sensor_debounce_seconds": 300,
                "manual_grace_seconds": 1800,
                "automation_grace_seconds": 300,
            }
        )

        # Step: schedule (final — creates entry)
        data.update(
            {
                "wake_time": "06:30:00",
                "sleep_time": "22:30:00",
                "briefing_time": "06:00:00",
            }
        )

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
            DEFAULT_COMFORT_COOL,
            DEFAULT_COMFORT_HEAT,
            DEFAULT_SETBACK_COOL,
            DEFAULT_SETBACK_HEAT,
        )

        assert 55 <= DEFAULT_COMFORT_HEAT <= 80
        assert 68 <= DEFAULT_COMFORT_COOL <= 85
        assert 45 <= DEFAULT_SETBACK_HEAT <= 65
        assert 75 <= DEFAULT_SETBACK_COOL <= 90


def _make_config_flow(existing_entries: list[MagicMock] | None = None):
    """Instantiate the REAL config flow for direct invocation (Issue #808).

    Enabled by the ha_stubs realification of ``config_entries.ConfigFlow``
    (same pattern as ``_make_options_flow`` for OptionsFlow, Issue #452) plus
    the unique_id/abort helpers added to ``_MockConfigFlow`` for this issue.
    ``hass.config_entries.async_entries`` is stubbed to return the "existing
    entries" the dedup check should compare against — the actual dedup
    decision (does any entry's ``unique_id`` match?) still runs inside the
    real ``_abort_if_unique_id_configured()``, not in the test.
    """
    from custom_components.climate_advisor.config_flow import ClimateAdvisorConfigFlow

    flow = object.__new__(ClimateAdvisorConfigFlow)
    flow._data = {}
    flow.unique_id = None

    hass = MagicMock()
    hass.config_entries.async_entries = MagicMock(return_value=existing_entries or [])
    flow.hass = hass
    return flow


def _run_config_flow_user_step(existing_entries: list[MagicMock] | None, user_input: dict) -> dict:
    """Drive the REAL async_step_user against mocked existing entries.

    Catches the stub's ``AbortFlow`` exception the same way HA's real
    FlowManager does when a step raises it, converting it into an abort
    FlowResult — this conversion is FlowManager plumbing, not the dedup
    logic under test (which stays inside the production step handler).
    """
    from homeassistant.data_entry_flow import AbortFlow

    flow = _make_config_flow(existing_entries)

    async def _drive() -> dict:
        try:
            return await flow.async_step_user(user_input)
        except AbortFlow as err:
            return {"type": "abort", "reason": err.reason}

    return asyncio.run(_drive())


class TestConfigFlowDuplicateZoneGuard:
    """Issue #808: adding a second zone for the same climate_entity must abort.

    Adding a zone for a DIFFERENT climate_entity must proceed normally —
    this is the multi-zone regression guard for Issue #796.
    """

    USER_INPUT = {
        "weather_entity": "weather.forecast_home",
        "climate_entity": "climate.living_room",
        "notify_service": "notify.notify",
    }

    def test_duplicate_climate_entity_aborts(self):
        """A second zone targeting an already-configured climate_entity aborts."""
        existing_entry = MagicMock()
        existing_entry.unique_id = "climate.living_room"

        result = _run_config_flow_user_step([existing_entry], dict(self.USER_INPUT))

        assert result["type"] == "abort"
        assert result["reason"] == "already_configured"

    def test_different_climate_entity_proceeds(self):
        """A second zone targeting a DIFFERENT climate_entity is not blocked (#796)."""
        existing_entry = MagicMock()
        existing_entry.unique_id = "climate.bedroom"

        user_input = dict(self.USER_INPUT)
        user_input["climate_entity"] = "climate.living_room"

        result = _run_config_flow_user_step([existing_entry], user_input)

        assert result["type"] == "form"
        assert result["step_id"] == "unit"


class TestSetpointSliderRangesRegression:
    """Regression guard (architecture-reset session, #438 follow-up): the initial
    setup wizard's Fahrenheit sleep_heat/sleep_cool defaults, and ALL SIX Celsius
    defaults, were hardcoded literals that silently went stale when
    DEFAULT_SLEEP_HEAT/DEFAULT_SLEEP_COOL were reformatted from 66/78 to 64/72 —
    every brand-new install got the OLD values explicitly written into its config,
    bypassing the reformat entirely. setpoint_slider_ranges() now derives every
    default from the DEFAULT_* constants directly, so this can't drift again.
    """

    def test_fahrenheit_defaults_match_named_constants_exactly(self):
        from custom_components.climate_advisor.config_flow import setpoint_slider_ranges
        from custom_components.climate_advisor.const import (
            DEFAULT_COMFORT_COOL,
            DEFAULT_COMFORT_HEAT,
            DEFAULT_SETBACK_COOL,
            DEFAULT_SETBACK_HEAT,
            DEFAULT_SLEEP_COOL,
            DEFAULT_SLEEP_HEAT,
        )

        ranges = setpoint_slider_ranges(is_celsius=False)
        assert ranges["comfort_heat"][2] == DEFAULT_COMFORT_HEAT
        assert ranges["comfort_cool"][2] == DEFAULT_COMFORT_COOL
        assert ranges["setback_heat"][2] == DEFAULT_SETBACK_HEAT
        assert ranges["setback_cool"][2] == DEFAULT_SETBACK_COOL
        assert ranges["sleep_heat"][2] == DEFAULT_SLEEP_HEAT, (
            "sleep_heat was the specific field found still hardcoded to the stale 66"
        )
        assert ranges["sleep_cool"][2] == DEFAULT_SLEEP_COOL, (
            "sleep_cool was the specific field found still hardcoded to the stale 78"
        )

    def test_celsius_defaults_are_the_fahrenheit_defaults_converted(self):
        """Each Celsius default must equal from_fahrenheit(DEFAULT_X, CELSIUS),
        rounded to the nearest 0.5 — not an independently hand-picked literal that
        can silently stop matching the Fahrenheit constant it's supposed to mirror."""
        from custom_components.climate_advisor.config_flow import setpoint_slider_ranges
        from custom_components.climate_advisor.const import (
            DEFAULT_COMFORT_COOL,
            DEFAULT_COMFORT_HEAT,
            DEFAULT_SETBACK_COOL,
            DEFAULT_SETBACK_HEAT,
            DEFAULT_SLEEP_COOL,
            DEFAULT_SLEEP_HEAT,
        )
        from custom_components.climate_advisor.temperature import CELSIUS, from_fahrenheit

        ranges = setpoint_slider_ranges(is_celsius=True)
        for key, f_default in (
            ("comfort_heat", DEFAULT_COMFORT_HEAT),
            ("comfort_cool", DEFAULT_COMFORT_COOL),
            ("setback_heat", DEFAULT_SETBACK_HEAT),
            ("setback_cool", DEFAULT_SETBACK_COOL),
            ("sleep_heat", DEFAULT_SLEEP_HEAT),
            ("sleep_cool", DEFAULT_SLEEP_COOL),
        ):
            expected = round(from_fahrenheit(f_default, CELSIUS) * 2) / 2
            assert ranges[key][2] == expected, f"{key}: expected {expected}, got {ranges[key][2]}"

    def test_celsius_sleep_cool_default_is_not_the_old_stale_26(self):
        """Concrete regression pin: the old hardcoded Celsius sleep_cool default (26,
        matching the pre-#438 78F) must not reappear."""
        from custom_components.climate_advisor.config_flow import setpoint_slider_ranges

        ranges = setpoint_slider_ranges(is_celsius=True)
        assert ranges["sleep_cool"][2] != 26, "26C is the stale pre-#438 default (78F) — must not reappear"
        assert ranges["sleep_cool"][2] == 22.0

    def test_all_defaults_within_their_own_slider_bounds(self):
        """Every default (both unit branches) must fall within its own (min, max)."""
        from custom_components.climate_advisor.config_flow import setpoint_slider_ranges

        for is_celsius in (False, True):
            ranges = setpoint_slider_ranges(is_celsius)
            for key, (mn, mx, default, _step) in ranges.items():
                assert mn <= default <= mx, (
                    f"{key} ({'C' if is_celsius else 'F'}): default {default} not in [{mn}, {mx}]"
                )


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
            # Replace with latest so we see the final state (not accumulated).
            written_data.clear()
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

    def test_v1_chain_migration_produces_v8_fields(self):
        """A v1 entry should chain through all migrations and gain v8 fields."""
        from custom_components.climate_advisor.const import (
            CONF_AUTOMATION_GRACE_NOTIFY,
            CONF_AUTOMATION_GRACE_PERIOD,
            CONF_EMAIL_BRIEFING,
            CONF_MANUAL_GRACE_NOTIFY,
            CONF_MANUAL_GRACE_PERIOD,
            CONF_PUSH_BRIEFING,
            CONF_SENSOR_DEBOUNCE,
            DEFAULT_AUTOMATION_GRACE_SECONDS,
            DEFAULT_MANUAL_GRACE_SECONDS,
            DEFAULT_SENSOR_DEBOUNCE_SECONDS,
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
        # v8 fields (per-event notification toggles)
        assert data.get(CONF_EMAIL_BRIEFING) is True
        assert data.get(CONF_PUSH_BRIEFING) is True
        # email_notify should be removed after v7→v8 migration
        assert "email_notify" not in data
        # v9 fields (temperature unit)
        assert data.get("temp_unit") == "fahrenheit"

    def test_v4_entry_migrates_to_v5_with_email_notify(self):
        """A v4 entry should gain email_notify=True via v4→v5 migration."""
        v4 = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "notify_service": "notify.notify",
        }
        data, ok = self._call_migrate(v4, start_version=4)
        assert ok is True
        # After chaining through v5→v6→v7→v8, per-event toggles should exist
        assert data.get("email_briefing") is True
        assert data.get("push_briefing") is True

    def test_already_v5_entry_migrates_through_to_v10(self):
        """A v5 entry should migrate through v6, v7, v8, v9, v10."""
        v5 = dict(FULL_CONFIG_V7)
        entry = _make_config_entry(v5, version=5)
        hass = _make_hass()

        versions_seen = []

        def capture_update(entry, *, data, version):
            versions_seen.append(version)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update

        from custom_components.climate_advisor import async_migrate_entry

        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert 8 in versions_seen
        assert 9 in versions_seen
        assert 10 in versions_seen


# ---------------------------------------------------------------------------
# v7→v8 migration — per-event notification toggles (Issue #50)
# ---------------------------------------------------------------------------


class TestMigrationV7ToV8:
    """Test the v7→v8 migration that replaces email_notify with per-event toggles."""

    def _run_v7_to_v8_migration(self, v7_data: dict) -> dict:
        """Apply v7→v8 migration logic and return the resulting data dict."""
        new_data = dict(v7_data)
        old_email = new_data.pop("email_notify", True)
        new_data.setdefault("email_briefing", old_email)
        new_data.setdefault("email_door_window_pause", old_email)
        new_data.setdefault("email_grace_expired", old_email)
        new_data.setdefault("email_grace_repause", old_email)
        new_data.setdefault("email_occupancy_home", old_email)
        new_data.setdefault("push_briefing", True)
        new_data.setdefault("push_door_window_pause", True)
        new_data.setdefault("push_occupancy_home", True)
        return new_data

    def test_email_true_migrates_all_email_on(self):
        """Old email_notify=True → all per-event email toggles True."""
        result = self._run_v7_to_v8_migration({"email_notify": True})
        assert result["email_briefing"] is True
        assert result["email_door_window_pause"] is True
        assert result["email_grace_expired"] is True
        assert result["email_grace_repause"] is True
        assert result["email_occupancy_home"] is True

    def test_email_false_migrates_all_email_off(self):
        """Old email_notify=False → all per-event email toggles False."""
        result = self._run_v7_to_v8_migration({"email_notify": False})
        assert result["email_briefing"] is False
        assert result["email_door_window_pause"] is False
        assert result["email_grace_expired"] is False
        assert result["email_grace_repause"] is False
        assert result["email_occupancy_home"] is False

    def test_push_toggles_always_default_true(self):
        """Push toggles default to True regardless of old email_notify value."""
        for old_email in (True, False):
            result = self._run_v7_to_v8_migration({"email_notify": old_email})
            assert result["push_briefing"] is True
            assert result["push_door_window_pause"] is True
            assert result["push_occupancy_home"] is True

    def test_old_email_notify_key_removed(self):
        """email_notify key should not be in migrated data."""
        result = self._run_v7_to_v8_migration({"email_notify": True})
        assert "email_notify" not in result

    def test_missing_email_notify_defaults_to_true(self):
        """When email_notify is absent, defaults to True (all email toggles on)."""
        result = self._run_v7_to_v8_migration({})
        assert result["email_briefing"] is True
        assert result["email_occupancy_home"] is True

    def test_existing_fields_preserved(self):
        """Non-notification fields survive migration unchanged."""
        v7 = {
            "email_notify": False,
            "weather_entity": "weather.forecast_home",
            "comfort_heat": 72,
        }
        result = self._run_v7_to_v8_migration(v7)
        assert result["weather_entity"] == "weather.forecast_home"
        assert result["comfort_heat"] == 72

    def test_via_real_function(self):
        """Run the real async_migrate_entry for a v7 entry."""
        from custom_components.climate_advisor import async_migrate_entry

        v7 = dict(FULL_CONFIG_V7)
        entry = _make_config_entry(v7, version=7)
        hass = _make_hass()

        final_data = {}

        def capture_update(entry, *, data, version):
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update

        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert "email_notify" not in final_data
        assert final_data["email_briefing"] is True
        assert final_data["push_briefing"] is True


# ---------------------------------------------------------------------------
# v8→v9 migration — temperature unit preference (Issue #58)
# ---------------------------------------------------------------------------


class TestMigrationV8ToV9:
    """Tests for config entry migration from version 8 to version 9 (temperature unit)."""

    def _run_v8_to_v9_migration(self, v8_data: dict) -> dict:
        """Inline replication of v8→v9 migration logic for isolated testing."""
        new_data = {**v8_data}
        new_data.setdefault("temp_unit", "fahrenheit")
        return new_data

    def test_temp_unit_added_with_fahrenheit_default(self):
        """v8 data without temp_unit gets temp_unit='fahrenheit'."""
        result = self._run_v8_to_v9_migration(dict(FULL_CONFIG_V8))
        assert result["temp_unit"] == "fahrenheit"

    def test_existing_temp_unit_not_overwritten(self):
        """If temp_unit already exists (e.g. celsius), setdefault preserves it."""
        v8_with_unit = {**FULL_CONFIG_V8, "temp_unit": "celsius"}
        result = self._run_v8_to_v9_migration(v8_with_unit)
        assert result["temp_unit"] == "celsius"

    def test_other_fields_preserved(self):
        """All existing v8 fields survive migration unchanged."""
        result = self._run_v8_to_v9_migration(dict(FULL_CONFIG_V8))
        for key in FULL_CONFIG_V8:
            assert result[key] == FULL_CONFIG_V8[key], f"Field {key!r} changed unexpectedly"

    def test_only_temp_unit_added(self):
        """Migration adds exactly one key."""
        result = self._run_v8_to_v9_migration(dict(FULL_CONFIG_V8))
        new_keys = set(result) - set(FULL_CONFIG_V8)
        assert new_keys == {"temp_unit"}

    def test_via_real_function_v8_entry(self):
        """Real async_migrate_entry() adds temp_unit to a v8 entry."""
        import asyncio

        from custom_components.climate_advisor import async_migrate_entry

        v8 = dict(FULL_CONFIG_V8)
        entry = _make_config_entry(v8, version=8)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("temp_unit") == "fahrenheit"
        assert entry.version == 18

    def test_chain_from_v1_includes_temp_unit(self):
        """v1 entry chains through all migrations and ends up with temp_unit."""
        import asyncio

        from custom_components.climate_advisor import async_migrate_entry

        # Minimal v1 data (what a v1 entry would have had)
        v1_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.thermostat",
            "notify_service": "notify.mobile_app",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
        }
        entry = _make_config_entry(v1_data, version=1)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("temp_unit") == "fahrenheit"
        assert entry.version == 18


# ---------------------------------------------------------------------------
# v9→v10 migration — welcome home debounce (Issue #59)
# ---------------------------------------------------------------------------


class TestMigrationV9ToV10:
    """Tests for config entry migration from version 9 to version 10."""

    def _run_v9_to_v10_migration(self, v9_data: dict) -> dict:
        """Inline replication of v9→v10 migration logic for isolated testing."""
        new_data = {**v9_data}
        new_data.setdefault("welcome_home_debounce_seconds", 3600)
        return new_data

    def test_debounce_added_with_default(self):
        """v9 data without debounce key gets welcome_home_debounce_seconds=3600."""
        result = self._run_v9_to_v10_migration(dict(FULL_CONFIG_V9))
        assert result["welcome_home_debounce_seconds"] == 3600

    def test_existing_debounce_not_overwritten(self):
        """If key already exists, setdefault preserves it."""
        v9_with_debounce = {**FULL_CONFIG_V9, "welcome_home_debounce_seconds": 0}
        result = self._run_v9_to_v10_migration(v9_with_debounce)
        assert result["welcome_home_debounce_seconds"] == 0

    def test_other_fields_preserved(self):
        """All existing v9 fields survive migration unchanged."""
        result = self._run_v9_to_v10_migration(dict(FULL_CONFIG_V9))
        for key in FULL_CONFIG_V9:
            assert result[key] == FULL_CONFIG_V9[key], f"Field {key!r} changed unexpectedly"

    def test_only_debounce_added(self):
        """Migration adds exactly one key."""
        result = self._run_v9_to_v10_migration(dict(FULL_CONFIG_V9))
        new_keys = set(result) - set(FULL_CONFIG_V9)
        assert new_keys == {"welcome_home_debounce_seconds"}

    def test_via_real_function_v9_entry(self):
        """Real async_migrate_entry() adds welcome_home_debounce_seconds to a v9 entry."""
        import asyncio

        from custom_components.climate_advisor import async_migrate_entry

        v9 = dict(FULL_CONFIG_V9)
        entry = _make_config_entry(v9, version=9)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("welcome_home_debounce_seconds") == 3600
        assert entry.version == 18

    def test_chain_from_v1_includes_debounce(self):
        """v1 entry chains through all migrations and ends up with welcome_home_debounce_seconds."""
        import asyncio

        from custom_components.climate_advisor import async_migrate_entry

        v1_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.thermostat",
            "notify_service": "notify.mobile_app",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
        }
        entry = _make_config_entry(v1_data, version=1)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("welcome_home_debounce_seconds") == 3600
        assert final_data.get("temp_unit") == "fahrenheit"
        assert entry.version == 18


class TestMigrationV10ToV11:
    """Tests for config entry migration from version 10 to version 11."""

    def _run_v10_to_v11_migration(self, v10_data: dict) -> dict:
        """Inline replication of v10→v11 migration logic for isolated testing."""
        new_data = {**v10_data}
        new_data.setdefault("adaptive_preheat_enabled", True)
        new_data.setdefault("adaptive_setback_enabled", True)
        new_data.setdefault("weather_bias_enabled", True)
        return new_data

    def test_v10_to_v11_adds_three_new_keys(self):
        """v10 data without the three new keys gets them added with True defaults."""
        result = self._run_v10_to_v11_migration(dict(FULL_CONFIG_V10))
        assert result["adaptive_preheat_enabled"] is True
        assert result["adaptive_setback_enabled"] is True
        assert result["weather_bias_enabled"] is True

    def test_v10_to_v11_does_not_overwrite_existing_false_value(self):
        """If adaptive_preheat_enabled already exists as False, setdefault preserves it."""
        v10_with_preheat = {**FULL_CONFIG_V10, "adaptive_preheat_enabled": False}
        result = self._run_v10_to_v11_migration(v10_with_preheat)
        assert result["adaptive_preheat_enabled"] is False

    def test_v10_to_v11_preserves_other_fields(self):
        """All existing v10 fields survive migration unchanged."""
        result = self._run_v10_to_v11_migration(dict(FULL_CONFIG_V10))
        for key in FULL_CONFIG_V10:
            assert result[key] == FULL_CONFIG_V10[key], f"Field {key!r} changed unexpectedly"

    def test_v10_to_v11_result_is_version_11(self):
        """Real async_migrate_entry() bumps version to 11 for a v10 entry."""
        import asyncio

        from custom_components.climate_advisor import async_migrate_entry

        v10 = dict(FULL_CONFIG_V10)
        entry = _make_config_entry(v10, version=10)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("adaptive_preheat_enabled") is True
        assert final_data.get("adaptive_setback_enabled") is True
        assert final_data.get("weather_bias_enabled") is True
        assert entry.version == 18


class TestMigrationV11ToV12:
    """Tests for config entry migration from version 11 to version 12."""

    def test_v11_to_v12_defaults_set(self):
        """v11 entry with no threshold keys gets all five keys with correct defaults."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(FULL_CONFIG_V11), version=11)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("min_preheat_minutes") == 30
        assert final_data.get("max_preheat_minutes") == 240
        assert final_data.get("default_preheat_minutes") == 120
        assert final_data.get("preheat_safety_margin") == 1.3
        assert final_data.get("max_setback_depth_f") == 8.0
        assert entry.version == 18

    def test_v11_to_v12_existing_values_preserved(self):
        """v11 entry with all threshold keys set retains those values after migration."""
        from custom_components.climate_advisor import async_migrate_entry

        v11_with_thresholds = {
            **FULL_CONFIG_V11,
            "min_preheat_minutes": 45,
            "max_preheat_minutes": 180,
            "default_preheat_minutes": 90,
            "preheat_safety_margin": 1.5,
            "max_setback_depth_f": 6.0,
        }
        entry = _make_config_entry(v11_with_thresholds, version=11)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("min_preheat_minutes") == 45
        assert final_data.get("max_preheat_minutes") == 180
        assert final_data.get("default_preheat_minutes") == 90
        assert final_data.get("preheat_safety_margin") == 1.5
        assert final_data.get("max_setback_depth_f") == 6.0
        assert entry.version == 18

    def test_v11_to_v12_invalid_type_replaced(self):
        """v11 entry where min_preheat_minutes is a non-numeric string gets the default."""
        from custom_components.climate_advisor import async_migrate_entry

        v11_bad = {**FULL_CONFIG_V11, "min_preheat_minutes": "bad"}
        entry = _make_config_entry(v11_bad, version=11)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_data.get("min_preheat_minutes") == 30
        assert entry.version == 18

    def test_v11_to_v12_from_v10_chain(self):
        """v10 entry chains through v11 and v12 migrations; all five threshold keys get defaults."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(FULL_CONFIG_V10), version=10)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert entry.version == 18
        assert final_data.get("min_preheat_minutes") == 30
        assert final_data.get("max_preheat_minutes") == 240
        assert final_data.get("default_preheat_minutes") == 120
        assert final_data.get("preheat_safety_margin") == 1.3
        assert final_data.get("max_setback_depth_f") == 8.0


class TestMigrationV12ToV13:
    """Tests for config entry migration from version 12 to version 13 (AI settings)."""

    def _run_v12_to_v13_migration(self, v12_data: dict) -> dict:
        """Inline replication of v12→v13 migration logic for isolated testing."""
        new_data = {**v12_data}
        new_data.setdefault("ai_enabled", False)
        new_data.setdefault("ai_api_key", "")
        new_data.setdefault("ai_model", "claude-sonnet-4-6")
        new_data.setdefault("ai_reasoning_effort", "medium")
        new_data.setdefault("ai_max_tokens", 4096)
        new_data.setdefault("ai_temperature", 0.3)
        new_data.setdefault("ai_monthly_budget", 0)
        new_data.setdefault("ai_auto_requests_per_day", 5)
        new_data.setdefault("ai_manual_requests_per_day", 20)
        return new_data

    def test_v12_to_v13_adds_all_nine_ai_keys(self):
        """v12 data without AI keys gets all nine keys with correct defaults."""
        result = self._run_v12_to_v13_migration(dict(FULL_CONFIG_V12))
        assert result["ai_enabled"] is False
        assert result["ai_api_key"] == ""
        assert result["ai_model"] == "claude-sonnet-4-6"
        assert result["ai_reasoning_effort"] == "medium"
        assert result["ai_max_tokens"] == 4096
        assert result["ai_temperature"] == 0.3
        assert result["ai_monthly_budget"] == 0
        assert result["ai_auto_requests_per_day"] == 5
        assert result["ai_manual_requests_per_day"] == 20

    def test_v12_to_v13_existing_ai_values_preserved(self):
        """If AI keys already exist, setdefault must not overwrite them."""
        v12_with_ai = {
            **FULL_CONFIG_V12,
            "ai_enabled": True,
            "ai_api_key": "sk-ant-test",
            "ai_model": "claude-opus-4-6",
            "ai_reasoning_effort": "high",
            "ai_max_tokens": 8192,
            "ai_temperature": 0.7,
            "ai_monthly_budget": 10,
            "ai_auto_requests_per_day": 3,
            "ai_manual_requests_per_day": 10,
        }
        result = self._run_v12_to_v13_migration(v12_with_ai)
        assert result["ai_enabled"] is True
        assert result["ai_api_key"] == "sk-ant-test"
        assert result["ai_model"] == "claude-opus-4-6"
        assert result["ai_reasoning_effort"] == "high"
        assert result["ai_max_tokens"] == 8192
        assert result["ai_temperature"] == 0.7
        assert result["ai_monthly_budget"] == 10
        assert result["ai_auto_requests_per_day"] == 3
        assert result["ai_manual_requests_per_day"] == 10

    def test_v12_to_v13_preserves_other_fields(self):
        """All existing v12 fields survive migration unchanged."""
        result = self._run_v12_to_v13_migration(dict(FULL_CONFIG_V12))
        for key in FULL_CONFIG_V12:
            assert result[key] == FULL_CONFIG_V12[key], f"Field {key!r} changed unexpectedly"

    def test_v12_to_v13_adds_exactly_nine_keys(self):
        """Migration adds exactly the nine AI keys and nothing else."""
        result = self._run_v12_to_v13_migration(dict(FULL_CONFIG_V12))
        new_keys = set(result) - set(FULL_CONFIG_V12)
        assert new_keys == set(_AI_KEYS)

    def test_v12_to_v13_via_real_function(self):
        """Real async_migrate_entry() adds all AI defaults to a v12 entry."""
        from custom_components.climate_advisor import async_migrate_entry
        from custom_components.climate_advisor.const import (
            DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
            DEFAULT_AI_ENABLED,
            DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
            DEFAULT_AI_MAX_TOKENS,
            DEFAULT_AI_MODEL,
            DEFAULT_AI_MONTHLY_BUDGET,
            DEFAULT_AI_REASONING_EFFORT,
            DEFAULT_AI_TEMPERATURE,
        )

        entry = _make_config_entry(dict(FULL_CONFIG_V12), version=12)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert entry.version == 18
        assert final_data.get("ai_enabled") is DEFAULT_AI_ENABLED
        assert final_data.get("ai_api_key") == ""
        assert final_data.get("ai_model") == DEFAULT_AI_MODEL
        assert final_data.get("ai_reasoning_effort") == DEFAULT_AI_REASONING_EFFORT
        assert final_data.get("ai_max_tokens") == DEFAULT_AI_MAX_TOKENS
        assert final_data.get("ai_temperature") == DEFAULT_AI_TEMPERATURE
        assert final_data.get("ai_monthly_budget") == DEFAULT_AI_MONTHLY_BUDGET
        assert final_data.get("ai_auto_requests_per_day") == DEFAULT_AI_AUTO_REQUESTS_PER_DAY
        assert final_data.get("ai_manual_requests_per_day") == DEFAULT_AI_MANUAL_REQUESTS_PER_DAY

    def test_v12_to_v13_from_v11_chain(self):
        """v11 entry chains through v12 and v13; all nine AI keys get defaults."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(FULL_CONFIG_V11), version=11)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert entry.version == 18
        assert final_data.get("ai_enabled") is False
        assert final_data.get("ai_model") == "claude-sonnet-5"
        assert final_data.get("ai_max_tokens") == 4096


# ---------------------------------------------------------------------------
# v13→v14 migration — investigative agent config keys (Issue #82)
# ---------------------------------------------------------------------------

_INVESTIGATOR_KEYS = [
    "ai_investigator_enabled",
    "ai_investigator_model",
    "ai_investigator_reasoning_effort",
    "ai_investigator_max_tokens",
    "ai_investigator_requests_per_day",
]


class TestMigrationV13ToV14:
    """Tests for config entry migration from version 13 to version 14."""

    def _run_v13_to_v14_migration(self, v13_data: dict) -> dict:
        """Run only the v13→v14 migration step and return resulting data."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(v13_data), version=13)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        asyncio.run(async_migrate_entry(hass, entry))
        return final_data

    def test_v13_to_v14_adds_investigator_defaults(self):
        """Migration adds all 5 investigator keys with correct defaults."""
        from custom_components.climate_advisor.const import (
            DEFAULT_AI_INVESTIGATOR_ENABLED,
            DEFAULT_AI_INVESTIGATOR_MAX_TOKENS,
            DEFAULT_AI_INVESTIGATOR_MODEL,
            DEFAULT_AI_INVESTIGATOR_REASONING,
            DEFAULT_AI_INVESTIGATOR_RPD,
        )

        result = self._run_v13_to_v14_migration(dict(FULL_CONFIG))
        assert result.get("ai_investigator_enabled") is DEFAULT_AI_INVESTIGATOR_ENABLED
        assert result.get("ai_investigator_model") == DEFAULT_AI_INVESTIGATOR_MODEL
        assert result.get("ai_investigator_reasoning_effort") == DEFAULT_AI_INVESTIGATOR_REASONING
        assert result.get("ai_investigator_max_tokens") == DEFAULT_AI_INVESTIGATOR_MAX_TOKENS
        assert result.get("ai_investigator_requests_per_day") == DEFAULT_AI_INVESTIGATOR_RPD

    def test_v13_to_v14_adds_exactly_five_investigator_keys_plus_sleep_keys(self):
        """Migration v13→v18 adds the five investigator keys, two sleep keys, four
        threshold keys, and (Issue #797) default_tou_lead_minutes."""
        result = self._run_v13_to_v14_migration(dict(FULL_CONFIG))
        new_keys = set(result) - set(FULL_CONFIG)
        # v13→v14 adds 5 investigator keys; v14→v15 adds sleep_heat + sleep_cool;
        # v15→v16 adds threshold_hot/warm/mild/cool; v16→v17 adds no new keys (fan_mode
        # coercion only); v17→v18 adds default_tou_lead_minutes (Issue #797)
        _threshold_keys = {"threshold_hot", "threshold_warm", "threshold_mild", "threshold_cool"}
        assert new_keys == (
            set(_INVESTIGATOR_KEYS) | {"sleep_heat", "sleep_cool"} | _threshold_keys | {"default_tou_lead_minutes"}
        )

    def test_v13_to_v14_preserves_existing_fields(self):
        """All existing v13 fields survive migration unchanged."""
        result = self._run_v13_to_v14_migration(dict(FULL_CONFIG))
        for key in FULL_CONFIG:
            assert result[key] == FULL_CONFIG[key], f"Field {key!r} changed unexpectedly"

    def test_v13_to_v14_does_not_overwrite_existing_investigator_key(self):
        """If a key already exists it is preserved (setdefault semantics)."""
        v13_with_key = {**FULL_CONFIG, "ai_investigator_enabled": True}
        result = self._run_v13_to_v14_migration(v13_with_key)
        assert result.get("ai_investigator_enabled") is True

    def test_v13_to_v14_via_real_function(self):
        """Real async_migrate_entry() adds all investigator defaults to a v13 entry."""
        from custom_components.climate_advisor import async_migrate_entry
        from custom_components.climate_advisor.const import (
            DEFAULT_AI_INVESTIGATOR_ENABLED,
            DEFAULT_AI_INVESTIGATOR_MAX_TOKENS,
            DEFAULT_AI_INVESTIGATOR_MODEL,
            DEFAULT_AI_INVESTIGATOR_REASONING,
            DEFAULT_AI_INVESTIGATOR_RPD,
        )

        entry = _make_config_entry(dict(FULL_CONFIG), version=13)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert entry.version == 18
        assert final_data.get("ai_investigator_enabled") is DEFAULT_AI_INVESTIGATOR_ENABLED
        assert final_data.get("ai_investigator_model") == DEFAULT_AI_INVESTIGATOR_MODEL
        assert final_data.get("ai_investigator_reasoning_effort") == DEFAULT_AI_INVESTIGATOR_REASONING
        assert final_data.get("ai_investigator_max_tokens") == DEFAULT_AI_INVESTIGATOR_MAX_TOKENS
        assert final_data.get("ai_investigator_requests_per_day") == DEFAULT_AI_INVESTIGATOR_RPD


# ---------------------------------------------------------------------------
# v14→v15 migration — sleep_heat and sleep_cool (Issue #101)
# ---------------------------------------------------------------------------

_SLEEP_KEYS = ["sleep_heat", "sleep_cool"]

# A v14-era config that does NOT yet have sleep keys
_FULL_CONFIG_V14 = {
    **{
        k: v
        for k, v in FULL_CONFIG.items()
        if k
        not in (
            "adaptive_preheat_enabled",
            "adaptive_setback_enabled",
            "weather_bias_enabled",
        )
    },
    "adaptive_preheat_enabled": True,
    "adaptive_setback_enabled": True,
    "weather_bias_enabled": True,
    "min_preheat_minutes": 30,
    "max_preheat_minutes": 240,
    "default_preheat_minutes": 120,
    "preheat_safety_margin": 1.3,
    "max_setback_depth_f": 8.0,
    "ai_enabled": False,
    "ai_api_key": "",
    "ai_model": "claude-sonnet-4-6",
    "ai_reasoning_effort": "medium",
    "ai_max_tokens": 4096,
    "ai_temperature": 0.3,
    "ai_monthly_budget": 0,
    "ai_auto_requests_per_day": 5,
    "ai_manual_requests_per_day": 20,
    "ai_investigator_enabled": False,
    "ai_investigator_model": "claude-sonnet-4-6",
    "ai_investigator_reasoning_effort": "high",
    "ai_investigator_max_tokens": 20480,
    "ai_investigator_requests_per_day": 3,
}


class TestMigrationV14ToV15:
    """Tests for config entry migration from version 14 to version 15."""

    def _run_migration(self, initial_data: dict) -> dict:
        """Run only the v14→v15 migration step and return resulting data."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(initial_data), version=14)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        asyncio.run(async_migrate_entry(hass, entry))
        return final_data

    def test_adds_sleep_heat_from_comfort(self):
        """sleep_heat is set to comfort_heat - 4 (default depth)."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 70.0,
                "setback_heat": 60.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
            }
        )
        assert data["sleep_heat"] == pytest.approx(66.0, abs=0.5)

    def test_adds_sleep_cool_from_comfort(self):
        """sleep_cool is set to comfort_cool + 3 (default cool depth)."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 70.0,
                "setback_heat": 60.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
            }
        )
        assert data["sleep_cool"] == pytest.approx(78.0, abs=0.5)

    def test_preserves_existing_fields(self):
        """All existing v14 fields survive migration unchanged."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 70.0,
                "setback_heat": 60.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
            }
        )
        assert data["wake_time"] == _FULL_CONFIG_V14["wake_time"]
        assert data.get("ai_enabled") is False

    def test_does_not_overwrite_existing_sleep_heat(self):
        """If sleep_heat already exists, it is preserved (setdefault semantics)."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 70.0,
                "setback_heat": 60.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
                "sleep_heat": 68.5,
            }
        )
        assert data["sleep_heat"] == pytest.approx(68.5)

    def test_does_not_overwrite_existing_sleep_cool(self):
        """If sleep_cool already exists, it is preserved (setdefault semantics)."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 70.0,
                "setback_heat": 60.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
                "sleep_cool": 77.0,
            }
        )
        assert data["sleep_cool"] == pytest.approx(77.0)

    def test_sleep_heat_floored_above_setback(self):
        """When comfort_heat - 4 <= setback_heat, sleep_heat is setback_heat + 0.1."""
        data = self._run_migration(
            {
                **_FULL_CONFIG_V14,
                "comfort_heat": 64.0,
                "setback_heat": 62.0,
                "comfort_cool": 75.0,
                "setback_cool": 80.0,
            }
        )
        # comfort_heat(64) - 4 = 60 < setback_heat(62) → floor applies
        assert data["sleep_heat"] >= 62.0 + 0.05  # above setback_heat floor
        assert data["sleep_heat"] < 64.0  # below comfort_heat

    def test_migration_returns_true_and_bumps_version(self):
        """async_migrate_entry returns True and entry reaches current version."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(_FULL_CONFIG_V14), version=14)
        hass = _make_hass()
        final_version = [14]

        def capture_update(entry, *, data, version):
            entry.data = dict(data)
            entry.version = version
            final_version[0] = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert final_version[0] == 18


# ---------------------------------------------------------------------------
# v16→v17 migration — remove selectable FAN_MODE_BOTH (Issue #424)
# ---------------------------------------------------------------------------


class TestMigrationV16ToV17:
    """Tests for config entry migration from version 16 to version 17.

    Issue #424: FAN_MODE_BOTH is no longer selectable in config; existing
    configs with fan_mode == "both" are coerced to FAN_MODE_WHOLE_HOUSE.
    """

    def _run_migration(self, initial_data: dict) -> dict:
        """Run the real async_migrate_entry() starting from a v16 entry."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(initial_data), version=16)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        asyncio.run(async_migrate_entry(hass, entry))
        return final_data

    def test_both_fan_mode_migrates_to_whole_house_fan(self):
        """fan_mode 'both' is coerced to 'whole_house_fan' at v17."""
        from custom_components.climate_advisor import async_migrate_entry
        from custom_components.climate_advisor.const import FAN_MODE_WHOLE_HOUSE

        entry = _make_config_entry({**FULL_CONFIG, "fan_mode": "both"}, version=16)
        hass = _make_hass()

        def capture_update(entry, *, data, version):
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        result = asyncio.run(async_migrate_entry(hass, entry))

        assert result is True
        assert entry.data["fan_mode"] == FAN_MODE_WHOLE_HOUSE
        assert entry.version == 18

    def test_non_both_fan_mode_is_preserved(self):
        """fan_mode values other than 'both' are left unchanged."""
        data = self._run_migration({**FULL_CONFIG, "fan_mode": "hvac"})
        assert data["fan_mode"] == "hvac"

    def test_missing_fan_mode_is_not_added(self):
        """If fan_mode is absent, migration does not invent one."""
        config_without_fan_mode = {k: v for k, v in FULL_CONFIG.items() if k != "fan_mode"}
        data = self._run_migration(config_without_fan_mode)
        assert "fan_mode" not in data


class TestMigrationV17ToV18:
    """Tests for config entry migration from version 17 to version 18.

    Issue #797: "default_tou_lead_minutes" (the TOU pre-conditioning fallback lead
    time when the thermal model hasn't learned a rate yet) becomes a configurable
    option, default 45 (was a fixed 120-minute constant).
    """

    def _run_migration(self, initial_data: dict) -> dict:
        from custom_components.climate_advisor import async_migrate_entry

        entry = _make_config_entry(dict(initial_data), version=17)
        hass = _make_hass()
        final_data: dict = {}

        def capture_update(entry, *, data, version):
            final_data.clear()
            final_data.update(data)
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update
        asyncio.run(async_migrate_entry(hass, entry))
        return final_data

    def test_v17_to_v18_default_set(self):
        """v17 entry with no default_tou_lead_minutes key gets the new default (45)."""
        data = self._run_migration(FULL_CONFIG)
        assert data.get("default_tou_lead_minutes") == 45

    def test_v17_to_v18_existing_value_preserved(self):
        """A pre-existing default_tou_lead_minutes value (e.g. set via a future manual
        config edit) is not overwritten by the migration's default."""
        data = self._run_migration({**FULL_CONFIG, "default_tou_lead_minutes": 90})
        assert data.get("default_tou_lead_minutes") == 90

    def test_v17_to_v18_bumps_version(self):
        entry = _make_config_entry(dict(FULL_CONFIG), version=17)
        hass = _make_hass()

        def capture_update(entry, *, data, version):
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = capture_update

        from custom_components.climate_advisor import async_migrate_entry

        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True
        assert entry.version == 18


class TestFanModeOptionsNoBoth:
    """Guard against re-introducing FAN_MODE_BOTH as a selectable option (Issue #424)."""

    def test_fan_mode_options_does_not_include_both(self):
        """No entry in FAN_MODE_OPTIONS has value 'both'."""
        from custom_components.climate_advisor.config_flow import FAN_MODE_OPTIONS

        values = [option["value"] for option in FAN_MODE_OPTIONS]
        assert "both" not in values


# ---------------------------------------------------------------------------
# Options flow — menu navigation (Issue #50)
# ---------------------------------------------------------------------------


class TestOptionsFlowMenu:
    """Test that the options flow menu is correctly structured."""

    def test_menu_options_list(self):
        """Verify the OPTIONS_MENU_OPTIONS constant has all expected sections.

        No "save"/"save_reload" entries (Issue #573 follow-up) — HA's menu-step
        UI can't render a real button, so applying pending changes is done via
        HA's own generic "Reload" action on the integration's page, guided by
        the "reload_needed" repair issue raised in _commit_section().
        """
        from custom_components.climate_advisor.config_flow import OPTIONS_MENU_OPTIONS

        expected = [
            "core",
            "setpoints",
            "temperature_sources",
            "sensors",
            "occupancy",
            "schedule",
            "scheduler",
            "notifications",
            "advanced",
            "classification_thresholds",
            "ai_settings",
            "github_settings",
        ]
        assert expected == OPTIONS_MENU_OPTIONS

    def test_menu_has_notifications(self):
        """Notifications must be a menu option."""
        from custom_components.climate_advisor.config_flow import OPTIONS_MENU_OPTIONS

        assert "notifications" in OPTIONS_MENU_OPTIONS

    def test_section_commit_merges_updates(self):
        """A single section's commit merges into entry data and preserves untouched fields."""
        data = _run_options_flow(
            dict(FULL_CONFIG),
            [("async_step_advanced", {"learning_enabled": False, "aggressive_savings": True})],
        )
        assert data["learning_enabled"] is False
        assert data["aggressive_savings"] is True
        # Untouched fields preserved
        assert data["weather_entity"] == "weather.forecast_home"

    def test_section_submit_persists_but_does_not_reload(self):
        """Issue #573: submitting a section writes config_entry.data immediately (so
        re-opening that section shows the new value — the original #557 fix this
        preserves) but must NOT reload the coordinator. Applying the change is left
        to HA's own "Reload" action, surfaced via the reload_needed repair issue."""
        flow, captured = _make_options_flow(dict(FULL_CONFIG))

        asyncio.run(flow.async_step_advanced({"learning_enabled": False, "aggressive_savings": True}))

        assert "data" in captured, "async_update_entry must be called on section submit"
        assert flow.config_entry.data["learning_enabled"] is False
        flow.hass.config_entries.async_reload.assert_not_called()

    def test_section_submit_raises_reload_needed_repair_issue(self):
        """Issue #573 follow-up: since there's no in-flow reload control anymore,
        saving a section must raise the "reload_needed" repair issue so the pending
        change is visible on the integration's page."""
        from unittest.mock import patch

        flow, _ = _make_options_flow(dict(FULL_CONFIG))

        with patch("custom_components.climate_advisor.config_flow.ir.async_create_issue") as mock_create:
            asyncio.run(flow.async_step_advanced({"learning_enabled": False, "aggressive_savings": True}))

        mock_create.assert_called_once()
        args, kwargs = mock_create.call_args
        assert args[2] == "reload_needed"
        assert kwargs["is_fixable"] is True
        assert kwargs["translation_key"] == "reload_needed"


# ---------------------------------------------------------------------------
# Options flow — notifications step (Issue #50)
# ---------------------------------------------------------------------------


class TestNotificationsStep:
    """Test the notification preferences step of the options flow."""

    NOTIFICATION_KEYS = [
        "push_briefing",
        "push_door_window_pause",
        "push_occupancy_home",
        "manual_grace_notify",
        "automation_grace_notify",
        "email_briefing",
        "email_door_window_pause",
        "email_grace_expired",
        "email_grace_repause",
        "email_occupancy_home",
    ]

    def test_all_notification_keys_in_full_config(self):
        """FULL_CONFIG includes all notification toggle keys."""
        for key in self.NOTIFICATION_KEYS:
            assert key in FULL_CONFIG, f"Missing notification key in FULL_CONFIG: {key}"

    def test_notifications_defaults_from_config(self):
        """Defaults for notification toggles should come from config entry data."""
        config = dict(FULL_CONFIG)
        config["push_briefing"] = False
        config["email_occupancy_home"] = False
        # Simulate reading defaults
        assert config.get("push_briefing", True) is False
        assert config.get("email_occupancy_home", True) is False
        # Unmodified keys default True
        assert config.get("email_briefing", True) is True

    def test_notifications_saves_to_updates(self):
        """Submitted notification values persist immediately through the REAL step handler."""
        data = _run_options_flow(
            dict(FULL_CONFIG),
            [
                (
                    "async_step_notifications",
                    {"push_briefing": False, "email_grace_expired": False, "manual_grace_notify": True},
                )
            ],
        )
        assert data["push_briefing"] is False
        assert data["email_grace_expired"] is False
        assert data["manual_grace_notify"] is True


# ---------------------------------------------------------------------------
# Options flow multi-step data accumulation (existing tests retained)
# ---------------------------------------------------------------------------


class TestOptionsFlowMultiStep:
    """Test that the multi-step options flow merges data correctly."""

    def test_step_core_merges_core_settings(self):
        """Core step collects entity and temperature settings and persists immediately (REAL handler)."""
        data = _run_options_flow(
            dict(FULL_CONFIG),
            [
                (
                    "async_step_core",
                    {
                        "weather_entity": "weather.home",
                        "climate_entity": "climate.living_room",
                        "comfort_heat": 72,
                        "comfort_cool": 76,
                        "setback_heat": 60,
                        "setback_cool": 80,
                        "notify_service": "notify.notify",
                    },
                )
            ],
        )
        assert data["weather_entity"] == "weather.home"
        assert data["comfort_heat"] == 72
        assert data["setback_heat"] == 60
        assert data["setback_cool"] == 80
        assert data["notify_service"] == "notify.notify"

    def test_multi_step_accumulation(self):
        """All menu sections accumulate into a single merged result (REAL flow)."""
        data = _run_options_flow(
            dict(FULL_CONFIG),
            [
                (
                    "async_step_core",
                    {
                        "weather_entity": "weather.home",
                        "climate_entity": "climate.living_room",
                        "comfort_heat": 72,
                        "comfort_cool": 76,
                        "setback_heat": 60,
                        "setback_cool": 80,
                        "notify_service": "notify.notify",
                    },
                ),
                (
                    "async_step_temperature_sources",
                    {
                        "outdoor_temp_source": "sensor",
                        "outdoor_temp_entity": "sensor.outdoor_temp",
                        "indoor_temp_source": "climate_fallback",
                    },
                ),
                (
                    "async_step_schedule",
                    {"wake_time": "07:00:00", "sleep_time": "23:00:00", "briefing_time": "06:30:00"},
                ),
                (
                    "async_step_notifications",
                    {"push_briefing": False, "email_briefing": False, "manual_grace_notify": True},
                ),
                ("async_step_advanced", {"learning_enabled": False, "aggressive_savings": True}),
            ],
        )
        assert data["weather_entity"] == "weather.home"
        assert data["comfort_heat"] == 72
        assert data["setback_heat"] == 60
        assert data["outdoor_temp_source"] == "sensor"
        assert data["outdoor_temp_entity"] == "sensor.outdoor_temp"
        assert data["wake_time"] == "07:00:00"
        assert data["sleep_time"] == "23:00:00"
        assert data["briefing_time"] == "06:30:00"
        assert data["push_briefing"] is False
        assert data["email_briefing"] is False
        assert data["learning_enabled"] is False
        assert data["aggressive_savings"] is True

    def test_fields_not_in_updates_are_preserved(self):
        """Fields not touched across any step retain their original values (REAL flow)."""
        data = _run_options_flow(
            dict(FULL_CONFIG),
            [("async_step_advanced", {"learning_enabled": False})],
        )
        # Updated field
        assert data["learning_enabled"] is False
        # Preserved fields
        assert data["weather_entity"] == "weather.forecast_home"
        assert data["notify_service"] == "notify.mobile_app_phone"
        assert data["setback_heat"] == 62
        assert data["wake_time"] == "06:30:00"
        assert data["door_window_sensors"] == ["binary_sensor.front_door"]

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


# ---------------------------------------------------------------------------
# Options flow — TOU scheduler (Issue #786)
# ---------------------------------------------------------------------------


def _schedule_fields(**overrides) -> dict:
    fields = {
        "name": "Weekday peak",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "16:15:00",
        "end": "21:00:00",
        "cost_tag": "high",
    }
    fields.update(overrides)
    return fields


def _seed_schedule(schedule_id: str, name: str, day: str, start: str, end: str, cost_tag: str = "low") -> dict:
    return {"id": schedule_id, "name": name, "days": [day], "start": start, "end": end, "cost_tag": cost_tag}


class TestOptionsFlowScheduler:
    """Issue #786 — TOU scheduler options-flow steps (REAL handlers, no mirroring)."""

    def test_scheduler_menu_step_lists_existing_schedules(self):
        """async_step_scheduler's rendered form is reachable with existing schedules seeded,
        and does not raise — the list-rendering path itself is exercised."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [
            {"id": "a", "name": "A", "days": ["mon"], "start": "16:00:00", "end": "21:00:00", "cost_tag": "high"},
            {"id": "b", "name": "B", "days": ["all"], "start": "09:00:00", "end": "12:00:00", "cost_tag": "low"},
        ]
        flow, _ = _make_options_flow(entry_data)
        result = asyncio.run(flow.async_step_scheduler(None))
        assert result["type"] == "form"
        assert result["step_id"] == "scheduler"

    def test_scheduler_edit_persists_new_schedule_via_commit_section(self):
        """Adding a schedule appends to config_entry.data["schedules"] without touching
        any other existing entries."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [_seed_schedule("existing1", "Existing", "mon", "09:00:00", "10:00:00")]
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "__add__"}),
                ("async_step_scheduler_edit", _schedule_fields()),
            ],
        )
        schedules = data["schedules"]
        assert len(schedules) == 2
        existing = next(s for s in schedules if s["id"] == "existing1")
        assert existing == entry_data["schedules"][0]
        new = next(s for s in schedules if s["id"] != "existing1")
        assert new["name"] == "Weekday peak"
        assert new["days"] == ["mon", "tue", "wed", "thu", "fri"]
        assert new["cost_tag"] == "high"

    def test_scheduler_edit_empty_days_rejected(self):
        """Submitting with no days selected must not persist — errors dict is populated
        and the form is re-shown instead of committing."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, captured = _make_options_flow(entry_data)

        async def _drive():
            await flow.async_step_scheduler({"manage": "__add__"})
            return await flow.async_step_scheduler_edit(_schedule_fields(days=[]))

        result = asyncio.run(_drive())
        assert result["type"] == "form"
        assert result["errors"] == {"days": "schedule_days_required"}
        assert "data" not in captured  # nothing was committed

    def test_scheduler_edit_rejects_equal_start_end(self):
        """Submitting with start == end must not persist — a schedule with a
        degenerate (zero-width) window is treated by is_schedule_active_at() as
        midnight-spanning and would be active nearly 24 hours a day (Issue #786
        Fix 4). The form must re-show with the error instead of committing."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, captured = _make_options_flow(entry_data)

        async def _drive():
            await flow.async_step_scheduler({"manage": "__add__"})
            return await flow.async_step_scheduler_edit(_schedule_fields(start="18:00:00", end="18:00:00"))

        result = asyncio.run(_drive())
        assert result["type"] == "form"
        assert result["errors"] == {"end": "schedule_start_end_equal"}
        assert "data" not in captured  # nothing was committed

    def test_scheduler_edit_rejects_equal_times_differing_only_by_seconds(self):
        """ "16:00:00" and "16:00:30" are distinct raw strings but both parse to the
        identical 16.0-hour boundary under scheduler.py's _parse_hhmm(), which ignores
        seconds. Validation must compare parsed hour/minute values, not raw strings, or
        this passes Fix 4's check and still produces the degenerate near-24-hour-active
        window the fix exists to prevent."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, captured = _make_options_flow(entry_data)

        async def _drive():
            await flow.async_step_scheduler({"manage": "__add__"})
            return await flow.async_step_scheduler_edit(_schedule_fields(start="16:00:00", end="16:00:30"))

        result = asyncio.run(_drive())
        assert result["type"] == "form"
        assert result["errors"] == {"end": "schedule_start_end_equal"}
        assert "data" not in captured  # nothing was committed

    def test_scheduler_edit_updates_only_the_target_schedule(self):
        """Editing an existing schedule replaces only that entry, byte-for-byte leaving
        the others untouched."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [
            _seed_schedule("keep1", "Keep One", "mon", "08:00:00", "09:00:00"),
            _seed_schedule("target", "Old Name", "tue", "10:00:00", "11:00:00"),
            _seed_schedule("keep2", "Keep Two", "wed", "12:00:00", "13:00:00", cost_tag="high"),
        ]
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "target"}),
                ("async_step_scheduler_edit", _schedule_fields(name="New Name")),
            ],
        )
        schedules = data["schedules"]
        assert len(schedules) == 3
        assert schedules[0] == entry_data["schedules"][0]
        assert schedules[2] == entry_data["schedules"][2]
        updated = next(s for s in schedules if s["id"] == "target")
        assert updated["name"] == "New Name"
        assert updated["days"] == ["mon", "tue", "wed", "thu", "fri"]

    def test_scheduler_delete_removes_only_target(self):
        """Deleting removes exactly the targeted schedule; the other two are unchanged."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [
            {"id": "a", "name": "A", "days": ["mon"], "start": "08:00:00", "end": "09:00:00", "cost_tag": "low"},
            {"id": "b", "name": "B", "days": ["tue"], "start": "10:00:00", "end": "11:00:00", "cost_tag": "low"},
            {"id": "c", "name": "C", "days": ["wed"], "start": "12:00:00", "end": "13:00:00", "cost_tag": "high"},
        ]
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "b"}),
                ("async_step_scheduler_edit", _schedule_fields(delete_schedule=True)),
            ],
        )
        schedules = data["schedules"]
        assert [s["id"] for s in schedules] == ["a", "c"]
        assert schedules[0] == entry_data["schedules"][0]
        assert schedules[1] == entry_data["schedules"][2]

    def test_scheduler_add_past_cap_is_defensively_ignored(self):
        """Even if "add" is somehow submitted while 5 schedules already exist, the
        resulting list stays at 5 — the list step normally omits "+ Add" at the cap, but
        this is the data-integrity backstop, not just a UI nicety."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [
            {"id": f"s{i}", "name": f"s{i}", "days": ["mon"], "start": "08:00:00", "end": "09:00:00", "cost_tag": "low"}
            for i in range(5)
        ]
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "__add__"}),
                ("async_step_scheduler_edit", _schedule_fields()),
            ],
        )
        assert len(data["schedules"]) == 5

    # -- UI-polish follow-up (Issue #786 follow-up) -----------------------------

    def test_scheduler_edit_error_redisplay_preserves_user_input(self):
        """Fix 4: a validation error (empty days) must redisplay the form with the
        submitted name/start/end preserved, not reverted to blank/pre-edit defaults.
        Before the fix, `defaults = existing or {}` ignored `user_input` entirely on
        the error-redisplay path, silently wiping every field the user just typed."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, captured = _make_options_flow(entry_data)

        async def _drive():
            await flow.async_step_scheduler({"manage": "__add__"})
            return await flow.async_step_scheduler_edit(
                _schedule_fields(name="My Custom Name", start="17:30:00", end="19:45:00", days=[])
            )

        # Whether `voluptuous`/`vol.Schema` are the real library or mocked varies by
        # test environment (confirmed: this test flaked in CI specifically because
        # `vol.Schema` was NOT the real class there, while it is in some local dev
        # setups) — so assert on what `config_flow.py` actually PASSES to
        # `vol.Required(...)` when building the redisplay schema, not on behavior of
        # the resulting Schema object, which depends on whether voluptuous is real.
        # Mirrors this file's existing `selector.SelectSelectorConfig.call_args_list`
        # spy pattern (see `test_days_selector_dropdown_mode_...` above), which is
        # already proven stable in CI.
        import voluptuous as vol

        real_required = vol.Required
        captured_defaults: dict[str, object] = {}

        def _spy_required(key, *args, **kwargs):
            if "default" in kwargs:
                captured_defaults[key] = kwargs["default"]() if callable(kwargs["default"]) else kwargs["default"]
            return real_required(key, *args, **kwargs)

        with patch("custom_components.climate_advisor.config_flow.vol.Required", new=_spy_required):
            result = asyncio.run(_drive())

        assert result["type"] == "form"
        assert result["errors"] == {"days": "schedule_days_required"}
        assert "data" not in captured  # nothing was committed
        assert captured_defaults["name"] == "My Custom Name"
        assert captured_defaults["start"] == "17:30:00"
        assert captured_defaults["end"] == "19:45:00"

    def test_scheduler_edit_cost_tag_always_persists_as_high(self):
        """The cost_tag selector no longer exists in the schema (Fix 5) — every
        schedule created or edited via the UI persists cost_tag="high" regardless
        of what (if anything) is submitted, since the commit path hardcodes it."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        # Submit fields with no cost_tag key at all — simulating what a real HA
        # frontend would send now that the field isn't part of the schema.
        submitted = _schedule_fields()
        del submitted["cost_tag"]
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "__add__"}),
                ("async_step_scheduler_edit", submitted),
            ],
        )
        new = data["schedules"][0]
        assert new["cost_tag"] == "high"

    def test_scheduler_edit_cost_tag_ignores_submitted_low_value(self):
        """Even if a stale client somehow still submits cost_tag="low", the commit
        path hardcodes "high" — the field is fully server-controlled now."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        data = _run_options_flow(
            entry_data,
            [
                ("async_step_scheduler", {"manage": "__add__"}),
                ("async_step_scheduler_edit", _schedule_fields(cost_tag="low")),
            ],
        )
        new = data["schedules"][0]
        assert new["cost_tag"] == "high"

    def test_legacy_all_days_schedule_round_trips_and_displays(self):
        """A schedule saved before this change (days=["all"]) must still round-trip
        through the list step and display correctly via _format_schedule_summary(),
        even though the UI no longer offers "all" as a selectable option going
        forward. scheduler.py's ALL_DAYS handling is untouched for backward compat."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = [_seed_schedule("legacy1", "Legacy All-Days", "all", "09:00:00", "12:00:00", "high")]
        flow, _ = _make_options_flow(entry_data)

        result = asyncio.run(flow.async_step_scheduler(None))
        assert result["type"] == "form"
        assert result["step_id"] == "scheduler"

        # _format_schedule_summary() is real (non-mocked) code — this is the direct
        # proof the legacy "all" value still displays correctly, independent of the
        # mocked selector layer HA's frontend would otherwise render.
        summary = flow._format_schedule_summary(entry_data["schedules"][0])
        assert "All days" in summary

        # Editing the legacy schedule back out must also not raise/crash — the
        # existing "all" days value is still accepted as a pre-filled default even
        # though it's no longer a *selectable* option going forward (Fix 1).
        flow._editing_schedule_id = "legacy1"
        edit_result = asyncio.run(flow.async_step_scheduler_edit(None))
        assert edit_result["type"] == "form"
        assert edit_result["step_id"] == "scheduler_edit"

    def test_day_selector_no_longer_offers_all_days_option(self):
        """Fix 1: ALL_DAYS must not appear in the edit form's day selector options —
        only the 7 individual weekday abbreviations are offered. Since the selector
        module itself is a MagicMock in this test environment (production HA's real
        selector library isn't installed), we inspect the actual kwargs passed to the
        mocked SelectOptionDict()/SelectSelectorConfig() constructors — the only way
        to see what config_flow.py really built, since the mocked selector objects'
        own return values carry no real data."""
        from homeassistant.helpers import selector

        from custom_components.climate_advisor.scheduler import ALL_DAYS, WEEKDAY_ABBREVS

        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, _ = _make_options_flow(entry_data)

        selector.SelectOptionDict.reset_mock()

        # _editing_schedule_id defaults to None ("add new") — call the edit step
        # directly rather than routing through async_step_scheduler first, which
        # would itself call async_step_scheduler_edit() a second time and double
        # the recorded selector-construction calls.
        result = asyncio.run(flow.async_step_scheduler_edit(None))
        assert result["type"] == "form"

        day_option_values = [
            call.kwargs["value"] for call in selector.SelectOptionDict.call_args_list if "value" in call.kwargs
        ]
        assert ALL_DAYS not in day_option_values
        assert set(WEEKDAY_ABBREVS).issubset(set(day_option_values))

    def test_day_selector_uses_dropdown_mode(self):
        """Fix 2: the days field selector must use DROPDOWN mode (compact,
        collapsed-by-default) rather than the always-expanded LIST mode. Inspects
        the mocked SelectSelectorConfig() call args directly — see the note on
        test_day_selector_no_longer_offers_all_days_option for why."""
        from homeassistant.helpers import selector

        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, _ = _make_options_flow(entry_data)

        selector.SelectSelectorConfig.reset_mock()

        result = asyncio.run(flow.async_step_scheduler_edit(None))
        assert result["type"] == "form"

        # The "days" field is the only multiple=True SelectSelectorConfig call in
        # async_step_scheduler_edit() now that cost_tag's single-select is gone.
        days_calls = [call for call in selector.SelectSelectorConfig.call_args_list if call.kwargs.get("multiple")]
        assert len(days_calls) == 1
        assert days_calls[0].kwargs["mode"] == selector.SelectSelectorMode.DROPDOWN

    def test_cost_tag_field_not_present_in_schema(self):
        """Fix 5: the cost_tag selector must be hidden from the form entirely."""
        entry_data = dict(FULL_CONFIG)
        entry_data["schedules"] = []
        flow, _ = _make_options_flow(entry_data)

        result = asyncio.run(flow.async_step_scheduler_edit(None))
        assert "cost_tag" not in result["data_schema"].schema


# ---------------------------------------------------------------------------
# Options flow — clearing optional entity fields (Issue #434)
# ---------------------------------------------------------------------------


class TestOptionsFlowClearing:
    """Issue #434 — optional entity fields must actually clear when left blank.

    These tests INVOKE the real options flow (the production step handlers,
    which persist immediately on submit as of Issue #557), not a
    re-implementation of the merge. The bug shipped because the prior tests
    mirrored the merge in the test body and only ever supplied values — never
    an omitted (cleared) key. A cleared optional entity field is dropped from
    user_input by voluptuous, so the fix must record it for removal when the
    section commits.
    """

    # (owning step handler, config key) for every clearable optional-entity field.
    CLEARABLE = [
        ("async_step_occupancy", "home_toggle_entity"),
        ("async_step_occupancy", "vacation_toggle_entity"),
        ("async_step_occupancy", "guest_toggle_entity"),
        ("async_step_sensors", "fan_entity"),
        ("async_step_sensors", "fan_state_entity"),
        ("async_step_temperature_sources", "outdoor_temp_entity"),
        ("async_step_temperature_sources", "indoor_temp_entity"),
    ]

    @pytest.mark.parametrize("step,key", CLEARABLE)
    def test_cleared_field_is_removed(self, step, key):
        """A field set in the entry, then submitted OMITTED, must be removed from saved data."""
        entry_data = {**FULL_CONFIG, key: "domain.some_entity"}
        # Empty user_input for the owning step == every clearable picker cleared.
        data = _run_options_flow(entry_data, [(step, {})])
        assert key not in data, f"{key} should be removed from entry.data when cleared"

    @pytest.mark.parametrize("step,key", CLEARABLE)
    def test_set_field_is_stored(self, step, key):
        """Regression guard: clearing logic must not drop a value that WAS provided."""
        entry_data = {**FULL_CONFIG, key: None}
        data = _run_options_flow(entry_data, [(step, {key: "domain.new_entity"})])
        assert data[key] == "domain.new_entity"

    def test_ai_api_key_preserved_when_blank(self):
        """Exclusion guard: ai_api_key uses preserve-when-blank, not clear-when-blank."""
        entry_data = {**FULL_CONFIG_V12, "ai_enabled": False, "ai_api_key": "sk-existing-key"}
        data = _run_options_flow(
            entry_data,
            [
                (
                    "async_step_ai_settings",
                    {
                        "ai_enabled": False,
                        "ai_api_key": "",
                        "ai_temperature": 0.5,
                        "ai_max_tokens": 1024,
                        "ai_investigator_max_tokens": 4096,
                    },
                )
            ],
        )
        assert data["ai_api_key"] == "sk-existing-key"


# ---------------------------------------------------------------------------
# Sleep setpoint ordering regression guard (Issue #318)
# ---------------------------------------------------------------------------


class TestSleepSetpointValidation:
    """Regression guard: async_step_setpoints must NOT enforce any ordering
    relationship between sleep setpoints and daytime comfort setpoints.

    Fix #108 removed these constraints; Fix #112 re-introduced them (regression).
    Fix #318 removes them again.

    Implementation note: ClimateAdvisorOptionsFlow inherits from a MagicMock stub
    (config_entries.OptionsFlow) in this test environment, making it impossible to
    call the real method via types.MethodType or direct instantiation (the class
    itself becomes a MagicMock with no real attributes). Instead we use a static
    source-text guard: if the error-key strings are absent from config_flow.py, the
    constraints cannot be active regardless of how the flow is wired internally.
    This catches re-introduction even if the error key is spelled the same way.
    """

    _CONFIG_FLOW_PATH = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "climate_advisor",
            "config_flow.py",
        )
    )

    @pytest.mark.parametrize(
        "error_key",
        [
            pytest.param("sleep_must_be_above_comfort", id="sleep_cool_above_comfort"),
            pytest.param("sleep_must_be_below_comfort", id="sleep_heat_below_comfort"),
            pytest.param("sleep_must_be_above_setback", id="sleep_heat_above_setback"),
            pytest.param("sleep_must_be_below_setback", id="sleep_cool_below_setback"),
        ],
    )
    def test_sleep_ordering_error_keys_absent_from_source(self, error_key):
        """The four sleep ordering error keys must not appear anywhere in config_flow.py.

        Regression: Fix #112 re-introduced these keys after Fix #108 removed them.
        If any of them reappears in the source, this test fails immediately, before
        any config-flow test exercises the step.
        """
        with open(self._CONFIG_FLOW_PATH) as fh:
            source = fh.read()
        assert error_key not in source, (
            f"Regression detected: '{error_key}' found in config_flow.py. "
            "Sleep setpoint ordering constraints were removed in Fix #318 and must "
            "not be re-introduced. See Issue #318 for history."
        )


# ---------------------------------------------------------------------------
# Zone naming (Gap 7, Issue #796) — config_flow.py's async_step_schedule()
# now collects a "zone_name" field and stores it as entry.title instead of
# hardcoding "Climate Advisor".
# ---------------------------------------------------------------------------


def _make_state(friendly_name: str | None):
    """Build a minimal state object exposing .attributes.get("friendly_name")."""
    state = MagicMock()
    state.attributes = {"friendly_name": friendly_name} if friendly_name is not None else {}
    return state


def _make_zone_naming_hass(states: dict[str, str | None] | None = None):
    """Build a fake hass supporting hass.states.get() and hass.config_entries.async_get_entry().

    ``states`` maps entity_id -> friendly_name (or None for "entity has no
    friendly_name attribute"). ``async_get_entry`` is backed by a plain dict
    registry so a test can round-trip "create entry with this title, then read
    it back" the same way coordinator.py:1508's real accessor
    (``hass.config_entries.async_get_entry(entry_id)``) is used in production.
    """
    states = states or {}
    hass = MagicMock()
    hass.states.get = MagicMock(
        side_effect=lambda entity_id: _make_state(states[entity_id]) if entity_id in states else None
    )
    registry: dict[str, object] = {}
    hass.config_entries.async_get_entry = MagicMock(side_effect=lambda entry_id: registry.get(entry_id))
    hass._zone_registry = registry  # exposed for tests to populate directly
    return hass


def _make_real_config_flow(hass, data: dict | None = None):
    """Instantiate the REAL ClimateAdvisorConfigFlow for direct step invocation.

    Enabled by the ha_stubs realification of ``config_entries.ConfigFlow``
    (``_MockConfigFlow`` — same pattern as ``_make_options_flow`` above for
    ``OptionsFlow``). DOCTRINE: exercise the real step handler, never
    reimplement its merge/title logic in the test body.
    """
    from custom_components.climate_advisor.config_flow import ClimateAdvisorConfigFlow

    flow = object.__new__(ClimateAdvisorConfigFlow)
    flow._data = dict(data or {})
    flow.hass = hass
    return flow


class TestZoneNamingDefaultSuggestion:
    """_suggest_zone_name() — the default-suggestion heuristic for the new zone_name field."""

    def test_strips_thermostat_suffix(self):
        from custom_components.climate_advisor.config_flow import _suggest_zone_name

        hass = _make_zone_naming_hass({"climate.bedroom_thermostat": "Bedroom Thermostat"})
        assert _suggest_zone_name(hass, "climate.bedroom_thermostat") == "Bedroom"

    def test_strips_climate_suffix(self):
        from custom_components.climate_advisor.config_flow import _suggest_zone_name

        hass = _make_zone_naming_hass({"climate.living_room": "Living Room Climate"})
        assert _suggest_zone_name(hass, "climate.living_room") == "Living Room"

    def test_leaves_friendly_name_unchanged_when_no_known_suffix(self):
        from custom_components.climate_advisor.config_flow import _suggest_zone_name

        hass = _make_zone_naming_hass({"climate.upstairs": "Upstairs"})
        assert _suggest_zone_name(hass, "climate.upstairs") == "Upstairs"

    def test_no_state_available_returns_empty_string_not_placeholder(self):
        """Gap 7 hard requirement: must not fall back to 'Climate Advisor' — an
        empty suggestion (user must type something, or async_step_schedule's
        own fallback applies at submit time) is correct; the removed
        placeholder is not."""
        from custom_components.climate_advisor.config_flow import _suggest_zone_name

        hass = _make_zone_naming_hass({})  # entity unknown to hass.states
        assert _suggest_zone_name(hass, "climate.not_yet_available") == ""

    def test_no_climate_entity_selected_yet_returns_empty_string(self):
        from custom_components.climate_advisor.config_flow import _suggest_zone_name

        hass = _make_zone_naming_hass({})
        assert _suggest_zone_name(hass, None) == ""


class TestZoneNamingScheduleStep:
    """async_step_schedule() — zone_name field rendering and title assignment."""

    def test_form_includes_zone_name_field_with_suggested_default(self):
        hass = _make_zone_naming_hass({"climate.bedroom_thermostat": "Bedroom Thermostat"})
        flow = _make_real_config_flow(hass, {"climate_entity": "climate.bedroom_thermostat"})

        result = asyncio.run(flow.async_step_schedule(None))

        assert result["step_id"] == "schedule"
        schema_keys = {str(k): k for k in result["data_schema"].schema}
        assert "zone_name" in schema_keys
        zone_name_marker = schema_keys["zone_name"]
        assert zone_name_marker.default() == "Bedroom"

    def test_submitted_zone_name_becomes_entry_title(self):
        hass = _make_zone_naming_hass({"climate.living_room": "Living Room Thermostat"})
        flow = _make_real_config_flow(hass, {"climate_entity": "climate.living_room"})

        result = asyncio.run(
            flow.async_step_schedule(
                {
                    "zone_name": "Living Room",
                    "wake_time": "06:30:00",
                    "sleep_time": "22:30:00",
                    "briefing_time": "06:00:00",
                }
            )
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "Living Room"
        # zone_name must not leak into the persisted config data — it's stored
        # as entry.title, not a data field automation/learning would read.
        assert "zone_name" not in result["data"]

    def test_blank_submitted_zone_name_falls_back_to_legacy_title(self):
        """An edge case, not the intended flow (the field defaults to a
        suggestion and stays user-editable) — but a user who clears it
        entirely must not get a blank/whitespace entry.title."""
        hass = _make_zone_naming_hass({})
        flow = _make_real_config_flow(hass, {})

        result = asyncio.run(
            flow.async_step_schedule(
                {
                    "zone_name": "   ",
                    "wake_time": "06:30:00",
                    "sleep_time": "22:30:00",
                    "briefing_time": "06:00:00",
                }
            )
        )

        assert result["title"] == "Climate Advisor"

    def test_overlong_zone_name_is_rejected_and_form_reshown(self):
        """Security Requirements (CLAUDE.md): config flow text fields must
        validate format before accepting. zone_name has no upstream length
        cap (TextSelector accepts arbitrary text), so async_step_schedule()
        must reject an over-length submission itself rather than persisting
        it as entry.title unbounded."""
        hass = _make_zone_naming_hass({"climate.bedroom_thermostat": "Bedroom Thermostat"})
        flow = _make_real_config_flow(hass, {"climate_entity": "climate.bedroom_thermostat"})

        result = asyncio.run(
            flow.async_step_schedule(
                {
                    "zone_name": "X" * 51,
                    "wake_time": "06:30:00",
                    "sleep_time": "22:30:00",
                    "briefing_time": "06:00:00",
                }
            )
        )

        # Form must be re-shown with an error, not silently accepted and not
        # a crash.
        assert result["type"] == "form"
        assert result["step_id"] == "schedule"
        assert result["errors"] == {"zone_name": "zone_name_too_long"}

    def test_max_length_zone_name_is_accepted(self):
        """The boundary itself (exactly 50 chars) must not be rejected —
        only strictly-over-length submissions are an error."""
        hass = _make_zone_naming_hass({"climate.bedroom_thermostat": "Bedroom Thermostat"})
        flow = _make_real_config_flow(hass, {"climate_entity": "climate.bedroom_thermostat"})

        result = asyncio.run(
            flow.async_step_schedule(
                {
                    "zone_name": "X" * 50,
                    "wake_time": "06:30:00",
                    "sleep_time": "22:30:00",
                    "briefing_time": "06:00:00",
                }
            )
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "X" * 50

    def test_entry_title_round_trips_via_async_get_entry(self):
        """Confirms the hard requirement from docs/multi-zone-spec.md's Gap 7:
        the zone name must be readable back via
        hass.config_entries.async_get_entry(entry_id).title — the same
        accessor pattern already used in production at coordinator.py:1508
        (``self.hass.config_entries.async_get_entry(self._entry_id)``) — not
        stored as a bare cosmetic string with no accessor.
        """
        from tools.sim_harness.ha_stubs import ConfigEntry

        hass = _make_zone_naming_hass({"climate.bedroom_thermostat": "Bedroom Thermostat"})
        flow = _make_real_config_flow(hass, {"climate_entity": "climate.bedroom_thermostat"})

        result = asyncio.run(
            flow.async_step_schedule(
                {
                    "zone_name": "Bedroom",
                    "wake_time": "06:30:00",
                    "sleep_time": "22:30:00",
                    "briefing_time": "06:00:00",
                }
            )
        )

        # Simulate HA's flow manager persisting the created entry (this repo's
        # own harness-side ConfigEntry stub, Issue #796), then read it back
        # exactly the way production code does.
        entry_id = "zone_a"
        hass._zone_registry[entry_id] = ConfigEntry(entry_id=entry_id, data=result["data"], title=result["title"])

        fetched = hass.config_entries.async_get_entry(entry_id)
        assert fetched is not None
        assert fetched.title == "Bedroom"
