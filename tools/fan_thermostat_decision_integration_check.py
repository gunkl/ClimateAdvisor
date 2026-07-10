"""fan_thermostat_decision_integration_check — CLI: prove the pure-core extraction is load-bearing.

Usage:
  python tools/fan_thermostat_decision_integration_check.py                 # goldens, positive control
  python tools/fan_thermostat_decision_integration_check.py --synthetic all # + full synthetic sweep
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.differential import diff_runs  # noqa: E402
from tools.sim_harness.fan_thermostat_decision_integration import break_fan_thermostat_decision  # noqa: E402
from tools.sim_harness.fan_thermostat_two_phase import build_two_phase_scenarios  # noqa: E402

_GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"


def _load_goldens() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        with path.open(encoding="utf-8") as fh, contextlib.suppress(json.JSONDecodeError):
            out.append((path.stem, json.load(fh)))
    return out


def run_positive_control(verbose: bool) -> int:
    print("=== FAN THERMOSTAT CHECK integration positive control ===")
    print("Rotate every real decide_fan_thermostat_check() outcome to a different one; confirm every")
    print("scenario that reaches the function diverges from an untouched baseline.\n")

    goldens = _load_goldens()
    total = 0
    diverged = 0
    for name, scen in goldens:
        diff = diff_runs(scen, mutate_b=break_fan_thermostat_decision, scenario_name=f"{name}_BROKEN")
        if diff.a_error or diff.b_error:
            print(f"  [ERROR] {name}: a_error={diff.a_error} b_error={diff.b_error}")
            continue
        total += 1
        if not diff.is_clean:
            diverged += 1
        elif verbose:
            print(f"  (no divergence — {name} likely never reaches fan_thermostat_check)")

    print(f"\nScenarios: {total}  Diverged: {diverged}")
    if diverged == 0:
        print("\nPOSITIVE CONTROL FAILED — no scenario diverged; the extraction may not be load-bearing.")
        return 1
    print("\nPOSITIVE CONTROL PASSED — corrupting the pure function changes real production behavior.")
    return 0


def run_sweep(synthetic: str | None, verbose: bool) -> int:
    from tools.sim_harness.fan_thermostat_decision_compare import FanThermostatComparisonRun, compare_scenario

    goldens = _load_goldens()
    run = FanThermostatComparisonRun()
    for name, scen in goldens:
        compare_scenario(scen, name, run)

    print(f"=== FAN THERMOSTAT CHECK two-phase synthetic sweep: {len(goldens)} goldens", end="")
    if synthetic:
        two_phase = build_two_phase_scenarios(t=3, limit=None if synthetic == "all" else int(synthetic))
        print(f" + {len(two_phase)} two-phase synthetic ===\n")
        for tp in two_phase:
            compare_scenario(tp.scenario, tp.name, run)
    else:
        print(" ===\n")

    from collections import Counter

    print(f"Calls: {run.n_calls}  Agree: {run.n_agree}  Disagree: {len(run.disagreements)}  Errors: {len(run.errors)}")
    print(f"Real outcome distribution: {Counter(c.real_outcome for c in run.calls)}")

    if run.disagreements:
        print("\n--- DISAGREEMENTS ---")
        for c in run.disagreements[: (50 if verbose else 10)]:
            print(f"  [{c.scenario_name}] real={c.real_outcome} new={c.new_outcome}")
    if run.errors:
        print("\n--- ERRORS ---")
        for e in run.errors[:10]:
            print(f"  {e}")

    print(
        f"\nFAN_THERMOSTAT_TWO_PHASE_RESULT calls={run.n_calls} agree={run.n_agree} "
        f"disagree={len(run.disagreements)} errors={len(run.errors)}"
    )
    if not run.disagreements and not run.errors:
        print(f"\nEXIT CRITERION MET: {run.n_calls}/{run.n_calls} calls agree.")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    logging.disable(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Prove the fan_thermostat_check pure-core extraction is load-bearing.")
    parser.add_argument("--synthetic", type=str, default=None, help="Also run two-phase synthetic scenarios.")
    parser.add_argument("--positive-control", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.positive_control:
        return run_positive_control(args.verbose)
    return run_sweep(args.synthetic, args.verbose)


if __name__ == "__main__":
    _exit_code = main()
    close_loop()
    raise SystemExit(_exit_code)
