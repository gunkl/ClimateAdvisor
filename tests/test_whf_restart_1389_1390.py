"""Issues #1389 / #1390: a Home Assistant restart while the whole-house fan ventilates through open windows.

Occupant story (2026-11-06, 17:15). The windows are open and Climate Advisor is cooling the house with the
whole-house fan. Home Assistant restarts. The door/window sensor group reads "unavailable" from the shutdown until
its members load, about 1.5 minutes into the new start. Climate Advisor counted the dark group as "closed" after
60 s -- a minute measured from the shutdown, while Home Assistant was still starting -- decided the house was
sealed, sent the fan off and e-mailed "door/window sensor unavailable" (#1389). Four minutes later the startup
reconcile wanted the fan off (the house had cooled below the comfort floor), but the 5-minute toggle floor
deferred it and nothing asked again: the fan kept running, unmanaged, with the heating held behind it (#1390).

After the fix: a dark sensor's 60 s grace waits for Home Assistant to finish starting, and a reconcile turn-off
the floor defers is re-run when it can land.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import custom_components.climate_advisor.coordinator as coordinator_module
from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.whf_safety_decision import (
    WHF_SENSOR_UNKNOWN_GRACE_S,
    house_is_sealed,
    sensor_reading,
)
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

_NOW = datetime(2026, 11, 7, 1, 16, 4, tzinfo=UTC)
GROUP = "binary_sensor.group_contact_sensors_climate_advisor"


# ---------------------------------------------------------------------------
# #1389: the one sensor reading every "is the house sealed" judge shares
# ---------------------------------------------------------------------------


def _dark(age_s: float, readable_for_s: float | None):
    return sensor_reading(
        state="unavailable", is_open=False, age_s=age_s, since_start_s=60.0, readable_for_s=readable_for_s
    )


@pytest.mark.parametrize(
    ("readable_for_s", "sealed"),
    [
        (0.0, False),  # HA still starting: the dark group is loading, not dead -- counted open
        (30.0, False),  # HA started 30 s ago: inside the grace
        (WHF_SENSOR_UNKNOWN_GRACE_S + 30.0, True),  # dark 90 s after HA started: dead, counted closed
        (None, True),  # a reload (HA was running): the sensor's own 67 s age decides, as before
    ],
)
def test_the_grace_for_a_dark_sensor_waits_for_home_assistant_to_start(readable_for_s, sealed):
    # The production timeline: dark since the shutdown 67 s ago (17:14:57 -> 17:16:04).
    assert house_is_sealed((_dark(67.0, readable_for_s),)) is sealed


def test_a_readable_sensor_is_unaffected_by_the_start():
    closed = sensor_reading(state="off", is_open=False, age_s=5.0, since_start_s=5.0, readable_for_s=0.0)
    opened = sensor_reading(state="on", is_open=True, age_s=5.0, since_start_s=5.0, readable_for_s=0.0)
    assert house_is_sealed((closed,)) is True, "a sensor reading closed is closed, start-up or not"
    assert house_is_sealed((opened,)) is False


def _coordinator(*, ha_starting: bool, readable_s_ago: float | None):
    """A real coordinator's real sensor reading over one dark group (dark since the shutdown, 67 s ago)."""
    coord = object.__new__(coordinator_module.ClimateAdvisorCoordinator)
    coord.config = {CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whf", "climate_entity": "climate.t"}
    coord._resolved_sensors = [GROUP]
    coord._whf_safety_started_at = _NOW - timedelta(seconds=60)
    coord._whf_ha_starting = ha_starting
    coord._whf_sensors_readable_at = _NOW - timedelta(seconds=readable_s_ago) if readable_s_ago is not None else None
    group = MagicMock()
    group.state, group.last_changed = "unavailable", _NOW - timedelta(seconds=67)
    coord.hass = MagicMock()
    coord.hass.states.get = lambda eid: group if eid == GROUP else None
    coord._state_is_open = lambda st: False
    return coord


@pytest.mark.parametrize(
    ("ha_starting", "readable_s_ago", "sealed"),
    [
        (True, None, False),  # #1389: 60 s after Climate Advisor started, HA still starting -- not sealed
        (False, 30.0, False),  # HA started 30 s ago
        (False, 90.0, True),  # a dead sensor is still acted on, 60 s after HA started
        (False, None, True),  # reload control: unchanged
    ],
)
def test_the_coordinator_reading_follows_home_assistants_start(ha_starting, readable_s_ago, sealed):
    coord = _coordinator(ha_starting=ha_starting, readable_s_ago=readable_s_ago)
    with patch.object(coordinator_module.dt_util, "utcnow", return_value=_NOW):
        readings, stale = coord._whf_sensor_readings()
    assert house_is_sealed(readings) is sealed
    # The "door/window sensor unavailable" notice follows the same reading (no e-mail while HA is starting).
    assert (stale == [GROUP]) is sealed


def test_home_assistant_starting_arms_the_recheck_for_a_still_dark_sensor():
    coord = _coordinator(ha_starting=True, readable_s_ago=None)
    coord._arm_whf_unknown_sensor_check = MagicMock()
    with patch.object(coordinator_module.dt_util, "utcnow", return_value=_NOW):
        coord._on_whf_ha_started()
    assert coord._whf_ha_starting is False
    assert coord._whf_sensors_readable_at == _NOW
    coord._arm_whf_unknown_sensor_check.assert_called_once()


# ---------------------------------------------------------------------------
# #1390: a reconcile turn-off the 5-minute floor defers is re-run when it can land
# ---------------------------------------------------------------------------


def _whf_coordinator():
    cfg = {
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "fan_entity": "fan.whf",
        "fan_state_feedback": True,
        "door_window_sensors": [GROUP],
        "comfort_heat": 70,
        "comfort_cool": 75,
    }
    co, fh, sch, ev = build_headless_coordinator(config=cfg, climate_state="off", skip_startup_coalesce=True)
    co._read_live_remote_timer_provenance = lambda: None
    co._resolved_sensors = [GROUP]
    fh.states.set(GROUP, FakeState(state="on", attributes={}))
    fh.states.set("fan.whf", FakeState(state="on", attributes={}))
    fh.states.set(
        co.config["climate_entity"],
        FakeState(
            state="off",
            attributes={
                "current_temperature": 69.4,
                "hvac_action": "idle",
                "fan_mode": "auto",
                "fan_modes": ["auto", "on"],
                "hvac_modes": ["off", "heat", "cool"],
            },
        ),
    )
    return co, fh, sch, ev


def _fan_offs(fh) -> list[dict]:
    return [c for c in fh.action_log if (c["domain"], c["service"]) == ("fan", "turn_off")]


def test_a_deferred_reconcile_turn_off_lands_once_the_floor_allows_it():
    co, fh, sch, _ev = _whf_coordinator()
    ae = co.automation_engine
    # #1389's stop 220 s ago started the 5-minute toggle floor.
    ae._fan_toggle_command_time = sch.now() - timedelta(seconds=220)
    with sch.installed():
        # Indoor 69.4 is below the 70 comfort floor: the restart reconcile wants the fan off.
        run_coro(
            ae.reconcile_fan_on_startup(
                indoor=69.4, outdoor=68.7, thermostat_fan_running=True, any_sensor_open=True, trigger="ha_restart"
            )
        )
        sch.advance_to(sch.now())
        assert not _fan_offs(fh), "the 5-minute floor must still defer the turn-off (control)"
        assert ae._deferred_reconcile_cancel is not None, "#1390: nothing re-runs the deferred turn-off"

        sch.advance_to(sch.now() + timedelta(seconds=90))  # past the floor (300 s since the last toggle)

    offs = _fan_offs(fh)
    assert len(offs) == 1, f"#1390: the deferred fan-off never went out (fan left running, unmanaged): {offs}"
    assert ae._fan_active is False
    assert ae._deferred_reconcile_cancel is None


def test_a_turn_off_that_is_not_deferred_arms_no_retry():
    """Control: with no recent toggle the reconcile stops the fan at once, as before."""
    co, fh, sch, _ev = _whf_coordinator()
    ae = co.automation_engine
    with sch.installed():
        run_coro(
            ae.reconcile_fan_on_startup(
                indoor=69.4, outdoor=68.7, thermostat_fan_running=True, any_sensor_open=True, trigger="ha_restart"
            )
        )
        sch.advance_to(sch.now())
    assert len(_fan_offs(fh)) == 1
    assert ae._deferred_reconcile_cancel is None


def test_the_retry_is_cancelled_on_unload():
    co, _fh, sch, _ev = _whf_coordinator()
    ae = co.automation_engine
    ae._fan_toggle_command_time = sch.now() - timedelta(seconds=220)
    with sch.installed():
        run_coro(
            ae.reconcile_fan_on_startup(
                indoor=69.4, outdoor=68.7, thermostat_fan_running=True, any_sensor_open=True, trigger="ha_restart"
            )
        )
        assert ae._deferred_reconcile_cancel is not None
        ae.cleanup()
    assert ae._deferred_reconcile_cancel is None
