"""Issue #1049 change 6: thermostat capabilities, last command and redaction in the diagnostic snapshot.

The snapshot builder is shared by HA's "Download diagnostics" file, the log/Activity Record
downloads and the AI investigator (which goes to the Claude API), so identifying thermostat
attributes (serial/MAC/IP/location/owner name) must be redacted INSIDE the builder.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.climate_advisor import ai_skills_investigator as investigator_module
from custom_components.climate_advisor import api as api_module
from custom_components.climate_advisor.automation import HvacCommandResult, HvacWriteRecord
from custom_components.climate_advisor.diagnostic_snapshot import (
    REDACTED,
    build_diagnostic_snapshot,
    recent_hvac_events,
    redact_sensitive,
    render_diagnostic_snapshot_compact,
    render_diagnostic_snapshot_text,
)

_SENSI_ATTRS = {
    "hvac_modes": ["off", "heat", "cool", "heat_cool"],
    "supported_features": 395,  # 1 + 2 + 8 + 128 + 256
    "min_temp": 7.0,
    "max_temp": 35.0,
    "target_temp_step": 0.5,
    "current_temperature": 21.5,
    "temperature": 20.0,
    "fan_modes": ["auto", "on"],
    "hvac_action": "idle",
    "friendly_name": "Davids Upstairs Thermostat",
    "serial_number": "SN-12345-ABC",
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "ip_address": "192.168.1.77",
    "latitude": 34.95,
    "longitude": -120.43,
    "wifi_ssid": "HomeNet",
    "firmware_version": "4.1.2.3",
}

_LEAKS = ("SN-12345-ABC", "AA:BB:CC:DD:EE:FF", "192.168.1.77", "34.95", "-120.43", "HomeNet", "Davids")


def _hass_with(attrs, state="heat", fan_attrs=None):
    states = {"climate.t": SimpleNamespace(state=state, attributes=attrs)}
    if fan_attrs is not None:
        states["switch.whf"] = SimpleNamespace(state="on", attributes=fan_attrs)
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: states.get(eid)
    return hass


def _engine(**overrides):
    base = {
        "_last_commanded_hvac_mode": "heat",
        "_last_commanded_hvac_time": datetime(2026, 10, 9, 6, 30),
        "_pending_setpoint_single": 68.0,
        "_temp_command_time": datetime(2026, 10, 9, 6, 30),
        "_hvac_write_record": HvacWriteRecord(
            result=HvacCommandResult.EXECUTED,
            apply_skip_reason="excluded_manual_override",
            mismatch_since=datetime(2026, 10, 9, 6, 31),
            episode_reason="heat",
        ),
        "_hvac_cmd_fail_since": None,
        "_hvac_cmd_fail_announced": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _coordinator(engine=None, **config):
    cfg = {"climate_entity": "climate.t", "temp_unit": "celsius", **config}
    return SimpleNamespace(config=cfg, automation_engine=engine, _event_log=[])


class TestThermostatCapabilitiesBlock:
    def test_capabilities_parsed_from_live_attributes(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(_engine()))
        caps = snap["climate_entity"]["capabilities"]
        assert caps["hvac_modes"] == ["off", "heat", "cool", "heat_cool"]
        assert caps["supports_heat"] is True and caps["supports_heat_cool"] is True
        assert caps["supported_features"] == 395
        assert {"target_temperature", "target_temperature_range", "fan_mode", "turn_off", "turn_on"} <= set(
            caps["supported_feature_names"]
        )
        assert (caps["min_temp"], caps["max_temp"], caps["target_temp_step"]) == (7.0, 35.0, 0.5)

    def test_unavailable_entity_has_no_capabilities(self):
        coord = _coordinator(_engine(), climate_entity="climate.gone")
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), coord)
        assert snap["climate_entity"]["capabilities"] is None

    def test_current_state_attributes_are_kept(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(_engine()))
        attrs = snap["climate_entity"]["attributes"]
        assert attrs["hvac_action"] == "idle"
        assert attrs["current_temperature"] == 21.5
        assert attrs["firmware_version"] == "4.1.2.3"  # a version string must not be mistaken for an IP


class TestLastCommandBlock:
    def test_last_command_reports_mode_setpoint_time_result(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS, state="off"), _coordinator(_engine()))
        last = snap["last_command"]
        assert last["mode"] == "heat"
        assert last["mode_time"] == "2026-10-09T06:30:00"
        assert last["setpoint_f"] == 68.0
        assert last["setpoint"] == 20.0 and last["setpoint_unit"] == "celsius"
        assert last["result"] == "executed"
        assert last["live_state"] == "off"
        assert last["live_state_matches_commanded"] is False  # the #1048 symptom, visible at a glance
        assert last["read_back_skip_reason"] == "excluded_manual_override"
        assert last["mode_mismatch_since"] == "2026-10-09T06:31:00"
        assert last["mode_mismatch_episode_mode"] == "heat"

    def test_no_engine_gives_none_and_partial_engine_does_not_raise(self):
        assert build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(None))["last_command"] is None
        partial = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(SimpleNamespace()))["last_command"]
        assert partial["mode"] is None and partial["result"] is None and partial["setpoint"] is None

    def test_magicmock_engine_values_are_not_leaked(self):
        last = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(MagicMock()))["last_command"]
        assert last["mode"] is None and last["mode_time"] is None and last["result"] is None

    def test_text_renderer_includes_new_blocks(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(_engine()))
        text = render_diagnostic_snapshot_text(snap)
        assert "thermostat_capabilities:" in text and "last_command" in text and "hvac_modes" in text


class TestRedactionInsideBuilder:
    def test_identifying_attributes_redacted_in_snapshot(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(_engine()))
        attrs = snap["climate_entity"]["attributes"]
        for key in (
            "serial_number",
            "mac_address",
            "ip_address",
            "latitude",
            "longitude",
            "wifi_ssid",
            "friendly_name",
        ):
            assert attrs[key] == REDACTED, key
        blob = repr(snap)
        for leak in _LEAKS:
            assert leak not in blob

    def test_value_based_redaction_catches_unknown_key_names(self):
        out = redact_sensitive({"weird": "aa-bb-cc-dd-ee-ff", "other": "10.0.0.5", "version": "1.2.3.4"})
        assert out["weird"] == REDACTED and out["other"] == REDACTED
        assert out["version"] == "1.2.3.4"  # version-named keys are exempt from the IPv4 value rule

    def test_fan_entity_attributes_redacted(self):
        hass = _hass_with(_SENSI_ATTRS, fan_attrs={"serial": "FAN-SN-9", "speed": "low"})
        snap = build_diagnostic_snapshot(hass, _coordinator(_engine(), fan_entity="switch.whf"))
        assert snap["fan_entity"]["attributes"] == {"serial": REDACTED, "speed": "low"}

    def test_other_consumers_receive_redacted_text_and_compact(self):
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), _coordinator(_engine()))
        text = render_diagnostic_snapshot_text(snap) + render_diagnostic_snapshot_compact(snap)
        for leak in _LEAKS:
            assert leak not in text

    def test_api_and_investigator_use_the_redacting_builder_only(self):
        """Both leave-the-machine consumers must go through build_diagnostic_snapshot."""
        for module in (api_module, investigator_module):
            assert "build_diagnostic_snapshot(hass, coordinator)" in inspect.getsource(module)

    def test_hvac_events_filtered_and_redacted(self):
        coord = _coordinator(_engine())
        coord._event_log = [
            {"type": "hvac_command_failed", "mode": "heat", "serial": "X1"},
            {"type": "briefing_sent"},
            {"type": "setpoint_rejected", "ip": "10.1.1.1"},
        ]
        events = recent_hvac_events(coord)
        assert [e["type"] for e in events] == ["hvac_command_failed", "setpoint_rejected"]
        assert "X1" not in repr(events) and "10.1.1.1" not in repr(events)


class TestPromiseStatsExcusedTime:
    """Issue #1260: time the thermostat's deadband excused is in the snapshot, beside (never inside) the hits."""

    def test_excused_time_is_carried_apart_from_hits(self):
        coord = _coordinator(_engine())
        excused = {"thermostat_deadband": {"runs": 2, "minutes": 75.0, "last_ts": "2026-11-02T10:00:00"}}
        coord._promise_stats = {
            "comfort_kept": {"hits": 0, "first_ts": None, "last_ts": None, "excused": excused},
            "settling_flip": {"hits": 3, "first_ts": "a", "last_ts": "b"},
        }
        snap = build_diagnostic_snapshot(_hass_with(_SENSI_ATTRS), coord, include_control_quality=False)
        ps = snap["promise_stats"]
        assert ps["comfort_kept"] == {"hits": 0, "first_ts": None, "last_ts": None, "excused": excused}
        assert ps["settling_flip"] == {"hits": 3, "first_ts": "a", "last_ts": "b"}  # no empty excused key
