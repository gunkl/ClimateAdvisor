"""nat_vent_gate_integration_check — CLI: prove the pure-core gate extraction is load-bearing.

Usage:
  python tools/nat_vent_gate_integration_check.py --positive-control
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
from tools.sim_harness.nat_vent_gate_integration import break_nat_vent_gate  # noqa: E402

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
    print("=== NAT-VENT GATE integration positive control ===")
    print("Invert every real decide_nat_vent_gate() outcome; confirm every scenario that")
    print("reaches the gate diverges from an untouched baseline.\n")

    goldens = _load_goldens()
    total = 0
    diverged = 0
    for name, scen in goldens:
        diff = diff_runs(scen, mutate_b=break_nat_vent_gate, scenario_name=f"{name}_BROKEN")
        if diff.a_error or diff.b_error:
            print(f"  [ERROR] {name}: a_error={diff.a_error} b_error={diff.b_error}")
            continue
        total += 1
        if not diff.is_clean:
            diverged += 1
        elif verbose:
            print(f"  (no divergence — {name} likely never reaches the reactivation gate)")

    print(f"\nScenarios: {total}  Diverged: {diverged}")
    if diverged == 0:
        print("\nPOSITIVE CONTROL FAILED — no scenario diverged; the extraction may not be load-bearing.")
        return 1
    print("\nPOSITIVE CONTROL PASSED — corrupting the pure function changes real production behavior.")
    return 0


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    logging.disable(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Prove the nat-vent gate pure-core extraction is load-bearing.")
    parser.add_argument("--positive-control", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.positive_control:
        return run_positive_control(args.verbose)
    parser.print_help()
    return 1


if __name__ == "__main__":
    _exit_code = main()
    close_loop()
    raise SystemExit(_exit_code)
