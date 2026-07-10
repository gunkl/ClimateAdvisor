"""Regression test for the fan-thermostat-check old-vs-new comparator (architecture-reset Step 2, slice 2).

Full-scale shadow validation (51 goldens + full t=3 synthetic sweep) is run via
tools/fan_thermostat_decision_diff.py and recorded in the Step-2 status report —
too large for the default test suite. This keeps a small, fast regression check
that the comparator itself still intercepts real calls and agrees.

Coverage history (see the Step-2 status report): this comparator's calls only
fire when self._fan_running (_fan_active or _natural_vent_active) is already
True — this file's tests (goldens only) keep guarding that baseline. The
former "synthetic contributes zero calls" gap is now CLOSED: a two-phase
synthetic driver (tools/sim_harness/fan_thermostat_two_phase.py) wraps each t=3
assignment with a real activation preamble (a nat-vent-session activation via
sensor_open, or a real start_min_fan_runtime_cycles() call for a fan-only
state) before the assignment's own boundary tick — see
tests/test_fan_thermostat_two_phase.py and
tools/fan_thermostat_decision_integration_check.py for that full-scale
validation (7438 calls, all 4 outcomes genuinely exercised, 0 disagreements).

A sim-harness fidelity fix (tools/sim_harness/run_production.py, adding the
missing `_async_thermostat_changed`-equivalent dispatch on ordinary temp ticks)
let 2 goldens (nat-vent-outdoor-rises-above-indoor-exit,
warm_day_ceiling_breach_ac_defense) newly exercise a real STOP_VIA_NAT_VENT_EXIT
outcome for the first time — previously every real call was KEEP. That fix also
uncovered and fixed a real comparator bug: `_reconstruct_inputs` was capturing
`natural_vent_active` AFTER the real check ran, reading state the STOP_VIA_NAT_VENT_EXIT
branch itself had already cleared, producing a false disagreement. Fixed by
capturing inputs before the real call.

`fan_thermostat_check()` in automation.py now calls `decide_fan_thermostat_check()`
directly (Issue #435 follow-up extraction) — this comparator's "new_outcome" is
computed via a local import of the SOURCE module
(`custom_components.climate_advisor.fan_thermostat_decision`), independent of
automation.py's own top-level-bound import, so patching the source here still
correctly breaks only the comparator's reconstruction, not real production —
see tools/sim_harness/fan_thermostat_decision_integration.py for the separate
positive control that breaks REAL production (patched at
`automation.decide_fan_thermostat_check`, the name automation.py actually
calls) to prove the extraction is load-bearing.

Also includes a POSITIVE CONTROL: proves the comparator can actually detect a
disagreement, not just that it hasn't found one. Forces STOP_DEACTIVATE, an
outcome genuinely distinct from every real one observed (KEEP,
STOP_VIA_NAT_VENT_EXIT) — forcing an outcome that already occurs naturally
would trivially show zero disagreement regardless of whether detection works.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.fan_thermostat_decision_compare import FanThermostatComparisonRun, compare_scenario  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"


def _load_goldens() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        out.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
    return out


def test_comparator_intercepts_real_calls_and_agrees_on_goldens():
    run = FanThermostatComparisonRun()
    for name, scen in _load_goldens():
        compare_scenario(scen, name, run)

    assert run.n_calls > 0, "comparator intercepted zero fan_thermostat_check calls — instrumentation broke"
    assert not run.errors, run.errors
    assert not run.disagreements, [(c.scenario_name, c.real_outcome, c.new_outcome) for c in run.disagreements]


def test_real_outcomes_never_include_stop_deactivate_or_stop_cooled_to_floor():
    """Guards the exact premise the positive control below depends on: STOP_DEACTIVATE
    must remain an outcome no real golden-driven call produces, or forcing it as the
    positive control's broken value would no longer be a genuinely different outcome.
    KEEP and STOP_VIA_NAT_VENT_EXIT are both real, observed outcomes (the latter since
    the Step-2 harness fidelity fix); STOP_DEACTIVATE and STOP_COOLED_TO_FLOOR are not
    yet exercised by any golden — a real, separate coverage gap, not asserted away here."""
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome

    run = FanThermostatComparisonRun()
    for name, scen in _load_goldens():
        compare_scenario(scen, name, run)

    outcomes = Counter(c.real_outcome for c in run.calls)
    assert run.n_calls > 0
    unexpected = {FanThermostatOutcome.STOP_DEACTIVATE, FanThermostatOutcome.STOP_COOLED_TO_FLOOR} & set(outcomes)
    assert not unexpected, (
        f"a golden now exercises {unexpected} — the positive control's forced STOP_DEACTIVATE value "
        f"is no longer distinct from every real outcome; got full distribution {outcomes}"
    )


def test_comparator_positive_control_detects_a_broken_new_function():
    """Test the test: force decide_fan_thermostat_check() to always return
    STOP_DEACTIVATE (an outcome distinct from every real observed outcome, per
    the coverage-gap test above) and confirm the comparator flags every call as
    a disagreement.
    """
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome

    run = FanThermostatComparisonRun()
    with patch(
        "custom_components.climate_advisor.fan_thermostat_decision.decide_fan_thermostat_check",
        lambda inputs: FanThermostatOutcome.STOP_DEACTIVATE,
    ):
        for name, scen in _load_goldens():
            compare_scenario(scen, f"{name}_POSITIVE_CONTROL", run)

    assert run.n_calls > 0, "positive control needs real calls to compare against"
    assert len(run.disagreements) == run.n_calls, (
        "positive control FAILED: forcing decide_fan_thermostat_check() to STOP_DEACTIVATE should make "
        f"EVERY call disagree, but only {len(run.disagreements)}/{run.n_calls} did — "
        "the comparator cannot be trusted to detect a real bug"
    )
