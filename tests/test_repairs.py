"""Tests for the weather entity repairs flow and setup-time auto-resolution.

Covers:
- Fixable repair issue creation when weather entity is missing
- Auto-resolution at setup time when exactly one weather entity exists
- Repair issue creation when auto-resolution is ambiguous (2+ entities)
- WeatherEntityRepairFlow: form display and entity selection
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.repairs import (
    ReloadNeededRepairFlow,
    WeatherEntityRepairFlow,
    async_create_fix_flow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry(data: dict, version: int = 7) -> MagicMock:
    """Create a mock ConfigEntry."""
    entry = MagicMock()
    entry.data = dict(data)
    entry.entry_id = "test_entry_id"
    entry.version = version
    return entry


def _make_hass(weather_entities: list[str] | None = None) -> MagicMock:
    """Create a minimal mock hass for repair tests.

    Args:
        weather_entities: entity IDs that exist in HA. If None, no entities.
    """
    hass = MagicMock()
    existing = set(weather_entities or [])

    def mock_states_get(entity_id):
        if entity_id in existing:
            state = MagicMock()
            state.entity_id = entity_id
            return state
        return None

    hass.states.get = mock_states_get

    # async_all returns list of state objects for a domain
    def mock_async_all(domain=None):
        if domain == "weather":
            return [MagicMock(entity_id=eid) for eid in existing if eid.startswith("weather.")]
        return []

    hass.states.async_all = mock_async_all
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_reload = MagicMock()
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
}


# ---------------------------------------------------------------------------
# async_create_fix_flow
# ---------------------------------------------------------------------------


class TestAsyncCreateFixFlow:
    """Test the HA entry point for repair flows."""

    def test_returns_weather_repair_flow(self):
        hass = _make_hass()
        flow = asyncio.run(async_create_fix_flow(hass, "weather_entity_not_found", None))
        assert isinstance(flow, WeatherEntityRepairFlow)

    def test_returns_confirm_flow_for_unknown_issue(self):
        from homeassistant.components.repairs import ConfirmRepairFlow

        hass = _make_hass()
        flow = asyncio.run(async_create_fix_flow(hass, "some_other_issue", None))
        assert isinstance(flow, ConfirmRepairFlow)
        assert not isinstance(flow, WeatherEntityRepairFlow)

    def test_returns_reload_needed_repair_flow(self):
        hass = _make_hass()
        flow = asyncio.run(async_create_fix_flow(hass, "reload_needed", None))
        assert isinstance(flow, ReloadNeededRepairFlow)


# ---------------------------------------------------------------------------
# ReloadNeededRepairFlow (Issue #573 follow-up)
# ---------------------------------------------------------------------------


class TestReloadNeededRepairFlow:
    """Confirming this repair reloads Climate Advisor and clears the issue."""

    def test_shows_form_when_no_input(self):
        flow = ReloadNeededRepairFlow()
        flow.hass = _make_hass()

        result = asyncio.run(flow.async_step_init(user_input=None))

        assert result["type"] == "form"
        assert result["step_id"] == "init"

    def test_confirm_reloads_entry_and_clears_issue(self):
        flow = ReloadNeededRepairFlow()
        hass = _make_hass()
        entry = _make_config_entry(FULL_CONFIG)
        hass.config_entries.async_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_reload = AsyncMock()
        flow.hass = hass

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue") as mock_delete:
            result = asyncio.run(flow.async_step_init(user_input={}))

        assert result["type"] == "create_entry"
        hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)
        mock_delete.assert_called_once_with(hass, "climate_advisor", "reload_needed")

    def test_no_config_entries_graceful(self):
        """If no config entries exist, flow completes without error (no reload attempted)."""
        flow = ReloadNeededRepairFlow()
        hass = _make_hass()
        hass.config_entries.async_entries = MagicMock(return_value=[])
        flow.hass = hass

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue"):
            result = asyncio.run(flow.async_step_init(user_input={}))

        assert result["type"] == "create_entry"
        hass.config_entries.async_reload.assert_not_called()


# ---------------------------------------------------------------------------
# Setup-time auto-resolution (tests the __init__.py logic)
# ---------------------------------------------------------------------------


class TestSetupAutoResolution:
    """Test auto-resolution of stale weather entity at setup time."""

    def test_auto_resolves_when_one_weather_entity_exists(self):
        """If exactly one weather entity exists, _resolve_weather_entity returns it."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.home"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result == "weather.home"

    def test_returns_none_when_no_weather_entities(self):
        """If no weather entities exist, returns None (ambiguous)."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass([])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result is None

    def test_returns_none_when_multiple_weather_entities(self):
        """If 2+ weather entities exist, returns None (ambiguous)."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.home", "weather.openweathermap"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result is None

    def test_returns_configured_when_it_exists(self):
        """If the configured entity still exists, returns it unchanged."""
        from custom_components.climate_advisor.__init__ import _resolve_weather_entity

        hass = _make_hass(["weather.forecast_home", "weather.other"])
        result = _resolve_weather_entity(hass, "weather.forecast_home")
        assert result == "weather.forecast_home"


