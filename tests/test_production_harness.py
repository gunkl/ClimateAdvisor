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
