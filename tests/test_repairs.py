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
        # Issue #812: entry-scoped issue_id — no entry_id was passed to the
        # flow constructor here, so it falls back to the old "first entry"
        # resolution (matching pre-#812 single-zone behavior) but still
        # deletes the NEW entry-scoped id, derived from that resolved entry.
        mock_delete.assert_called_once_with(hass, "climate_advisor", f"reload_needed_{entry.entry_id}")

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
    """Issue #796/#813: ambiguous-zone-selection Repairs issue lifecycle.

    Issue #813: this used to be raised unconditionally at zone setup, purely
    because 2+ zones were loaded — regardless of whether anything had ever
    actually hit the ambiguous fallback. That meant the Repairs card never
    went away on a multi-zone install even after #812's dashboard/API fixes
    closed off the practical guessing paths, which is exactly the "still
    seeing the ambiguous zone error" regression the user reported. It is now
    raised from inside zone_registry.get_default_coordinator() itself, at the
    moment it actually resolves an ambiguous fallback — same throttle token
    as its WARNING log line."""

    def test_not_raised_merely_by_setting_up_second_zone(self):
        """Setting up a second zone alone must NOT raise the issue — only an
        actual ambiguous resolution should. This is the #820 regression check:
        the old behavior raised this unconditionally right here."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            zones, _fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        assert _zone_resolution_ambiguous_calls(mock_create) == []

    def test_raised_when_ambiguous_fallback_is_actually_taken(self):
        from custom_components.climate_advisor import zone_registry
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            coordinator = zone_registry.get_default_coordinator(fake_hass)

        assert coordinator is not None
        calls = _zone_resolution_ambiguous_calls(mock_create)
        assert len(calls) == 1, f"expected exactly one zone_resolution_ambiguous raise, got {calls}"
        # is_fixable=False, WARNING severity — informational only, nothing to configure.
        kwargs = calls[0].kwargs
        assert kwargs["is_fixable"] is False
        from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

        assert kwargs["severity"] == ir.IssueSeverity.WARNING

    def test_not_raised_for_single_zone(self):
        from custom_components.climate_advisor import zone_registry
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)

        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            coordinator = zone_registry.get_default_coordinator(fake_hass)

        assert len(zones) == 1
        assert coordinator is not None
        assert _zone_resolution_ambiguous_calls(mock_create) == []

    def test_not_raised_twice_for_same_outcome(self):
        """Repeated calls resolving to the SAME fallback zone must not re-raise
        the issue — matches the WARNING log line's throttle (same token)."""
        from custom_components.climate_advisor import zone_registry
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            zone_registry.get_default_coordinator(fake_hass)
            zone_registry.get_default_coordinator(fake_hass)
            zone_registry.get_default_coordinator(fake_hass)

        assert len(_zone_resolution_ambiguous_calls(mock_create)) == 1

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


# ---------------------------------------------------------------------------
# Issue #812: repairs.py always resolved the target config entry via
# hass.config_entries.async_entries(DOMAIN)[0] — the FIRST zone by that
# ordering — regardless of which zone's issue was actually being fixed. With
# 2+ zones, clicking "Fix" on either zone's card could silently patch/reload
# the WRONG zone while deleting the issue as if the actually-broken zone were
# resolved. Compounded by both issue_ids being domain-wide (not
# entry-scoped), so a second zone's create call for the same issue_id could
# collide with the first's.
#
# Fix: entry-scoped issue_ids (f"weather_entity_not_found_{entry_id}" /
# f"reload_needed_{entry_id}") plus data={"entry_id": ...} threaded through
# async_create_fix_flow() into the flow's constructor, resolved via
# repairs.py::_resolve_target_entry() instead of "first entry".
#
# These tests drive the REAL async_create_fix_flow() and the REAL flow
# classes against REAL config entries from build_headless_multi_zone() (per
# this project's no-mirror-tests doctrine) — not a re-implementation of the
# entry-resolution logic in the test body.
# ---------------------------------------------------------------------------