# ---------------------------------------------------------------------------
# WeatherEntityRepairFlow
# ---------------------------------------------------------------------------


class TestWeatherEntityRepairFlow:
    """Test the repair flow step logic."""

    def test_shows_form_when_no_input(self):
        """First call shows entity selector form."""
        flow = WeatherEntityRepairFlow()
        flow.hass = _make_hass(["weather.home"])

        result = asyncio.run(flow.async_step_init(user_input=None))

        assert result["type"] == "form"
        assert result["step_id"] == "init"

    def test_shows_form_when_empty_dict_input(self):
        """Empty dict from repairs websocket API shows form instead of KeyError."""
        flow = WeatherEntityRepairFlow()
        flow.hass = _make_hass(["weather.home"])

        result = asyncio.run(flow.async_step_init(user_input={}))

        assert result["type"] == "form"
        assert result["step_id"] == "init"

    def test_updates_config_on_valid_selection(self):
        """Selecting a valid entity updates config, deletes issue, reloads."""
        flow = WeatherEntityRepairFlow()
        hass = _make_hass(["weather.home"])
        entry = _make_config_entry(FULL_CONFIG)
        hass.config_entries.async_entries = MagicMock(return_value=[entry])
        flow.hass = hass

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue") as mock_delete:
            result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.home"}))

        assert result["type"] == "create_entry"
        # Verify config entry was updated
        hass.config_entries.async_update_entry.assert_called_once()
        call_kwargs = hass.config_entries.async_update_entry.call_args
        new_data = call_kwargs[1]["data"] if "data" in call_kwargs[1] else call_kwargs[0][1]
        assert new_data["weather_entity"] == "weather.home"
        # Verify issue was deleted
        mock_delete.assert_called_once()
        # Verify integration reload was deferred via async_create_task
        hass.async_create_task.assert_called_once()

    def test_shows_error_on_invalid_entity(self):
        """Selecting an entity that doesn't exist shows error."""
        flow = WeatherEntityRepairFlow()
        flow.hass = _make_hass([])  # No entities exist

        result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.nonexistent"}))

        assert result["type"] == "form"
        assert result["errors"]["weather_entity"] == "entity_not_found"

    def test_no_config_entries_graceful(self):
        """If no config entries exist, flow completes without error."""
        flow = WeatherEntityRepairFlow()
        hass = _make_hass(["weather.home"])
        hass.config_entries.async_entries = MagicMock(return_value=[])
        flow.hass = hass

        result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.home"}))

        assert result["type"] == "create_entry"
        hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# zone_resolution_ambiguous Repairs issue (Issue #796 Transitional Safety
# Window)
#
# Drives the REAL async_setup_entry()/async_unload_entry() via
# build_headless_multi_zone() — per this project's no-mirror-tests doctrine
# (CLAUDE.md) — rather than re-implementing the len(hass.data[DOMAIN]) > 1 /
# <= 1 threshold checks in the test body. ir.async_create_issue/
# async_delete_issue are patched per-test (matching this file's existing
# WeatherEntityRepairFlow/ReloadNeededRepairFlow pattern above) because
# homeassistant.helpers.issue_registry is a single shared MagicMock module
# for the whole test process — patching scopes the call-count assertions to
# just this test's setup/unload calls instead of accumulating across the
# full suite.
# ---------------------------------------------------------------------------


