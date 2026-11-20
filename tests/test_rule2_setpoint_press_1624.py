"""Issue #1624: a thermostat setpoint change while it already cools/heats is the occupant's newest HVAC action.

Occupant story (nightly seed 26009000121): automation is switched off. A window is open. You turn the whole-house fan
on, and three seconds later you turn the AC down to 66.5 F. The thermostat was already in Cool, so only the setpoint
changes. Climate Advisor records the change as your manual override with a 15-minute grace. You switch automation back
on 8 minutes later and Climate Advisor turns your AC Off, saying the fan was turned on after the HVAC. It was not:
your thermostat change was the newer one.

Cause: rule 2 (Issue #1144, newest occupant action wins) stamped ``_occupant_hvac_on_at`` only when the thermostat MODE
changed, while the fan side re-stamps every whole-house-fan press, including one on a fan already running. A setpoint
change in an active mode never counted, so the earlier fan press looked newer.

Fix: ``start_override_confirmation(source="setpoint")`` with the thermostat in an active mode stamps the HVAC action.
On re-enable the newer HVAC action now stops the fan (``STOP_MUTEX_FAN``), and your Cool setting keeps its grace.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind
from tests.test_whf_safety_enforcer import OPEN, _Clock, _engine, _fan_off
from tools.sim_harness.generator import run_day

NIGHTLY_SEED_1624 = 26009000121


@pytest.fixture
def clock():
    c = _Clock()
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", side_effect=c.now),
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
    ):
        yield c


def _hvac_off(engine):
    return [
        c
        for c in engine.hass.services.async_call.call_args_list
        if c.args[:2] == ("climate", "set_hvac_mode") and (c.args[2] or {}).get("hvac_mode") == "off"
    ]


def _setpoint_override(engine, *, old: float, new: float, mode: str) -> None:
    engine.start_override_confirmation(
        "setpoint",
        event_kind=OverrideGraceFsmEventKind.OVERRIDE_DETECTED,
        old_mode=mode,
        new_mode=mode,
        old_setpoint_f=old,
        new_setpoint_f=new,
    )


class TestSetpointChangeStampsTheHvacAction:
    def test_a_setpoint_change_in_cool_is_stamped_as_the_occupants_hvac_action(self, clock):
        engine, _ = _engine(thermostat_mode="cool")
        assert engine._occupant_hvac_on_at is None
        _setpoint_override(engine, old=72.0, new=66.5, mode="cool")
        assert engine._occupant_hvac_on_at == clock.now()

    def test_a_setpoint_change_while_off_is_not_an_hvac_action(self, clock):
        """Control: a thermostat that is Off is not heating or cooling (and its setpoint is ignored, #1325)."""
        engine, _ = _engine(thermostat_mode="off")
        _setpoint_override(engine, old=72.0, new=66.5, mode="off")
        assert engine._occupant_hvac_on_at is None


class TestNewerSetpointBeatsAnOlderFanPress:
    def test_fan_press_then_setpoint_change_stops_the_fan_not_the_ac(self, clock):
        engine, world = _engine(thermostat_mode="cool")
        world["sensors"] = (OPEN,)
        fan_pressed_at = clock.now()
        engine._fan_override_active = True
        engine._fan_override_time = engine._whf_press_time = fan_pressed_at.isoformat()
        clock.advance(3)  # the occupant turns the AC down three seconds after the fan press
        _setpoint_override(engine, old=72.0, new=66.5, mode="cool")

        with patch.object(AutomationEngine, "_occupant_fan_on_at", return_value=fan_pressed_at):
            result = asyncio.run(engine.enforce_whf_safety("automation_enabled"))

        assert result is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 1, "the newer HVAC action stops the fan"
        assert _hvac_off(engine) == [], "the occupant's newer Cool setting is not switched Off"

    def test_control_setpoint_change_then_newer_fan_press_still_suppresses_hvac(self, clock):
        """Mirror control: when the fan press IS the newer action, rule 2 still suppresses the HVAC."""
        engine, world = _engine(thermostat_mode="cool")
        world["sensors"] = (OPEN,)
        _setpoint_override(engine, old=72.0, new=66.5, mode="cool")
        clock.advance(60)
        fan_pressed_at = clock.now()
        engine._fan_override_active = True
        engine._fan_override_time = engine._whf_press_time = fan_pressed_at.isoformat()

        with patch.object(AutomationEngine, "_occupant_fan_on_at", return_value=fan_pressed_at):
            asyncio.run(engine.enforce_whf_safety("automation_enabled"))

        assert _fan_off(engine) == []
        assert len(_hvac_off(engine)) == 1


def test_nightly_seed_1624_keeps_the_occupants_override_through_the_real_coordinator() -> None:
    """End to end: the nightly day that failed, through the real coordinator and the household promises."""
    report = run_day(NIGHTLY_SEED_1624, "coordinator").report
    assert not [(h.ts, h.detail) for h in report.hard if h.promise == "override_respected"]
    assert not [(h.ts, h.detail) for h in report.hard if h.promise == "ac_whf_mutex"]
