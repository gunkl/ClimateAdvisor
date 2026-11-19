"""Issue #1272: the post-fan setpoint verifies must not re-send a setpoint a slow cloud thermostat simply has not
answered yet.

Occupant outcome: with a Nest/Ecobee-style cloud thermostat that reports CA's setpoint 30-180 s late, a fan command
shortly after a setpoint write made Climate Advisor send the SAME setpoint again 30 s later
(``post-fan-verify/repair``): an extra thermostat write and an Activity line for a write that had worked. The verifies
now wait for #1232's echo window (``OWN_WRITE_ECHO_WINDOW_S``, 180 s) while that write is still outstanding in the echo
memory and the thermostat still reports what it had before -- the same rule the 10 s read-back verify uses (#1194 e).
A thermostat that never answers inside the window is repaired once, when the window ends; one that shows a different
value (an Ecobee comfort-program revert) is repaired at 30 s as before.

Real coordinator + engine, virtual clock (the #1194 rig), heat and cool. ``hvac_fan`` drives both the fan-on and the
fan-off verify. ``whole_house_fan`` drives the fan-on verify: the house must be open for the fan to start, so its
repair is a blocked write attempt (the fan owns the thermostat); a fan-off verify there never judges a fresh write
(every setpoint write is blocked while the fan runs, and the restore after it is a newer write).
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import OWN_WRITE_ECHO_WINDOW_S
from custom_components.climate_advisor.own_write_echo import outstanding_own_write
from tests.test_remote_offset_gates_1194b import DIRECTIONS, WINDOW, FakeState, Night

HEAT_AND_COOL = ["heat", "cool"]
REPAIR = "post-fan-verify/repair"
# (fan mode, which fan command follows the write). whole_house_fan fan-off: see the module docstring.
CASES = [("hvac_fan", "on"), ("hvac_fan", "off"), ("whole_house_fan", "on")]
NEVER = 100_000
# A newer write or an occupant's dial needs a thermostat the whole-house fan does not own (it blocks both).
HVAC_FAN_CASES = [c for c in CASES if c[0] == "hvac_fan"]


def _rig(fan_mode: str, direction: str, command: str) -> Night:
    """Bedroom in band (the loop holds), every CA setpoint call timestamped; the fan is ready for ``command``."""
    if fan_mode == "whole_house_fan" and direction == "cool":
        # The open house would start nat-vent by itself on a hot night; this test owns the fan command.
        n = Night(fan_mode, direction, bedroom_f=DIRECTIONS[direction]["target"], outdoor_f=95.0)
    else:
        n = Night(fan_mode, direction, bedroom_f=DIRECTIONS[direction]["target"])
    if fan_mode == "whole_house_fan":
        n.fh.states.set(WINDOW, FakeState(state="on"))  # open before HA started: the fan may start, no pause event
    t0 = n.sch.now()
    n.calls = []
    write = n.ae._set_temperature

    async def recording(target, *a, **k):
        n.calls.append(((n.sch.now() - t0).total_seconds(), str(k.get("reason", ""))))
        return await write(target, *a, **k)

    n.ae._set_temperature = recording
    n.t0 = t0
    if command == "off":
        assert n.run(n.ae._activate_fan(reason="setup")).name == "EXECUTED"
        n.wait(900)  # past the fan's toggle floor
    # The coordinator re-applies the plan 30 s after start and then every 300 s; write 10 s after one of those
    # cycles so the whole echo window (write + 180 s) passes before the next one supersedes the write.
    n.wait(40)
    return n


def _now_s(n: Night) -> float:
    return (n.sch.now() - n.t0).total_seconds()


def _write_then_fan(n: Night, command: str, echo_s: float) -> float:
    """The thermostat sits 1 F off the sleep target; CA writes it; the fan command follows 5 s later. Returns t."""
    n.fh.set_echo_delay(n.ce, echo_s)
    cs = n.fh.states.get(n.ce)
    n.before = n.target - n.d["sign"] * 1.0
    n.fh.states.set(n.ce, FakeState(state=cs.state, attributes={**cs.attributes, "temperature": n.before}))
    t = _now_s(n)
    n.ca_writes(n.target, reason="band apply")
    n.wait(5)
    fan = n.ae._activate_fan if command == "on" else n.ae._deactivate_fan
    assert n.run(fan(reason="test fan command")).name == "EXECUTED"
    return t


def _repairs(n: Night, t: float) -> list[float]:
    return [at - t for at, reason in n.calls if reason == REPAIR and at >= t]


def _silently_report(n: Night, temperature: float) -> None:
    """The thermostat's setpoint changes without a state event the coordinator would read as the occupant."""
    cs = n.fh.states.get(n.ce)
    n.fh.states.set(n.ce, FakeState(state=cs.state, attributes={**cs.attributes, "temperature": temperature}))


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize(("fan_mode", "command"), CASES)
class TestPostFanVerifyLateEcho:
    @pytest.mark.parametrize("echo_s", [45, 110])
    def test_a_late_echo_is_not_repaired(self, fan_mode, command, direction, echo_s):
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, echo_s)
        n.wait(35)
        assert n.sp == n.before, "precondition: the 30 s post-fan verify ran before the thermostat answered"
        n.wait(220)  # past the echo window, before the next coordinator cycle
        if fan_mode != "whole_house_fan":
            assert n.sp == n.target, "precondition: the echo arrived"
        assert _repairs(n, t) == [], f"a late echo was re-sent: {n.calls}"

    def test_no_echo_within_the_window_is_repaired_once_at_its_end(self, fan_mode, command, direction):
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, NEVER)
        n.wait(OWN_WRITE_ECHO_WINDOW_S - 20)
        assert _repairs(n, t) == [], "repaired before the echo window ended"
        n.wait(70)
        repairs = _repairs(n, t)
        assert len(repairs) == 1, f"expected exactly one repair at the window end: {n.calls}"
        assert repairs[0] >= OWN_WRITE_ECHO_WINDOW_S

    def test_a_different_value_is_repaired_at_thirty_seconds(self, fan_mode, command, direction):
        """An Ecobee comfort-program revert to another value: the thermostat answered, so no waiting."""
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, NEVER)
        n.wait(15)
        _silently_report(n, n.target - n.d["sign"] * 3.0)  # neither CA's value nor the one it had before
        n.wait(25)
        repairs = _repairs(n, t)
        assert len(repairs) == 1 and 30 <= repairs[0] <= 40, n.calls

    def test_a_revert_after_the_echo_came_back_is_repaired_at_thirty_seconds(self, fan_mode, command, direction):
        """The echo arrived (and was consumed); the program then put the old value back: nothing is outstanding."""
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, 2)
        if fan_mode != "whole_house_fan":
            assert n.sp == n.target, "precondition: the echo arrived"
        writes = getattr(n.ae, "_own_setpoint_writes", [])
        now = n.sch.now()
        assert not [w for w in writes if outstanding_own_write([w], **_echo_kw(n, w, now))], "precondition: consumed"
        n.wait(10)
        _silently_report(n, n.before)
        n.wait(25)
        repairs = _repairs(n, t)
        assert len(repairs) == 1 and 30 <= repairs[0] <= 40, n.calls


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize(("fan_mode", "command"), HVAC_FAN_CASES)
class TestPostFanVerifyWaitIsRejudged:
    """The re-check at the window end re-runs the whole decision, not just the value comparison."""

    def test_a_newer_write_during_the_wait_makes_the_verify_stale(self, fan_mode, command, direction):
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, NEVER)
        n.wait(55)
        n.ca_writes(n.target, reason="newer write")
        n.wait(OWN_WRITE_ECHO_WINDOW_S)
        assert _repairs(n, t) == [], f"a superseded post-fan verify still repaired: {n.calls}"

    def test_a_manual_change_during_the_wait_is_not_repaired(self, fan_mode, command, direction):
        n = _rig(fan_mode, direction, command)
        t = _write_then_fan(n, command, NEVER)
        n.wait(125)  # clear of CA's 120 s command-recency guard, so the dial reads as the occupant
        n.occupant_dials(n.target + n.d["sign"] * 3.0)
        assert n.ae._manual_change_protected(), "precondition: the occupant's change was seen"
        n.wait(120)
        assert _repairs(n, t) == [], f"CA re-sent its setpoint over the occupant's change: {n.calls}"


def _echo_kw(n: Night, w, now) -> dict:
    return {"entity_id": w.entity_id, "attribute": w.attribute, "value": w.value, "now": now, "step": 1.0}
