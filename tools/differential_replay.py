"""differential_replay — CLI driver for the differential-replay harness (architecture-reset Step 1).

Two jobs:

  --positive-control   Run the "test the test" checks: a deliberately mutated run
                       MUST diverge from the baseline, per log type (action + event).
                       If these ever pass as "clean", the diff engine is broken and
                       no zero result below can be trusted.

  (default)            Old-vs-old hidden-input hunt: run every golden scenario twice
                       through the REAL production engine and report any divergence.
                       Divergence between two runs of identical code == a hidden input
                       the harness failed to control (wall-clock, RNG, iteration order,
                       un-snapshotted state). Zero divergence across all goldens is the
                       Step-1 exit criterion.

  --chart-log PATH     Same old-vs-old hunt, but driven by real (indoor, outdoor)
                       trajectories from a CA chart_log file instead of the goldens.
                       Recorded hvac/fan/windows decisions in the log are discarded —
                       only the physical input trajectory is replayed (see
                       tools/sim_harness/chart_log_driver.py). This is an INPUT
                       source, never an output oracle.

Pure test/tooling — imports the sim_harness and reads golden JSON. Touches no
production code.

Usage:
  python tools/differential_replay.py                    # old-vs-old over all goldens
  python tools/differential_replay.py --positive-control # test-the-test
  python tools/differential_replay.py --limit 5 -v       # first 5 goldens, verbose
  python tools/differential_replay.py --chart-log scratch_chart_log.json --chart-log-max-entries 500
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.chart_log_driver import build_scenario_from_chart_log, load_chart_log  # noqa: E402
from tools.sim_harness.differential import (  # noqa: E402
    ScenarioDiff,
    bump_setpoint_mutation,
    diff_runs,
    extra_event_mutation,
    old_vs_old,
)

_GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"


def _load_goldens(limit: int | None = None) -> list[tuple[str, dict]]:
    """Load golden scenario JSONs (name, dict), excluding MANIFEST.json."""
    out: list[tuple[str, dict]] = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        with path.open(encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                print(f"  SKIP {path.name}: invalid JSON ({exc})")
                continue
        out.append((path.stem, data))
        if limit is not None and len(out) >= limit:
            break
    return out


def _print_divergences(diff: ScenarioDiff, verbose: bool) -> None:
    """Print the first few divergences of a non-clean diff."""
    show = 3 if not verbose else 20
    for label, divs in (("event", diff.event_divergences), ("action", diff.action_divergences)):
        for d in divs[:show]:
            print(f"    [{label} #{d.index}]")
            print(f"      A: {d.a}")
            print(f"      B: {d.b}")
        if len(divs) > show:
            print(f"    … +{len(divs) - show} more {label} divergences")


def run_positive_control(verbose: bool) -> int:
    """Prove the diff engine detects a real difference in each log type."""
    goldens = _load_goldens()
    if not goldens:
        print("No golden scenarios found — cannot run positive control.")
        return 1

    print("=== POSITIVE CONTROL (test the test) ===")
    print("A deliberately mutated run MUST diverge. If any check reports 'clean', the")
    print("diff engine is broken and no old-vs-old zero result can be trusted.\n")

    # ACTION-log positive control: find a golden whose baseline emits >=1 setpoint,
    # then confirm the +5F setpoint mutation makes the action_log diverge.
    action_ok = False
    action_probe: str | None = None
    for name, scen in goldens:
        d = diff_runs(scen, mutate_b=lambda: bump_setpoint_mutation(5.0), scenario_name=name)
        if d.a_error or d.b_error:
            continue
        if d.action_divergences:
            action_ok = True
            action_probe = name
            print(
                f"[action] DETECTED on '{name}': {len(d.action_divergences)} action divergence(s) "
                f"under +5F setpoint mutation [OK]"
            )
            if verbose:
                _print_divergences(d, verbose)
            break
    if not action_ok:
        print("[action] FAILED — no golden showed an action_log divergence under setpoint mutation. [FAIL]")

    # EVENT-log positive control: the extra-event mutation must add an event.
    event_ok = False
    event_probe: str | None = None
    for name, scen in goldens:
        d = diff_runs(scen, mutate_b=extra_event_mutation, scenario_name=name)
        if d.a_error or d.b_error:
            continue
        if d.event_divergences:
            event_ok = True
            event_probe = name
            print(
                f"[event]  DETECTED on '{name}': {len(d.event_divergences)} event divergence(s) "
                f"under extra-event mutation [OK]"
            )
            if verbose:
                _print_divergences(d, verbose)
            break
    if not event_ok:
        print("[event]  FAILED — extra-event mutation produced no event_log divergence. [FAIL]")

    print()
    if action_ok and event_ok:
        print(
            f"POSITIVE CONTROL PASSED — diff engine detects action (via '{action_probe}') "
            f"and event (via '{event_probe}') differences."
        )
        return 0
    print("POSITIVE CONTROL FAILED — do not trust any zero old-vs-old result until fixed.")
    return 1


def run_old_vs_old(limit: int | None, verbose: bool) -> int:
    """Run every golden twice with identical code and report divergences."""
    goldens = _load_goldens(limit)
    print(f"=== OLD-vs-OLD hidden-input hunt ({len(goldens)} golden scenarios) ===")
    print("Any divergence == a hidden input the harness failed to control.\n")

    clean = 0
    diverged: list[ScenarioDiff] = []
    errored: list[ScenarioDiff] = []

    for name, scen in goldens:
        d = old_vs_old(scen, scenario_name=name)
        if d.a_error or d.b_error:
            errored.append(d)
            print(f"  ERROR   {name}: a={d.a_error} b={d.b_error}")
            continue
        if d.is_clean:
            clean += 1
            if verbose:
                print(f"  clean   {name}")
        else:
            diverged.append(d)
            print(f"  DIVERGE {d.summary()}")
            _print_divergences(d, verbose)

    print("\n--- summary ---")
    print(f"  clean:    {clean}")
    print(f"  diverged: {len(diverged)}")
    print(f"  errored:  {len(errored)}")
    if diverged:
        print("\nHidden inputs to capture (scenarios that diverged old-vs-old):")
        for d in diverged:
            print(f"  - {d.scenario_name}")
    if not diverged and not errored:
        print("\nEXIT CRITERION MET: zero old-vs-old divergence across all goldens.")
        return 0
    return 1


def run_old_vs_old_chart_log(path: str, max_entries: int | None, stride: int, verbose: bool) -> int:
    """Old-vs-old hunt driven by real chart_log input trajectories (input-only, no oracle)."""
    entries = load_chart_log(path)
    if not entries:
        print(f"No usable chart_log entries found at {path!r}.")
        return 1

    scen = build_scenario_from_chart_log(entries, max_entries=max_entries, stride=stride)
    n_events = len(scen["events"])
    print(f"=== OLD-vs-OLD chart_log replay: {path} ({n_events} temp_update events) ===")
    print("Input trajectories only — recorded hvac/fan/windows decisions in the log are")
    print("discarded; any divergence == a hidden input the harness failed to control.\n")

    if n_events == 0:
        print("No usable (indoor, outdoor) pairs in this log slice.")
        return 1

    d = old_vs_old(scen, scenario_name=Path(path).stem)
    if d.a_error or d.b_error:
        print(f"  ERROR   a={d.a_error} b={d.b_error}")
        return 1
    if d.is_clean:
        print(f"  clean   {d.scenario_name}  ({n_events} events)")
        print("\nEXIT CRITERION MET: zero old-vs-old divergence over this chart_log slice.")
        return 0
    print(f"  DIVERGE {d.summary()}")
    _print_divergences(d, verbose)
    return 1


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; production code and our reports emit °F, em-dashes,
    # etc. Reconfigure to UTF-8 (replace on failure) so reporting never crashes on encoding.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(description="Differential-replay harness driver.")
    parser.add_argument("--positive-control", action="store_true", help="Run the test-the-test checks.")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N goldens.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show clean scenarios / more divergences.")
    parser.add_argument("--chart-log", type=str, default=None, help="Path to a CA chart_log JSON file to replay.")
    parser.add_argument(
        "--chart-log-max-entries",
        type=int,
        default=500,
        help="Cap usable (indoor,outdoor) pairs taken from the end of the log (default 500).",
    )
    parser.add_argument("--chart-log-stride", type=int, default=1, help="Use every Nth usable entry.")
    args = parser.parse_args(argv)

    if args.chart_log:
        return run_old_vs_old_chart_log(args.chart_log, args.chart_log_max_entries, args.chart_log_stride, args.verbose)
    if args.positive_control:
        return run_positive_control(args.verbose)
    return run_old_vs_old(args.limit, args.verbose)


if __name__ == "__main__":
    _exit_code = main()
    close_loop()  # avoid a ResourceWarning: unclosed event loop at interpreter exit
    raise SystemExit(_exit_code)
