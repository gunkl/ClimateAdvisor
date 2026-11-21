"""diagnostic_snapshot: the footer and download sections render every block, and never break (Silver coverage).

Occupant view: when the person downloads diagnostics or runs the AI Investigator, the thermostat profile, last
command and fan entity all appear, identifying values stay redacted, and a half-built engine or a failing
profile never turns the download into an error.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.climate_advisor import diagnostic_snapshot as ds
from custom_components.climate_advisor.remote_offset import RemoteLoopState, RemoteOffsetState


def _hass(states: dict):
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: states.get(eid)
    return hass


def _coord(**kw):
    base = {"config": {"climate_entity": "climate.t", "temp_unit": "fahrenheit"}, "automation_engine": None}
    base.update(kw)
    return SimpleNamespace(**base)


class TestPrimitives:
    def test_non_string_key_is_never_sensitive(self):
        assert ds.redact_sensitive({1: "x", "serial_number": "SN1"}) == {1: "x", "serial_number": ds.REDACTED}

    def test_decode_supported_features_rejects_non_int_and_bool(self):
        assert ds.decode_supported_features("8") == []
        assert ds.decode_supported_features(True) == []
        assert ds.decode_supported_features(9) == ["target_temperature", "fan_mode"]

    def test_recent_hvac_events_without_a_log(self):
        assert ds.recent_hvac_events(SimpleNamespace(_event_log=None)) == []

    def test_recent_hvac_events_filters_and_limits(self):
        log = [{"type": "hvac_on"}, {"type": "other"}, "junk", {"type": "setpoint_change"}, {"type": "hvac_off"}]
        assert ds.recent_hvac_events(SimpleNamespace(_event_log=log), limit=2) == [
            {"type": "setpoint_change"},
            {"type": "hvac_off"},
        ]


class TestBuilder:
    def test_empty_attributes_give_no_capabilities(self):
        hass = _hass({"climate.t": SimpleNamespace(state="heat", attributes={})})
        snap = ds.build_diagnostic_snapshot(hass, _coord(), include_control_quality=False)
        assert snap["climate_entity"]["capabilities"] is None
        assert "control_quality" not in snap

    def test_control_quality_inputs_failure_yields_none(self):
        class _Exploding:
            config = {"climate_entity": None}
            automation_engine = None

            @property
            def _chart_log(self):
                raise RuntimeError("chart log broken")

        snap = ds.build_diagnostic_snapshot(_hass({}), _Exploding())
        assert snap["control_quality"] is None
        assert snap["climate_entity"] == {"entity_id": None, "state": None, "attributes": None, "capabilities": None}

    def test_async_without_chart_log_skips_executor(self):
        hass = _hass({})
        snap = asyncio.run(ds.async_build_diagnostic_snapshot(hass, _coord()))
        assert snap["control_quality"] is None
        hass.async_add_executor_job.assert_not_called()

    def test_thermostat_profile_included_when_dict(self):
        coord = _coord(thermostat_profile_diagnostics=lambda: {"device": "Sensi", "fan": {}})
        snap = ds.build_diagnostic_snapshot(_hass({}), coord, include_control_quality=False)
        assert snap["thermostat_profile"] == {"device": "Sensi", "fan": {}}

    def test_thermostat_profile_failure_or_non_dict_is_omitted(self):
        def _boom():
            raise RuntimeError("no profile")

        for fn in (_boom, lambda: "not a dict"):
            snap = ds.build_diagnostic_snapshot(
                _hass({}), _coord(thermostat_profile_diagnostics=fn), include_control_quality=False
            )
            assert "thermostat_profile" not in snap

    def test_remote_offset_view_included(self):
        st = RemoteOffsetState(
            state=RemoteLoopState.ACTIVE,
            context="sleep",
            mode="heat",
            comfort_target_f=66.0,
            commanded_f=70.0,
            offset_f=4.0,
            gate_reason=None,
            clamp_limit=None,
            last_reason="raised",
            remote_value_f=64.0,
            remote_age_s=30.0,
            cap_limit_f=4.0,
        )
        engine = SimpleNamespace(remote_offset_state=lambda: st)
        snap = ds.build_diagnostic_snapshot(_hass({}), _coord(automation_engine=engine), include_control_quality=False)
        assert snap["remote_offset"]["offset_applied_f"] == 4.0


class TestRenderText:
    def _snapshot(self) -> dict:
        return {
            "version": "9.9.9",
            "config_summary": {"climate_entity": "climate.t"},
            "climate_entity": {"state": "cool", "attributes": {"hvac_action": "cooling"}, "capabilities": None},
            "thermostat_profile": {
                "device": None,
                "fan": {
                    "off_value": "auto",
                    "on_value": "on",
                    "status": "ok",
                    "reports_hvac_action": True,
                    "roles": {
                        "circulate": {
                            "role": "periodic",
                            "source": "learned",
                            "name_guess": "periodic",
                            "fan_min": 20,
                            "idle_min": 40,
                        }
                    },
                    "problems": ["on value unconfirmed"],
                },
                "archived_devices": [{"device": None, "fan_modes": ["auto", "on"]}],
            },
            "last_command": None,
            "fan_entity": {"state": "on", "attributes": {"speed": "high"}},
        }

    def test_profile_fan_entity_rendered_and_no_last_command_block(self):
        text = ds.render_diagnostic_snapshot_text(self._snapshot())
        assert "  thermostat_profile (device: unknown):" in text
        assert "fan off -> 'auto', fan on -> 'on', status=ok, reports_hvac_action=True" in text
        assert "'circulate': periodic (learned; name guess periodic; fan 20 min, idle 40 min)" in text
        assert "    problem: on value unconfirmed" in text
        assert "    earlier thermostat: unknown fan_modes=['auto', 'on']" in text
        assert "last_command" not in text
        assert "  fan_entity.state: on" in text
        assert "    speed: 'high'" in text

    def test_profile_without_fan_block_is_skipped(self):
        snap = self._snapshot()
        snap["thermostat_profile"] = {"device": "Sensi"}
        snap["fan_entity"] = {"state": "off", "attributes": None}
        text = ds.render_diagnostic_snapshot_text(snap)
        assert "thermostat_profile" not in text
        assert "  fan_entity.state: off" in text
