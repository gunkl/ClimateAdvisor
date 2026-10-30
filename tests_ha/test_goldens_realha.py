"""All golden scenarios through real Home Assistant (Step 2 Increment 7, design 8.5).

Same scenario files, same assertions, same outcomes layer as the FakeHass tier; only the world underneath
differs (``RealHASession``).  A golden that fails here is a finding, not something to hide: it is triaged
(design 8.3) and recorded in ``tools/sim_harness/realha_ledger.json`` with a class and evidence.  The ledger
is a ratchet: an unlisted failure fails, and a listed scenario that now passes (or fails differently) fails.
Goldens are never edited to make them agree.

Run:  python scripts/test.py ha -m goldens          (add CA_HA_WORKERS=4 for xdist)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness import divergence

pytestmark = pytest.mark.goldens

GOLDEN_DIR = Path(divergence.GOLDEN_DIR)
GOLDENS = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
SCENARIOS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in GOLDENS}
LEDGER = divergence.load_ledger()


def _write_log(name: str, caplog) -> None:
    """Climate Advisor's own log for the run, next to the dump, for triage (never asserted on)."""
    lines = [
        f"{r.levelname[0]} {r.name.rsplit('.', 1)[-1]}: {r.getMessage()}"
        for r in caplog.records
        if r.name.startswith("custom_components.climate_advisor")
    ]
    out = divergence.DEFAULT_DUMP_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.real.log").write_text("\n".join(lines), encoding="utf-8")


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.stem)
async def test_golden_on_real_ha(path: Path, hass, freezer, tmp_path, caplog) -> None:
    scenario = SCENARIOS[path.stem]
    caplog.set_level(logging.DEBUG, logger="custom_components.climate_advisor")
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    try:
        result = await async_drive_scenario(session, scenario)
    finally:
        _write_log(path.stem, caplog)
    dump = divergence.dump_run(
        path.stem, "real", scenario, result, use_coordinator=True, dispatch=session.dispatch_modes, notes=session.notes
    )
    dump["leaked_timers"] = session.leaked_timers
    divergence.write_dump(dump, divergence.DEFAULT_DUMP_DIR)

    failing, outcomes = divergence.failing_outcomes(dump)
    problems = divergence.check_ledger(
        {path.stem: failing} if failing else {},
        LEDGER,
        run={path.stem},
        outcomes={path.stem: outcomes} if failing else {},
    )
    detail = "; ".join(
        f"a{r['index']}@{r['at']} expected={r['expected']!r} actual={r['actual']!r}"
        for r in dump["assertions"]
        if r["pass"] is False
    )
    assert not problems, (
        "\n".join(problems)
        + (f"\n  real-HA result: {detail}" if detail else "")
        + (f"\n  callback errors: {dump['callback_errors'][:3]}" if dump["callback_errors"] else "")
    )
