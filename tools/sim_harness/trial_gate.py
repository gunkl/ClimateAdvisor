"""trial_gate - the Step 3 trial (Issue #1128 I4): can generated days find a known regression unaided?

Run from the ROOT OF A PRODUCTION TREE (cwd decides which ``custom_components`` is imported):

    python -m tools.sim_harness.trial_gate --seeds 1 2 3 4 5 --days 200 [--promise settling_flip] [--out f.json]

The generator knows nothing about the settling guard; the promise registry (the code in the tree
it runs in) decides what is broken.  Prints one JSON document: per master seed, days run, days on
which ``--promise`` broke, the first such day (its replay seed) and every other hard promise seen.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from tools.sim_harness.generator import PROFILES, run_batch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--profile", choices=PROFILES, default="coordinator")
    ap.add_argument("--promise", default="settling_flip")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    rows = []
    for seed in args.seeds:
        res = run_batch(seed, args.days, args.profile)
        s = res.to_summary()
        hits = res.hits.get(args.promise, [])
        rows.append(
            {
                "master_seed": seed,
                "days": res.days,
                "seconds": s["seconds"],
                f"days_with_{args.promise}": len({d for d, _ in hits}),
                "first_hit_day_seed": hits[0][0] if hits else None,
                "first_hit": (hits[0][1].detail, dict(hits[0][1].evidence)) if hits else None,
                "other_hard": {k: v for k, v in s["days_with_hit"].items() if k != args.promise},
                "run_errors": len(res.errors),
                "callback_error_days": len(res.callback_error_days),
                "coverage": {
                    "day_types": s["day_types"],
                    "days_cooling_then_heating_within_2h": s["days_cooling_then_heating"],
                },
            }
        )
    doc = {"promise": args.promise, "profile": args.profile, "days_per_seed": args.days, "seeds": rows}
    text = json.dumps(doc, indent=1, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
