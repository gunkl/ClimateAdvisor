"""Issue #1595: the 130 s mode read-back re-check must not announce "HVAC mode not applied" for a mode change a slow
cloud thermostat has not reported yet.

Occupant outcome: Climate Advisor switches the thermostat's mode (heat to off, heat to cool, ...) on a cloud thermostat
that reports the change 130-180 s later. The person saw "HVAC mode not applied" (a once-per-episode notice, a Status
warning) for a change that took effect seconds after the check. Now, while that write is still outstanding in the #1246
mode echo memory (same 180 s window, ``OWN_WRITE_ECHO_WINDOW_S``) and the thermostat still reports the mode it had
before, the re-check waits for the end of the window -- the mode twin of #1194 (e)'s setpoint read-back wait. A
thermostat that never applies the mode is still announced, at the window end; one that reports a different mode is
announced at 130 s as before.

Real coordinator + engine, virtual clock (the #1194 rig), heat and cool nights, both fan modes.
``FakeHass.set_mode_echo_delay`` holds the thermostat's mode back like a cloud integration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor.automation import MODE_ALIGNMENT_RECHECK_S
from custom_components.climate_advisor.const import OWN_WRITE_ECHO_WINDOW_S
from custom_components.climate_advisor.own_write_echo import (
    OwnModeWrite,
    echo_window_wait_s,
    outstanding_own_mode_write,
)
from tests.test_remote_offset_gates_1194b import DIRECTIONS, FAN_MODES, FakeState, Night

HEAT_AND_COOL = ["heat", "cool"]
NEVER = 100_000
OTHER = {"heat": "cool", "cool": "heat"}


def _rig(fan_mode: str, direction: str) -> Night:
    n = Night(fan_mode, direction, bedroom_f=DIRECTIONS[direction]["target"])  # bedroom in band: the loop holds
    n.notices: list = []
    original = n.ae._emit_event_callback

    def record(name, data):
        if name == "hvac_mode_not_applied" and data.get("kind") == "not_obeyed":
            n.notices.append(((n.sch.now() - n.t), data))
        if original:
            original(name, data)

    n.ae._emit_event_callback = record
    # The coordinator re-applies the plan 30 s after start and then every 300 s; write 10 s after one of those
    # cycles so the whole echo window (write + 180 s) passes before the next one writes again.
    n.wait(40)
    return n


def _switch(n: Night, requested: str, echo_s: float) -> None:
    n.fh.set_mode_echo_delay(n.ce, echo_s)
    n.before = n.mode
    n.t = n.sch.now()
    assert n.run(n.ae._set_hvac_mode(requested, reason="test mode change")).name in ("EXECUTED", "UNCONFIRMED")


def _seconds(n: Night) -> list[float]:
    return [d.total_seconds() for d, _ in n.notices]


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("requested", ["off", "other"])
class TestModeRecheckEchoWindow:
    def _requested(self, requested: str, direction: str) -> str:
        return OTHER[direction] if requested == "other" else requested

    def test_an_echo_at_150_s_is_not_announced(self, fan_mode, direction, requested):
        n = _rig(fan_mode, direction)
        mode = self._requested(requested, direction)
        _switch(n, mode, 150)
        n.wait(MODE_ALIGNMENT_RECHECK_S + 5)
        assert n.mode == n.before, "precondition: the 130 s re-check ran before the thermostat answered"
        n.wait(OWN_WRITE_ECHO_WINDOW_S - MODE_ALIGNMENT_RECHECK_S + 20)
        assert n.mode == mode, "precondition: the echo arrived"
        assert n.notices == [], f"a late mode echo was announced as not applied: {n.notices}"
        assert n.ae._hvac_write_record.episode_reason is None

    def test_no_echo_by_the_window_end_is_announced_once(self, fan_mode, direction, requested):
        n = _rig(fan_mode, direction)
        _switch(n, self._requested(requested, direction), NEVER)
        n.wait(OWN_WRITE_ECHO_WINDOW_S - 10)
        assert n.notices == [], "announced before the echo window ended"
        n.wait(60)
        assert len(n.notices) == 1, n.notices
        assert _seconds(n)[0] >= OWN_WRITE_ECHO_WINDOW_S
        assert n.notices[0][1]["actual_mode"] == n.before

    def test_a_different_mode_is_announced_at_the_recheck(self, fan_mode, direction, requested):
        """The thermostat answered, with a third mode: no waiting for an echo."""
        n = _rig(fan_mode, direction)
        mode = self._requested(requested, direction)
        _switch(n, mode, NEVER)
        third = next(m for m in ("off", "heat", "cool") if m not in (mode, n.before))
        n.wait(60)
        cs = n.fh.states.get(n.ce)
        n.fh.states.set(n.ce, FakeState(state=third, attributes=dict(cs.attributes)))  # no event: not read as occupant
        n.wait(MODE_ALIGNMENT_RECHECK_S - 60 + 5)
        assert len(n.notices) == 1, n.notices
        assert MODE_ALIGNMENT_RECHECK_S <= _seconds(n)[0] < MODE_ALIGNMENT_RECHECK_S + 5
        assert n.notices[0][1]["actual_mode"] == third


def test_outstanding_own_mode_write_is_the_echo_memory_rule():
    t0 = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    heat, cool = OwnModeWrite("heat", t0), OwnModeWrite("cool", t0 + timedelta(seconds=5))
    at = t0 + timedelta(seconds=100)
    assert outstanding_own_mode_write([heat, cool], mode="heat", now=at) is heat
    assert outstanding_own_mode_write([heat, cool], mode="off", now=at) is None
    assert outstanding_own_mode_write([heat], mode="heat", now=t0 + timedelta(seconds=OWN_WRITE_ECHO_WINDOW_S)) is None
    assert echo_window_wait_s(heat, at) == pytest.approx(OWN_WRITE_ECHO_WINDOW_S - 100 + 1)
    assert echo_window_wait_s(heat, t0 + timedelta(seconds=500)) == 1.0
