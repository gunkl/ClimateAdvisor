# Trial gate report: can generated days find the #1073 regression unaided? (Step 3, I4)

*Scribe, 2026-10-30. Design reference: `03-step3-design.md` (I4, trial gate). Numbers below were supplied by the Coordinator from the trial runs; the code facts (command, defaults) were checked against `tools/sim_harness/trial_gate.py`.*

## What was tested

Issue #1073 was a regression: after the AC ran overnight, the wake-up floor rose and the furnace started a few minutes later, a heat-after-cool flip that the settling guard exists to stop. It entered with #1066/#1072 (`d7209f4`, nearest-edge arming without the settling guard) and was fixed in `a80d773`.

The question: can seeded random days, with a generator that knows nothing about the settling guard, find that bug on `d7209f4` and stay quiet on the fixed code?

| Item | Value |
|---|---|
| Promise | `settling_flip` (Promise 1) |
| Pre-fix tree | `d7209f4` (parent of the fix) |
| Fix tree | `a80d773` |
| Current tree | HEAD of `feature/1125-test-automation` |
| Days | 100 per master seed, master seeds 1-5 |
| Profile | `coordinator`; every simulated day runs the 30-minute update cycle (generator fix, 2026-10-30) |
| Overlay | the current `tools/sim_harness/` and promise files copied onto each exported tree; production code under test is the tree's own |
| Runtime | about 0.4 s per simulated day |

## How to re-run

Run from the ROOT OF THE PRODUCTION TREE under test (the working directory decides which `custom_components` is imported; the module docstring says so):

```
python -m tools.sim_harness.trial_gate --seeds 1 2 3 4 5 --days 100 --profile coordinator --promise settling_flip --out trial.json
```

The tool defaults are `--seeds 1 2 3 4 5`, `--days 200`, `--profile coordinator`, `--promise settling_flip`; pass `--days 100` to match this report. It prints one JSON document with, per master seed: days run, `days_with_settling_flip`, `first_hit_day_seed`, the first hit's detail and evidence, other hard promises seen, run and callback errors, and coverage (`day_types`, `days_cooling_then_heating_within_2h`).

To reproduce on the pre-fix tree: export `d7209f4` to a scratch directory, copy the current `tools/sim_harness/` and the promise files (`invariant_watchdog.py`, `promise_journal.py`) over it, then run the command above from that directory. Replay one finding: `python tools/simulate.py --generate-day <day_seed>`.

## Results

`settling_flip` days out of 100, by master seed:

| Master seed | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Pre-fix `d7209f4` | 2 | 2 | 2 | 2 | 4 |
| First hit (day seed) | 1000028 | 2000048 | 3000026 | 4000034 | 5000000 |
| Fix `a80d773` | 0 | 0 | 0 | 0 | 0 |

## Pass criteria

| | Criterion | Result |
|---|---|---|
| a | Hit on at least 3 of 5 seeds on the pre-fix tree | Met, 5 of 5 |
| b | The hit shows the #1073 mechanism, not a coincidence | Met. Day 1000028: the AC runs overnight; at wake-up the floor rises to 68 with indoor at 67, and heat starts 35 min after the AC (dip 1.0 F, hot day). On `a80d773` and on HEAD the same replay is clean. |
| c | No `settling_flip` hits on the fix, and none on HEAD | Met. 0 on `a80d773`. HEAD calibration: 0 hits in 1000 days (10 seeds x 100 days). |
| d | The generator reaches the relevant situations | Met. Each seed saw 12 to 26 days per day type; 8 to 18 of 100 days had cooling then heating within 2 h. |

Result: the gate passes. The generator found the known regression on every seed without being told about it, and a correct tree stays quiet.

## Other promise findings on HEAD

`override_respected` (Promise 4) hit on 18 of 1000 HEAD days (10 seeds x 100). All 18 were triaged; none is left unexplained:

| Outcome | Issue | Days (day seeds) |
|---|---|---|
| Real defect | #1131 (6 days) | 1000016, 2000092, 3000000, 3000032, 5000001, 6000014 |
| Real defect | #1132 (3 days) | 3000020, 3000029, 9000029 |
| Owner decision | #1133 (12 days, includes the three #1132 days) | see the issue |
| False positives (two quick occupant changes) | fixed in the promise code | n/a |

Note the overlap: #1133 counts 12 days of which the three #1132 days are a subset, so the sets above are not additive.

## Not verified by the Scribe

- Every number in the Results, criteria and `override_respected` sections comes from the Coordinator's brief, not from a re-run by the Scribe (about 0.4 s per day, so the full set is roughly 200 s per tree; not run here because other agents are editing code).
- The #1131/#1132/#1133 issue contents were not read; titles and the "real defect / owner decision" labels are as supplied.
- The exact set of "promise files" overlaid on the pre-fix tree is assumed to be `invariant_watchdog.py` and `promise_journal.py` (the files `promise_probe.py` imports from the package); confirm against the trial scripts used.
