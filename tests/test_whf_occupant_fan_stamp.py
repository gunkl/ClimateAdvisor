"""Occupant fan-on stamp from ground truth while override detection is suppressed (Issue #1144, seed 7000031).

Occupant story: an HA restart, then within five minutes the occupant switches the whole-house fan on while the AC
is cooling. Fan-override detection is suppressed during startup coalescing, so Climate Advisor used to leave the
AC running beside the fan until coalescing ended. The coordinator now derives the occupant's fan-on time from the
fan's physical on-time -- but never for a fan Climate Advisor started itself (owner decision 7).
"""

from __future__ import annotations

import importlib
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

NOW = datetime(2026, 10, 31, 13, 35, 0, tzinfo=UTC)


def _coord(*, coalescing=True, fan_active=False, nat_vent=False, last_cmd=None):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord._startup_coalesce_active = coalescing
    ae = MagicMock()
    ae._fan_active = fan_active
    ae._natural_vent_active = nat_vent
    ae._fan_toggle_command_time = last_cmd
    coord.automation_engine = ae
    coord._occupant = types.MethodType(mod.ClimateAdvisorCoordinator._occupant_fan_on_at_from_ground_truth, coord)
    return coord


def _stamp(coord, fan_on=True, age=20.0):
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=NOW):
        return coord._occupant(fan_on, age)


def test_occupant_fan_on_during_coalescing_is_stamped_at_the_physical_on_time():
    assert _stamp(_coord()) == NOW - timedelta(seconds=20)


def test_outside_coalescing_the_override_stamp_is_authoritative():
    assert _stamp(_coord(coalescing=False)) is None


def test_a_fan_climate_advisor_owns_is_never_an_occupant_action():
    assert _stamp(_coord(fan_active=True)) is None
    assert _stamp(_coord(nat_vent=True)) is None


def test_an_on_transition_right_after_ca_own_fan_command_is_not_the_occupant():
    own = NOW - timedelta(seconds=22)  # CA commanded the fan 22 s ago; the entity changed 20 s ago
    assert _stamp(_coord(last_cmd=own)) is None


def test_a_ca_command_long_before_the_fan_came_on_does_not_hide_the_occupant():
    assert _stamp(_coord(last_cmd=NOW - timedelta(hours=2))) == NOW - timedelta(seconds=20)


def test_fan_off_or_unknown_age_gives_no_stamp():
    assert _stamp(_coord(), fan_on=False) is None
    assert _stamp(_coord(), fan_on=None) is None
    assert _stamp(_coord(), age=None) is None


