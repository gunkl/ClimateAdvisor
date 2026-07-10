"""Regression tests for the fan_thermostat_check two-phase synthetic driver (Step 2, slice 2).

Full-scale sweep (51 goldens + full 6819-scenario two-phase synthetic set) is
run via tools/fan_thermostat_decision_integration_check.py --synthetic all and
recorded in the Step-2 status report — too large for the default test suite.
This keeps a small, fast regression check that both preamble variants
("nat_vent" and "fan_only") build correctly and genuinely reach the comparator,
including outcomes the single-tick enumerator could never produce
(STOP_DEACTIVATE, STOP_COOLED_TO_FLOOR, STOP_VIA_NAT_VENT_EXIT).
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.fan_thermostat_decision_compare import FanThermostatComparisonRun, compare_scenario  # noqa: E402
from tools.sim_harness.fan_thermostat_two_phase import build_two_phase_scenarios  # noqa: E402


def test_both_preamble_variants_are_built():
    # limit=600: the first non-disabled fan_mode assignment appears at index 501 in the
    # fixed t=3 generation order (most early combos hold fan_mode at its "disabled"
    # baseline until it's one of the actively-varied t-wise dimensions) — 600 reliably
    # includes at least one fan_only-eligible assignment without depending on luck.
    two_phase = build_two_phase_scenarios(t=3, limit=600)
    by_preamble = Counter(s.preamble for s in two_phase)
    assert by_preamble["nat_vent"] > 0, "nat_vent preamble variant missing — every assignment should get one"
    assert by_preamble["fan_only"] > 0, (
        "fan_only preamble variant missing from a 600-assignment sample — "
        "the reachability gate likely regressed (no fan_mode != 'disabled' assignment reached)"
    )


def test_fan_only_preamble_skipped_when_fan_mode_disabled():
    from tools.sim_harness.enumerator import generate_t_wise_assignments
    from tools.sim_harness.fan_thermostat_two_phase import build_two_phase_scenario

    disabled_assignment = next(a for a in generate_t_wise_assignments(t=3) if a.get("fan_mode") == "disabled")
    scen = build_two_phase_scenario(disabled_assignment, preamble="fan_only", name="probe")
    assert scen is None, "fan_only preamble must be skipped (return None) when fan_mode is disabled"


def test_two_phase_sweep_agrees_and_exercises_real_outcomes_beyond_keep():
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome

    two_phase = build_two_phase_scenarios(t=3, limit=500)
    run = FanThermostatComparisonRun()
    for tp in two_phase:
        compare_scenario(tp.scenario, tp.name, run)

    assert run.n_calls > 0, "two-phase driver intercepted zero calls — instrumentation or preamble broke"
    assert not run.errors, run.errors
    assert not run.disagreements, [(c.scenario_name, c.real_outcome, c.new_outcome) for c in run.disagreements]

    outcomes = set(c.real_outcome for c in run.calls)
    assert outcomes != {FanThermostatOutcome.KEEP}, (
        "two-phase driver only produced KEEP outcomes on this sample — the activation preambles "
        "aren't genuinely exercising Check 1/Check 2's stop branches"
    )
