"""Issue #1256: a restart within 2 minutes of a setpoint change must not teach the swing a command jump.

What the occupant experiences: Climate Advisor (or the occupant) moves the setpoint and Home Assistant
restarts within the next 2 minutes. The next heat-on (or cool-on) was learned as the thermostat's own
hysteresis when it was really the jump from that setpoint change, inflating the learned swing - and
from #1194 on the remote-sensor loop holds the room within target +/- swing, so an inflated swing
loosens it beyond the occupant's own thermostat.

Cause (design gap in #1234, 205ca38): ``_last_setpoint_change_ts`` is in memory only, so after a restart
the 120 s exclusion (``THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S``) had no change to measure from.
Fix: setup counts as a setpoint change. At most one legitimate sample is lost per restart; a jump is
never accepted.

Driven through the REAL thermostat listener and a real LearningEngine (the #1234 rig).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.const import THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S as W
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from tests.test_swing_hysteresis_1234 import _MODES, _T0, _past, _Rig, _settled

_DT = "custom_components.climate_advisor.coordinator.dt_util"


@pytest.fixture(params=["heat", "cool"])
def mode(request) -> str:
    return request.param


def _restart(before: _Rig, tmp_path, at) -> _Rig:
    """A new coordinator process at ``at``: same learning DB and thermostat state, no in-memory stamps."""
    after = _Rig(tmp_path, before.mode)
    after.setpoint, after.action, after.state_mode = before.setpoint, before.action, before.state_mode
    after._indoor, after.indoor_attr = before._indoor, before.indoor_attr
    assert after.coord._last_setpoint_change_ts is None  # what a restart leaves today
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = at
        # What async_setup does on startup (the call order is pinned by the last test). Looked up with
        # getattr so a revert fails on the learned jump itself, not on a missing name.
        stamp = getattr(after.coord, "_stamp_setpoint_change_at_startup", None)
        if stamp is not None:
            stamp(at)
    return after


def _jump_then_restart(tmp_path, mode) -> tuple[_Rig, float]:
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    new_sp = sp + (4.0 if mode == "heat" else -4.0)  # a 4 F jump: the start would read 4 F "past" it
    rig.fire(_T0, setpoint=new_sp, indoor=sp)
    return _restart(rig, tmp_path, _T0 + timedelta(seconds=60)), new_sp


def test_on_edge_inside_the_window_after_a_restart_is_not_sampled(tmp_path, mode):
    """Setpoint change, restart 60 s later, equipment starts at 90 s: the jump is not learned."""
    rig, new_sp = _jump_then_restart(tmp_path, mode)
    rig.fire(_T0 + timedelta(seconds=90), action=_MODES[mode]["running"], indoor=_past(mode, new_sp, 4.0))
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, new_sp, 0.5))
    assert rig.count == 0
    assert rig.swing is None


def test_on_edge_after_the_window_following_a_restart_is_sampled(tmp_path, mode):
    """Positive control: a start 130 s after the restart is the thermostat's hysteresis again."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    restart_at = _T0 + timedelta(hours=1)
    rig = _restart(rig, tmp_path, restart_at)
    t_on = restart_at + timedelta(seconds=W + 10)
    rig.fire(t_on, action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    rig.fire(t_on + timedelta(minutes=8), action="idle", indoor=_past(mode, sp, 0.2))
    assert rig.count == 1
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5


def test_async_setup_stamps_before_the_thermostat_listener_exists():
    """The stamp must be in place before the first thermostat event can be processed."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ClimateAdvisorCoordinator.async_setup)))
    stamp_line = listener_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "_stamp_setpoint_change_at_startup" and stamp_line is None:
                stamp_line = node.lineno
            if node.func.attr == "_async_thermostat_changed" and listener_line is None:
                listener_line = node.lineno
        if isinstance(node, ast.Attribute) and node.attr == "_async_thermostat_changed" and listener_line is None:
            listener_line = node.lineno
    assert stamp_line is not None, "async_setup must stamp the setpoint-change time"
    assert listener_line is not None
    assert stamp_line < listener_line
