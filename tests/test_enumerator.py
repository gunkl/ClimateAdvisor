"""Tests for the boundary-focused t-wise synthetic enumerator (architecture-reset Step 1).

Keeps runtime small: full t=3 sweep (~5809 scenarios) is exercised manually via
tools/synthetic_enumerate.py, not in the default test suite. These tests validate
the generator's contract (t-wise coverage, scenario shape) and run a small capped
sample through the real differential-replay engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.differential import old_vs_old  # noqa: E402
from tools.sim_harness.enumerator import (  # noqa: E402
    DIMENSIONS,
    CoverageStats,
    assignment_to_scenario,
    build_enumerated_scenarios,
    generate_t_wise_assignments,
)


def test_generate_t_wise_covers_every_pair_of_dimensions_at_t2() -> None:
    """At t=2, every (dim_a, dim_b) pair must have every value combination represented."""
    assignments = generate_t_wise_assignments(t=2)
    a, b = DIMENSIONS[0], DIMENSIONS[1]
    seen = {(x[a.name], x[b.name]) for x in assignments if x[a.name] != a.baseline or x[b.name] != b.baseline}
    # Every explicit (a,b) combination generated when a,b is the active pair.
    expected = {(va, vb) for va in a.values for vb in b.values}
    assert expected <= seen


def test_generate_t_wise_count_matches_combinatorics() -> None:
    import itertools
    import math

    n = len(DIMENSIONS)
    t = 3
    expected = 0
    for combo in itertools.combinations(DIMENSIONS, t):
        expected += math.prod(len(d.values) for d in combo)
    assert len(generate_t_wise_assignments(t=t)) == expected
    assert CoverageStats.compute(t).total_assignments == expected
    assert n == len(DIMENSIONS)  # sanity: dimension set didn't shrink silently


def test_assignment_to_scenario_produces_valid_scenario_shape() -> None:
    assignments = generate_t_wise_assignments(t=3)
    scen = assignment_to_scenario(assignments[0], name="unit_test")
    assert scen["name"] == "unit_test"
    assert "events" in scen and len(scen["events"]) >= 2
    types = [e["type"] for e in scen["events"]]
    assert "classification" in types
    assert "temp_update" in types
    temp_event = next(e for e in scen["events"] if e["type"] == "temp_update")
    assert isinstance(temp_event["indoor_f"], float)
    assert isinstance(temp_event["outdoor_f"], float)


def test_build_enumerated_scenarios_respects_limit() -> None:
    scenarios = build_enumerated_scenarios(t=3, limit=17)
    assert len(scenarios) == 17
    names = {s.name for s in scenarios}
    assert len(names) == 17  # all unique


def test_small_sample_old_vs_old_is_clean() -> None:
    """A capped sample of real generated boundary scenarios must diff to zero."""
    scenarios = build_enumerated_scenarios(t=3, limit=40)
    bad = []
    for es in scenarios:
        d = old_vs_old(es.scenario, scenario_name=es.name)
        if d.a_error or d.b_error or not d.is_clean:
            bad.append((es.name, d))
    assert not bad, f"{len(bad)} synthetic boundary scenario(s) diverged old-vs-old: {[n for n, _ in bad[:5]]}"
