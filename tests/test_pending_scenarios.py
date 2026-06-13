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

# tools/simulate.py reconfigures its stdout to UTF-8 (simulate.py:35-36), so the child
# always emits UTF-8. The parent must decode with UTF-8 too -- not the OS locale codec
# (cp1252 on Windows), which kills the subprocess reader thread on any byte undefined in
# that codec and silently empties stdout. This single dict is the one home for that
# contract; both the scenario runner and the regression test use it (#262).
_SUBPROCESS_TEXT_KWARGS: dict = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",  # decode to match simulate.py's UTF-8 stdout, not the OS locale codec
    "errors": "replace",  # belt-and-suspenders: never let a stray byte crash the reader thread
}


def _run_simulate(scenario_stem: str) -> subprocess.CompletedProcess:
    """Run a scenario through simulate.py, decoding its UTF-8 stdout correctly."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "simulate.py"), "-s", scenario_stem],
        cwd=str(REPO_ROOT),
        **_SUBPROCESS_TEXT_KWARGS,
    )


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

    result = _run_simulate(scenario_path.stem)

    assert result.returncode == 0, (
        f"Scenario '{scenario_path.stem}' FAILED.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_simulate_subprocess_decodes_utf8_stdout(recwarn) -> None:
    """The subprocess text kwargs must decode UTF-8 child stdout, not the OS locale (#262).

    Windows-only bug: simulate.py emits UTF-8; under the locale codec (cp1252) the parent
    reader thread dies on any byte undefined in cp1252 (e.g. 0x9d) and silently empties
    stdout while returncode stays 0 -- so a returncode-only assertion never catches it.
    This drives a controlled child that prints U+045D (UTF-8 ``d1 9d``) through the SAME
    ``_SUBPROCESS_TEXT_KWARGS`` the scenario runner uses, so removing ``encoding="utf-8"``
    re-breaks this test. On Linux the locale is already UTF-8, so this passes there
    regardless -- the bug, and thus the guard, are Windows-specific.
    """
    child = "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('verdict ѝ ok')"
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=str(REPO_ROOT),
        **_SUBPROCESS_TEXT_KWARGS,
    )
    assert result.stdout and result.stdout.strip(), (
        "stdout empty/None -- reader thread died decoding UTF-8 as the OS locale codec"
    )
    assert "ѝ" in result.stdout, "non-ASCII char lost -- stream not decoded as UTF-8"
    assert not any(issubclass(w.category, pytest.PytestUnhandledThreadExceptionWarning) for w in recwarn.list), (
        "reader-thread UnicodeDecodeError surfaced as a PytestUnhandledThreadExceptionWarning"
    )