def test_leaving_observe_only_asks_the_enforcer_to_check_at_once():
    """A fan that ran on a sealed house while automation was off is stopped when automation comes back on."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.automation_engine = MagicMock()
    coord._async_save_state = MagicMock()
    coord._spawn = MagicMock(side_effect=lambda coro: None)
    coord._request_whf_safety_check = MagicMock()

    coord.set_automation_enabled(True)
    coord._request_whf_safety_check.assert_called_once_with("automation_enabled")
    coord._request_whf_safety_check.reset_mock()
    coord.set_automation_enabled(False)
    coord._request_whf_safety_check.assert_not_called()


# ── the mirror: the occupant turns HVAC ON during startup coalescing ──────────────────────────────────────


def _hvac_coord(*, coalescing=True, mode="cool", changed=None, started=None, cmd_times=()):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord._startup_coalesce_active = coalescing
    coord._whf_safety_started_at = started
    coord.config = {"climate_entity": "climate.t"}
    state = MagicMock()
    state.state = mode
    state.last_changed = changed
    coord.hass = MagicMock()
    coord.hass.states.get = lambda eid: state if eid == "climate.t" else None
    ae = MagicMock()
    ae._last_commanded_hvac_time = cmd_times[0] if cmd_times else None
    ae._hvac_command_time = cmd_times[1] if len(cmd_times) > 1 else None
    coord.automation_engine = ae
    coord._stamp = types.MethodType(mod.ClimateAdvisorCoordinator._occupant_hvac_on_at_from_ground_truth, coord)
    coord._transition_after_startup = types.MethodType(mod.ClimateAdvisorCoordinator._transition_after_startup, coord)
    return coord


def test_occupant_hvac_on_during_coalescing_is_stamped_at_the_mode_change():
    changed = NOW - timedelta(seconds=30)
    assert _hvac_coord(changed=changed, started=NOW - timedelta(minutes=5))._stamp() == changed


def test_hvac_outside_coalescing_off_mode_or_ca_own_command_gives_no_stamp():
    changed = NOW - timedelta(seconds=30)
    started = NOW - timedelta(minutes=5)
    assert _hvac_coord(coalescing=False, changed=changed, started=started)._stamp() is None
    assert _hvac_coord(mode="off", changed=changed, started=started)._stamp() is None
    own = changed + timedelta(seconds=3)  # CA wrote this mode a few seconds before the entity reported it
    assert _hvac_coord(changed=changed, started=started, cmd_times=(own, None))._stamp() is None
    assert _hvac_coord(changed=changed, started=started, cmd_times=(None, own))._stamp() is None


def test_a_mode_restored_at_startup_is_not_an_occupant_action():
    started = NOW - timedelta(minutes=5)
    assert _hvac_coord(changed=started - timedelta(seconds=1), started=started)._stamp() is None


def _event(t, **kw):
    return {"time": "2026-08-20T" + t, **kw}


def test_through_the_real_coordinator_a_newer_hvac_press_stops_the_older_fan_during_coalescing():
    """Harness evidence for the mirror: fan on, HA restarts, the occupant sets cool 30 s later.

    Before the mirror fix Climate Advisor sent set_hvac_mode off (the OLDER fan press won, undoing the occupant's
    newest action); rule 2 says the newer HVAC-on wins, so the fan must stop and HVAC must stay on.
    """
    import logging

    from tools.sim_harness.session import drive_scenario, make_session

    logging.disable(logging.CRITICAL)
    try:
        scenario = {
            "name": "coalescing_hvac_on_after_fan",
            "use_coordinator": True,
            "skip_startup_coalesce": False,
            "config": {
                "comfort_heat": 68,
                "comfort_cool": 74,
                "setback_heat": 60,
                "setback_cool": 80,
                "fan_mode": "whole_house_fan",
                "fan_entity": "fan.whf",
                "fan_state_feedback": True,
                "door_window_sensors": ["binary_sensor.window"],
            },
            "events": [
                _event("14:00:00", type="external_fan_state_change", entity="fan.whf", state="off"),
                _event("14:00:01", type="sensor_open", entity="binary_sensor.window"),
                _event("14:00:05", type="temp_update", indoor_f=72.0, outdoor_f=80.0),
                _event(
                    "14:00:10",
                    type="classification",
                    day_type="warm",
                    hvac_mode="off",
                    windows_recommended=False,
                    today_high=85,
                    today_low=60,
                    tomorrow_high=85,
                    tomorrow_low=60,
                    trend_direction="stable",
                    trend_magnitude=0,
                    setback_modifier=0,
                ),
                _event("14:01:00", type="external_fan_state_change", entity="fan.whf", state="on"),
                _event("14:02:00", type="simulate_restart"),
                _event(
                    "14:02:30",
                    type="thermostat_state_changed",
                    hvac_mode="cool",
                    temperature=70.0,
                    hvac_action="cooling",
                ),
                _event("14:09:00", type="temp_update", indoor_f=72.0, outdoor_f=80.0),
            ],
        }
        session = make_session(scenario, engine="fakehass", use_coordinator=True)
        try:
            drive_scenario(session, scenario)
            acts = [(a["domain"], a["service"], a["data"].get("hvac_mode")) for a in session.fake_hass.action_log]
        finally:
            session.close()
    finally:
        logging.disable(logging.NOTSET)
    assert ("fan", "turn_off", None) in acts, f"the occupant's newer HVAC-on did not stop the fan: {acts}"
    assert ("climate", "set_hvac_mode", "off") not in acts, "the older fan press undid the occupant's HVAC-on"


class TestSafetyTickBody:
    """The 15 s tick is the backstop for a missed event; the unit/sim tiers cannot run the @callback wrapper."""

    @staticmethod
    def _coord(*, fan_mode, fan_on):
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = object.__new__(mod.ClimateAdvisorCoordinator)
        coord.config = {"fan_mode": fan_mode}
        coord._get_fan_physical_state = lambda: fan_on
        coord._request_whf_safety_check = MagicMock()
        coord._body = types.MethodType(mod.ClimateAdvisorCoordinator._whf_safety_tick_body, coord)
        return coord

    def test_a_physically_running_whole_house_fan_asks_for_a_check(self):
        coord = self._coord(fan_mode="whole_house_fan", fan_on=True)
        coord._body()
        coord._request_whf_safety_check.assert_called_once_with("tick")

    def test_no_check_when_the_fan_is_off_unknown_or_not_a_whole_house_install(self):
        for mode, on in (("whole_house_fan", False), ("whole_house_fan", None), ("hvac_fan", True), ("disabled", True)):
            coord = self._coord(fan_mode=mode, fan_on=on)
            coord._body()
            coord._request_whf_safety_check.assert_not_called()
