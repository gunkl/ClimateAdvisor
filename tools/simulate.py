#!/usr/bin/env python3
"""Climate Advisor Behavior Simulator.

Replays activity scenarios through the automation decision logic
to verify expected behavior and detect regressions.

Scenario states:
  pending/       - ingested, awaiting human review
  golden/        - approved, passing — protected regression tests
  pending-fix/   - wrong outcome, code fix needed (linked to issue)
  unsupported/   - wrong outcome, intentionally out of scope
  synthetic/     - constructed (not from real events), lower authority

Usage:
  python3 tools/simulate.py              # run all golden scenarios
  python3 tools/simulate.py -s NAME      # run specific scenario (any state)
  python3 tools/simulate.py --pending    # run all pending scenarios (for review)
  python3 tools/simulate.py --list       # list all scenarios by state
  python3 tools/simulate.py --cases      # summary table of all scenarios across all states
  python3 tools/simulate.py -v           # verbose (show full decision timeline)

Lifecycle:
  Real event → /simulate add → pending/ → review → golden/ or pending-fix/ or unsupported/
  All golden scenarios must pass on every run — failures = regression.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SIMULATIONS_DIR = Path(__file__).parent / "simulations"
STATE_DIRS: dict[str, Path] = {
    "pending": SIMULATIONS_DIR / "pending",
    "golden": SIMULATIONS_DIR / "golden",
    "pending-fix": SIMULATIONS_DIR / "pending-fix",
    "unsupported": SIMULATIONS_DIR / "unsupported",
    "synthetic": SIMULATIONS_DIR / "synthetic",
}


@dataclass
class SimState:
    """Mirrors AutomationEngine state variables used in door/window handling.

    Keep this in sync with automation.py. When automation.py state variables
    change, update this class and the ClimateSimulator methods below.
    """

    indoor_temp: float | None = None
    outdoor_temp: float | None = None
    sensors_open: set = field(default_factory=set)
    hvac_mode: str = "off"
    fan_mode: str = "auto"
    paused_by_door: bool = False
    natural_vent_active: bool = False
    grace_active: bool = False


@dataclass
class Decision:
    """A single automation decision recorded at a point in time."""

    time: str
    event: str
    outcome: str
    reason: str
    hvac_mode: str
    fan_mode: str


class ClimateSimulator:
    """Pure-Python state machine mirroring automation.py door/window logic.

    This class intentionally duplicates the decision logic from automation.py
    so it can run without a Home Assistant instance. When automation.py logic
    changes, update the corresponding methods here. Golden scenario failures
    will alert you when the two diverge.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.state = SimState()
        self.decisions: list[Decision] = []

    def process_event(self, event: dict) -> Decision | None:
        """Process one scenario event and return any decision made."""
        etype = event["type"]
        ts = event["time"]

        if etype == "temp_update":
            if "indoor_f" in event:
                self.state.indoor_temp = event["indoor_f"]
            if "outdoor_f" in event:
                self.state.outdoor_temp = event["outdoor_f"]
            return self._check_conditions_on_temp_update(ts)

        if etype == "sensor_open":
            self.state.sensors_open.add(event.get("entity", "sensor"))
            return self._handle_sensor_open(ts)

        if etype == "sensor_close":
            self.state.sensors_open.discard(event.get("entity", "sensor"))
            if not self.state.sensors_open:
                return self._handle_all_closed(ts)
            return None

        if etype == "classification":
            return None  # classification affects briefing, not nat-vent logic

        if etype == "grace_start":
            self.state.grace_active = True
            return None

        if etype == "grace_end":
            self.state.grace_active = False
            return None

        return None

    # ------------------------------------------------------------------
    # Internal decision logic
    # ------------------------------------------------------------------

    def _handle_sensor_open(self, ts: str) -> Decision:
        """Mirror automation.py handle_door_window_open() logic."""
        if self.state.paused_by_door:
            d = Decision(ts, "sensor_open", "no_action", "already paused", self.state.hvac_mode, self.state.fan_mode)
            self.decisions.append(d)
            return d

        if self.state.grace_active:
            d = Decision(
                ts,
                "sensor_open",
                "no_action",
                "grace period active — skip pause",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        return self._apply_nat_vent_or_pause(ts, event_type="sensor_open")

    def _check_conditions_on_temp_update(self, ts: str) -> Decision | None:
        """Re-evaluate automation state when temperatures change.

        Mirrors the monitoring loop that runs on each coordinator update:
        - If natural vent active and outdoor climbed → exit to pause
        - If paused and outdoor dropped → activate natural vent
        """
        if self.state.natural_vent_active:
            return self._check_natural_vent_exit(ts)
        if self.state.paused_by_door and self.state.sensors_open:
            return self._check_natural_vent_entry(ts)
        return None

    def _apply_nat_vent_or_pause(self, ts: str, event_type: str) -> Decision:
        """Decide: natural ventilation or full pause? Returns and records the decision."""
        outdoor = self.state.outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor <= threshold:
            self.state.hvac_mode = "off"
            self.state.fan_mode = "on"
            self.state.natural_vent_active = True
            self.state.paused_by_door = False
            d = Decision(
                ts,
                event_type,
                "natural_ventilation",
                f"outdoor {outdoor}F <= target {comfort_cool}F + delta {delta}F",
                "off",
                "on",
            )
        else:
            self.state.hvac_mode = "off"
            self.state.paused_by_door = True
            self.state.natural_vent_active = False
            reason = (
                f"outdoor {outdoor}F > threshold {threshold}F"
                if outdoor is not None
                else "outdoor temp unknown — defaulting to pause"
            )
            d = Decision(ts, event_type, "paused", reason, "off", self.state.fan_mode)

        self.decisions.append(d)
        return d

    def _check_natural_vent_exit(self, ts: str) -> Decision | None:
        """Exit natural ventilation if outdoor climbed above threshold."""
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        outdoor = self.state.outdoor_temp
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor > threshold and self.state.sensors_open:
            self.state.natural_vent_active = False
            self.state.paused_by_door = True
            self.state.fan_mode = "auto"
            d = Decision(
                ts,
                "temp_update",
                "paused",
                f"outdoor {outdoor}F exceeded threshold {threshold}F — sensors still open",
                "off",
                "auto",
            )
            self.decisions.append(d)
            return d
        return None

    def _check_natural_vent_entry(self, ts: str) -> Decision | None:
        """Activate natural ventilation if outdoor dropped below threshold while paused."""
        outdoor = self.state.outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor <= threshold:
            return self._apply_nat_vent_or_pause(ts, event_type="temp_update")
        return None

    def _handle_all_closed(self, ts: str) -> Decision:
        """Resume automation when all sensors close."""
        was_nat_vent = self.state.natural_vent_active
        self.state.paused_by_door = False
        self.state.natural_vent_active = False
        self.state.fan_mode = "auto"
        reason = (
            "all sensors closed — exiting natural ventilation, resuming automation"
            if was_nat_vent
            else "all sensors closed — resuming automation"
        )
        d = Decision(ts, "sensor_close", "resumed", reason, self.state.hvac_mode, "auto")
        self.decisions.append(d)
        return d


# ------------------------------------------------------------------
# Scenario I/O
# ------------------------------------------------------------------


def _find_scenario(name: str) -> tuple[Path, str] | None:
    """Find a scenario file by name across all state directories."""
    for state, d in STATE_DIRS.items():
        p = d / f"{name}.json"
        if p.exists():
            return p, state
    return None


def _outcome_at(decisions: list[Decision], iso_time: str) -> str:
    """Return the most recent outcome at or before iso_time."""
    matching = [d for d in decisions if d.time <= iso_time]
    return matching[-1].outcome if matching else "no_decision"


def run_scenario(scenario_file: Path, state: str | None = None) -> dict:
    """Run a single scenario file and return a results dict."""
    with open(scenario_file) as f:
        scenario = json.load(f)

    sim = ClimateSimulator(scenario.get("config", {}))
    for event in sorted(scenario.get("events", []), key=lambda e: e["time"]):
        sim.process_event(event)

    assertion_results = []
    any_real_assertion = False
    for a in scenario.get("assertions", []):
        if a.get("simulator_support") is False:
            assertion_results.append(
                {
                    "at": a["at"],
                    "expected": a.get("expect", ""),
                    "actual": None,
                    "pass": None,
                    "skipped": True,
                    "reason": "event type not simulated",
                }
            )
            continue
        any_real_assertion = True
        actual = _outcome_at(sim.decisions, a["at"])
        assertion_results.append(
            {
                "at": a["at"],
                "expected": a["expect"],
                "actual": actual,
                "pass": actual == a["expect"],
                "skipped": False,
                "reason": a.get("reason", ""),
            }
        )

    real_results = [r for r in assertion_results if not r.get("skipped")]
    passed = all(r["pass"] for r in real_results) if any_real_assertion else None

    return {
        "name": scenario.get("name", scenario_file.stem),
        "description": scenario.get("description", ""),
        "issue": scenario.get("issue"),
        "verdict": scenario.get("verdict"),
        "state": state,
        "decisions": [{"time": d.time, "outcome": d.outcome, "reason": d.reason} for d in sim.decisions],
        "assertions": assertion_results,
        "passed": passed,
    }


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------


def _status_label(result: dict) -> str:
    """Return the display status string for a result, accounting for pending-fix expected fails."""
    passed = result["passed"]
    state = result.get("state")
    verdict = result.get("verdict") or {}
    verdict_type = verdict.get("type")

    if state == "pending-fix" and verdict_type == "negative":
        if passed is False:
            return "EXPECTED FAIL"
        if passed is True:
            return "PASS"

    if passed is True:
        return "PASS"
    if passed is False:
        return "FAIL"
    return "SKIP (no assertions)"


def print_result(result: dict, verbose: bool = False) -> None:
    """Print simulation result in human-readable form."""
    status = _status_label(result)
    verdict = result.get("verdict") or {}

    issue_tag = f" [#{result['issue']}]" if result.get("issue") else ""
    print(f"\n{'=' * 60}")
    print(f"Scenario: {result['name']}{issue_tag}")
    print(f"  {result['description']}")

    if verdict:
        verdict_type = verdict.get("type", "")
        summary = verdict.get("summary", "")
        print(f"  Verdict: {verdict_type} — {summary}")

    print(f"  Status: {status}")

    if status == "PASS" and result.get("state") == "pending-fix":
        print("  NOTE: pending-fix scenario now passes — consider promoting to golden/")

    if verbose and result["decisions"]:
        print("\nDecision timeline:")
        for d in result["decisions"]:
            print(f"  {d['time']}  [{d['outcome']}]  {d['reason']}")

    if result["assertions"]:
        print("\nAssertions:")
        for a in result["assertions"]:
            if a.get("skipped"):
                print(f"  [SKIP] at {a['at']}: {a['reason']}")
            elif a["pass"]:
                print(f"  [OK]   at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
            else:
                print(f"  [FAIL] at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
                if a["reason"]:
                    print(f"         {a['reason']}")


# ------------------------------------------------------------------
# Cases summary
# ------------------------------------------------------------------


def print_cases_summary() -> None:
    """Scan all directories and print a summary table of all scenarios."""
    print("\nSCENARIO CASE SUMMARY")
    print("======================")

    for state, state_dir in STATE_DIRS.items():
        if not state_dir.exists():
            continue
        files = sorted(state_dir.glob("*.json"))
        if not files:
            continue

        print(f"\n{state.upper()} ({len(files)} scenario{'s' if len(files) != 1 else ''})")

        for f in files:
            try:
                result = run_scenario(f, state=state)
            except (json.JSONDecodeError, OSError, KeyError):
                print(f"  [ERROR] {f.stem}: unreadable or invalid")
                continue

            status = _status_label(result)
            verdict = result.get("verdict") or {}
            verdict_type = verdict.get("type", "")
            issue_tag = f" #{result['issue']}" if result.get("issue") else ""

            verdict_tag = f" [{verdict_type}]" if verdict_type else ""
            print(f"  [{status}]{verdict_tag} {result['name']}{issue_tag}")
            print(f"         {result['description']}")

            if verdict_type == "negative":
                observed = verdict.get("observed_behavior", "")
                expected = verdict.get("expected_behavior", "")
                if observed:
                    print(f"         Observed: {observed}")
                if expected:
                    print(f"         Expected: {expected}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Climate Advisor behavior simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--scenario", metavar="NAME", help="Run a specific scenario by name (searches all state dirs)"
    )
    parser.add_argument("--pending", action="store_true", help="Run all pending scenarios (for review)")
    parser.add_argument("--list", action="store_true", dest="list_all", help="List all scenarios by state")
    parser.add_argument("--cases", action="store_true", help="Show summary table of all scenarios across all states")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full decision timeline for each scenario")
    args = parser.parse_args()

    for d in STATE_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

    # Cases summary mode
    if args.cases:
        print_cases_summary()
        return 0

    # List mode
    if args.list_all:
        for state, d in STATE_DIRS.items():
            files = sorted(d.glob("*.json"))
            if files:
                print(f"\n{state.upper()} ({len(files)}):")
                for f in files:
                    try:
                        with open(f) as fh:
                            s = json.load(fh)
                        desc = s.get("description", "")[:70]
                        issue = f" [#{s['issue']}]" if s.get("issue") else ""
                        verdict = s.get("verdict") or {}
                        verdict_tag = f" [{verdict['type']}]" if verdict.get("type") else ""
                        print(f"  {f.stem}{issue}{verdict_tag}: {desc}")
                    except (json.JSONDecodeError, OSError):
                        print(f"  {f.stem}: [unreadable]")
        return 0

    # Single scenario
    if args.scenario:
        found = _find_scenario(args.scenario)
        if not found:
            print(f"Scenario not found: {args.scenario}")
            print("Available:")
            for state, d in STATE_DIRS.items():
                for f in sorted(d.glob("*.json")):
                    print(f"  [{state}] {f.stem}")
            return 1
        scenario_path, scenario_state = found
        result = run_scenario(scenario_path, state=scenario_state)
        print_result(result, verbose=args.verbose)
        status = _status_label(result)
        return 0 if status != "FAIL" else 1

    # Run a batch (golden by default, pending with --pending)
    source_key = "pending" if args.pending else "golden"
    source_dir = STATE_DIRS[source_key]
    files = sorted(source_dir.glob("*.json")) if source_dir.exists() else []

    if not files:
        print(f"No {source_key} scenarios found.")
        if source_key == "golden":
            print("  Promote a scenario from pending/ to golden/ after review.")
        return 0

    results = [run_scenario(f, state=source_key) for f in files]
    for r in results:
        print_result(r, verbose=args.verbose)

    total = len(results)
    passed = sum(1 for r in results if r["passed"] is True)
    failed = sum(1 for r in results if _status_label(r) == "FAIL")
    skipped = sum(1 for r in results if r["passed"] is None)

    print(f"\n{'=' * 60}")
    summary = f"{passed}/{total} {source_key} scenarios passed"
    if failed:
        summary += f" — {failed} FAILED"
    if skipped:
        summary += f" — {skipped} skipped (no assertions)"
    print(summary)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
