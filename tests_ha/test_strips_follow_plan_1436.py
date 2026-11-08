"""Real-HA test for Issue #1436: the chart's faded activity strips follow the plan.

Occupant story: a warm September afternoon, the living-room window open and the whole-house fan running, the house
cooling toward its 68 F floor as the evening air drops. The plan says "7:00 PM: close the windows before the house
drifts below 68F". Before the fix the faded strips under the chart (what the plan expects) came from
``chart_data.predicted_activity``, an older predictor that ignored the floor and the close step, and drew the windows
plan and natural ventilation again from 11 PM to 2 AM. Now each faded strip is a span of the plan.

The pending golden ``tools/simulations/pending/issue_1436_*`` (unsigned, for owner review) is driven through real Home
Assistant and the real coordinator by the same adapter as ``test_goldens_realha.py``; the shared capture
(``scenario_model.capture_next_action`` -> ``chart_strips``) must show strips that follow the plan real HA made, and the
run must log no exception from Climate Advisor. Real HA's plan closes at 9:00 PM, not 7:00 PM (Issue #1440, see the
note in the test).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness import divergence

pytestmark = pytest.mark.house

_PATH = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "simulations"
    / "pending"
    / "issue_1436_warm_day_evening_floor_close_strips_follow_plan__whf.json"
)


async def test_pending_golden_strips_follow_the_plan_on_real_ha(hass, freezer, tmp_path) -> None:
    scenario = json.loads(_PATH.read_text(encoding="utf-8"))
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    dump = divergence.dump_run(
        _PATH.stem, "real", scenario, result, use_coordinator=True, dispatch=session.dispatch_modes, notes=session.notes
    )
    assert not dump["callback_errors"], dump["callback_errors"][:3]
    capture = result.engine_state["_next_action"]
    strips = capture["chart_strips"]
    # The strips follow the plan real HA made, whatever its close time: P8 chart_strips finds nothing.
    assert strips["agree"] is True and strips["problems"] == [], strips
    # Each faded Win plan span is one of the plan's window periods, ending exactly at its close step, and the fan and
    # nat-vent strips are the same spans (the plan's session runs while its window is open, never after the close).
    steps = capture["timeline"]["you_steps"]
    closes = [s.split()[0] for s in steps if s.endswith("windows_close")]
    win = strips["strips"]["windowsplan"]
    assert win and [w.split("-")[1] for w in win] == closes[: len(win)], (win, steps)
    assert strips["strips"]["natvent"] == win and strips["strips"]["fan"] == win, strips
    assert capture["text"] == f"Close windows by {_clock(closes[0])}", capture["text"]
    # The Tier A golden pins the comfort_edge close at 7:00 PM. Real HA closes at 9:00 PM, the classifier's
    # window_close_time ("the planned window period ends", windows_close.planned_end), because the plan has no forecast
    # here: realha_session._apply_injected_inputs seeds only the ENGINE's hourly forecast, not the coordinator's, and
    # the coordinator's weather refresh then hands the engine its own empty copy (Issue #1440). The scenario's thermal
    # model does reach the plan. The golden's timing assertions (0-6) are therefore Tier A only, and this test holds
    # real HA to the invariant the golden is about.
    failing = [r["index"] for r in dump["assertions"] if r["pass"] is False]
    timing_only = {0, 1, 2, 3, 4, 5, 6}
    assert set(failing) <= timing_only, failing


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
