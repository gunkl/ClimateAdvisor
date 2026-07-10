"""Regression test for the nat-vent gate production integration.

automation.py's `_nat_vent_may_reactivate()` now calls `decide_nat_vent_gate()`
directly (mirroring Issue #435's fan_thermostat_check extraction) — production
simply IS the pure function's caller now, so the shadow/substitution
distinction Step 1/2 built for this gate has collapsed into one. What remains
to prove is that the extraction is genuinely LOAD-BEARING: if
decide_nat_vent_gate() were broken, real production behavior (the full
action_log/event_log) must actually change.

Full-scale validation (all 56 goldens) is run via
tools/nat_vent_gate_integration_check.py --positive-control and recorded in
the Step-2 status report. This keeps a small, fast regression check on one
known-good probe golden.
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
from tools.sim_harness.nat_vent_gate_integration import break_nat_vent_gate  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"

# Reused as a probe elsewhere in this project too (genuinely exercises the gate).
_PROBE_GOLDEN = "2026-03-28-overnight"


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
    """The actual load-bearing proof: inverting decide_nat_vent_gate()'s outcome
    (preserving its None-input safety guarantee, which downstream formatting code
    relies on) must cascade into a real, detectable full-scenario divergence —
    proving automation.py's _nat_vent_may_reactivate() really does call this
    function to decide, not some other code path that happens to agree.
    """
    scen = _load(_PROBE_GOLDEN)
    diff = diff_runs(scen, mutate_b=break_nat_vent_gate, scenario_name=f"{_PROBE_GOLDEN}_BROKEN")
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert not diff.is_clean, (
        "positive control FAILED: inverting decide_nat_vent_gate()'s outcome produced no "
        f"divergence on {_PROBE_GOLDEN} — the extraction may not be load-bearing in production"
    )
