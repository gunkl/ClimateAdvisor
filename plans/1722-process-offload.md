# Plan: Issue #1722: long pure-Python work runs in a worker process

Issue: #1722. Credit: the report, the GIL diagnosis and the first implementation (PR #1723) are by
Jeffrey Sica (@jeefy). This change builds on that design.

## Desired outcome

Home Assistant stays responsive while Climate Advisor does any long background computation, on slow hardware
(Raspberry Pi 4) as well as fast. v4 and the cycle model learn exactly as before.

## Evidence (two verification passes, x86 4-core, Python 3.14, synthetic history)

- **The executor-thread fit starves the event loop through the GIL.** Loop I/O round-trips fell from about
  64k/s to about 72/s (15 ms per wake). The same fit in a separate process left the loop at idle level, with or
  without `nice`.
- **Jobs above 0.5 s on x86** (about 5x longer on a Pi):

  | Job | Time on x86 | When it runs |
  |---|---|---|
  | v4 fit, 4 sensors | 21-24 s | nightly; 6 h retries; after every restart |
  | night replay (10 fits) | 34.5 s | nightly, after a usable fit |
  | cycle-model refit | 1.2-1.5 s | at startup for both modes; up to every 30 min |

  The display fit takes 0.16-0.22 s.
- **Under 3 days of history every fit is "none"** (`predictor_v4.py:2076` gates on `sample_days` alone). Over 98%
  of its cost comes after `sample_days` is known (`:1682`).
- **`_v4_last_fit_attempt` is monotonic and in-memory**, so every restart refits once coalescing ends.
  `last_fit` is written only when a fit completes, so a restart mid-fit leaves no record.

## Owner decisions (2026-11-26)

1. Worker processes are an approved precedent for this class of problem. Log it in
   `docs/HA-BOUNDARY-EXCEPTIONS.md`.
2. We build the change ourselves and credit the contributor.
3. The cycle-model refit moves in the same change.
4. claude.md's thread-safety rule is updated: long pure-Python CPU work goes to a worker process.

## Design

### 1. `process_offload.py` (new, stdlib only)
- `JOBS`: a fixed allowlist `{job_name: "module:function"}` of hass-free modules in this package:
  - `v4_fit` -> `v4_shadow:run_fit_job`
  - `v4_display_fit` -> `v4_shadow:run_display_fit_job`
  - `v4_night_replay` -> `night_replay:run_nightly_job`
  - `cycle_refit` -> `cycle_model:refit_job`
- `run_job(job, payload)`: resolves the allowlisted function and calls it. It is the one code path used by both
  the child and the thread fallback.
- `async_run_job(job, payload, *, executor_job, logger, timeout_s, mode=None)`. In process mode:
  1. JSON-encode the payload in the executor.
  2. Spawn `sys.executable -I -c <bootstrap> <pkg_dir> <parent_dir>` (no shell; `-I`, so neither `/config` nor
     `PYTHON*` env vars are on the path). The child applies `os.nice(10)`.
  3. Pipe the payload through stdin and stdout.
  4. Wait with a timeout. On timeout or cancel, kill and reap the child.
  5. Relay the child's log records (JSON lines on stderr) to `logger` at their own level.
  6. Decode the result in the executor.

  A child failure raises `ProcessJobError`. Only a failed spawn (`OSError`) falls back to thread mode, with one
  WARNING.
- Thread mode runs the same `run_job` in the executor on a JSON round-trip of the payload, so tests exercise the
  same data path. `default_mode` is a module setting: `"process"` in production, `"thread"` for the unit tier,
  set in `tests/conftest.py`. Production code never sniffs for test doubles.
- The bootstrap stubs only `homeassistant.util.dt` (`now`, `utcnow`, `as_utc`), which `time_utils` imports. A
  guard test fails if an allowlisted module's import closure reaches any other `homeassistant` module.

### 2. Job functions: data in, data out, no callables
- `v4_shadow.run_fit_job`, `run_display_fit_job` and `night_replay.run_nightly_job` rebuild the two callables
  (`snapshots_fn`, `in_sleep`) from data:
  - prefetched snapshots, using a shared window helper that `run_fit` / `run_nightly` also use;
  - `sleep_window` `(sleep, wake)` strings evaluated with the existing hass-free `predictor_v4.in_sleep_window`
    (no third copy of the sleep rule);
  - `tz` as an IANA key or offset;
  - `now` as ISO.

  They call the module-level functions by attribute, so existing monkeypatches keep working.
- `cycle_model.refit_job(payload)` takes `{cycles, mode, snap, now_ts}`. It returns the `fit_cycles` result,
  including `w_by_id`. The coordinator applies `store.set_weights` itself.

