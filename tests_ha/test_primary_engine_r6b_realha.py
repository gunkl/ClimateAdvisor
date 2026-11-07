"""The R6b primary-engine scenarios through real Home Assistant (Issue #1230).

Same pending scenario files, same assertions, same outcomes layer as the FakeHass tier (``tools/simulate.py``); only
the world underneath differs (``RealHASession``). Each scenario states ``config.primary_engine`` (v4 or v3) and, for v4,
a fitted-model spec, so the decisions read the primary engine's path and rates exactly as production does.

Run:  python scripts/test.py ha tests_ha/test_primary_engine_r6b_realha.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness import divergence

PENDING = Path(divergence.GOLDEN_DIR).parent / "pending"
SCENARIOS = sorted(PENDING.glob("r6b_*.json"))
SPECS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in SCENARIOS}


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
async def test_r6b_scenario_on_real_ha(path: Path, hass, freezer, tmp_path, caplog) -> None:
    scenario = SPECS[path.stem]
    caplog.set_level(logging.DEBUG, logger="custom_components.climate_advisor")
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    dump = divergence.dump_run(
        path.stem, "real", scenario, result, use_coordinator=True, dispatch=session.dispatch_modes, notes=session.notes
    )
    failing, _outcomes = divergence.failing_outcomes(dump)
    detail = "; ".join(
        f"a{r['index']}@{r['at']} expected={r['expected']!r} actual={r['actual']!r}"
        for r in dump["assertions"]
        if r["pass"] is False
    )
    assert not failing, f"{path.stem} failed on real HA: {detail}"
