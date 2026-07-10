"""Tests for substitution testing (architecture-reset Step 2, "real end-to-end" gap closed).

Shadow-mode comparison (test_nat_vent_gate_compare.py) only proves "the new
function WOULD have agreed" — it never lets the new function's answer drive
real behavior. These tests prove the stronger claim: substituting the new
function's answer into the live engine produces an IDENTICAL full scenario
outcome (entire action_log/event_log, not just one function's boolean) to an
untouched baseline. Full-scale validation (51 goldens + full t=3 synthetic
sweep, 5860 scenarios, 0 divergences) is run via
tools/nat_vent_gate_substitution_diff.py and recorded in the Step-2 status
report — too large for the default test suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.differential import diff_runs  # noqa: E402
from tools.sim_harness.nat_vent_gate_compare import substitute_new_gate  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"

# A scenario known to actually exercise the nat-vent reactivation gate at least
# once — chosen because it's used as the positive-control probe elsewhere too.
_PROBE_GOLDEN = "2026-03-28-overnight"


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_substitution_produces_identical_full_scenario_on_real_golden():
    """The real (unbroken) new function, actually driving behavior, must produce
    a byte-identical action_log/event_log to the untouched baseline."""
    scen = _load(_PROBE_GOLDEN)
    diff = diff_runs(scen, mutate_b=substitute_new_gate, scenario_name=_PROBE_GOLDEN)
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.is_clean, (
        f"substitution diverged from baseline on a real scenario: "
        f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
        "letting the new function actually decide changed the outcome, not just its own boolean"
    )


def test_substitution_positive_control_detects_a_broken_new_function():
    """Test the test: a deliberately broken new function (always False), substituted
    in, MUST cascade into a real, detectable full-scenario divergence — not just
    a shadow disagreement. Proves the substitution mechanism can catch downstream
    effects (e.g. a nat-vent activation event never firing), not only the immediate
    call site.
    """
    scen = _load(_PROBE_GOLDEN)
    with patch("custom_components.climate_advisor.nat_vent_gate.decide_nat_vent_gate", lambda inputs: False):
        diff = diff_runs(scen, mutate_b=substitute_new_gate, scenario_name=f"{_PROBE_GOLDEN}_POSITIVE_CONTROL")

    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert not diff.is_clean, (
        f"positive control FAILED: forcing decide_nat_vent_gate() to always return False "
        f"produced no divergence on {_PROBE_GOLDEN} — the substitution mechanism cannot be "
        "trusted to detect a real behavioral bug"
    )
