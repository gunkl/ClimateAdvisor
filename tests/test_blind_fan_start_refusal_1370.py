"""Issue #1370: Climate Advisor never STARTS a fan while it cannot read the indoor temperature (household promise P5).

Occupant experience being protected: the indoor sensor stops reporting with a window open. Before this fix Climate
Advisor could still switch the whole-house fan on -- a command-only fan someone had turned off at the wall was
re-commanded on by the coordinator's 30-minute assert, and the min-runtime cycle and the economizer never looked at
indoor at all -- pulling outdoor air into a house whose temperature nobody could see.

The refusal lives at the engine's one start choke point (``_activate_fan``) plus the coordinator's command-only
assert, the one fan command path that bypasses it. A fan that is already running and every stop are untouched.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult  # noqa: E402
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent


def _engine(fan_mode: str = FAN_MODE_WHOLE_HOUSE) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config={
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            CONF_FAN_MODE: fan_mode,
        },
    )
    engine._emit_event_callback = lambda *_a: None
    engine._request_refresh_callback = MagicMock()
    return engine


def _fan_on_calls(engine: AutomationEngine) -> list:
    return [
        c
        for c in engine.hass.services.async_call.await_args_list
        if c.args[:2] in (("fan", "turn_on"), ("switch", "turn_on"))
        or (c.args[:2] == ("climate", "set_fan_mode") and str(c.args[2].get("fan_mode")).lower() == "on")
    ]


class TestActivateFanRefusesBlindStart:
    def test_unreadable_indoor_refuses_the_start_and_sends_nothing(self):
        engine = _engine()
        with patch.object(engine, "_get_indoor_temp_f", return_value=None):
            result = asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
        assert result is FanCommandResult.SUPPRESSED
        assert _fan_on_calls(engine) == []
        assert engine._fan_active is False

    def test_hvac_blower_start_is_refused_too(self):
        """P5 covers every fan Climate Advisor starts, the thermostat blower included."""
        engine = _engine(FAN_MODE_HVAC)
        thermostat = MagicMock()
        thermostat.state = "off"
        thermostat.attributes = {"fan_modes": ["auto", "on"], "fan_mode": "auto"}
        engine.hass.states.get.return_value = thermostat
        with patch.object(engine, "_get_indoor_temp_f", return_value=None):
            result = asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
        assert result is FanCommandResult.SUPPRESSED
        assert _fan_on_calls(engine) == []

    def test_hvac_blower_control_starts_with_a_reading(self):
        """Effectiveness for the test above: the same rig with a reading really sends the blower-on command."""
        engine = _engine(FAN_MODE_HVAC)
        thermostat = MagicMock()
        thermostat.state = "off"
        thermostat.attributes = {"fan_modes": ["auto", "on"], "fan_mode": "auto"}
        engine.hass.states.get.return_value = thermostat
        with patch.object(engine, "_get_indoor_temp_f", return_value=74.0):
            asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
        assert _fan_on_calls(engine), "control rig must reach the blower command, or the refusal test proves nothing"

    def test_refusal_is_a_not_applied_result_for_every_caller(self):
        """The min-runtime cycle and nat-vent read SUPPRESSED as 'no start happened' (no session, retry later)."""
        from custom_components.climate_advisor.automation import (  # noqa: PLC0415
            _FAN_START_NOT_APPLIED_RESULTS,
            _NAT_VENT_NO_WORKING_FAN_RESULTS,
        )

        assert FanCommandResult.SUPPRESSED in _FAN_START_NOT_APPLIED_RESULTS
        assert FanCommandResult.SUPPRESSED in _NAT_VENT_NO_WORKING_FAN_RESULTS

    def test_a_fan_already_running_is_left_alone(self):
        """Already on is a no-op as before; the blind-fan stop (#1037) owns a running fan."""
        engine = _engine()
        engine._fan_active = True
        with patch.object(engine, "_get_indoor_temp_f", return_value=None):
            result = asyncio.run(engine._activate_fan(reason="natural ventilation"))
        assert result is FanCommandResult.ALREADY_IN_STATE

    def test_readable_indoor_still_starts(self):
        """Control: with a reading the refusal never fires (the start reaches the fan command)."""
        engine = _engine()
        with patch.object(engine, "_get_indoor_temp_f", return_value=78.0):
            result = asyncio.run(engine._activate_fan(reason="natural ventilation"))
        assert result is not FanCommandResult.SUPPRESSED


# The nightly day that found it (run 42232297070, seed 10010000065), shrunk 365 -> 4 events by the harness: a window
# open, nat-vent starts the command-only fan at 22:25 with a good reading, the reading drops out at 22:29, and the
# 22:30 coordinator cycle re-commanded the fan on blind.
_SHRUNK_DAY = {
    "name": "nightly_10010000065_shrunk",
    "description": "Issue #1370: command-only fan re-commanded on while indoor unreadable",
    "use_coordinator": True,
    "skip_startup_coalesce": True,
    "config": {
        "comfort_heat": 68.0,
        "comfort_cool": 78.0,
        "sleep_heat": 64.0,
        "sleep_cool": 74.0,
        "sleep_time": "23:00",
        "wake_time": "07:00",
        "setback_heat": 60.0,
        "setback_cool": 84.0,
        "initial_thermostat_mode": "off",
        "temp_unit": "fahrenheit",
        "fan_mode": "whole_house_fan",
        "manual_grace_seconds": 0,
        "aggressive_savings": True,
        "door_window_sensors": ["binary_sensor.door_window_1"],
        "fan_entity": "fan.whf",
        "fan_state_feedback": False,
        "promise_p2_min_overlap_s": 30.0,
    },
    "events": [
        {"time": "2026-05-12T20:54:04", "type": "sensor_open", "entity": "binary_sensor.door_window_1"},
        {"time": "2026-05-12T22:25:00", "type": "temp_update", "indoor_f": 82.0, "outdoor_f": 80.9},
        {"time": "2026-05-12T22:29:00", "type": "indoor_unreadable", "unreadable": True},
        {"time": "2026-05-12T22:30:00", "type": "classification", "day_type": "hot", "hvac_mode": "cool"},
    ],
}


class TestNightlyDayRegression:
    @staticmethod
    def _drive(scenario: dict):
        """Drive the REAL engine + coordinator through the harness; return (promise report, action log)."""
        sys.path.insert(0, str(_REPO))
        from tools.sim_harness.generator import (  # noqa: PLC0415
            PromiseProbe,
            make_session,
            own_thermostat,
            patch_stub_gaps,
        )
        from tools.sim_harness.session import drive_scenario  # noqa: PLC0415

        sc = json.loads(json.dumps(scenario))
        session = make_session(
            sc, engine="fakehass", probe=PromiseProbe(scenario_name=sc["name"]), use_coordinator=True
        )
        try:
            patch_stub_gaps(session)
            own_thermostat(session)
            result = drive_scenario(session, sc)
        finally:
            session.close()
        return result.promise_report, result.action_log

    def test_shrunk_nightly_day_keeps_promise_p5(self):
        report, _ = self._drive(_SHRUNK_DAY)
        assert [h for h in report.hard if h.promise == "no_blind_fan_start"] == []

    def test_fan_started_with_a_reading_and_not_recommanded_once_blind(self):
        """Effectiveness: the day really starts the fan at 22:25 (a reading) and goes blind while it runs, so the
        test above cannot pass for the wrong reason; and no fan-on command is sent after the 22:29 outage."""
        _, actions = self._drive(_SHRUNK_DAY)
        fan_on = [a for a in actions if a["domain"] == "fan" and a["service"] == "turn_on"]
        assert [str(a["ts"])[:16] for a in fan_on] == ["2026-05-12 22:25"]

    def test_a_stop_is_never_refused_while_blind(self):
        """The refusal is for starts only: past the 30-min blind deadline the real blind-fan stop (#1037) still
        turns the fan off with indoor unreadable."""
        later = {
            **_SHRUNK_DAY,
            "events": [
                *_SHRUNK_DAY["events"],
                {"time": "2026-05-12T23:00:00", "type": "classification", "day_type": "hot", "hvac_mode": "cool"},
                {"time": "2026-05-12T23:10:00", "type": "classification", "day_type": "hot", "hvac_mode": "cool"},
            ],
        }
        _, actions = self._drive(later)
        assert [
            a
            for a in actions
            if a["domain"] == "fan" and a["service"] == "turn_off" and str(a["ts"]) > "2026-05-12 22:29"
        ]