### 3. Candidate A: skip the search when the fit cannot be usable
In `predictor_v4.fit()`, right after `sample_days` (`:1682`), if `sample_days < GATE_MIN_DAYS_NONE`, return the
default "none" model the way the existing early returns do:
- through `_keep_carry_source`;
- with `fit["reason"]` = "under 3 days of samples";
- with `n_anchors`, `modes` and the other Debug fields set as on the existing paths.

### 4. Candidate B (corrected): a restart does not refit at once
- Save a wall-clock `fit_attempt_at` in the existing v4 model file (`V4ModelStore`, an additive key) before every
  fit and night replay starts.
- On restore, an attempt younger than `V4_FIT_RETRY_MIN_INTERVAL_S` seeds `_v4_last_fit_attempt` as
  `_monotonic() - age`.
- The nightly 03:15 fit is unchanged.

### 5. Coordinator wiring
- `_process_job(job, payload, timeout_s)` sits next to `_executor_job`, zone-scoped.
- `_v4_run_fit`, `_v4_run_display_fit`, `_v4_run_night_replay` and `_cycle_refit` use it. All row windowing,
  snapshot prefetch and encoding run in the executor, not on the loop.
- `_v4_cancel_fit` also cancels the night-replay task, which has a stored handle (PR #1723's mirror fix).
- `_cycle_judge` (about 26 ms per new cycle) stays in the executor. It is allowlisted in the offload guard with a
  reason.
- Timeouts live in `const.py`.

### 6. Docs (Scribe)
- `claude.md`: the Thread-Safety rule.
- `docs/HA-BOUNDARY-EXCEPTIONS.md`: the new exception.
- A new Tier 2 brief, `docs/process-offload-brief.md`.
- `docs/02-ARCHITECTURE-REFERENCE.md`: the module map and anchors.
- `docs/v4-shadow-spec.md` and `docs/state-persistence.md`: the new key and the triggers.
- `docs/thermal-model-v3-spec.md`: the refit.
- CHANGELOG with credit, version bump, and `fix_history` via `tools/add_fix_entry.py`.

### Out of scope (follow-up issues)
- The warm-started fit always runs a second, cold LM pass (`_fit_problem`; resolved by #1726: skipped when the warm LM converged within `WARM_RESTART_MIN_GAIN` of its start cost).
- Event-loop jobs that are fine on x86 but may cost 0.1-0.3 s on a Pi: `build_timeline` (up to 29 ms on x86),
  `_v4_display_pending` (16 ms).

## One-way vs two-way doors

- **One-way, ask the owner:**
  - closing PR #1723, or commenting on it or on #1722;
  - merging;
  - deploying (never);
  - any golden scenario edit;
  - any change to an existing entity id or stored format that an older release cannot read.
- **Two-way, do what is recommended:**
  - everything in the design above;
  - the new additive JSON key (older releases ignore it);
  - constants;
  - docs;
  - filing the follow-up issues.

## Execution Structure

| Agent | Phases | Files |
|---|---|---|
| P (Sonnet) | 1 | `process_offload.py` (new), `tests/test_process_offload_1722.py` (new), `tests/conftest.py` (default mode) |
| J (Sonnet) | 2 | `v4_shadow.py`, `night_replay.py`, `cycle_model.py`, `tests/test_offload_jobs_1722.py` (new) |
| A (Sonnet) | 3 | `predictor_v4.py`, `tests/test_v4_early_none_1722.py` (new) |
| → Verification agent (Opus) | | ruff, the group's tests, revert-proofs, the full unit tier |
| W (Sonnet) | 4, 5 | `coordinator.py`, `v4_archive.py`, `const.py` (timeouts only), `tests/test_v4_fit_attempt_1722.py` (new), `tests/test_process_offload_wiring_1722.py` (new), `tests/test_executor_offload.py` |
| → Verification agent (Opus) | | ruff, unit + HA tiers, simulate golden/pending/integrity/surfaces, calibration |
| S (Scribe, Sonnet) | 6 | the docs listed in section 6, CHANGELOG, `fix_history.jsonl`, `const.py`/`manifest.json` VERSION |
| → Final verification (Opus), recheck sequence twice | | all changed files |

## Checks before the PR

- `ruff check . && ruff format --check .`
- `python scripts/test.py unit` and `python scripts/test.py ha`
- `tools/simulate.py`, plus `--pending`, `--check-integrity` and `--surfaces`
- calibration `--generate 100`, seeds 1-10, main vs branch, one seed per process
- the loop-lag measurement (`looplag` harness) on the branch: in-process vs offloaded
- every new test revert-proved
