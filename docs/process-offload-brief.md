<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [process_offload.py](../custom_components/climate_advisor/process_offload.py) | ↔ [v4 Shadow Spec](v4-shadow-spec.md) [Thermal Model v3](thermal-model-v3-spec.md) [HA Boundary Exceptions](HA-BOUNDARY-EXCEPTIONS.md) -->

# Process Offload — Architecture Brief (Tier 2)

Issue #1722. Credit: Jeffrey Sica (@jeefy) reported the freeze, diagnosed the GIL cause and wrote the first implementation (PR #1723). This module is built on that design.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Why does Climate Advisor start worker processes? | Pure-Python CPU work in an executor thread holds the GIL and stalls the Home Assistant event loop. The v4 fit took 21-24 s on x86 and froze a Raspberry Pi 4 for up to 90 s. A child process has its own GIL. | [Purpose](#purpose) |
| Which jobs run in a worker? | Four, on the fixed allowlist `process_offload.JOBS`: `v4_fit`, `v4_display_fit`, `v4_night_replay`, `cycle_refit`. Nothing else can be named. | [Job allowlist](#job-allowlist) |
| How does the coordinator run one? | `coordinator._process_job(job, payload_fn, timeout_s)`: the payload is built in the executor, `process_offload.async_run_job` runs the worker, the caller applies the result. | [Interfaces](#interfaces) |
| What goes over the pipe? | stdin: `{"job", "payload", "log_level"}`. stdout: `{"ok": true, "result"}` or `{"ok": false, "error"}`. JSON both ways; stderr carries log lines. | [Wire format](#wire-format) |
| What does the worker know about Home Assistant? | Only a stub of `homeassistant.util.dt` (`now`, `utcnow`, `as_utc`), installed by `process_offload._install_ha_stub()`. A guard test fails if a job module imports any other `homeassistant` module. | [Bootstrap](#bootstrap) |
| What happens when a worker fails, hangs or cannot start? | Failed, killed or timed out: `ProcessJobError`, the caller keeps the previous model or rates. Cannot start (`OSError`): the same job runs in a thread, one WARNING. Unload or HA stop kills the worker. | [Timeouts](#timeouts-and-cancel), [Fallback](#fallback) |
| How do I add a job? | A JSON-in, JSON-out function in a hass-free module, a `*_job_payload` builder, an entry in `JOBS`, a `WORKER_TIMEOUT_*` constant, a coordinator call through `_process_job`, and the guard tests. | [Adding a job](#adding-a-job) |
| What stops someone running a heavy fit in the executor again? | `tests/test_executor_offload.py::TestHeavyFitsRunInWorkerProcess` (AST: no coordinator method names `run_fit`, `run_display_fit`, `run_nightly` or `fit_cycles`) and `tests/test_process_offload_1722.py::test_import_closure_of_job_modules_is_hass_free`. | [Guards](#guards) |

## Purpose

CPU-bound pure-Python work in an executor thread starves the event loop. The thread holds the GIL while it runs bytecode, and the loop needs the GIL to run. Measured on x86 (Python 3.14, synthetic history): loop I/O round-trips fell from about 64k/s to about 72/s (15 ms per wake) during an executor fit, and stayed at idle level when the same fit ran in a separate process, with or without `nice`. Issue #1722 reported 90 s freezes on a Raspberry Pi 4, which reset a Zigbee coordinator and raised false "entity not responding" alerts.

Jobs longer than about 0.5 s on x86 (about 5 times longer on a Pi):

| Job | Time on x86 | When it runs |
|---|---|---|
| v4 fit, 4 sensors | 21-24 s | nightly; 6 h retries; first fit |
| night replay (10 fits) | 34.5 s | after a usable fit |
| cycle-model refit | 1.2-1.5 s | startup (both modes); up to every 30 min |

The display fit takes 0.16-0.22 s on x86 but is a fit, so it uses the same path.

The rule for new code is in [claude.md, Thread-Safety Requirements](../claude.md#thread-safety-requirements-critical) (third hazard). The boundary approval is [HA Boundary Exceptions §4](HA-BOUNDARY-EXCEPTIONS.md#4-worker-processes-for-long-pure-python-jobs-issue-1722).

## Scope

**Owns:**
- The allowlist of worker jobs (`JOBS`) and the code that runs one: spawn, pipe, timeout, kill and reap, log relay, thread fallback (`process_offload.py`).
- The child entry point and its minimal `homeassistant.util.dt` stub.
- The JSON job functions and payload builders, which live with their domain: `v4_shadow.run_fit_job` / `run_display_fit_job` (+ `fit_job_payload` / `display_fit_job_payload`), `night_replay.run_nightly_job` (+ `nightly_job_payload`), `cycle_model.refit_job` (+ `refit_job_payload`).
- The coordinator hook `_process_job` and the worker-timeout constants (`const.py`).

**Explicitly does NOT own:**
- The fit math: `predictor_v4.py`, `cycle_model.fit_cycles`. The worker calls them; it does not change them.
- What happens with a result: the coordinator stores the model, applies cycle weights (`store.set_weights`) and writes files. The worker never writes anything.
- The locks that serialise jobs: the domain-wide v4 fit lock, the domain-wide cycle-refit worker lock and the per-zone cycle lock stay in the coordinator.
- Short work. Under about 0.5 s on x86 stays in `_executor_job`.

## Responsibilities

- Keep the number of concurrent workers at or under `worker_limit()` (cores - 1, at least 1).
- Run an allowlisted job in a short-lived `sys.executable -I -B` child, with an environment stripped of secrets, and return its decoded JSON result.
- Refuse any job name that is not on the allowlist, in both modes, before anything starts.
- Kill and reap the child on timeout and on cancel (unload, HA stop), even when cancelled twice.
- Relay the child's log records to the caller's logger at their own level.
- Run the same job in an executor thread when a process cannot start, and in the unit tier.

## Interfaces

```python
# process_offload.py
JOBS: Final[Mapping[str, str]]          # job name -> "module:function" (module in this package)
default_mode: str = "process"           # "thread" in the unit tier (tests/conftest.py)

async def async_run_job(job, payload, *, executor_job, logger, timeout_s, mode=None) -> Any: ...
def run_job(job, payload) -> Any:       # one code path for the child and thread mode
class ProcessJobError(RuntimeError): ...

# coordinator.py
async def _process_job(self, job: str, payload_fn: Callable[[], dict], timeout_s: float) -> Any: ...
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `coordinator._process_job()` | `_v4_run_fit`, `_v4_run_display_fit`, `_v4_run_night_replay`, `_cycle_refit` | Builds the payload in the executor, runs the worker, returns the result. Call it inside `log_capture.zone_scope` so relayed log lines carry the zone. |
| `process_offload.async_run_job()` | `_process_job` | Spawn, pipe, wait with a timeout, relay logs, decode. |
| `process_offload.run_job()` | the child, `_thread_run` | Resolve the job from `JOBS` and call it. Looks the function up by attribute at call time, so monkeypatches work in thread mode. |
| `coordinator._v4_cancel_fit()` | unload, HA stop | Cancels the fit task and the night-replay task (its handle is kept); cancelling kills the worker. |
| `coordinator._cycle_cancel_refit()` | unload, HA stop | Cancels a running cycle refit, so the stop-time save is not blocked on the cycle lock. |

## Job allowlist

`JOBS` maps a name to `module:function` in this package. The module is resolved lazily with `importlib` as `custom_components.climate_advisor.<module>`.

| Job | Function | Payload builder (runs in the executor) | Timeout constant | Coordinator call site |
|---|---|---|---|---|
| `v4_fit` | `v4_shadow.run_fit_job` | `v4_shadow.fit_job_payload` | `WORKER_TIMEOUT_V4_FIT_S` (900 s) | `_v4_run_fit` |
| `v4_display_fit` | `v4_shadow.run_display_fit_job` | `v4_shadow.display_fit_job_payload` | `WORKER_TIMEOUT_V4_DISPLAY_FIT_S` (600 s) | `_v4_run_display_fit` |
| `v4_night_replay` | `night_replay.run_nightly_job` | `night_replay.nightly_job_payload` | `WORKER_TIMEOUT_V4_NIGHT_REPLAY_S` (1800 s) | `_v4_run_night_replay` |
| `cycle_refit` | `cycle_model.refit_job` | `cycle_model.refit_job_payload` | `WORKER_TIMEOUT_CYCLE_REFIT_S` (600 s) | `_cycle_refit` |

`_cycle_judge` (judges one just-closed cycle, typically about 28 ms, 420 ms worst case at 3 cycles x 400 samples) stays in the executor: a worker would cost more to start than the work takes. It is the one allowlisted entry in the offload guard.

## Data Structures

### Wire format

One request, one response, per process.

- **stdin:** `{"job": "<name>", "payload": {...}, "log_level": <int>}`, UTF-8 JSON, one element per line (`store_json.dumps(depth=3, lossless=True)`, Issue #1730: no single long GIL hold; stdlib NaN tokens kept), written once and closed. `log_level` is the caller logger's effective level; the child logs at it (a missing or non-integer value means INFO).
- **stdout:** (same one-element-per-line encoding, read with `store_json.loads`) `{"ok": true, "result": <json>}` or `{"ok": false, "error": "<Type>: <message>"}` (error capped at 500 characters). Exit code 0 in both cases.
- **stderr:** one JSON object per line, `{"level": <int>, "name": <logger>, "msg": <text>}`; tracebacks are folded into `msg`.
- **Payloads are data only.** The callables the in-process fits took are rebuilt in the child from data:
  - `snapshots_fn` becomes `snapshots_raw` (Issue #1730), the forecast-archive snapshots in their compact stored form from `ForecastArchive.get_snapshots_raw`, decoded in the worker by the hass-free `forecast_archive.decode_snapshot` (`v4_shadow.job_clock_and_archive`; one undecodable snapshot degrades the archive to `[]`, as before). A payload with a decoded `snapshots` list (no raw reader) still works; no `snapshots` key and no `snapshots_raw` = no archive;
  - `in_sleep` becomes `sleep_window`, `[sleep, wake]` as "HH:MM", evaluated by the existing hass-free `predictor_v4.in_sleep_window` (no second copy of the sleep rule);
  - `tz` becomes `{"key": <IANA>, "offset_s": n}` or `{"offset_s": n}` (the worker falls back to the offset, with a WARNING, if it cannot load the key) (`v4_shadow.encode_tz` / `decode_tz`);
  - `now` becomes an ISO string.
- **Cycle refit payload:** `{cycles, mode, snap, now_ts}`. The result is the `fit_cycles` dict including `w_by_id` (cycle id to weight). The coordinator applies `store.set_weights` after the job.
- A result that cannot be encoded as JSON raises `ProcessJobError`.

**Persistence:** none. The worker writes no files. The one new persisted value is `fit_attempt_at` in the v4 model file, written by the coordinator ([v4 Shadow Spec](v4-shadow-spec.md#climate_advisor_v4_model_entry_idjson-v4modelstore-v4_archivepy)).

### Bootstrap

The child is started as `sys.executable -I -B -c <_BOOTSTRAP> <package dir> <its parent>` (no shell). `-I` keeps `/config`, the working directory and `PYTHON*` variables off the import path. `-B` is needed as well: `-I` also ignores `PYTHONDONTWRITEBYTECODE`, so without it the child would write `__pycache__` files into the integration folder.

**Environment.** The child gets a copy of Home Assistant's environment from `process_offload._child_env()`, without anything secret-shaped: `SUPERVISOR_TOKEN`, `HASSIO_TOKEN` and every name ending in `_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PASSWD` or `_PASS` (case-insensitive). Everything else (`PATH`, `LANG`, `TZ`, `PYTHONTZPATH`, ...) is kept, because the job needs the time-zone data.

`_BOOTSTRAP` (in `process_offload.py`) builds two namespace modules, `custom_components` and `custom_components.climate_advisor`, with their `__path__` set, so the package `__init__` (which needs Home Assistant) never runs. It then imports `_child_main` and exits with its return code.

`_child_main()`:

0. The wire codec is `store_json` (orjson and the stdlib only), imported lazily inside `process_offload._encode_value` / `_decode`; it is part of the import closure.
1. Installs the `homeassistant.util.dt` stub (`_install_ha_stub`): `now`, `utcnow` and `as_utc`, which `time_utils` imports. Nothing else of Home Assistant exists in the child.
2. Calls `os.nice(10)`; failure (no `nice` on the platform) is ignored.
3. Reads stdin, then points fd 1 and `sys.stdout` at stderr (the real stdout is kept for the envelope), so a stray `print` or a write straight to fd 1 cannot corrupt the reply.
4. Adds the stderr JSON log handler at INFO, then sets handler and root logger to the request's `log_level` (INFO when it is missing), so DEBUG lines are relayed exactly when the parent would show them.
5. Runs `run_job` and writes the envelope on the real stdout.

### Timeouts and cancel

The timeouts are in `const.py` (`WORKER_TIMEOUT_V4_FIT_S` 900, `WORKER_TIMEOUT_V4_DISPLAY_FIT_S` 600, `WORKER_TIMEOUT_V4_NIGHT_REPLAY_S` 1800, `WORKER_TIMEOUT_CYCLE_REFIT_S` 600). They are set well above the estimated Pi times (about 5x the x86 measurements: a fit about 120 s, a replay about 3 min; the reported Pi fit took 99 s) so only a hung worker reaches them. A hung worker would otherwise hold the domain-wide v4 fit lock forever.

### Worker cap (Issue #1730)

At most `worker_limit()` workers run at once per event loop, across every zone and job kind: cores - 1 (`os.process_cpu_count()` on Python 3.13+, else `os.cpu_count()`), at least 1; `worker_limit_override` wins (tests). The semaphore lives in a `WeakKeyDictionary` keyed by the running loop, so a restart or a test loop never inherits another's slots. A job over the cap waits in `async_run_job` before it takes a slot and before its payload is encoded. The caller has already built the payload dict, so a waiting job still holds that memory; only the encoded bytes wait. The timeout (`timeout_s`) is measured from the moment the slot is acquired (`_run_in_slot`), not from the call, so waiting never eats a job's time. A waiter that is cancelled (unload) leaves the queue and takes no slot; `async with` frees the slot on every exit. A DEBUG line is logged when a job has to wait. Tests: `tests/test_worker_cap_1730.py`.

On timeout, `_kill_and_reap` kills the child and waits for it, and `ProcessJobError("... timed out after N s")` is raised. On cancel (unload, HA stop) the child is killed and reaped and the cancel propagates; a second cancel during the reap is held until the child is gone, so no zombie is left. The timeout applies in process mode only.

### Log relay

After the child exits, `_relay_logs` re-emits each stderr JSON line through the logger named in the record (the record's own module logger, so per-module log levels still apply), at the record's own level, as `worker <job>: <msg>`. A message over 8,000 characters keeps its head and its tail (a traceback's failing frame). A line that is not JSON goes out at WARNING on the caller's logger. The child logs at the caller logger's effective level, sent in the request envelope (`log_level`), so DEBUG is relayed when the parent logs DEBUG. The child points fd 1 at stderr and writes the envelope to a saved copy of the real stdout, so even a write straight to fd 1 cannot corrupt it; an envelope that is not a JSON object raises `ProcessJobError`. A job error carries its last frame (`RuntimeError: msg (at file.py:12 in func)`). The coordinator runs the job inside `log_capture.zone_scope`, so the lines are zone-tagged. A non-zero exit raises `ProcessJobError` with the last 500 characters of stderr. DEBUG lines in the coordinator mark start and return (`Worker job started` / `Worker job returned`, with seconds).

### Fallback

If the process cannot be spawned (`OSError`), `async_run_job` logs one WARNING (`Worker process unavailable; running in a thread: job=...`) and runs `_thread_run` in the executor. This is the old behaviour: correct, but it can stall the loop on slow hardware. Any other spawn error is raised as it is (no fallback), and any failure after the child started is a `ProcessJobError` (no fallback).

### Thread mode (unit tier)

`process_offload.default_mode` is `"process"` in production. `tests/conftest.py` sets it to `"thread"` so unit tests run the same job in an executor thread. Thread mode sends the payload and the result through a JSON round trip (`_thread_run`), so tests see the same data types as the real worker. Production code never checks for test doubles. The real-HA tier (`tests_ha/conftest.py`) is in thread mode too: a simulated clock cannot see a subprocess pipe, and the worker timeout would run on the simulated clock. `tests_ha/test_process_offload_realha_1722.py` switches to process mode for its own tests and holds the clock still. Real workers are exercised explicitly in `tests/test_process_offload_1722.py`, using test-only jobs registered with `_register_job_for_tests` (production never sends that registry).

## Invariants

1. Only names in `JOBS` can run. An unknown name raises `ValueError` before any process starts, in both modes.
2. The child gets no Home Assistant objects, no callables and no file paths to write: JSON data in, JSON data out. It writes no files (not even bytecode) and makes no network or HA calls.
3. The only `homeassistant` module the child can import is the `homeassistant.util.dt` stub (guarded).
4. Every result is applied by the coordinator, after the job returns and after the `_shut_down` check.
5. A failed, killed or timed-out worker never replaces a model or rates: the caller's existing failure handling keeps the previous ones.
6. At most one worker per job kind runs at a time, across all zones: the v4 jobs hold the domain-wide v4 fit lock; the cycle refit holds the domain-wide cycle-refit worker lock (`coordinator._cycle_refit_worker_lock`, key `_CYCLE_FIT_LOCK_KEY` in `hass.data`), taken inside the zone's cycle lock and held only while the worker runs.
6a. No worker starts once Home Assistant is stopping (`_ha_stopping`): `_v4_start_fit`, `_v4_start_night_replay`, `_cycle_spawn_refit` and the refit step of `_cycle_process` check it.
7. A worker never outlives its task: cancel and timeout kill and reap it.
8. Payload building (row windowing, forecast-archive reads, JSON encoding) and result decoding run in the executor, never on the event loop.

## Adding a job

1. Write the function in a hass-free module of this package (it may import only the standard library, sibling hass-free modules and `homeassistant.util.dt`). Signature: `def my_job(payload: Mapping[str, Any]) -> <JSON-able>`. Take data, not callables.
2. Write a `my_job_payload(...)` builder next to it. It runs in the executor and returns plain lists, dicts, strings and numbers.
3. Add the name to `JOBS` in `process_offload.py`.
4. Add a `WORKER_TIMEOUT_*` constant to `const.py`, about five times the longest measured time on a Pi.
5. Call it from the coordinator with `await self._process_job("my_job", functools.partial(my_job_payload, ...), WORKER_TIMEOUT_...)`, inside `log_capture.zone_scope`, under the lock that serialises the work. Apply the result in the coordinator. Let `ProcessJobError` reach the site's existing failure handler.
6. Cancel it on unload: keep the task handle and cancel it from `_v4_cancel_fit` or an equivalent.
7. Classify any new executor callable in `tests/test_executor_job_registry_1728.py`. Add the function name to `_HEAVY_FIT_FUNCS` in `tests/test_executor_offload.py` if a direct in-process call would be a mistake, add the module to the import-closure test, and add a wiring test (see `tests/test_process_offload_wiring_1722.py`) and a job test (see `tests/test_offload_jobs_1722.py`).
8. Update this brief's job table and the [HA Boundary Exceptions §4](HA-BOUNDARY-EXCEPTIONS.md#4-worker-processes-for-long-pure-python-jobs-issue-1722) scope list.

## Guards

| Guard | What it fails on |
|---|---|
| `tests/test_executor_offload.py::TestHeavyFitsRunInWorkerProcess` | Any coordinator method that references `v4_shadow.run_fit` / `run_display_fit`, `night_replay.run_nightly` or `cycle_model.fit_cycles` (direct call, executor hand-over, `functools.partial` or from-import). `_cycle_judge` is the one allowlisted use; a stale allowlist entry also fails. |
| `tests/test_process_offload_1722.py::test_import_closure_of_job_modules_is_hass_free` | A job module (`v4_shadow`, `night_replay`, `cycle_model`, `predictor_v4`, `forecast_archive`, `store_json`) whose import closure reaches any `homeassistant` module other than `homeassistant.util.dt`. |
| `tests/test_process_offload_1722.py::test_child_homeassistant_stub_is_minimal` | A stub that grows beyond `now`, `utcnow`, `as_utc`. |
| `tests/test_process_offload_1722.py` (timeout, cancel, double cancel, spawn failure, isolation tests) | A worker that is not killed and reaped, a spawn failure that does not fall back once, or a child that sees the working directory or `PYTHONPATH`. |
| `tests/test_process_offload_1722.py` (bytecode, environment tests) | A child that writes `__pycache__` into the folder it imports from (no `-B`), or that sees a planted `*_TOKEN` / `SUPERVISOR_TOKEN`, or loses `TZ`. |
| `tests/test_worker_gates_1722.py` | A worker started after the HA stop began (each start path), or two zones' cycle refit workers running at once. |
| `tests_ha/test_process_offload_realha_1722.py` | On real Home Assistant: a refit that does not run in a child, or an unload, reload or HA stop that leaves the worker running or hangs on it. |
| `tests/test_v4_shadow_structural.py` | The runner naming a v4 job outside its `JOBS` allowlist. |
| `tests/test_process_offload_wiring_1722.py` | A call site that stops using `_process_job` with its payload built in the executor, a failed worker that does not keep the previous model, record or rates, or an unload that does not cancel a running replay or refit. |
| `tests/test_executor_job_registry_1728.py` | A callable handed to `_executor_job`, `async_add_executor_job` or `_process_job` that is not classified in the registry (`io`, `short_cpu` under 0.5 s on x86, `worker`, `forwarder`, `over_budget` (pinned set empty since Issue #1730), `worker_fallback`), a stale entry, or a `worker` entry that is not a `JOBS` name. Add a new job here too. |
| `tests_ha/test_loop_responsiveness_1728.py` | On real Home Assistant: a v4 fit, night replay or cycle refit that makes the event loop lag (p99 above max(15 ms, 4 x idle p99), 25 ms on one core, or over 15% ticks lost). Thread-mode controls prove the probe sees the #1722 starvation. |

## Disclosure Path

← Tier 1 parent: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) (module map, source files)
→ Tier 3 specs: [v4 Shadow Spec](v4-shadow-spec.md) (fits, display fit, replay, `fit_attempt_at`), [Thermal Model v3](thermal-model-v3-spec.md) (cycle refit), [Night Strategy Spec](night-strategy-spec.md) (replay)
↔ Siblings: [HA Boundary Exceptions §4](HA-BOUNDARY-EXCEPTIONS.md#4-worker-processes-for-long-pure-python-jobs-issue-1722), [State Persistence](state-persistence.md)
