# Plan: Issue #1728: tests that catch background work that could freeze Home Assistant

## Outcome
A change that would let Climate Advisor's background work starve Home Assistant's event loop must fail CI before it
reaches a home: a long job in an executor thread, or slow work on the loop itself. #1722 was caught by a Pi user.
Production behaviour does not change.

## Budgets (owner-approved recommendations, 2026-11-26; two-way: constants, easy to change)
- Executor job: 0.5 s CPU on x86. A Pi is about 5x slower (estimate; #1722: 99 s Pi vs 21-24 s x86).
- Loop lag while a heavy job runs: p99 at most max(15 ms, 4x the idle p99), 25 ms on one core, and at most 15% of
  ticks lost. The first draft said p99 3 ms; it flaked (4.20 ms on 2 busy cores), so it was loosened during review.
  Tick loss is the main discriminator (process mode at most 3%, the thread controls 49% or more).
- Loop callback (report only in phase 2): 10 ms flagged, 20 ms highlighted.

## Phase 1
### A. Executor-job registry guard (unit tier, AST, deterministic)
- `tests/test_executor_job_registry_1728.py`.
- Every callable handed to `_executor_job(...)`, `hass.async_add_executor_job(...)`, `self.hass.async_add_executor_job(...)`
  or `_process_job(...)` anywhere in `custom_components/climate_advisor/` is classified in one registry.
- Classes:
  - `io`: file, network or client construction;
  - `short_cpu`: a measured bound, with a reason;
  - `worker`: a `process_offload.JOBS` name.
- Fails on an unclassified callable and on a stale registry entry.
- A `short_cpu` entry must state its measured x86 cost, which must be under the 0.5 s budget.
- Reuses the AST helpers in `tests/test_executor_offload.py` where possible.

### C. Loop-responsiveness test (real-HA tier)
- `tests_ha/test_loop_responsiveness_1728.py`.
- Real house fixture, worker jobs in process mode for these tests only, the sim clock held still, a real-time 5 ms
  ticker probe.
- For each heavy job (v4 fit, night replay, cycle refit) on a realistic-size history, the loop stays within budget.
- A control runs the same job in thread mode and shows the probe would have failed. This proves the test detects the
  #1722 mechanism.
- Thresholds are relative to an idle baseline measured in the same test, to tolerate shared CI runners.

## Phase 2
### B. Report-only performance report from the sim harness
- `tools/sim_harness/perf_probe.py` (new): records thread CPU per executor callable and per loop callback (task step)
  during `tools/simulate.py` runs.
- The GC is disabled during measurement, or GC time is subtracted: GC pauses were 200-400 ms spikes in the
  prototype.
- Deterministic line counts (`sys.monitoring`, the #1331 technique) for named hot functions.
- `tools/simulate.py --perf-report [path]` prints a table and writes JSON: top jobs and callbacks by p99 and max,
  flagged against the budgets.
- `nightly.yml` runs it report-only (never fails) and uploads the JSON.
- A ratchet against a baseline is a later step, once the numbers are stable.

## Docs and bookkeeping
- `claude.md` Thread-Safety "Enforcement": name the new guards.
- `docs/DEVELOPMENT.md`, `docs/sim-harness-brief.md`, `docs/process-offload-brief.md` guards table.
- Version bump, CHANGELOG (test-only), `fix_history` (no user summary).

## One-way vs two-way
- **One-way (ask the owner):**
  - merging;
  - any production behaviour or stored-format change (none planned);
  - a Repairs notice (not in scope);
  - a new CI job that costs meaningful minutes. Phase 2 adds a step to the existing nightly job, not a new job.
- **Two-way:** everything else above.

## Execution Structure
| Agent | Phases | Files |
|---|---|---|
| A (Sonnet) | 1A | `tests/test_executor_job_registry_1728.py` |
| C (Sonnet) | 1C | `tests_ha/test_loop_responsiveness_1728.py` |
| B (Sonnet) | 2 | `tools/sim_harness/perf_probe.py`, `tools/simulate.py` (flag only), `tests/test_perf_probe_1728.py`, `.github/workflows/nightly.yml` (one step) |
| → Verification (Opus), pass 1 | | all of the above, full CI parity |
| Scribe (Sonnet) | docs | `claude.md`, `docs/*`, CHANGELOG, `fix_history`, VERSION |
| → Verification (Opus), pass 2 | | everything |
