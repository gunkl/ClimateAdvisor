"""Issue #1553: the v4 forecast and the plan hold the BEDROOM at the sleep band, as the house does.

Occupant view: on a home with a bedroom (sleep) sensor, CA's remote-sensor outer loop (#1194) raises the hallway
thermostat's setpoint at night until the bedroom reaches the sleep floor. The Status chart's v4 line and the plan's
faded heating strips left that loop out: they held the HALLWAY at the floor, so the bedroom line slid 2-4 F under it
overnight with no heating (one home: 62.4 F against a 66 F floor), then jumped back at wake-up. Now:

* the display forecast emulates the loop with the live rule (``remote_offset.decide_remote_offset``) inside the sleep
  window, and is unchanged outside it;
* the plan's simulation holds the thermostat's room the bedroom gap away from the band in the sleep window;
* decisions are untouched: they read the setpoint-free baseline.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from tests.test_v4_control_node_1432 import _fit, _house_log
from tests.test_v4_display_nodes_1422 import BED, END, THERMO, TZ, _asleep, _cycle_kwargs

SLEEP, WAKE = "22:00", "06:00"  # the fixture's window (``_asleep``)


@pytest.fixture(scope="module")
def home() -> tuple[list[dict], dict]:
    rows = _house_log()
    return rows, _fit(rows)


def _predict(md: dict, kw: dict, loop) -> dict:
    model = p4.from_dict(md)
    return p4.predict(
        model, now=END, readings=kw["readings"], mass_f=None, inputs=kw["inputs"], tz=TZ,
        ca_sleep_node=BED, ca_sleep_time=SLEEP, ca_wake_time=WAKE, sleep_loop=loop,
    )  # fmt: skip


def _emulator(swing: float = 1.0) -> v4_shadow.RemoteLoopEmulator:
    return v4_shadow.RemoteLoopEmulator(swing_heat_f=swing, swing_cool_f=swing, unit="fahrenheit")


def _sleep_floor(kw: dict, floor: float = 66.0) -> dict:
    """The fixture's sleep band floor is 64 F, where its bedroom stays inside the swing; at 66 F the bedroom runs about
    2.5 F under it overnight, the case the occupant saw."""
    inputs = [dataclasses.replace(i, heat_sp_f=floor) if _asleep(i.ts) else i for i in kw["inputs"]]
    return {**kw, "inputs": inputs}


# ------------------------------------------------------------------------------------------------ the forecast
class TestForecast:
    def test_a_loop_that_moves_nothing_reproduces_the_single_rollout_exactly(self, home):
        """Stepping the rollout one step at a time (to let the loop act) changes no number by itself."""
        rows, md = home
        kw = _cycle_kwargs(md, rows)
        plain = _predict(md, kw, None)
        stepped = _predict(md, kw, p4.SleepLoop())
        assert plain.keys() == stepped.keys()
        for k in plain:
            assert plain[k] == stepped[k], k

    def test_the_bedroom_is_defended_at_night_when_it_runs_colder_than_the_hallway(self, home):
        rows, md = home
        kw = _sleep_floor(_cycle_kwargs(md, rows))
        plain = _predict(md, kw, None)
        loop = _predict(md, kw, _emulator())
        night = [t for t, _ in plain[BED] if _asleep(t)]
        assert night, "the forecast must reach the sleep window"
        low_plain = min(v for t, v in plain[BED] if _asleep(t))
        low_loop = min(v for t, v in loop[BED] if _asleep(t))
        assert low_loop > low_plain + 0.3, (low_plain, low_loop)
        # the thermostat's room is raised (that is how the bedroom is reached), never the reverse
        assert max(v for t, v in loop[THERMO] if _asleep(t)) > max(v for t, v in plain[THERMO] if _asleep(t))

    def test_nothing_before_the_first_sleep_window_changes(self, home):
        rows, md = home
        kw = _sleep_floor(_cycle_kwargs(md, rows))
        plain = _predict(md, kw, None)
        loop = _predict(md, kw, _emulator())
        first = min(t for t, _ in plain[BED] if _asleep(t))
        for k in plain:
            before = [(a, b) for a, b in zip(plain[k], loop[k], strict=True) if a[0] < first]
            assert before and all(a == b for a, b in before), k

    def test_run_cycle_passes_the_loop_to_the_display_cycle_only(self, home):
        rows, md = home
        kw = _sleep_floor(_cycle_kwargs(md, rows))
        shown, _ = v4_shadow.run_cycle(display=True, sleep_loop=_emulator(), **kw)
        plain, _ = v4_shadow.run_cycle(display=True, **kw)
        assert shown[BED] != plain[BED]
        # decisions: the setpoint-free baseline cannot be moved by the loop
        base_loop, _ = v4_shadow.run_cycle(baseline=True, sleep_loop=_emulator(), **kw)
        base, _ = v4_shadow.run_cycle(baseline=True, **kw)
        assert base_loop == base


# ------------------------------------------------------------------------------------------------ the live rule
class TestEmulatorFollowsTheLiveRule:
    def _call(self, emu, target, *, in_window=True, heat=66.0, cool=72.0, running=0):
        return emu.setpoints(in_window=in_window, target_f=target, heat_sp=heat, cool_sp=cool, running=running)

    def test_raises_one_step_when_the_bedroom_is_more_than_the_swing_below(self):
        emu = _emulator(1.0)
        assert self._call(emu, 65.5) == (66.0, 72.0)  # inside the swing: hold
        assert self._call(emu, 64.9) == (68.0, 72.0)  # 1.1 below: one 2 F step
        assert self._call(emu, 64.9) == (70.0, 72.0)  # still below, idle, past the startup wait: another
        assert self._call(emu, 64.9) == (70.0, 72.0)  # capped at 4 F

    def test_never_raises_while_heating_and_steps_down_once_past_the_target(self):
        emu = _emulator(1.0)
        self._call(emu, 64.0)
        assert self._call(emu, 64.0, running=1) == (68.0, 72.0)  # equipment running: hold
        assert self._call(emu, 67.5) == (66.0, 72.0)  # 1.5 above the target: step down
        assert self._call(emu, 67.5) == (66.0, 72.0)  # nothing left to lower

    def test_the_offset_never_crosses_the_opposite_band_edge(self):
        emu = _emulator(1.0)
        assert self._call(emu, 60.0, heat=66.0, cool=67.0)[0] == pytest.approx(67.0)

    def test_cleared_outside_the_window_and_when_a_side_is_not_armed(self):
        emu = _emulator(1.0)
        self._call(emu, 64.0)
        assert self._call(emu, 64.0, in_window=False) == (66.0, 72.0)
        self._call(emu, 64.0)
        assert self._call(emu, 64.0, heat=None) == (None, 72.0)
        assert self._call(emu, 64.9) == (68.0, 72.0)  # starts again from the plain floor

    def test_a_warm_bedroom_lowers_the_cool_edge(self):
        emu = _emulator(1.0)
        assert self._call(emu, 73.5) == (66.0, 70.0)

    def test_celsius_homes_step_one_degree_capped_at_two(self):
        emu = v4_shadow.RemoteLoopEmulator(swing_heat_f=1.0, swing_cool_f=1.0, unit="celsius")
        hsp = [self._call(emu, 60.0)[0] for _ in range(3)]
        assert hsp == pytest.approx([66.0 + 1.8, 66.0 + 3.6, 66.0 + 3.6])


# ------------------------------------------------------------------------------------------------ the plan
def _seg(asleep: bool, floor: float = 66.0, ceiling: float = 72.0) -> ot.Segment:
    t = datetime(2026, 11, 14, 1, 0, tzinfo=TZ)
    return ot.Segment(t, t, floor, ceiling, "floor", "sleep" if asleep else "comfort", "heat", asleep, "home")


class TestPlanEdges:
    def _snap(self, gap, label="bedroom sensor"):
        from tests.timeline_fixtures import make_snapshot

        return dataclasses.replace(make_snapshot("cold", sleep_sensor=label), sleep_gap_f=gap)

    def test_a_colder_bedroom_raises_the_heat_edge_by_the_gap(self):
        assert ot._sleep_loop_edges(self._snap(2.3), _seg(True)) == pytest.approx((68.3, 72.0))

    def test_capped_like_the_live_offset_and_by_the_opposite_edge(self):
        assert ot._sleep_loop_edges(self._snap(6.0), _seg(True)) == pytest.approx((70.0, 72.0))
        assert ot._sleep_loop_edges(self._snap(6.0), _seg(True, 66.0, 67.0)) == pytest.approx((67.0, 67.0))

    def test_a_warmer_bedroom_lowers_the_cool_edge(self):
        assert ot._sleep_loop_edges(self._snap(-1.5), _seg(True)) == pytest.approx((66.0, 70.5))

    def test_unchanged_awake_without_a_sleep_sensor_or_a_reading(self):
        assert ot._sleep_loop_edges(self._snap(2.3), _seg(False)) == (66.0, 72.0)
        assert ot._sleep_loop_edges(self._snap(2.3, label=None), _seg(True)) == (66.0, 72.0)
        assert ot._sleep_loop_edges(self._snap(None), _seg(True)) == (66.0, 72.0)

    def test_the_plan_heats_more_at_night_when_the_bedroom_is_colder(self):
        from tests.timeline_fixtures import make_snapshot

        def night_heat(gap):
            snap = dataclasses.replace(make_snapshot("cold", sleep_sensor="bedroom sensor"), sleep_gap_f=gap)
            tl = ot.build_timeline(snap)
            return sum(r.minutes for r in tl.runs if r.mode == "heat")

        assert night_heat(2.5) > night_heat(0.0)


# ------------------------------------------------------------------------------------------------ one rule (#1554)
def test_the_night_card_the_chart_and_the_live_rule_share_one_emulated_loop():
    """Issue #1554: ``night_strategy`` had its own copy of the loop (Fahrenheit steps on Celsius homes, a 1 F edge
    clamp, the offset kept through a pause). It now uses the emulator that the v4 display forecast uses."""
    import inspect

    from custom_components.climate_advisor import night_strategy, remote_offset

    assert v4_shadow.RemoteLoopEmulator is remote_offset.RemoteLoopEmulator
    src = inspect.getsource(night_strategy._simulate)
    assert "RemoteLoopEmulator(" in src
    assert "REMOTE_OFFSET_STEP_F" not in src and "REMOTE_OFFSET_CAP_F" not in src
