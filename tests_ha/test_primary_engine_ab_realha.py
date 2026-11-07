"""A/B a recorded day through real Home Assistant, v3 against v4 as the primary engine (Issue #1230 R6b).

Occupant view: the same recorded day, identical in every input but ``config.primary_engine``, is driven through the real
Home Assistant core and the real coordinator; the commands sent to the thermostat are written next to the scenario so
the two engines can be compared (``tools/primary_engine_ab.py --write-scenarios DIR`` writes the scenarios).

Skipped unless ``CA_AB_DIR`` names a directory holding ``ab_*.json``; asserts only that the run completes without a
callback error (the comparison is read from the written ``*.real.json`` files).

Run:  CA_AB_DIR=/path python scripts/test.py ha tests_ha/test_primary_engine_ab_realha.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario

_DIR = Path(os.environ["CA_AB_DIR"]) if os.environ.get("CA_AB_DIR") else None
FILES = sorted(_DIR.glob("ab_*.json")) if _DIR is not None else []
SPECS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FILES}

pytestmark = pytest.mark.skipif(not FILES, reason="CA_AB_DIR not set or holds no ab_*.json")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
async def test_recorded_day_on_real_ha(path: Path, hass, freezer, tmp_path) -> None:
    scenario = SPECS[path.stem]
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    commands = [
        [a.get("service"), {k: v for k, v in (a.get("data") or {}).items() if k != "entity_id"}]
        for a in result.action_log
        if a.get("domain") == "climate"
    ]
    kinds: dict[str, int] = {}
    for ev_type, _payload, _ts in result.event_log:
        kinds[ev_type] = kinds.get(ev_type, 0) + 1
    out = {
        "scenario": path.stem,
        "primary": scenario["config"]["primary_engine"],
        "commands": len(commands),
        "set_hvac_mode": sum(1 for c in commands if c[0] == "set_hvac_mode"),
        "set_temperature": sum(1 for c in commands if c[0] == "set_temperature"),
        "ceiling_guard_fired": kinds.get("ceiling_guard_fired", 0),
        "tou_precondition_applied": kinds.get("tou_precondition_applied", 0),
        "callback_errors": len(result.callback_errors),
        "command_list": commands,
    }
    path.with_suffix(".real.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    assert not result.callback_errors, result.callback_errors[:3]
