"""replay - replay, shrink and report generated days (Issue #1128 I3).

A finding is an ordinary scenario file (``tools/simulations/generated/failures/*.json``, gitignored)
holding the realised events of one generated day.  Replaying it needs no generator:

    python tools/simulate.py --replay-generated tools/simulations/generated/failures/<file>.json
    python -m tools.sim_harness.replay <file> [--shrink]

``shrink`` is a small delta-debugging pass over the recorded events: it keeps removing chunks of
events for as long as the same promise still breaks, so the saved artifact is the short story, not
the whole day.  The replay is open loop (the injections were recorded), so removing an injection
just means the thermostat never heard about it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.sim_harness.generator import (
    FAILURES_DIR,
    PROFILES,
    day_seed_of,
    replay_file,
    replay_scenario,
    run_batch,
    run_day,
    save_finding,
)


def format_hit(h) -> str:
    return f"  [{h.severity}] {h.promise} at {h.ts.isoformat()} ({h.step}): {h.detail}  evidence={dict(h.evidence)}"


def _hits_promise(scenario: dict, promise: str) -> bool:
    return any(h.promise == promise for h in replay_scenario(scenario).hard)


def shrink(scenario: dict, promise: str, *, max_runs: int = 150) -> dict:
    """Return a smaller scenario that still breaks ``promise`` (best effort, bounded by ``max_runs``)."""
    report = replay_scenario(scenario)
    hit = next((h for h in report.hard if h.promise == promise), None)
    if hit is None:
        return scenario
    # 1. Nothing after the hit can have caused it.
    cut = hit.ts + timedelta(minutes=5)

    def t_of(ev: dict) -> datetime:
        return datetime.fromisoformat(ev["time"]).replace(tzinfo=UTC)

    events = [e for e in scenario["events"] if t_of(e) <= cut]
    runs = 0

    def holds(candidate: list[dict]) -> bool:
        nonlocal runs
        runs += 1
        return bool(candidate) and _hits_promise({**scenario, "events": candidate}, promise)

    if not holds(events):
        events = list(scenario["events"])
    # 2. ddmin-lite over contiguous chunks.
    n = 2
    while len(events) >= 2 and runs < max_runs:
        size = max(1, len(events) // n)
        removed_any = False
        i = 0
        while i < len(events) and runs < max_runs:
            trial = events[:i] + events[i + size :]
            if holds(trial):
                events = trial
                removed_any = True
            else:
                i += size
        if not removed_any:
            if size == 1:
                break
            n = min(len(events), n * 2)
    shrunk = {**scenario, "events": events}
    gen = dict(shrunk.get("generated", {}))
    gen["shrunk"] = {"from_events": len(scenario["events"]), "to_events": len(events), "runs": runs}
    shrunk["generated"] = gen
    return shrunk


def _quiet() -> None:
    logging.disable(logging.CRITICAL)


def cli_replay(path: str, *, do_shrink: bool = False) -> int:
    """Replay one saved day; exit 1 when a hard promise breaks."""
    _quiet()
    p = Path(path)
    scenario = json.loads(p.read_text(encoding="utf-8"))
    gen = scenario.get("generated", {})
    print(f"Replaying {p.name} (day_seed={gen.get('day_seed')}, {len(scenario['events'])} events)")
    report = replay_file(p)
    for h in report.hard:
        print(format_hit(h))
    for h in report.warnings:
        print(format_hit(h))
    if not report.hard:
        print("  no hard promise broken")
    if do_shrink and report.hard:
        target = report.hard[0].promise
        small = shrink(scenario, target)
        out = p.with_name(p.stem + "_shrunk.json")
        out.write_text(json.dumps(small, indent=1), encoding="utf-8")
        info = small["generated"]["shrunk"]
        print(f"  shrunk {info['from_events']} -> {info['to_events']} events ({info['runs']} runs): {out}")
    return 1 if report.hard else 0


def cli_generate_day(day_seed: int, profile: str, *, save: bool = False) -> int:
    """Generate and judge one day by its seed (the one-command repro for a seed)."""
    _quiet()
    rec = run_day(day_seed, profile)
    print(
        f"day_seed={day_seed} profile={profile} unit={rec.plan.unit} high={rec.plan.forecast['high']}F "
        f"events={len(rec.scenario['events'])} {rec.seconds:.2f}s"
    )
    for h in rec.report.hard + rec.report.warnings:
        print(format_hit(h))
    if not rec.report.hard:
        print("  no hard promise broken")
    if save or rec.report.hard:
        print(f"  saved: {save_finding(rec)}")
    return 1 if rec.report.hard else 0


def cli_generate(master_seeds: list[int], days: int, profile: str, *, save: bool = True) -> int:
    """Run ``days`` generated days for each master seed; exit 1 if any hard promise broke or any day crashed."""
    _quiet()
    bad = 0
    crashed = 0
    for ms in master_seeds:
        res = run_batch(ms, days, profile, save_dir=FAILURES_DIR if save else None)
        s = res.to_summary()
        print(
            f"master_seed={ms}: {res.days} days in {res.seconds:.1f}s ({s['mean_seconds_per_day']}s/day), "
            f"hard={s['hits_by_promise'] or 'none'}, warnings={s['warnings_by_promise'] or 'none'}"
        )
        for promise, rows in res.hits.items():
            for ds, h in rows[:3]:
                print(f"  {promise} day_seed={ds}: {h.detail}")
                print(f"    replay: python tools/simulate.py --generate-day {ds} --profile {profile}")
        if res.errors:
            print(f"  {len(res.errors)} day(s) raised, first: {res.errors[0]}")
        if res.callback_error_days:
            first = res.callback_error_days[:5]
            print(f"  {len(res.callback_error_days)} day(s) logged a Climate Advisor exception: {first}")
        # A day that crashed or logged an exception was not judged at all: never report it as green.
        crashed += len(res.errors) + len(res.callback_error_days)
        bad += sum(len(v) for v in res.hits.values())
    return 1 if (bad or crashed) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay or generate household days")
    ap.add_argument("file", nargs="?", help="saved generated-day scenario to replay")
    ap.add_argument("--shrink", action="store_true")
    ap.add_argument("--generate-day", type=int, metavar="DAY_SEED")
    ap.add_argument("--generate", type=int, metavar="N", help="days per master seed")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1])
    ap.add_argument("--profile", choices=PROFILES, default="coordinator")
    args = ap.parse_args(argv)
    if args.file:
        return cli_replay(args.file, do_shrink=args.shrink)
    if args.generate_day is not None:
        return cli_generate_day(args.generate_day, args.profile)
    if args.generate:
        return cli_generate(args.seeds, args.generate, args.profile)
    ap.print_help()
    return 0


__all__ = ["cli_generate", "cli_generate_day", "cli_replay", "day_seed_of", "main", "shrink"]

if __name__ == "__main__":
    sys.exit(main())
