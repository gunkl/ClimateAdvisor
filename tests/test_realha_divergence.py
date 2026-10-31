"""Divergence tool and ledger (Step 2 Increment 6) - pure logic, FakeHass side.

* ``judge_scenario`` applies a scenario's assertions exactly as ``tools/simulate.py`` does (parity on every golden),
  so the real-HA side and the FakeHass side are judged by the same rule.
* ``compare_dumps`` names the first difference (negative control: drop one service call from a copy of a run).
* The ledger is a ratchet: unlisted failure, changed failing set and stale entry are all reported.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GOLDEN_DIR = TOOLS / "simulations" / "golden"
GOLDENS = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")


def _run_fake(path: Path):
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    scenario = json.loads(path.read_text(encoding="utf-8"))
    use_coord = bool(scenario.get("use_coordinator", False))
    return scenario, use_coord, run_production_scenario(scenario, use_coordinator=use_coord)


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.stem)
def test_judge_matches_simulate_on_every_golden(path: Path) -> None:
    """The shared judge and ``simulate.run_scenario_production`` agree on verdict and every assertion outcome."""
    from simulate import run_scenario_production  # noqa: PLC0415

    from tools.sim_harness import divergence  # noqa: PLC0415

    scenario, use_coord, result = _run_fake(path)
    mine = divergence.judge_scenario(scenario, result, use_coordinator=use_coord)
    theirs = run_scenario_production(path, state="golden")
    assert mine["passed"] == theirs["passed"]
    assert [(r["expected"], r["actual"], r["pass"]) for r in mine["assertions"]] == [
        (r["expected"], r["actual"], r["pass"]) for r in theirs["assertions"]
    ]


def _dump(name: str = "away_cold_day_setback_heat") -> dict:
    from tools.sim_harness import divergence  # noqa: PLC0415

    scenario, use_coord, result = _run_fake(GOLDEN_DIR / f"{name}.json")
    return divergence.dump_run(name, "fake", scenario, result, use_coordinator=use_coord)


def test_identical_runs_have_no_difference() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    dump = _dump()
    cmp = divergence.compare_dumps(dump, copy.deepcopy(dump))
    assert not cmp.diverges and cmp.differences == []


def test_negative_control_a_dropped_service_call_is_named() -> None:
    """Perturb a copy of the run (drop one service call): the tool names that call as the first difference."""
    from tools.sim_harness import divergence  # noqa: PLC0415

    fake = _dump()
    real = copy.deepcopy(fake)
    dropped = real["action_log"].pop(1)
    cmp = divergence.compare_dumps(fake, real)
    call = next(d for d in cmp.differences if d.kind == "service_call")
    assert call.index == 1
    assert dropped["service"] in call.summary and dropped["domain"] in call.summary
    text = divergence.render_report(cmp, fake, real)
    assert "service call[1]" in text and "Class: (unclassified)" in text


def test_a_changed_verdict_and_state_are_reported() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    fake = _dump()
    real = copy.deepcopy(fake)
    real["passed"] = False
    real["engine_state"]["_fan_active"] = not real["engine_state"].get("_fan_active")
    kinds = {d.kind for d in divergence.compare_dumps(fake, real).differences}
    assert {"verdict", "state"} <= kinds


def test_ledger_ratchet() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    ledger = {"a": {"failing_assertions": [0]}, "b": {"failing_assertions": [1]}}
    assert divergence.check_ledger({"a": [0]}, {"a": ledger["a"]}) == []
    unlisted = divergence.check_ledger({"c": [0]}, ledger, run={"c"})
    assert len(unlisted) == 1 and "not in the ledger" in unlisted[0]
    changed = divergence.check_ledger({"a": [0, 2]}, ledger, run={"a"})
    assert len(changed) == 1 and "update the entry" in changed[0]
    stale = divergence.check_ledger({}, ledger, run={"b"})
    assert len(stale) == 1 and "stale" in stale[0] and stale[0].startswith("b:")
    # entries of scenarios not run are not stale (a -k run of one golden)
    assert divergence.check_ledger({}, ledger, run={"zzz"}) == []


def test_ledger_catches_a_worse_or_different_failure_of_a_ledgered_assertion() -> None:
    """Same assertion index, different actual outcome (or an added crash) is a NEW finding, not the tolerated one."""
    from tools.sim_harness import divergence  # noqa: PLC0415

    entry = {"failing_assertions": [0], "failing_outcomes": {"0": "comfort_restored"}}
    ledger = {"a": entry}
    same = divergence.check_ledger({"a": [0]}, ledger, run={"a"}, outcomes={"a": {"0": "comfort_restored"}})
    assert same == []
    different = divergence.check_ledger({"a": [0]}, ledger, run={"a"}, outcomes={"a": {"0": "fan_activated"}})
    assert len(different) == 1 and "differently" in different[0]
    crash_added = divergence.check_ledger(
        {"a": [-1, 0]}, ledger, run={"a"}, outcomes={"a": {"0": "comfort_restored", "-1": "exception:KeyError"}}
    )
    assert len(crash_added) == 1 and "update the entry" in crash_added[0]


def test_failing_outcomes_reports_an_exception_even_when_assertions_also_fail() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    dump = {
        "assertions": [{"index": 3, "pass": False, "actual": "paused"}, {"index": 4, "pass": True, "actual": "x"}],
        "callback_errors": ["2026-07-15T12:00:00: KeyError('x')", "2026-07-15T12:05:00: KeyError('y')"],
        "leaked_timers": [],
    }
    idx, outcomes = divergence.failing_outcomes(dump)
    assert idx == [-1, 3]
    assert outcomes == {"3": "paused", "-1": "exception:KeyError"}
    idx, outcomes = divergence.failing_outcomes(
        {**dump, "assertions": [], "leaked_timers": ["<TimerHandle when=1.0 f(x)>"]}
    )
    assert idx == [-2, -1] and outcomes["-2"] == "timer:f"
    assert divergence.failing_outcomes({"assertions": [{"index": 0, "pass": True}]}) == ([], {})


def test_committed_ledger_pins_the_outcome_of_every_failure() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    for name, entry in divergence.load_ledger().items():
        assert sorted(int(k) for k in entry["failing_outcomes"]) == sorted(entry["failing_assertions"]), name


def test_committed_ledger_is_well_formed_and_names_real_goldens() -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    ledger = divergence.load_ledger()
    names = {p.stem for p in GOLDENS}
    assert set(ledger) <= names
    for entry in ledger.values():
        assert entry["class"] in divergence.CLASSES
        assert entry["evidence"] and entry["occupant_view"]


def test_ledger_rejects_unknown_class(tmp_path: Path) -> None:
    from tools.sim_harness import divergence  # noqa: PLC0415

    bad = tmp_path / "ledger.json"
    entry = {k: "x" for k in divergence._LEDGER_REQUIRED}
    entry.update({"class": "Z", "failing_assertions": [0]})
    bad.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match="class"):
        divergence.load_ledger(bad)
