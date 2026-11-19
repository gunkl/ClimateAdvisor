"""Issue #1243 (real HA): the three dashboard-cancel scenarios through real Home Assistant and the real API views.

Occupant story: the dashboard's Cancel Override / Cancel Fan Override buttons end what the occupant asked for: Climate
Advisor's own grace with no override behind it, the occupant's fan-off pause (free cooling resumes), and the
occupant's fan override (Climate Advisor takes the fan back). The Tier A harness replays these through the same
production calls (``tools/sim_harness/run_production.py``); here ``RealHASession`` posts to the real
``ClimateAdvisorCancelOverrideView`` / ``ClimateAdvisorCancelFanOverrideView`` instead. Same scenario files, same
assertions, same outcomes layer (``divergence.judge_scenario``, as ``test_goldens_realha`` uses).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness.divergence import judge_scenario

pytestmark = pytest.mark.house

_PENDING = Path(__file__).resolve().parent.parent / "tools" / "simulations" / "pending"
_SCENARIOS = sorted(p.stem for p in _PENDING.glob("issue_1243_*.json"))


def test_the_cancel_scenarios_are_discovered() -> None:
    assert len(_SCENARIOS) == 3, _SCENARIOS


@pytest.mark.parametrize("name", _SCENARIOS)
async def test_cancel_scenario_on_real_ha(name, hass, freezer, tmp_path) -> None:
    scenario = json.loads((_PENDING / f"{name}.json").read_text(encoding="utf-8"))
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    verdict = judge_scenario(scenario, result, use_coordinator=True)
    failed = [
        (r["at"], r["expected"], r["actual"], r.get("detail")) for r in verdict["assertions"] if r["pass"] is False
    ]
    assert verdict["passed"] is True, f"{name} on real Home Assistant: {failed} {verdict['callback_errors'][:3]}"
