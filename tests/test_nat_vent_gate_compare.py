"""Regression test for the nat-vent gate old-vs-new comparator (architecture-reset Step 2).

Full-scale validation (51 goldens + full t=3 synthetic sweep, 1106+ gate calls,
0 disagreements) is run via tools/nat_vent_gate_diff.py and recorded in the Step-2
status report — too large for the default test suite. This keeps a small, fast
regression check that the comparator itself still intercepts real calls and agrees.

Also includes a POSITIVE CONTROL: every "agrees" test above only proves the
comparator hasn't found a disagreement — not that it CAN find one (the same gap
the old-vs-old harness had before its own positive control was added, per
tests/test_differential_harness.py). Deliberately breaks the new pure function
and asserts the comparator flags every call as disagreeing.

Issue #757 Phase 6 Step 5: the synthetic t=3 sensor_open sweep this file used
to run (a dedicated ``test_comparator_agrees_on_a_synthetic_sample_with_sensor_open``)
was removed — ``_nat_vent_may_reactivate()`` now has exactly one real caller
left (``handle_door_window_open()``'s grace-period pre-check; every other of
its former 5 call sites dispatches through ``nat_vent_fsm.transition()``
directly instead), and the enumerator has no ``grace_active`` dimension to
construct a synthetic assignment that reaches it. The full-corpus test below
already proves reachability and correctness against real scenarios
(``grace_prevents_sensor_repause`` and 5 others in the golden set reach this
path), which is the more meaningful check now that reachability has narrowed
to one specific precondition rather than a broad temperature-only gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.nat_vent_gate_compare import GateComparisonRun, compare_scenario  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"
# Issue #757 Phase 6 Step 5: _nat_vent_may_reactivate() now has exactly one
# real caller left (handle_door_window_open()'s grace-period pre-check — every
# other of its former 5 call sites now dispatches through
# nat_vent_fsm.transition() directly instead). The probe must specifically
# reach that one path (grace active + a door/window opens); the previous
# probe (a plain away-mode activation, no grace involved) no longer does.
_PROBE_GOLDEN = "grace_prevents_sensor_repause"


def test_comparator_intercepts_real_gate_calls_and_agrees_on_goldens():
    run = GateComparisonRun()
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        import json  # noqa: PLC0415

        scen = json.loads(path.read_text(encoding="utf-8"))
        compare_scenario(scen, path.stem, run)

    assert run.n_calls > 0, "comparator intercepted zero gate calls — instrumentation broke"
    assert not run.errors, run.errors
    assert not run.disagreements, [(c.scenario_name, c.real_result, c.new_result) for c in run.disagreements]


def test_comparator_positive_control_detects_a_broken_new_function():
    """Test the test: deliberately invert decide_nat_vent_gate()'s answer and confirm
    the comparator flags every call as a disagreement. A comparator that always
    reports 'agrees' would also pass every test above — this proves it doesn't.

    Patched at the SOURCE module (custom_components...nat_vent_gate), not at
    nat_vent_gate_compare — the comparator imports decide_nat_vent_gate via a
    LOCAL `from ... import` inside its wrapper function, re-resolved fresh on
    every call, so patching the comparator module's own namespace is a no-op.
    """
    import json

    scen = json.loads((GOLDEN_DIR / f"{_PROBE_GOLDEN}.json").read_text(encoding="utf-8"))

    from custom_components.climate_advisor.nat_vent_gate import decide_nat_vent_gate as original_decide

    def _inverted_decide(inputs):
        return not original_decide(inputs)

    run = GateComparisonRun()
    with patch("custom_components.climate_advisor.nat_vent_gate.decide_nat_vent_gate", _inverted_decide):
        compare_scenario(scen, f"{_PROBE_GOLDEN}_POSITIVE_CONTROL", run)

    assert run.n_calls > 0, (
        f"positive control needs {_PROBE_GOLDEN} to actually reach the gate — pick a different probe scenario"
    )
    assert len(run.disagreements) == run.n_calls, (
        "positive control FAILED: inverting decide_nat_vent_gate() should make EVERY "
        f"call disagree, but only {len(run.disagreements)}/{run.n_calls} did — "
        "the comparator cannot be trusted to detect a real bug"
    )
