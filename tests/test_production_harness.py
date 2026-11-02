"""Tier-A production-harness validation (issue #236).

Runs every golden scenario through the REAL production ``AutomationEngine``
(headless, via ``tools/sim_harness``) and asserts it passes. This is the
source-of-truth enforcement that replaces the legacy standalone simulator:
the goldens are now validated against production code, so the integration and
its regression suite can no longer drift apart.

``track: integration`` assertions are deferred to Tier B (HeadlessTarry) and
``simulator_support: false`` assertions are skipped in the default validation
run (they are de-phantomed only in ``simulate.py --engine diff``); both are
handled inside ``run_scenario_production``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GOLDEN_DIR = TOOLS / "simulations" / "golden"
_GOLDEN_FILES = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")


@pytest.mark.parametrize("scenario_path", _GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden_passes_production_engine(scenario_path: Path) -> None:
    """Every golden scenario must pass against the real production engine."""
    from simulate import run_scenario_production  # noqa: PLC0415

    result = run_scenario_production(scenario_path, state="golden")

    # Unexpected callback errors make the run untrustworthy.
    assert not result.get("callback_errors"), (
        f"{scenario_path.stem}: production callback errors: {result['callback_errors']}"
    )

    # passed is True (assertions passed), or None (only integration/unsupported
    # assertions, deferred) — never False.
    assert result["passed"] is not False, f"{scenario_path.stem}: production failed assertions: " + "; ".join(
        f"@{a['at']} expected={a['expected']!r} actual={a['actual']!r}"
        for a in result["assertions"]
        if a.get("pass") is False
    )


def test_all_goldens_discovered() -> None:
    """Guard against an empty parametrization silently passing."""
    assert len(_GOLDEN_FILES) >= 20, f"expected the full golden set, found {len(_GOLDEN_FILES)}"


def test_no_scenario_runs_an_unsupported_sensorless_whole_house_fan() -> None:
    """Issue #1200: a whole-house fan install requires at least one door/window sensor.

    Without one Climate Advisor cannot enforce rule 1 (stop the fan on a sealed house) or rule 3 (pause HVAC
    with a window open), so a sensorless WHF is an unsupported configuration. The bare-engine harness hides
    this: its open-sensor tracker always gives the engine one sensor reading, whatever the config says, so a
    scenario with ``door_window_sensors: []`` silently runs as a one-sensor house. Scenarios that genuinely
    need zero sensors belong in ``unsupported/``, which no default run validates.
    """
    import json  # noqa: PLC0415

    offenders = []
    for state in ("golden", "pending", "pending-fix", "synthetic"):
        for path in sorted((TOOLS / "simulations" / state).glob("*.json")):
            if path.name == "MANIFEST.json":
                continue
            config = json.loads(path.read_text(encoding="utf-8")).get("config") or {}
            if config.get("fan_mode") in ("whole_house_fan", "both") and not config.get("door_window_sensors"):
                offenders.append(f"{state}/{path.stem}")
    assert not offenders, f"whole-house fan scenarios with no door/window sensor configured: {offenders}"
