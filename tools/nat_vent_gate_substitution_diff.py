"""nat_vent_gate_substitution_diff — CLI: full-scenario substitution testing.

Closes the gap shadow-mode comparison left open: proves that letting the new
pure ``decide_nat_vent_gate()`` actually DRIVE the live engine (not just answer
a shadow question) produces an IDENTICAL full scenario outcome — the entire
``action_log``/``event_log``, not just the one function's boolean — to an
untouched baseline run.

Includes its own positive control (a substitution CLI that always reports
"identical" would be worthless) — see ``run_positive_control()``.

Usage:
  python tools/nat_vent_gate_substitution_diff.py                  # goldens only
  python tools/nat_vent_gate_substitution_diff.py --synthetic all  # + full synthetic sweep
  python tools/nat_vent_gate_substitution_diff.py --positive-control
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
from tools.sim_harness.differential import ScenarioDiff, diff_runs  # noqa: E402
from tools.sim_harness.enumerator import build_enumerated_scenarios  # noqa: E402
from tools.sim_harness.nat_vent_gate_compare import substitute_new_gate  # noqa: E402

_GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"


def _load_goldens() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        with path.open(encoding="utf-8") as fh, contextlib.suppress(json.JSONDecodeError):
            out.append((path.stem, json.load(fh)))
    return out


def _print_divergences(diff: ScenarioDiff, verbose: bool) -> None:
    show = 3 if not verbose else 20
    for label, divs in (("event", diff.event_divergences), ("action", diff.action_divergences)):
        for d in divs[:show]:
            print(f"    [{label} #{d.index}] A(baseline)={d.a}  B(substituted)={d.b}")
        if len(divs) > show:
            print(f"    ... +{len(divs) - show} more {label} divergences")


def run_positive_control(verbose: bool) -> int:
    """Prove the substitution-diff CAN detect a real divergence, using a
    deliberately-broken new function (always returns False)."""
    from unittest.mock import patch

    print("=== SUBSTITUTION POSITIVE CONTROL (test the test) ===")
    print("Deliberately break decide_nat_vent_gate() (always return False), substitute it")
    print("in, and confirm the FULL scenario diverges from baseline.\n")

    goldens = _load_goldens()
    for name, scen in goldens:
        with patch("custom_components.climate_advisor.nat_vent_gate.decide_nat_vent_gate", lambda inputs: False):
            diff = diff_runs(scen, mutate_b=substitute_new_gate, scenario_name=f"{name}_BROKEN")
        if diff.a_error or diff.b_error:
            continue
        if not diff.is_clean:
            print(
                f"DETECTED on '{name}': {len(diff.event_divergences)} event, "
                f"{len(diff.action_divergences)} action divergence(s)"
            )
            if verbose:
                _print_divergences(diff, verbose)
            print("\nPOSITIVE CONTROL PASSED — substitution-diff detects a broken new function.")
            return 0
    print("POSITIVE CONTROL FAILED — no golden showed a divergence under a broken substitution.")
    return 1


def run_substitution_sweep(synthetic: str | None, verbose: bool) -> int:
    goldens = _load_goldens()
    scenarios: list[tuple[str, dict]] = list(goldens)
    if synthetic:
        n = None if synthetic == "all" else int(synthetic)
        synth = build_enumerated_scenarios(t=3, limit=n)
        scenarios += [(es.name, es.scenario) for es in synth]

    print(
        f"=== SUBSTITUTION TEST: {len(goldens)} goldens"
        f"{f' + {len(scenarios) - len(goldens)} synthetic' if synthetic else ''} ==="
    )
    print("Baseline run (untouched) vs substituted run (new function actually drives behavior).")
    print("Diffing the FULL action_log + event_log, not just the gate's boolean.\n")

    clean = 0
    diverged: list[ScenarioDiff] = []
    errored: list[ScenarioDiff] = []

    for name, scen in scenarios:
        diff = diff_runs(scen, mutate_b=substitute_new_gate, scenario_name=name)
        if diff.a_error or diff.b_error:
            errored.append(diff)
            print(f"  ERROR   {name}: a={diff.a_error} b={diff.b_error}")
        elif diff.is_clean:
            clean += 1
        else:
            diverged.append(diff)
            print(f"  DIVERGE {diff.summary()}")
            _print_divergences(diff, verbose)

    print("\n--- summary ---")
    print(f"  scenarios: {len(scenarios)}")
    print(f"  clean:     {clean}")
    print(f"  diverged:  {len(diverged)}")
    print(f"  errored:   {len(errored)}")
    print(f"\nSUBSTITUTION_RESULT n={len(scenarios)} clean={clean} diverged={len(diverged)} errored={len(errored)}")

    if not diverged and not errored:
        print(
            f"\nEXIT CRITERION MET: {len(scenarios)}/{len(scenarios)} scenarios produce IDENTICAL "
            f"full outcomes whether the gate is shadow-checked or actually substituted in."
        )
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    logging.disable(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Full-scenario substitution testing for the nat-vent gate.")
    parser.add_argument(
        "--synthetic", type=str, default=None, help="Also run N synthetic scenarios ('all' for full sweep)."
    )
    parser.add_argument("--positive-control", action="store_true", help="Run the test-the-test check.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.positive_control:
        return run_positive_control(args.verbose)
    return run_substitution_sweep(args.synthetic, args.verbose)


if __name__ == "__main__":
    _exit_code = main()
    close_loop()
    raise SystemExit(_exit_code)
