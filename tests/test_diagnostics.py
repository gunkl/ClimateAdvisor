"""Tests for the native diagnostics hook (custom_components/climate_advisor/diagnostics.py).

Covers PR1 of docs/multi-zone-spec.md ("Diagnostics and Field Feedback"): the
`async_get_config_entry_diagnostics` hook HA calls for its native "Download
Diagnostics" button, and the shared `async_get_diagnostics_payload` helper it
and the legacy `dump_diagnostics` service both build their output through.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.const import CONFIG_METADATA, DOMAIN
from custom_components.climate_advisor.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
    async_get_diagnostics_payload,
)


def _make_coordinator(config: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.config = config
    coordinator._outdoor_temp_history = [1, 2, 3]
    coordinator._indoor_temp_history = [1, 2]
    coordinator._briefing_sent_today = True
    coordinator._last_briefing = "Good morning! It's a mild day."
    coordinator.get_debug_state.return_value = {"hvac_mode": "heat"}
    coordinator.learning.get_compliance_summary.return_value = {"score": 0.9}
    return coordinator


def _make_entry(entry_id: str, title: str):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.title = title
    return entry


def _make_hass(zones: dict, all_entries: list):
    hass = MagicMock()
    hass.data = {DOMAIN: zones}
    hass.config_entries.async_entries.return_value = all_entries
    return hass


class TestPayloadFields:
    """New multi-zone diagnostic fields (Issue #796 PR1)."""

    def test_zone_count_reflects_hass_data(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        entry_b = _make_entry("entry_b", "Living Room")
        coord_a = _make_coordinator({"notify_service": "notify.mobile_app"})
        coord_b = _make_coordinator({"notify_service": "notify.mobile_app"})
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            [entry_a, entry_b],
        )

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["zone_count"] == 2
        assert payload["this_entry_id"] == "entry_a"
        assert payload["entry_title"] == "Bedroom"

    def test_entry_setup_order_matches_config_entries_position(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        entry_b = _make_entry("entry_b", "Living Room")
        coord_a = _make_coordinator({})
        coord_b = _make_coordinator({})
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            [entry_a, entry_b],
        )

        payload_a = asyncio.run(async_get_diagnostics_payload(hass, entry_a))
        payload_b = asyncio.run(async_get_diagnostics_payload(hass, entry_b))

        assert payload_a["entry_setup_order"] == 0
        assert payload_b["entry_setup_order"] == 1

    def test_entry_setup_order_none_when_entry_not_found(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({})
        # config_entries.async_entries() deliberately does NOT include entry_a,
        # simulating a stale/removed entry — must not raise.
        hass = _make_hass({"entry_a": coord_a}, [])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["entry_setup_order"] is None

    def test_active_service_bindings_reports_call_time_resolution_not_a_static_binding(self):
        """Since Issue #796 PR4, services resolve their target zone per-call from
        entry_id rather than being bound to one zone at registration time — this
        field must describe that design, not invent a plausible-looking static
        entry_id binding that no longer exists."""
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert "entry_a" not in payload["active_service_bindings"]
        assert "not applicable" in payload["active_service_bindings"]
        assert "entry_id" in payload["active_service_bindings"]

    def test_legacy_dump_diagnostics_fields_still_present(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"notify_service": "notify.mobile_app"})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["debug_state"] == {"hvac_mode": "heat"}
        assert payload["chart_data_summary"] == {"outdoor_points": 3, "indoor_points": 2}
        assert payload["learning_summary"] == {"score": 0.9}
        assert payload["briefing_state"] == {"sent_today": True, "briefing_length": len(coord_a._last_briefing)}

    def test_no_coordinator_for_entry_still_returns_top_level_fields(self):
        """A stale/mismatched entry_id (e.g. a Gap-9-class bug) must not crash the hook."""
        entry_a = _make_entry("entry_a", "Bedroom")
        hass = _make_hass({}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["zone_count"] == 0
        assert "debug_state" not in payload


class TestRedaction:
    """Redaction reuses the CONFIG_METADATA 'sensitive' flag (api.py:542 convention)."""

    def test_to_redact_includes_notify_service_and_sensitive_config_keys(self):
        sensitive_keys = {key for key, meta in CONFIG_METADATA.items() if meta.get("sensitive")}
        assert "notify_service" in TO_REDACT
        assert sensitive_keys, "expected at least one sensitive CONFIG_METADATA key (ai_api_key)"
        assert sensitive_keys <= TO_REDACT

    def test_notify_service_value_is_redacted_in_payload(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"notify_service": "notify.davids_phone"})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["config"]["notify_service"] != "notify.davids_phone"
        assert "davids_phone" not in str(payload)

    def test_sensitive_config_metadata_key_is_redacted_in_payload(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"ai_api_key": "sk-ant-super-secret-value"})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["config"]["ai_api_key"] != "sk-ant-super-secret-value"
        assert "super-secret" not in str(payload)

    def test_non_sensitive_config_values_pass_through_unredacted(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"comfort_heat": 68})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["config"]["comfort_heat"] == 68


class TestNativeHook:
    """async_get_config_entry_diagnostics is the actual HA-called entry point."""

    def test_native_hook_delegates_to_shared_payload_builder(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"notify_service": "notify.mobile_app"})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_config_entry_diagnostics(hass, entry_a))

        assert payload["zone_count"] == 1
        assert payload["entry_title"] == "Bedroom"
        assert payload["config"]["notify_service"] != "notify.mobile_app"
