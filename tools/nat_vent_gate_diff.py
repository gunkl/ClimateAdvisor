"""nat_vent_gate_diff — CLI: old-vs-new differential validation of the nat-vent gate.

Architecture-reset Step 2, first real old-vs-new comparison. For every scenario,
intercepts every call the real production engine makes to
``_nat_vent_may_reactivate()`` and independently evaluates the new pure
``decide_nat_vent_gate()`` on the same reconstructed inputs, comparing booleans.

Batching: large synthetic sweeps run as a single long-lived process show the same
progressive per-scenario slowdown documented for the Step-1 old-vs-old sweep
(asyncio.run() teardown overhead compounding across thousands of sequential
calls). Use ``--offset``/``--limit`` to run in batches — each a fresh top-level
process call (NEVER spawn this as a nested subprocess from within another
Python process; that reliably deadlocked in this environment during Step 1).
Aggregate the ``GATE_DIFF_RESULT`` line across batches.

**Tooling finding (Step 2): `| grep` on a backgrounded call reliably stalls in
this agent-harness environment, independent of workload size** — a 735-scenario
batch (smaller than four other 1000-scenario batches that each completed in
~5s) hung indefinitely when piped through `grep`, then completed instantly when
redirected to a file instead (`python tools/nat_vent_gate_diff.py ... > out.txt
2>&1`, then read/grep the file separately). Prefer file redirection over pipes
for any command run through this harness's backgrounding.

Usage:
  python tools/nat_vent_gate_diff.py                          # goldens only
  python tools/nat_vent_gate_diff.py --synthetic 500           # + first 500 synthetic
  python tools/nat_vent_gate_diff.py --synthetic all --skip-goldens --offset 0 --limit 1000
                                                                 # one batch of the full sweep
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

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.enumerator import build_enumerated_scenarios  # noqa: E402
from tools.sim_harness.nat_vent_gate_compare import GateComparisonRun, compare_scenario  # noqa: E402

_GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"


def _load_goldens() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        with path.open(encoding="utf-8") as fh, contextlib.suppress(json.JSONDecodeError):
            out.append((path.stem, json.load(fh)))
    return out


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(description="Old-vs-new differential validation of the nat-vent gate.")
    parser.add_argument(
        "--synthetic",
        type=str,
        default=None,
        help="Also run N synthetic t=3 scenarios ('all' for the full sweep, ~5809 scenarios).",
    )
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N synthetic assignments (batching).")
    parser.add_argument("--skip-goldens", action="store_true", help="Skip goldens (for synthetic-only batches).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show every disagreement in full.")
    parser.add_argument("--quiet-production-logs", action="store_true", default=True)
    args = parser.parse_args(argv)

    if args.quiet_production_logs:
        logging.disable(logging.CRITICAL)

    run = GateComparisonRun()

    goldens = [] if args.skip_goldens else _load_goldens()
    print(f"=== NAT-VENT GATE old-vs-new: {len(goldens)} goldens", end="")
    if args.synthetic:
        n = None if args.synthetic == "all" else int(args.synthetic)
        print(f" + {'all' if n is None else n} synthetic t=3 (offset={args.offset})", end="")
    print(" ===\n")

    for name, scen in goldens:
        try:
            compare_scenario(scen, name, run)
        except Exception as exc:  # noqa: BLE001
            run.errors.append(f"{name}: scenario run error: {type(exc).__name__}: {exc}")

    if args.synthetic:
        n = None if args.synthetic == "all" else int(args.synthetic)
        synth = build_enumerated_scenarios(t=3, limit=n, offset=args.offset)
        for es in synth:
            try:
                compare_scenario(es.scenario, es.name, run)
            except Exception as exc:  # noqa: BLE001
                run.errors.append(f"{es.name}: scenario run error: {type(exc).__name__}: {exc}")

    print(f"Gate calls intercepted: {run.n_calls}")
    print(f"Agree:                  {run.n_agree}")
    print(f"Disagree:               {len(run.disagreements)}")
    print(f"Comparator errors:      {len(run.errors)}")

    if run.disagreements:
        print("\n--- DISAGREEMENTS ---")
        for c in run.disagreements[: (50 if args.verbose else 10)]:
            print(f"  [{c.scenario_name}] real={c.real_result} new={c.new_result}")
            print(f"    real_kwargs: {c.real_kwargs}")
            print(f"    new_inputs:  {c.new_inputs}")
        if not args.verbose and len(run.disagreements) > 10:
            print(f"  ... +{len(run.disagreements) - 10} more (use -v to see all)")

    if run.errors:
        print("\n--- COMPARATOR ERRORS ---")
        for e in run.errors[:10]:
            print(f"  {e}")

    print(
        f"\nGATE_DIFF_RESULT calls={run.n_calls} agree={run.n_agree} "
        f"disagree={len(run.disagreements)} errors={len(run.errors)}"
    )

    if not run.disagreements and not run.errors and run.n_calls > 0:
        print(f"\nEXIT CRITERION MET: {run.n_calls}/{run.n_calls} gate calls agree, old vs new.")
        return 0
    if run.n_calls == 0:
        print("\nWARNING: zero gate calls intercepted — nothing was actually compared.")
        return 1
    return 1


if __name__ == "__main__":
    _exit_code = main()
    close_loop()  # avoid a ResourceWarning: unclosed event loop at interpreter exit
    raise SystemExit(_exit_code)
