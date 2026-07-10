"""synthetic_enumerate — CLI for the boundary-focused t-wise enumerator (Step 1).

Runs every t-wise assignment (see tools/sim_harness/enumerator.py) through the SAME
old_vs_old differential-replay engine already validated against 51 goldens and a real
~5700-entry chart_log. A divergence here means one of two things:

  1. A hidden input the harness still doesn't control at this specific boundary
     combination (same meaning as everywhere else in Step 1) — fix the harness.
  2. Once mutation testing is layered on top (a later step, not this CLI), a genuine
     bug at that boundary — fix production.

This run is old-vs-old only: it exercises detection reach, not correctness (there is
still no oracle at this stage — see the plan's "oracle is equivalence, not
correctness" note).

A full-scale sweep in a single process used to freeze around scenario ~2900-3000
(root cause: thousands of fresh ``asyncio.run()``-created event loops exhausting
a Windows kernel resource, invisible to Python-level gc/thread/warning tracking —
see the Step-2 status report). Fixed at the source in ``tools/sim_harness/_loop.py``
(one persistent event loop, reused for the whole process): the full 5809-scenario
sweep now completes in ~12s, single pass, no batching required.
``--offset``/``--limit`` remain available for manual partial runs.

Usage:
  python tools/synthetic_enumerate.py --t 3                 # full t=3 sweep (~5809 scenarios, ~12s)
  python tools/synthetic_enumerate.py --t 3 --limit 200 -v  # quick sample
  python tools/synthetic_enumerate.py --stats               # just report combination counts
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.differential import ScenarioDiff, old_vs_old  # noqa: E402
from tools.sim_harness.enumerator import (  # noqa: E402
    DIMENSIONS,
    CoverageStats,
    build_enumerated_scenarios,
)


def run_stats(t: int) -> int:
    stats = CoverageStats.compute(t)
    print(f"Dimensions considered: {stats.dims_considered}")
    for d in DIMENSIONS:
        print(f"  - {d.name}: {len(d.values)} boundary-focused value(s)")
    print(f"\nt={stats.t} full combinatorial sweep: {stats.total_assignments} scenarios")
    return 0


def run_enumeration(t: int, limit: int | None, offset: int, verbose: bool, quiet_production_logs: bool) -> int:
    if quiet_production_logs:
        logging.disable(logging.CRITICAL)
    scenarios = build_enumerated_scenarios(t=t, limit=limit, offset=offset)
    print(f"=== SYNTHETIC t={t} BOUNDARY ENUMERATION ({len(scenarios)} scenarios, offset={offset}) ===")
    print("Old-vs-old over each generated boundary combination. Divergence == a hidden")
    print("input the harness doesn't yet control at that specific boundary.\n")

    start = time.monotonic()
    clean = 0
    diverged: list[tuple[str, ScenarioDiff]] = []
    errored: list[tuple[str, ScenarioDiff]] = []

    for i, es in enumerate(scenarios):
        d = old_vs_old(es.scenario, scenario_name=es.name)
        if d.a_error or d.b_error:
            errored.append((es.name, d))
        elif d.is_clean:
            clean += 1
        else:
            diverged.append((es.name, d))
            print(f"  DIVERGE {es.name}: {es.assignment}")
            if verbose:
                for label, divs in (("event", d.event_divergences), ("action", d.action_divergences)):
                    for dv in divs[:3]:
                        print(f"    [{label} #{dv.index}] A={dv.a}  B={dv.b}")
        if verbose and (i + 1) % 500 == 0:
            print(f"  ... {i + 1}/{len(scenarios)} run ({time.monotonic() - start:.1f}s elapsed)")

    elapsed = time.monotonic() - start
    print("\n--- summary ---")
    print(f"  scenarios: {len(scenarios)}  ({elapsed:.1f}s, {elapsed / max(1, len(scenarios)) * 1000:.1f}ms/scenario)")
    print(f"  clean:     {clean}")
    print(f"  diverged:  {len(diverged)}")
    print(f"  errored:   {len(errored)}")

    if errored:
        print("\nErrored scenarios (production raised — investigate; these are NOT")
        print("necessarily hidden inputs, could be an invalid synthetic combination):")
        for name, d in errored[:10]:
            print(f"  - {name}: a={d.a_error} b={d.b_error}")
        if len(errored) > 10:
            print(f"  ... +{len(errored) - 10} more")

    # Machine-parseable summary line (useful if manually chaining --offset/--limit runs).
    print(
        f"BATCH_RESULT offset={offset} n={len(scenarios)} clean={clean} diverged={len(diverged)} "
        f"errored={len(errored)} elapsed_s={elapsed:.2f}"
    )

    if not diverged and not errored:
        print(f"\nEXIT CRITERION MET: zero divergence across all {len(scenarios)} t={t} boundary combinations.")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(description="Boundary-focused t-wise synthetic enumerator.")
    parser.add_argument("--t", type=int, default=3, help="Interaction depth (default 3; margin 4).")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of scenarios run.")
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N assignments (for batching).")
    parser.add_argument("--stats", action="store_true", help="Only report combination counts, don't run.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show divergence detail + progress.")
    parser.add_argument(
        "--quiet-production-logs",
        action="store_true",
        help="Disable production logging output (recommended for batch/CI runs).",
    )
    args = parser.parse_args(argv)

    if args.stats:
        return run_stats(args.t)
    return run_enumeration(args.t, args.limit, args.offset, args.verbose, args.quiet_production_logs)


if __name__ == "__main__":
    _exit_code = main()
    close_loop()  # avoid a ResourceWarning: unclosed event loop at interpreter exit
    raise SystemExit(_exit_code)
