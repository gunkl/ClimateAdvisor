"""Tests for pure rendering helpers moved from the retired Activity Report AI
skill (ai_skills_activity.py) into ai_skills_context.py (Issue #563).

The skill itself (parse_activity_response/activity_fallback/register_activity_skill/
async_build_activity_context) has been retired — the merged investigator skill
replaces it, with its own dedicated coverage in test_ai_investigator.py. What
survives here is coverage for the pure functions that were relocated, not
retired: _event_source_label and the EVENT_RENDERERS entries.
"""

from __future__ import annotations

import custom_components.climate_advisor.ai_skills_context as _ctx_mod
from custom_components.climate_advisor.ai_skills_context import _event_source_label

# ---------------------------------------------------------------------------
# Issue #216 — _event_source_label sensor event classification
# ---------------------------------------------------------------------------


class TestEventSourceLabelSensorEvents:
    """Verify _event_source_label classifies sensor hardware events correctly (Issue #216).

    sensor_opened and sensor_all_closed are physical HA state-change events;
    they should always return 'sensor', not 'automation' or 'manual'.
    """

    def test_sensor_opened_returns_sensor(self):
        """sensor_opened → 'sensor' regardless of event data."""
        assert _event_source_label("sensor_opened", {}) == "sensor"

    def test_sensor_all_closed_returns_sensor(self):
        """sensor_all_closed → 'sensor' regardless of event data."""
        assert _event_source_label("sensor_all_closed", {}) == "sensor"

    def test_sensor_opened_with_data_returns_sensor(self):
        """sensor_opened with payload data → still 'sensor'."""
        data = {"entity": "binary_sensor.front_door", "result": "paused", "hvac_mode_change": "heat→off"}
        assert _event_source_label("sensor_opened", data) == "sensor"

    def test_sensor_all_closed_with_data_returns_sensor(self):
        """sensor_all_closed with payload data → still 'sensor'."""
        data = {"was_paused": True, "was_nat_vent": False}
        assert _event_source_label("sensor_all_closed", data) == "sensor"

    def test_automation_event_returns_automation(self):
        """A known automation event type → 'automation'."""
        assert _event_source_label("warm_day_setback_applied", {}) == "automation"
        assert _event_source_label("warm_day_state_confirmed", {}) == "automation"


class TestRenderSensorAllClosedFanDevice:
    """Issue #504: the nat-vent-ending row must show the whf on->off transition in
    Settings, matching every other fan-transition row (_render_fan_deactivated etc.) —
    previously blank even though _exit_nat_vent() really did turn the fan off.
    """

    def test_was_nat_vent_shows_fan_device_off(self):
        label, settings = _ctx_mod._render_sensor_all_closed(
            {"was_paused": False, "was_nat_vent": True, "fan_device": "whf"}, "F"
        )
        assert label == "All sensors closed -- ending nat-vent"
        assert settings == "whf: on->off"

    def test_was_nat_vent_defaults_fan_device_label_when_missing(self):
        """Payload without fan_device (e.g. an older persisted event) still renders
        something reasonable instead of erroring."""
        label, settings = _ctx_mod._render_sensor_all_closed({"was_paused": False, "was_nat_vent": True}, "F")
        assert label == "All sensors closed -- ending nat-vent"
        assert settings == "fan: on->off"

    def test_was_paused_unaffected(self):
        """The resuming-HVAC case is unrelated to fan_device and must stay unchanged."""
        label, settings = _ctx_mod._render_sensor_all_closed(
            {"was_paused": True, "was_nat_vent": False, "fan_device": "whf"}, "F"
        )
        assert label == "All sensors closed -- resuming HVAC"
        assert settings == ""

    def test_neither_flag_unaffected(self):
        label, settings = _ctx_mod._render_sensor_all_closed({"was_paused": False, "was_nat_vent": False}, "F")
        assert label == "All sensors closed"
        assert settings == ""

    def test_manual_event_returns_manual(self):
        """A known manual event type → 'manual'."""
        assert _event_source_label("override_detected", {}) == "manual"

    def test_nat_vent_prefix_returns_automation(self):
        """nat_vent_* prefix → 'automation'."""
        assert _event_source_label("nat_vent_comfort_floor_exit", {}) == "automation"
        assert _event_source_label("nat_vent_predicted_floor_exit", {}) == "automation"
        assert _event_source_label("nat_vent_outdoor_rise_exit", {}) == "automation"
