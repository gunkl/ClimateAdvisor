"""Issue #1194 (e) F1: the 10 s setpoint read-back verify must not fail a write a slow cloud thermostat simply has not
answered yet.

Occupant outcome: with a Nest/Ecobee-style cloud thermostat that reports CA's setpoint 30-120 s late, the person saw
"Setpoint validation FAILED" in the log and the activity report, and 15 minutes later Climate Advisor re-sent the
setpoint and, after a streak, nudged the thermostat 1 F the wrong way and back -- for a write that had worked. Now the
verify waits for the echo window #1232 already uses (``OWN_WRITE_ECHO_WINDOW_S``, 180 s) while that write is still
outstanding in the echo memory and the thermostat still reports what it had before. A thermostat that never answers
inside the window, or that answers with a different value (clamped / refused), is still a rejection, as before.

Real coordinator + engine, virtual clock (the rig of ``tests/test_remote_offset_gates_1194b.py``), heat and cool, both
fan modes. ``FakeHass.set_echo_delay`` (#1194 e) holds the thermostat's state back like a cloud integration.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import OWN_WRITE_ECHO_WINDOW_S
from custom_components.climate_advisor.own_write_echo import OwnSetpointWrite, outstanding_own_write
from tests.test_remote_offset_gates_1194b import DIRECTIONS, FAN_MODES, FakeState, Night

HEAT_AND_COOL = ["heat", "cool"]


def _night(fan_mode: str, direction: str) -> Night:
    n = Night(fan_mode, direction, bedroom_f=DIRECTIONS[direction]["target"])  # bedroom in band: the loop holds
    n.rejected: list = []
    original = n.ae._emit_event_callback

    def record(name, data):
        if name in ("setpoint_rejected", "setpoint_nudge"):
            n.rejected.append((n.sch.now(), name, data))
        if original:
            original(name, data)

    n.ae._emit_event_callback = record
    n.reasons: list = []
    write = n.ae._set_temperature

    async def recording(target, *a, **k):
        n.reasons.append(str(k.get("reason", "")))
        return await write(target, *a, **k)

    n.ae._set_temperature = recording
    return n


def _write(n: Night) -> float:
    """The thermostat sits 1 F off the sleep target; CA writes the sleep target (every band writer agrees on it)."""
    cs = n.fh.states.get(n.ce)
    before = n.target - n.d["sign"] * 1.0
    n.fh.states.set(n.ce, FakeState(state=cs.state, attributes={**cs.attributes, "temperature": before}))
    n.before = before
    n.ca_writes(n.target, reason="band apply")
    return n.target


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestLateEcho:
    @pytest.mark.parametrize("echo_s", [45, 110])
    def test_a_late_echo_is_not_a_rejection(self, fan_mode, direction, echo_s):
        n = _night(fan_mode, direction)
        n.fh.set_echo_delay(n.ce, echo_s)
        mark = len(n.sp_writes())
        new = _write(n)
        n.wait(12)
        assert n.sp == n.before, "precondition: the 10 s check ran before the thermostat answered"
        n.wait(echo_s + 200)
        assert n.sp == new, "precondition: the echo arrived"
        n.wait(1800)  # past any 15-minute retry and a nudge's 30 s follow-up
        assert n.rejected == [], f"a late echo was treated as a rejection: {n.rejected}"
        assert n.ae._setpoint_reject_streak == 0
        assert set(n.writes_since(mark)) == {new}, f"a nudge was sent: {n.writes_since(mark)}"
        assert not [r for r in n.reasons if r.startswith("retry/")], f"a retry was sent: {n.reasons}"

    def test_no_echo_within_the_window_is_a_rejection(self, fan_mode, direction):
        n = _night(fan_mode, direction)
        n.fh.set_echo_delay(n.ce, 100_000)  # never answers
        start = n.sch.now()
        _write(n)
        n.wait(int(OWN_WRITE_ECHO_WINDOW_S) - 30)
        assert n.rejected == [], "judged before the echo window ended"
        n.wait(250)  # (the periodic band re-assert at +30 s supersedes the first check; its own window ends ~+211 s)
        assert n.rejected and n.rejected[0][1] == "setpoint_rejected", n.rejected
        assert (n.rejected[0][0] - start).total_seconds() >= OWN_WRITE_ECHO_WINDOW_S
        assert n.rejected[0][2]["reported"] == pytest.approx(n.before)
        assert n.ae._setpoint_reject_streak >= 1

    def test_a_different_value_is_a_rejection_at_once(self, fan_mode, direction):
        """The thermostat answered, with something else (clamped to its own limit): no waiting for an echo."""
        n = _night(fan_mode, direction)
        n.fh.set_echo_delay(n.ce, 100_000)
        new = _write(n)
        clamped = new + n.d["sign"] * 0.5 * 4  # 2 F off what CA wrote, and not the value it had before
        cs = n.fh.states.get(n.ce)
        n.fh.states.set(n.ce, FakeState(state=cs.state, attributes={**cs.attributes, "temperature": clamped}))
        n.wait(12)
        assert [e[1] for e in n.rejected] == ["setpoint_rejected"], n.rejected
        assert n.rejected[0][2]["reported"] == pytest.approx(clamped)

    def test_an_instant_echo_still_confirms_at_ten_seconds(self, fan_mode, direction):
        n = _night(fan_mode, direction)
        new = _write(n)
        n.wait(12)
        assert n.sp == new and n.rejected == [] and n.ae._setpoint_reject_streak == 0


def test_outstanding_own_write_is_the_echo_memory_rule():
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    w = OwnSetpointWrite("climate.x", "temperature", 68.0, t0)
    kw = {"entity_id": "climate.x", "attribute": "temperature", "step": 1.0}
    assert outstanding_own_write([w], value=68.0, now=t0 + timedelta(seconds=100), **kw) is w
    assert outstanding_own_write([w], value=68.0, now=t0 + timedelta(seconds=OWN_WRITE_ECHO_WINDOW_S), **kw) is None
    assert outstanding_own_write([w], value=69.0, now=t0 + timedelta(seconds=10), **kw) is None
    assert (
        outstanding_own_write([w], value=68.0, now=t0, entity_id="climate.y", attribute="temperature", step=1.0) is None
    )