def _zone_resolution_ambiguous_calls(mock_create_or_delete) -> list:
    """Filter a mocked ir.async_create_issue/async_delete_issue call list down to
    calls that named the zone_resolution_ambiguous issue_id specifically —
    other issues (e.g. weather_entity_not_found) may also fire during the same
    setup/unload and must not be mistaken for this one."""
    return [c for c in mock_create_or_delete.call_args_list if "zone_resolution_ambiguous" in c.args]


class TestZoneResolutionAmbiguousIssue:
    """Issue #796: ambiguous-zone-selection Repairs issue lifecycle."""

    def test_raised_when_second_zone_is_set_up(self):
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        with patch("custom_components.climate_advisor.ir.async_create_issue") as mock_create:
            zones, _fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        calls = _zone_resolution_ambiguous_calls(mock_create)
        assert len(calls) == 1, f"expected exactly one zone_resolution_ambiguous raise, got {calls}"
        # is_fixable=False, WARNING severity — informational only, nothing to configure.
        kwargs = calls[0].kwargs
        assert kwargs["is_fixable"] is False
        from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

        assert kwargs["severity"] == ir.IssueSeverity.WARNING

    def test_not_raised_for_single_zone(self):
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        with patch("custom_components.climate_advisor.ir.async_create_issue") as mock_create:
            zones, _fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)

        assert len(zones) == 1
        assert _zone_resolution_ambiguous_calls(mock_create) == []

    def test_cleared_when_unloaded_back_to_one_zone(self):
        from custom_components.climate_advisor import async_unload_entry
        from tools.sim_harness._loop import run_coro
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        entry_to_unload = zones["zone_1"]["entry"]

        with patch("custom_components.climate_advisor.ir.async_delete_issue") as mock_delete:
            run_coro(async_unload_entry(fake_hass, entry_to_unload))

        calls = _zone_resolution_ambiguous_calls(mock_delete)
        assert len(calls) == 1, f"expected exactly one zone_resolution_ambiguous clear, got {calls}"

    def test_not_cleared_while_more_than_one_zone_remains(self):
        """Three zones, unload one: two remain — the issue must stay active, not clear."""
        from custom_components.climate_advisor import async_unload_entry
        from tools.sim_harness._loop import run_coro
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=3)
        entry_to_unload = zones["zone_2"]["entry"]

        with patch("custom_components.climate_advisor.ir.async_delete_issue") as mock_delete:
            run_coro(async_unload_entry(fake_hass, entry_to_unload))

        assert _zone_resolution_ambiguous_calls(mock_delete) == []

    def test_cross_zone_isolation_unaffected_by_ambiguous_issue_lifecycle(self):
        """The Repairs issue is a domain-wide signal, not per-zone state — confirm
        raising/clearing it has no effect on a sibling zone's own coordinator state
        (the same cross_zone_isolation evaluator used for Gap 5's service-scoping
        tests, applied here to prove this new lifecycle code introduces no bleed)."""
        from tools.sim_harness._loop import run_coro
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.multi_zone_assertions import check_multi_zone_assertion

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, fake_hass, scheduler = build_headless_multi_zone(zone_count=2)

        zones["zone_1"]["coordinator"].learning._state.dismissed_suggestions = ["seeded_suggestion_key"]

        assertion = {
            "type": "cross_zone_isolation",
            "action_zone": "zone_0",
            "service": "force_reclassify",
            "service_data": {"entry_id": zones["zone_0"]["entry"].entry_id},
            "unaffected_zone": "zone_1",
            "unaffected_field": "learning._state.dismissed_suggestions",
        }
        # force_reclassify drives a REAL coordinator data-refresh cycle, which
        # reads homeassistant.util.dt.now() internally — that only resolves to
        # a real datetime (rather than a bare auto-mock) while the harness's
        # FakeScheduler is installed, which build_headless_multi_zone() only
        # keeps active for its own zone-setup phase. Re-enter it here for the
        # duration of the service call.
        with scheduler.installed():
            passed, detail = run_coro(check_multi_zone_assertion(zones, fake_hass, assertion))
        assert passed is True, detail
