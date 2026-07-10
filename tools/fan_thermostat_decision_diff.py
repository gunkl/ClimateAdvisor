"""fan_thermostat_decision_diff — CLI: shadow-mode validation for the fan thermostat check.

Substitution mode is deferred for this function — see
tools/sim_harness/fan_thermostat_decision_compare.py's module note for why
(faithful substitution would require hand-reconstructing production side
effects, which is itself a duplication risk).

Usage:
  python tools/fan_thermostat_decision_diff.py                  # goldens only
  python tools/fan_thermostat_decision_diff.py --synthetic all   # + full synthetic sweep
  python tools/fan_thermostat_decision_diff.py --positive-control
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

# Must run BEFORE any custom_components.climate_advisor.* import — importing any
# submodule (even a pure one with no HA dependency, like fan_thermostat_decision.py)
# executes the package's __init__.py first, which needs real `homeassistant`
# unless stubs are installed. Other CLIs in this repo get this transitively via
# run_production.py's own module-level install_ha_stubs() call; this one imports
# a pure decision module directly in run_positive_control() before that chain
# would otherwise fire, so it must be explicit here.
install_ha_stubs()

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.enumerator import build_enumerated_scenarios  # noqa: E402
from tools.sim_harness.fan_thermostat_decision_compare import (  # noqa: E402
    FanThermostatComparisonRun,
    compare_scenario,
)

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
    """Prove the comparator detects a broken new function.

    Forces the OPPOSITE of KEEP (STOP_DEACTIVATE), not KEEP itself — every real
    call across the golden corpus is already KEEP (a real, separate coverage
    finding; see the Step-2 status report), so forcing "always KEEP" would
    trivially show zero disagreement regardless of whether detection works at
    all. Forcing a genuinely different outcome is the only valid test here.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome

    print("=== FAN THERMOSTAT CHECK positive control ===")
    print("Deliberately break decide_fan_thermostat_check() (always STOP_DEACTIVATE), confirm detection.\n")

    goldens = _load_goldens()
    run = FanThermostatComparisonRun()
    with patch(
        "custom_components.climate_advisor.fan_thermostat_decision.decide_fan_thermostat_check",
        lambda inputs: FanThermostatOutcome.STOP_DEACTIVATE,
    ):
        for name, scen in goldens:
            compare_scenario(scen, f"{name}_BROKEN", run)

    print(f"Calls: {run.n_calls}  Disagree: {len(run.disagreements)}")
    if verbose:
        for c in run.disagreements[:10]:
            print(f"  {c.scenario_name}: real={c.real_outcome} new(broken)={c.new_outcome}")

    if run.n_calls > 0 and run.disagreements:
        print("\nPOSITIVE CONTROL PASSED — comparator detects a broken new function.")
        return 0
    print("\nPOSITIVE CONTROL FAILED (or zero calls intercepted).")
    return 1


def run_sweep(synthetic: str | None, verbose: bool) -> int:
    goldens = _load_goldens()
    run = FanThermostatComparisonRun()

    print(f"=== FAN THERMOSTAT CHECK shadow comparison: {len(goldens)} goldens", end="")
    for name, scen in goldens:
        compare_scenario(scen, name, run)

    if synthetic:
        n = None if synthetic == "all" else int(synthetic)
        synth = build_enumerated_scenarios(t=3, limit=n)
        print(f" + {len(synth)} synthetic ===\n")
        for es in synth:
            compare_scenario(es.scenario, es.name, run)
    else:
        print(" ===\n")

    print(f"Calls: {run.n_calls}  Agree: {run.n_agree}  Disagree: {len(run.disagreements)}  Errors: {len(run.errors)}")

    if run.disagreements:
        print("\n--- DISAGREEMENTS ---")
        for c in run.disagreements[: (50 if verbose else 10)]:
            print(f"  [{c.scenario_name}] real={c.real_outcome} new={c.new_outcome}")

    if run.errors:
        print("\n--- ERRORS ---")
        for e in run.errors[:10]:
            print(f"  {e}")

    print(
        f"\nFAN_THERMOSTAT_RESULT calls={run.n_calls} agree={run.n_agree} "
        f"disagree={len(run.disagreements)} errors={len(run.errors)}"
    )

    if not run.disagreements and not run.errors:
        print(f"\nEXIT CRITERION MET: {run.n_calls}/{run.n_calls} calls agree, old vs new.")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    logging.disable(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Shadow-mode validation for the fan thermostat check.")
    parser.add_argument(
        "--synthetic", type=str, default=None, help="Also run N synthetic scenarios ('all' for full sweep)."
    )
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
