"""Validate all pending simulation scenarios.

Pending scenarios are authored BSpecs in tools/simulations/pending/ that document
expected automation behavior. They run on every pytest invocation to catch regressions.

Lifecycle:
  1. Scenarios are created by simulation_loop.py (from production incidents) or
     authored manually to document bugs/features
  2. Scenarios with a pending_issue field are xfail until the bug is fixed
  3. Once passing: python tools/simulate.py --sign <name>
     then: git mv tools/simulations/pending/<name>.json tools/simulations/golden/<name>.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "tools" / "simulations" / "pending"


def _collect_pending_scenarios() -> list[Path]:
    if not PENDING_DIR.exists():
        return []
    return sorted(PENDING_DIR.glob("*.json"))


def _get_pending_issue(scenario_path: Path) -> int | None:
    """Return pending_issue number if the scenario documents an unfixed bug."""
    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
        val = data.get("pending_issue")
        return int(val) if val else None
    except Exception:
        return None


_SCENARIO_FILES = _collect_pending_scenarios()


@pytest.mark.parametrize(
    "scenario_path",
    _SCENARIO_FILES,
    ids=[f.stem for f in _SCENARIO_FILES],
)
def test_pending_scenario(scenario_path: Path) -> None:
    """Run a pending BSpec through simulate.py and assert it passes.

    Scenarios with a pending_issue field are xfail: they document unfixed bugs
    and will be promoted to golden once the fix is merged.
    """
    pending_issue = _get_pending_issue(scenario_path)
    if pending_issue:
        pytest.xfail(f"Pending fix for issue #{pending_issue} -- scenario passes when bug is resolved")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "simulate.py"), "-s", scenario_path.stem],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, (
        f"Scenario '{scenario_path.stem}' FAILED.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
