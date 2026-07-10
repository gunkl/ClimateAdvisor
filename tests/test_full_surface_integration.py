"""Regression test for the Step-4 full-surface simultaneous positive control.

Every mechanism on the nat-vent decision surface already has its own dedicated
positive control (test_nat_vent_gate_integration.py,
test_fan_thermostat_decision_integration.py, and inline positive controls for
fan drift reconciliation, reactivation lockout, grace, setpoint retry/verify,
pre-cool target, pre-cool reschedule). Those each corrupt ONE function at a
time. This test corrupts EVERY extracted decision point on the surface
SIMULTANEOUSLY and confirms real production behavior still diverges — proving
the surface holds together as a whole, not just each piece in isolation, per
the architecture-reset plan's Step 4 ("full substitution across the whole
nat-vent surface simultaneously... the actual point at which 'end-to-end' is
validated").

Full-scale validation (all 56 goldens) is run via
tools/full_surface_integration_check.py --positive-control (51/56 diverge —
substantially more than either individual control alone, 31/56 and 7/56
respectively). This keeps a small, fast regression check on one known-good
probe golden.
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
from tools.sim_harness.full_surface_integration import break_entire_nat_vent_surface  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"

# Exercises the gate, the tick-level stop check, and grace — a rich probe for the
# combined corruption (also used as the nat-vent-gate-only probe elsewhere).
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


def test_corrupting_the_entire_surface_simultaneously_changes_real_production_behavior():
    """The actual Step-4 proof: corrupting every extracted decision point on the
    nat-vent surface AT ONCE (gate, tick-level stop check, fan drift
    reconciliation, reactivation lockout, grace start, setpoint retry action,
    setpoint verify, pre-cool target, pre-cool reschedule) must still cascade
    into a real, detectable full-scenario divergence — proving none of the
    corruptions silently mask each other and the whole surface remains
    genuinely load-bearing when combined, not just each piece alone.
    """
    scen = _load(_PROBE_GOLDEN)
    diff = diff_runs(scen, mutate_b=break_entire_nat_vent_surface, scenario_name=f"{_PROBE_GOLDEN}_FULL_BROKEN")
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert not diff.is_clean, (
        "positive control FAILED: corrupting the entire nat-vent decision surface simultaneously produced "
        f"no divergence on {_PROBE_GOLDEN} — some corruption may be silently masking another"
    )
