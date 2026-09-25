"""Tests for diagnostic_snapshot.py (Issue #968, compact variant Issue #973)."""

from custom_components.climate_advisor.diagnostic_snapshot import (
    render_diagnostic_snapshot_compact,
)


class TestRenderDiagnosticSnapshotCompact:
    def test_full_snapshot_renders_key_attributes_only(self):
        snapshot = {
            "version": "0.7.65",
            "climate_entity": {
                "entity_id": "climate.living_room",
                "state": "cool",
                "attributes": {
                    "fan_mode": "low",
                    "hvac_action": "cooling",
                    "temperature_unit": "°F",
                    "friendly_name": "Living Room",
                    "current_temperature": 72,
                },
            },
            "fan_entity": {"entity_id": "switch.whf", "state": "on"},
        }

        text = render_diagnostic_snapshot_compact(snapshot)

        assert "CA v0.7.65" in text
        assert "climate.living_room hvac_mode=cool" in text
        assert "fan_mode=low" in text
        assert "hvac_action=cooling" in text
        assert "temperature_unit=°F" in text
        assert "switch.whf state=on" in text
        # Compact means single line and no raw attribute dump of unrelated fields
        assert "\n" not in text
        assert "friendly_name" not in text
        assert "current_temperature" not in text

    def test_no_climate_entity_configured(self):
        snapshot = {"version": "0.7.65", "climate_entity": {"entity_id": None, "state": None, "attributes": None}}

        text = render_diagnostic_snapshot_compact(snapshot)

        assert "climate entity: not configured" in text

    def test_no_fan_entity_omits_fan_segment(self):
        snapshot = {
            "version": "0.7.65",
            "climate_entity": {"entity_id": "climate.living_room", "state": "off", "attributes": {}},
            "fan_entity": None,
        }

        text = render_diagnostic_snapshot_compact(snapshot)

        assert "climate.living_room hvac_mode=off" in text
        assert "state=on" not in text

    def test_missing_optional_attributes_omit_detail_clause(self):
        snapshot = {
            "version": "0.7.65",
            "climate_entity": {"entity_id": "climate.living_room", "state": "off", "attributes": {}},
        }

        text = render_diagnostic_snapshot_compact(snapshot)

        assert text == "**System:** CA v0.7.65 · climate.living_room hvac_mode=off"
