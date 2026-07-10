"""Regression test for the fan_thermostat_check production integration (Step 2, slice 2).

automation.py's fan_thermostat_check() now calls decide_fan_thermostat_check()
directly (Issue #435 follow-up extraction, mirroring Issue #411's precedent for
the nat-vent gate) — there is no separate "old" implementation left to run
shadow/substitution comparisons against; production simply IS the pure
function's caller. What remains to prove is that the extraction is genuinely
LOAD-BEARING: if decide_fan_thermostat_check() were broken, real production
behavior (the full action_log/event_log) must actually change, not silently
keep working via some other code path.

Full-scale validation (all 51 goldens) is run via
tools/fan_thermostat_decision_integration_check.py --positive-control and
recorded in the Step-2 status report. This keeps a small, fast regression
check on one known-good probe golden.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.differential import diff_runs  # noqa: E402
from tools.sim_harness.fan_thermostat_decision_integration import break_fan_thermostat_decision  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"

# A long-running golden known to exercise fan_thermostat_check many times (76 calls).
_PROBE_GOLDEN = "whf_reactivates_after_sleep_floor_exit"


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_baseline_is_clean_old_vs_old():
    """Sanity check: two untouched runs of the same scenario must be identical —
    isolates any divergence found below to the deliberate corruption, not noise."""
    scen = _load(_PROBE_GOLDEN)
    diff = diff_runs(scen, scenario_name=_PROBE_GOLDEN)
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.is_clean, (
        f"untouched baseline diverged from itself: {len(diff.event_divergences)} event, "
        f"{len(diff.action_divergences)} action divergences — hidden nondeterminism, not this test's concern"
    )


def test_corrupting_the_pure_function_changes_real_production_behavior():
    """The actual load-bearing proof: rotating every decide_fan_thermostat_check()
    outcome to a different one must cascade into a real, detectable full-scenario
    divergence — proving automation.py's fan_thermostat_check() really does call
    this function to decide, not some other code path that happens to agree.
    """
    scen = _load(_PROBE_GOLDEN)
    diff = diff_runs(scen, mutate_b=break_fan_thermostat_decision, scenario_name=f"{_PROBE_GOLDEN}_BROKEN")
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert not diff.is_clean, (
        "positive control FAILED: rotating decide_fan_thermostat_check()'s outcome produced no "
        f"divergence on {_PROBE_GOLDEN} — the extraction may not be load-bearing in production"
    )
