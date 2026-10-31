"""Tests for the native diagnostics hook (custom_components/climate_advisor/diagnostics.py).

Covers PR1 of docs/multi-zone-spec.md ("Diagnostics and Field Feedback"): the
`async_get_config_entry_diagnostics` hook HA calls for its native "Download
Diagnostics" button, and the shared `async_get_diagnostics_payload` helper it
and the legacy `dump_diagnostics` service both build their output through.
"""

from __future__ import annotations

import asyncio
import math
import re
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
    # Real buffers hold (iso_ts, temp) tuples (Issue #1015: summary reads min/max/ts)
    coordinator._outdoor_temp_history = [("t1", 1.0), ("t2", 2.0), ("t3", 3.0)]
    coordinator._indoor_temp_history = [("t1", 1.0), ("t2", 2.0)]
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
        assert payload["chart_data_summary"] == {
            "outdoor_points": 3,
            "indoor_points": 2,
            "outdoor_min": 1.0,
            "outdoor_max": 3.0,
            "outdoor_oldest_ts": "t1",
            "outdoor_newest_ts": "t3",
        }
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
        assert "github_token" in TO_REDACT  # Issue #1062
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

    def test_github_token_is_redacted_in_payload_and_log_dump_text(self):
        """Issue #1062: the token must not appear anywhere in the payload (download or service log)."""
        import json

        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"github_token": "ghp_super_secret_token_value", "github_repo": "me/repo"})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["config"]["github_token"] != "ghp_super_secret_token_value"
        assert "ghp_super_secret" not in json.dumps(payload, default=str)
        assert payload["config"]["github_repo"] == "me/repo"  # not a secret

    def test_every_password_field_in_the_flows_is_redacted(self):
        """A future PASSWORD selector (a secret) must be added to the redaction set or CI fails."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "config_flow.py"
        const_mod = __import__("custom_components.climate_advisor.const", fromlist=["x"])
        password_keys = set()
        pairs = []  # (vol.Optional(KEY) call, selector value) from dict literals and schema[...] = ... assignments
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                pairs.extend(zip(node.keys, node.values, strict=True))
            elif isinstance(node, ast.Assign):
                pairs.extend((t.slice, node.value) for t in node.targets if isinstance(t, ast.Subscript))
        for key_node, val_node in pairs:
            if "PASSWORD" not in ast.dump(val_node) or not isinstance(key_node, ast.Call):
                continue
            arg = key_node.args[0]
            if isinstance(arg, ast.Name):
                password_keys.add(getattr(const_mod, arg.id))
            elif isinstance(arg, ast.Attribute):
                password_keys.add(getattr(const_mod, arg.attr))
            elif isinstance(arg, ast.Constant):
                password_keys.add(arg.value)
        assert {"ai_api_key", "github_token"} <= password_keys, password_keys
        assert password_keys <= TO_REDACT, f"password fields missing from TO_REDACT: {password_keys - TO_REDACT}"

    def test_non_secret_token_count_setting_is_not_redacted(self):
        entry_a = _make_entry("entry_a", "Bedroom")
        coord_a = _make_coordinator({"ai_max_tokens": 4096})
        hass = _make_hass({"entry_a": coord_a}, [entry_a])

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry_a))

        assert payload["config"]["ai_max_tokens"] == 4096

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


# ── coordinate leak detection (rounding-aware, timestamp-safe) ───────────────────────────────────
_FIXTURE_COORDS = (34.912345, -120.467891)
# Any ISO date / datetime fragment: its digits (seconds, fractions) are not coordinates.
_ISO_FRAGMENT = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d+)?"
)


def _leaf_values(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _leaf_values(v)
    elif isinstance(node, (list, tuple, set)):
        for v in node:
            yield from _leaf_values(v)
    else:
        yield node


def _coordinate_leaks(payload, coords, *, tolerance: float = 0.01) -> list:
    """Leaves of ``payload`` that disclose a coordinate, exactly or rounded.

    * a numeric leaf within ``tolerance`` of a coordinate (so 34.91 and 34.9123 both count);
    * a string leaf that contains a coordinate to 2+ decimals (rounded or truncated), after
      removing ISO date/time fragments (a timestamp's seconds are not a location).
    """
    patterns = []
    for c in coords:
        for places in range(2, 7):
            for text in (f"{c:.{places}f}", f"{math.trunc(c * 10**places) / 10**places:.{places}f}"):
                patterns.append(re.compile(r"(?<![\d.])" + re.escape(text)))
    leaks = []
    for leaf in _leaf_values(payload):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, (int, float)):
            if any(abs(leaf - c) <= tolerance for c in coords):
                leaks.append(leaf)
        elif isinstance(leaf, str):
            scrubbed = _ISO_FRAGMENT.sub("", leaf)
            if any(p.search(scrubbed) for p in patterns):
                leaks.append(leaf)
    return leaks


class TestThermostatSnapshotInPayload:
    """Issue #1049 change 6: thermostat capabilities, last command and what happened."""

    @staticmethod
    def _coordinator_with_thermostat():
        from datetime import datetime
        from types import SimpleNamespace

        from custom_components.climate_advisor.automation import HvacCommandResult, HvacWriteRecord

        coord = _make_coordinator(
            {
                "climate_entity": "climate.t",
                "temp_unit": "fahrenheit",
                "notify_service": "notify.davids_phone",
                "ai_api_key": "sk-ant-super-secret-value",
            }
        )
        coord.automation_engine = SimpleNamespace(
            _last_commanded_hvac_mode="heat",
            _last_commanded_hvac_time=datetime(2026, 10, 9, 6, 30),
            _pending_setpoint_single=68.0,
            _temp_command_time=datetime(2026, 10, 9, 6, 30),
            _hvac_write_record=HvacWriteRecord(result=HvacCommandResult.EXECUTED),
            _hvac_cmd_fail_since=None,
            _hvac_cmd_fail_announced=None,
        )
        coord._event_log = [{"type": "hvac_mode_not_applied", "requested_mode": "heat", "serial": "SN-999"}]
        return coord

    @staticmethod
    def _hass_for(coord, entry):
        hass = _make_hass({"entry_a": coord}, [entry])
        attrs = {
            "hvac_modes": ["off", "heat", "cool"],
            "supported_features": 9,
            "min_temp": 45,
            "max_temp": 95,
            "target_temp_step": 1,
            "serial_number": "SN-12345",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "ip_address": "192.168.1.50",
            "latitude": 34.912345,
            "longitude": -120.467891,
        }
        hass.states.get.side_effect = lambda eid: (
            MagicMock(state="off", attributes=attrs) if eid == "climate.t" else None
        )
        return hass

    def test_thermostat_snapshot_has_capabilities_and_last_command(self):
        entry = _make_entry("entry_a", "Bedroom")
        hass = self._hass_for(self._coordinator_with_thermostat(), entry)

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry))

        snap = payload["thermostat_snapshot"]
        assert snap["climate_entity"]["capabilities"]["hvac_modes"] == ["off", "heat", "cool"]
        assert snap["climate_entity"]["capabilities"]["min_temp"] == 45
        assert snap["last_command"]["mode"] == "heat"
        assert snap["last_command"]["setpoint"] == 68.0
        assert snap["last_command"]["result"] == "executed"
        assert snap["last_command"]["live_state_matches_commanded"] is False
        assert payload["recent_hvac_events"][0]["type"] == "hvac_mode_not_applied"

    def test_identifying_thermostat_attributes_and_secrets_absent_from_payload(self):
        entry = _make_entry("entry_a", "Bedroom")
        hass = self._hass_for(self._coordinator_with_thermostat(), entry)

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry))
        text = str(payload)

        # Identifier strings are distinctive enough for a substring check on the whole payload.
        for leak in ("SN-12345", "SN-999", "AA:BB:CC:DD:EE:FF", "192.168.1.50"):
            assert leak not in text, leak
        # Coordinates are numbers, so a raw substring check can collide with a timestamp's seconds
        # fraction ("...:03:34.912+00:00" contains "34.91"; about 1 run in 600). Check structured
        # leaves instead, with rounding-aware rules (see _coordinate_leaks).
        assert _coordinate_leaks(payload, _FIXTURE_COORDS) == []
        assert "davids_phone" not in text
        assert "super-secret" not in text

    def test_coordinate_leak_check_catches_rounded_and_embedded_coordinates(self):
        """Sabotage: inject a rounded latitude into the real payload; the check must flag it."""
        entry = _make_entry("entry_a", "Bedroom")
        hass = self._hass_for(self._coordinator_with_thermostat(), entry)
        clean = asyncio.run(async_get_diagnostics_payload(hass, entry))
        assert _coordinate_leaks(clean, _FIXTURE_COORDS) == []

        for planted in (34.91, 34.9123, -120.47, -120.46, "34.91", "lat=34.9124", "at -120.4679, somewhere"):
            sabotaged = {**clean, "recent_hvac_events": [*clean["recent_hvac_events"], {"where": planted}]}
            assert _coordinate_leaks(sabotaged, _FIXTURE_COORDS) == [planted], planted
        nested = {**clean, "thermostat_snapshot": {**clean["thermostat_snapshot"], "loc": {"lat": 34.912345}}}
        assert _coordinate_leaks(nested, _FIXTURE_COORDS) == [34.912345]

    def test_coordinate_leak_check_ignores_timestamps_and_ordinary_numbers(self):
        benign = {
            "ts": "2026-10-30T10:03:34.912345+00:00",
            "also": "2026-10-30 10:03:34.91",
            "clock": "10:03:34.912",
            "temp": 72.5,
            "n": 134,
            "span": "134.91 minutes",
            "ok": True,
        }
        assert _coordinate_leaks(benign, _FIXTURE_COORDS) == []

    def test_automation_enabled_debug_state_still_present(self):
        entry = _make_entry("entry_a", "Bedroom")
        coord = self._coordinator_with_thermostat()
        coord.get_debug_state.return_value = {"automation_enabled": True, "hvac_mode": "heat"}
        hass = self._hass_for(coord, entry)

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry))

        assert payload["debug_state"]["automation_enabled"] is True

    def test_snapshot_failure_does_not_lose_the_rest_of_the_payload(self):
        entry = _make_entry("entry_a", "Bedroom")
        coord = self._coordinator_with_thermostat()
        hass = self._hass_for(coord, entry)
        hass.states.get.side_effect = RuntimeError("boom")

        payload = asyncio.run(async_get_diagnostics_payload(hass, entry))

        assert payload["thermostat_snapshot"] is None
        assert payload["debug_state"] == {"hvac_mode": "heat"}
