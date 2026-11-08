"""Issue #1194 (c)+(d) integration, and the #1194 (b) window-open gap (F1).

Occupant outcomes:

* Status never says the bedroom loop is off while the hallway thermostat still holds the bedroom's extra degrees: in
  the 2-minute exit grace (and while a refused write is backing off) it says the offset is ENDING; at bedtime, before
  the bedtime band lands, it says the loop is WAITING for the bedtime setting instead of "holding"; and when the
  bedroom sensor is removed from the configuration mid-night it keeps describing the clearing instead of going blank.
* A window left open past its debounce with nobody owning the thermostat (no pause started: e.g. the sensor went dark
  after reading open) no longer leaves the hallway at the sleep target +/- 4 F: the loop puts the plain target back,
  once, in the thermostat's current mode (no heat/cool is turned on). With an owner (override, pause, away, nat-vent,
  whole-house fan) it writes nothing.

Runs on the real coordinator + engine rig of ``tests/test_remote_offset_context_1194c.py``, heat and cool, both fan
modes.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.remote_offset import (
    RemoteLoopState,
    RemoteOffsetGate,
    classify_loop_state,
    remote_offset_view,
)
from tests.test_remote_offset_context_1194c import (
    ENTRY_BEDROOM,
    GATES,
    HEAT_AND_COOL,
    CtxNight,
    TestExitDuringBackOff,
    _night_before_wake,
    at,
)
from tests.test_remote_offset_gates_1194b import FAN_MODES


def _view(n: CtxNight) -> dict:
    with n.sch.installed():
        st = n.ae.remote_offset_state()
    assert st is not None, "Status went blank while the loop still has something to describe"
    return remote_offset_view(st, "fahrenheit")


def _state(n: CtxNight) -> RemoteLoopState | None:
    with n.sch.installed():
        st = n.ae.remote_offset_state()
    return None if st is None else st.state


# ---------------------------------------------------------------------------------------------------------------------
# (a) exit grace / back-off: CLEARING, never INACTIVE while the thermostat still holds target + offset
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestStatusAtExit:
    def test_the_exit_grace_shows_the_offset_ending_then_nothing(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        n.defer("handle_morning_wakeup")
        n.defer("apply_classification")
        n.run_to(6, 30, 30)  # the context ended at 06:30; the grace (120 s) is running; the thermostat holds the cap
        assert n.ae._remote_context_key is None and n.sp == n.target + n.sign * 4.0, "precondition"
        v = _view(n)
        assert v["state"] == "clearing" and v["detail"] == "exit_grace", v
        assert v["offset_applied_f"] == pytest.approx(n.sign * 4.0)
        assert v["text"].startswith("Bedroom offset ending - thermostat still set"), v["text"]
        assert "clearing" in v["text"]
        assert v["expected_setpoint"] is None, "CA no longer intends the offset: the band-edge comparison applies"
        n.run_to(6, 33, 30)  # past the 120 s exit grace and the next 1-minute check
        assert n.sp == n.target, "precondition: the loop's one plain write landed"
        assert _state(n) is RemoteLoopState.INACTIVE

    def test_a_wake_band_that_lands_leaves_nothing_to_clear(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        n.run_to(6, 33, 0)
        assert n.sp == n.day
        assert _state(n) is RemoteLoopState.INACTIVE

    def test_an_exit_during_the_back_off_shows_the_offset_ending(self, fan_mode, direction):
        n, healthy, _failed_at = TestExitDuringBackOff()._backed_off(fan_mode, direction)
        n.defer("handle_morning_wakeup")
        n.defer("apply_classification")
        n.run_to(6, 34, 0)  # past the grace, still inside the 15-minute back-off; the thermostat holds +2
        assert n.ae._remote_context_key is None and n.ae._remote_write_failed_at is not None, "precondition"
        v = _view(n)
        assert v["state"] == "clearing" and v["detail"] == "backing_off", v
        assert "retrying after a refused write" in v["text"]
        n.heal_setpoints(healthy)
        n.run_to(7, 0, 0)
        assert n.sp == n.target and _state(n) is RemoteLoopState.INACTIVE

    def test_an_owner_at_exit_is_not_shown_as_our_offset_clearing(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        n.defer("handle_morning_wakeup")
        n.defer("apply_classification")
        n.ae._occupancy_mode = "away"  # an owner holds the thermostat as the context ends
        n.run_to(6, 30, 30)
        assert _state(n) is RemoteLoopState.INACTIVE


# ---------------------------------------------------------------------------------------------------------------------
# (b) entry hold: WAITING for the bedtime setting, not HOLDING
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestStatusAtEntry:
    def test_the_entry_hold_says_waiting_for_the_bedtime_setting(self, fan_mode, direction):
        n = CtxNight(at(20, 28), fan_mode, direction, bedroom_f=ENTRY_BEDROOM[direction])
        n.ca_writes(n.day, reason="evening band")
        bedtime = n.defer("handle_bedtime")
        n.defer("apply_classification")
        n.run_to(20, 31, 0)
        assert n.ae._remote_context_key is not None, "precondition: the context began"
        v = _view(n)
        assert v["state"] == "waiting" and v["detail"] == "context_band", v
        assert v["text"] == "Bedroom offset starting: waiting for the bedtime setting"
        n.restore("handle_bedtime", bedtime)
        n.run_routine(bedtime)  # the sleep band lands: the hold is over
        assert _state(n) is not RemoteLoopState.WAITING


# ---------------------------------------------------------------------------------------------------------------------
# (c) the sensor unconfigured while an offset is outstanding: Status keeps describing the clear
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_an_unconfigured_sensor_with_an_offset_outstanding_is_still_described(fan_mode, direction):
    n = _night_before_wake(fan_mode, direction)
    n.defer("handle_morning_wakeup")
    n.defer("apply_classification")
    n.ae.config["sleep_indoor_temp_entity"] = None
    assert n.sp == n.target + n.sign * 4.0, "precondition: the thermostat holds the cap"
    v = _view(n)
    assert v["state"] == "clearing", v
    n.wait(60 * 5)
    assert n.sp == n.target, "precondition: the check cleared it"
    with n.sch.installed():
        assert n.ae.remote_offset_state() is None, "nothing left to describe once cleared"


def test_classify_clearing_and_waiting_truth_table():
    common = {"fresh": True, "last_reason": None, "offset_f": 0.0}
    assert classify_loop_state(in_context=False, gate=RemoteOffsetGate.PROCEED, outstanding=True, **common) is (
        RemoteLoopState.CLEARING
    )
    assert classify_loop_state(in_context=False, gate=RemoteOffsetGate.CLOSED, outstanding=True, **common) is (
        RemoteLoopState.INACTIVE
    )
    assert classify_loop_state(in_context=False, gate=RemoteOffsetGate.PROCEED, **common) is RemoteLoopState.INACTIVE
    assert classify_loop_state(in_context=True, gate=RemoteOffsetGate.PROCEED, awaiting_band=True, **common) is (
        RemoteLoopState.WAITING
    )
    # a gate outranks the entry hold
    assert classify_loop_state(in_context=True, gate=RemoteOffsetGate.CLOSED, awaiting_band=True, **common) is (
        RemoteLoopState.GATED
    )


# ---------------------------------------------------------------------------------------------------------------------
# F1: CLOSED (window open past its debounce) with NO owner writing -> the plain target, once
# ---------------------------------------------------------------------------------------------------------------------
def _window_held_open_no_pause(n: CtxNight) -> None:
    """A monitored sensor holds open past its debounce (the #1181 dark-sensor hold) and no pause started."""
    n.ae._sensor_hold_callback = lambda: True
    n.ae._sensor_debounce_pending_callback = lambda: False
    n.ae._sensor_settled_open_callback = lambda: True  # the per-sensor read the open-window guards use (#1229)


def _whf_owns(n: CtxNight) -> None:
    n.ae._whf_owns_hvac = lambda: True


OWNERS = {**{k: v[0] for k, v in GATES.items()}, "whf": _whf_owns}


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestWindowOpenNoOwner:
    def test_the_plain_target_is_written_once(self, fan_mode, direction):
        n = CtxNight(at(1, 50), fan_mode, direction)
        n.to_cap()
        mode = n.mode
        mark = len(n.sp_writes())
        start = n.sch.now()
        _window_held_open_no_pause(n)
        with n.sch.installed():
            assert n.ae._remote_offset_gate_and_reason() == (RemoteOffsetGate.CLOSED, "window_open"), "precondition"
        assert not n.ae._paused_by_door
        n.wait(90)
        assert n.sp == n.target, f"the offset stayed with a window open: {n.writes_since(mark)}"
        assert n.mode == mode, "the loop must never change the thermostat's mode (rule 3)"
        n.cold_bedroom()
        n.wait(900)
        assert n.distinct_since(mark) == [n.target], f"exactly one plain write: {n.writes_since(mark)}"
        assert len(n.loop_writes(start)) == 1

    def test_a_thermostat_already_at_the_plain_target_gets_no_write(self, fan_mode, direction):
        n = CtxNight(at(1, 50), fan_mode, direction)
        n.ca_writes()
        mark = len(n.sp_writes())
        _window_held_open_no_pause(n)
        n.wait(600)
        assert n.writes_since(mark) == []

    @pytest.mark.parametrize("owner", sorted(OWNERS))
    def test_an_owner_holding_the_thermostat_gets_no_loop_write(self, fan_mode, direction, owner):
        n = CtxNight(at(1, 50), fan_mode, direction)
        n.to_cap()
        n.wait(600)  # the loop's own writes have settled (no echo window open for the occupant's change)
        OWNERS[owner](n)
        start = n.sch.now()
        _window_held_open_no_pause(n)
        n.cold_bedroom()
        n.wait(900)
        assert n.loop_writes(start) == [], f"the loop wrote over the owner ({owner}): {n.loop_writes(start)}"

    def test_a_thermostat_that_ignores_the_write_is_not_hammered(self, fan_mode, direction):
        """At most one loop write per CLOSED episode, even when the live setpoint never changes."""
        n = CtxNight(at(1, 50), fan_mode, direction)
        n.to_cap()
        orig = n.fh.services.async_call

        async def swallowed(domain, service, data=None, *a, **k):
            if service == "set_temperature":
                return None  # accepted by the cloud, never applied
            return await orig(domain, service, data, *a, **k)

        n.fh.services.async_call = swallowed
        start = n.sch.now()
        _window_held_open_no_pause(n)
        n.wait(900)
        assert len(n.loop_writes(start)) == 1, n.loop_writes(start)


# ---------------------------------------------------------------------------------------------------------------------
# P2: the dashed "Thermostat set" series has a legend item (only while it exists) and a hover value
# ---------------------------------------------------------------------------------------------------------------------
def _index_html() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # classic page + the shared chart module (R6fe moved the chart code out of the page)
    fe = root / "custom_components" / "climate_advisor" / "frontend"
    return (
        fe.joinpath("index.html").read_text(encoding="utf-8")
        + "\n"
        + fe.joinpath("chart_v2.js").read_text(encoding="utf-8")
    )


def test_the_thermostat_set_series_has_a_legend_item_shown_only_with_the_series():
    html = _index_html()
    assert 'id="legend-thermostat-set" style="display:none"' in html, "legend item missing or shown by default"
    assert "<span>Thermostat set</span>" in html
    assert "_cmdLegend.style.display = _hasCommanded ? '' : 'none'" in html
    assert "if (_hasCommanded) {\n      datasets.push({\n        caId: 'ov:target', label: 'Thermostat set'," in html


def test_the_hover_panel_shows_the_thermostat_set_value():
    """Chart v2 (#1230 R2): the readout's Overlays row reads the chart datasets, skipping hidden ones and gaps."""
    html = _index_html()
    body = html.split("function _renderReadout(", 1)[1].split("\n  function clearChartPin", 1)[0]
    # both ov:target datasets ('Target' and 'Thermostat set'); humidity / feels-like are series since Issue #1414
    assert "ds.caId !== 'ov:target'" in body
    assert "ds.hidden" in body, "a hidden (toggled-off) series must not show a hover value"
    assert "if (v == null) return;" in body, "no value across a gap (null rows outside offset stretches)"