class TestMultiZoneRepairFlowTargeting:
    """Issue #812: a repair Fix action must target the zone the issue was raised for."""

    def test_entry_scoped_issue_ids_do_not_collide_across_zones(self):
        """Two zones each raising 'their own' weather-entity issue must get
        DISTINCT issue_ids — proving the old bug (both zones sharing the bare
        "weather_entity_not_found" id, so a second create could silently
        collide with/overwrite the first) is fixed."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, _fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        entry_a = zones["zone_0"]["entry"]
        entry_b = zones["zone_1"]["entry"]

        issue_id_a = f"weather_entity_not_found_{entry_a.entry_id}"
        issue_id_b = f"weather_entity_not_found_{entry_b.entry_id}"
        assert issue_id_a != issue_id_b

        flow_a = asyncio.run(async_create_fix_flow(_fake_hass, issue_id_a, {"entry_id": entry_a.entry_id}))
        flow_b = asyncio.run(async_create_fix_flow(_fake_hass, issue_id_b, {"entry_id": entry_b.entry_id}))
        assert isinstance(flow_a, WeatherEntityRepairFlow)
        assert isinstance(flow_b, WeatherEntityRepairFlow)
        assert flow_a._entry_id == entry_a.entry_id
        assert flow_b._entry_id == entry_b.entry_id
        assert flow_a._entry_id != flow_b._entry_id

    def test_fixing_zone_a_weather_issue_touches_only_zone_a(self):
        """Fixing zone A's weather_entity_not_found issue must update zone A's
        config entry and leave zone B's config entry completely untouched."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        entry_a = zones["zone_0"]["entry"]
        entry_b = zones["zone_1"]["entry"]
        original_b_data = dict(entry_b.data)

        fake_hass.states.set("weather.new_forecast_for_a", FakeState(state="sunny", attributes={}))

        issue_id_a = f"weather_entity_not_found_{entry_a.entry_id}"
        flow = asyncio.run(async_create_fix_flow(fake_hass, issue_id_a, {"entry_id": entry_a.entry_id}))
        flow.hass = fake_hass
        # The flow defers the reload via hass.async_create_task() — close the
        # coroutine rather than letting the harness's FakeScheduler queue it
        # unrun, which would trigger a "coroutine was never awaited"
        # RuntimeWarning at GC (see CLAUDE.md's async-mock testing rules).
        fake_hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue") as mock_delete:
            result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.new_forecast_for_a"}))

        assert result["type"] == "create_entry"
        assert entry_a.data["weather_entity"] == "weather.new_forecast_for_a"
        # Zone B's config entry must be byte-for-byte untouched.
        assert entry_b.data == original_b_data
        mock_delete.assert_called_once_with(
            fake_hass, "climate_advisor", f"weather_entity_not_found_{entry_a.entry_id}"
        )

    def test_fixing_zone_b_weather_issue_touches_only_zone_b(self):
        """Same as above with the zones reversed — proves it's not incidentally
        always picking one zone regardless of which entry_id was supplied."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        entry_a = zones["zone_0"]["entry"]
        entry_b = zones["zone_1"]["entry"]
        original_a_data = dict(entry_a.data)

        fake_hass.states.set("weather.new_forecast_for_b", FakeState(state="sunny", attributes={}))

        issue_id_b = f"weather_entity_not_found_{entry_b.entry_id}"
        flow = asyncio.run(async_create_fix_flow(fake_hass, issue_id_b, {"entry_id": entry_b.entry_id}))
        flow.hass = fake_hass
        fake_hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue") as mock_delete:
            result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.new_forecast_for_b"}))

        assert result["type"] == "create_entry"
        assert entry_b.data["weather_entity"] == "weather.new_forecast_for_b"
        # Zone A's config entry must be byte-for-byte untouched.
        assert entry_a.data == original_a_data
        mock_delete.assert_called_once_with(
            fake_hass, "climate_advisor", f"weather_entity_not_found_{entry_b.entry_id}"
        )

    def test_fixing_zone_b_reload_needed_touches_only_zone_b(self):
        """Same targeting proof for the reload_needed issue/flow pair."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        entry_a = zones["zone_0"]["entry"]
        entry_b = zones["zone_1"]["entry"]

        fake_hass.config_entries.async_reload = AsyncMock(return_value=True)

        issue_id_b = f"reload_needed_{entry_b.entry_id}"
        flow = asyncio.run(async_create_fix_flow(fake_hass, issue_id_b, {"entry_id": entry_b.entry_id}))
        flow.hass = fake_hass

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue") as mock_delete:
            result = asyncio.run(flow.async_step_init(user_input={}))

        assert result["type"] == "create_entry"
        fake_hass.config_entries.async_reload.assert_called_once_with(entry_b.entry_id)
        assert entry_a.entry_id not in [c.args[0] for c in fake_hass.config_entries.async_reload.call_args_list]
        mock_delete.assert_called_once_with(fake_hass, "climate_advisor", f"reload_needed_{entry_b.entry_id}")

    def test_single_zone_no_entry_id_ambiguity_unchanged(self):
        """With exactly 1 zone, there is no ambiguity to resolve — confirm the
        flow still resolves and fixes that zone exactly as it did before
        Issue #812, including via the OLD unscoped issue_id/no entry_id data
        (a stale pre-#812 issue instance, or a caller that hasn't been
        updated to pass entry_id)."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState

        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)

        entry = zones["zone_0"]["entry"]
        fake_hass.states.set("weather.only_zone_new", FakeState(state="sunny", attributes={}))

        # Old unscoped issue_id, data=None — the pre-#812 call shape.
        flow = asyncio.run(async_create_fix_flow(fake_hass, "weather_entity_not_found", None))
        flow.hass = fake_hass
        fake_hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        with patch("custom_components.climate_advisor.repairs.ir.async_delete_issue"):
            result = asyncio.run(flow.async_step_init(user_input={"weather_entity": "weather.only_zone_new"}))

        assert result["type"] == "create_entry"
        assert entry.data["weather_entity"] == "weather.only_zone_new"
