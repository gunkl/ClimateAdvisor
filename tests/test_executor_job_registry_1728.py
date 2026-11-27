"""Issue #1728: every callable handed to an executor thread or a worker process is classified, with a cost.

Why this exists: the executor does not protect the event loop from pure-Python CPU work (claude.md Thread-Safety, third
hazard). A thread that runs Python holds the GIL, so a long job in ``_executor_job`` froze Home Assistant on a Raspberry
Pi (Issue #1722: 99 s on a Pi, 21-24 s on x86). ``tests/test_executor_offload.py`` pins the four known heavy fits to
``_process_job``. This module closes the other half: a NEW callable can no longer reach an executor thread without
somebody deciding, in review, what it costs.

What it checks (AST only, no timing, deterministic, well under a second):

* every callable found by :func:`scan_source` over ``custom_components/climate_advisor`` is a key of ``REGISTRY``;
* ``REGISTRY`` has no stale key (a callable that was removed or renamed) and no stale ``calls`` fingerprint;
* every ``short_cpu`` entry carries a measured x86 worst case that is under ``_EXECUTOR_CPU_BUDGET_S``, every ``worker``
  entry is a real ``process_offload.JOBS`` name, and nothing but a ``_process_job`` call is a ``worker``;
* the scanner is not blind: planted executor calls in a synthetic source are found, and a call appended to the real
  coordinator source is reported as unclassified.

What it does NOT check: the measured numbers themselves. They were taken by hand (see each ``reason``); the guard makes
adding a callable a conscious, reviewed act, and a human re-measures when a job's input grows. The callables that
``process_offload.async_run_job`` hands to its ``executor_job`` PARAMETER (the coordinator passes ``_executor_job``) are
scanned too: ``executor_job(...)`` is a scanned call form, so ``_encode``, ``_decode`` and ``_thread_run`` are entries.

Scanned call forms (the callable is the FIRST positional argument unless stated):

* ``_executor_job(fn, ...)``, ``executor_job(fn, ...)`` (the parameter ``process_offload`` receives it as),
  ``<anything>.async_add_executor_job(fn, ...)``, ``asyncio.to_thread(fn, ...)``;
* ``<loop>.run_in_executor(executor, fn, ...)`` (callable is the second argument);
* ``_process_job(job, payload_fn, timeout_s)``: the job NAME (a string) is the ``worker`` entry; the payload builder
  runs in the executor and is scanned as a callable too (second positional argument or ``payload_fn=``).

Id scheme (one, stable under line moves and call-site moves): ``"<file>:<callable>"``.

* ``<file>`` is the module path relative to the package, in POSIX form (``coordinator.py``; a subpackage module would be
  ``sub/mod.py``, so two modules with one base name cannot collide).
* ``<callable>`` is the unparsed callable expression after unwrapping ``functools.partial(f, ...)`` and
  ``log_capture.bind_zone_for_executor(f)`` to ``f``: ``"self._chart_log.save"``, ``"v4_shadow.run_cycle"``.
  Many call sites of one callable are ONE entry: the classification belongs to the callable, not the site.
* a local ``def`` or a ``lambda`` is ``"<qualified enclosing function>.<local name>"`` or ``"....<lambda>"``:
  ``"ClimateAdvisorCoordinator._cycle_judge._job"``. Its entry also carries ``calls``: the dotted names it calls
  (builtins left out), so a job that starts calling something heavier makes the entry stale and forces a re-read.
* a bare parameter of the enclosing function (a forwarding wrapper such as ``_executor_job(self, fn)``) is
  ``"<qualified enclosing function>(param <name>)"`` and is class ``forwarder``: its callers are scanned instead.
* a ``_process_job`` job is ``"<file>:process_job:<job name>"``, class ``worker``.

Classes (``REGISTRY[id]["cls"]``):

* ``io``: file, network or client construction. A latency spike, not CPU. If its serialisation is real Python work, it
  records ``cpu_ms`` (and ``at``): that is also held under the budget.
* ``short_cpu``: pure-Python CPU, with ``ms`` (measured worst case, x86, thread CPU time, GC off) and ``at`` (the input
  size it was measured on). Must be under the budget.
* ``worker``: a ``process_offload.JOBS`` name run by ``_process_job``.
* ``forwarder``: a wrapper that forwards its parameter. Nothing to cost.
* ``over_budget``: KNOWN DEBT, measured at or over the budget on a realistic input. A ratchet, not a classification:
  it exists so the guard can be merged while the owner decides (move to a worker job, or make the work cheaper).
  Once the cost falls under the budget the entry must be reclassified (the test fails on a stale ``over_budget``). The
  SET of ``over_budget`` ids is pinned (``_ALLOWED_OVER_BUDGET``): a new heavy job must become a worker job, it cannot
  be parked here. The set is empty since Issue #1730 (``V4Archive.load`` left it: a columnar load of 3 sensors x 7
  leads x 400 days is about 175 ms).
* ``worker_fallback``: ``process_offload._thread_run``, the whole worker job run in an executor thread. It can exceed
  the budget BY DESIGN, and only when a worker process cannot start (``OSError`` from the spawn, a degraded host); the
  production path is the process. The set of ids with this class is pinned (``_ALLOWED_FALLBACK``).
* ``short_cpu`` (by ``ms``) and ``io`` (by ``cpu_ms``) carry ``near_budget=True`` when the cost is above
  ``_NEAR_BUDGET_FRACTION`` of the budget: tracked for moving into a worker (the test lists the set and forces the
  marker both ways).

Costs are thread CPU with GC off. A GC pass can land inside any job; it is a process-wide cost, not charged to the job
it interrupts, and the perf probe (``tools/sim_harness/perf_probe.py``) reports it separately. Where a GC-on run of a
job was seen far above its GC-off cost, the entry's text says so.

To add a callable: classify it. If it can run longer than 0.5 s on x86 it must be a worker job (claude.md Thread-Safety,
third hazard; docs/process-offload-brief.md), not a ``short_cpu`` entry.
"""

from __future__ import annotations

import ast
import builtins
import functools
from dataclasses import dataclass
from typing import Any

from custom_components.climate_advisor import process_offload
from tests.test_executor_offload import API_PY, COORDINATOR_PY

PACKAGE_DIR = COORDINATOR_PY.parent
assert API_PY.parent == PACKAGE_DIR

# The one place the budget is named (plans/1728-freeze-guards.md): CPU work in an executor thread may take at most this
# long on x86 (a Pi is about 5x slower). Longer work is a worker job (``_process_job``).
_EXECUTOR_CPU_BUDGET_S = 0.5

_EXECUTOR_CALLS = {
    "_executor_job": 0,
    "executor_job": 0,
    "async_add_executor_job": 0,
    "to_thread": 0,
    "run_in_executor": 1,
}
_PROCESS_JOB = "_process_job"
_WRAPPERS = {"partial", "bind_zone_for_executor"}
_BUILTINS = frozenset(dir(builtins))


# --------------------------------------------------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Found:
    """One distinct callable handed to an executor or worker, with every line it was handed at."""

    ident: str
    kind: str  # "executor" | "process_job" | "forwarder"
    lines: list[int]
    calls: tuple[str, ...] | None = None  # what a local def / lambda calls; None for a named callable


def _name_of(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _calls_in(node: ast.AST) -> tuple[str, ...]:
    """Dotted names called anywhere inside ``node`` (a local def or lambda), builtins left out, sorted."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name | ast.Attribute):
            text = ast.unparse(sub.func)
            if text not in _BUILTINS:
                names.add(text)
    return tuple(sorted(names))


class _Scanner(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.found: dict[str, Found] = {}
        self._scope: list[ast.AST] = []  # enclosing ClassDef / FunctionDef nodes, outermost first

    # -- scope tracking ---------------------------------------------------------------------------------------------
    def _visit_scope(self, node: ast.AST) -> None:
        self._scope.append(node)
        self.generic_visit(node)
        self._scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope  # noqa: N815

    def _qualname(self, *, upto: int | None = None) -> str:
        nodes = self._scope if upto is None else self._scope[:upto]
        return ".".join(n.name for n in nodes if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef))

    def _functions(self) -> list[tuple[int, ast.FunctionDef | ast.AsyncFunctionDef]]:
        return [(i, n) for i, n in enumerate(self._scope) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]

    # -- resolving the callable ------------------------------------------------------------------------------------
    def _unwrap(self, node: ast.expr) -> ast.expr:
        while isinstance(node, ast.Call) and node.args and _name_of(node.func) in _WRAPPERS:
            node = node.args[0]
        return node

    def _local_def(self, name: str) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str] | None:
        """The ``def name`` nested in the nearest enclosing function that defines it, with that function's qualname."""
        for index, fn in reversed(self._functions()):
            for sub in ast.walk(fn):
                if sub is not fn and isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name == name:
                    return sub, self._qualname(upto=index + 1)
        return None

    def _record(self, ident: str, kind: str, line: int, calls: tuple[str, ...] | None = None) -> None:
        entry = self.found.setdefault(f"{self.filename}:{ident}", Found(f"{self.filename}:{ident}", kind, []))
        entry.lines.append(line)
        if calls is not None:
            entry.calls = tuple(sorted(set(entry.calls or ()) | set(calls)))

    def _classify_callable(self, arg: ast.expr, line: int) -> None:
        target = self._unwrap(arg)
        enclosing = self._qualname()
        if isinstance(target, ast.Lambda):
            self._record(f"{enclosing}.<lambda>", "executor", line, _calls_in(target))
            return
        if isinstance(target, ast.Name):
            local = self._local_def(target.id)
            if local is not None:
                node, owner = local
                self._record(f"{owner}.{node.name}", "executor", line, _calls_in(node))
                return
            for _, fn in reversed(self._functions()):
                params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
                if target.id in params:
                    self._record(f"{enclosing}(param {target.id})", "forwarder", line)
                    return
        self._record(ast.unparse(target), "executor", line)

    # -- the call sites ---------------------------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _name_of(node.func)
        if name in _EXECUTOR_CALLS and len(node.args) > _EXECUTOR_CALLS[name]:
            self._classify_callable(node.args[_EXECUTOR_CALLS[name]], node.lineno)
        elif name == _PROCESS_JOB and node.args:
            job = node.args[0]
            job_name = job.value if isinstance(job, ast.Constant) else ast.unparse(job)
            self._record(f"process_job:{job_name}", "process_job", node.lineno)
            payload = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "payload_fn"), None)
            )
            if payload is not None:
                self._classify_callable(payload, node.lineno)
        self.generic_visit(node)


def scan_source(source: str, filename: str) -> dict[str, Found]:
    """Every distinct callable handed to an executor / worker in ``source``, keyed by its registry id."""
    scanner = _Scanner(filename)
    scanner.visit(ast.parse(source))
    return scanner.found


@functools.cache
def scan_package() -> dict[str, Found]:
    found: dict[str, Found] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = path.relative_to(PACKAGE_DIR).as_posix()
        for ident, item in scan_source(path.read_text(encoding="utf-8"), rel).items():
            assert ident not in found, f"{ident} found in two modules"  # a collision must be loud, never an overwrite
            found[ident] = item
    return found


# --------------------------------------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------------------------------------


def io(reason: str, *, cpu_ms: float | None = None, at: str | None = None, near_budget: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"cls": "io", "reason": reason}
    if cpu_ms is not None:
        entry |= {"cpu_ms": cpu_ms, "at": at}
    if near_budget:
        entry["near_budget"] = True
    return entry


def short_cpu(
    ms: float, at: str, reason: str, calls: tuple[str, ...] | None = None, *, near_budget: bool = False
) -> dict[str, Any]:
    entry: dict[str, Any] = {"cls": "short_cpu", "ms": ms, "at": at, "reason": reason}
    if calls is not None:
        entry["calls"] = calls
    if near_budget:
        entry["near_budget"] = True
    return entry


def worker_fallback(reason: str) -> dict[str, Any]:
    return {"cls": "worker_fallback", "reason": reason}


def worker(reason: str) -> dict[str, Any]:
    return {"cls": "worker", "reason": reason}


def forwarder(reason: str) -> dict[str, Any]:
    return {"cls": "forwarder", "reason": reason}


def over_budget(ms: float, at: str, reason: str) -> dict[str, Any]:
    return {"cls": "over_budget", "ms": ms, "at": at, "reason": reason}


_LEARN = (
    "LearningEngine.save_state: store_json.dumps(asdict(state), lossless) + atomic write; "
    "85 ms at 90 records + 200 obs x 200 samples (3.6 MB)"
)
_MAX_LOG = "17,520-row (365-day) chart log"

REGISTRY: dict[str, dict[str, Any]] = {
    # ---- __init__.py --------------------------------------------------------------------------------------------
    "__init__.py:i18n.ensure_catalogs": io("reads the JSON translation catalogs once at setup"),
    "__init__.py:remove_entry_scoped_files": io("unlinks a removed zone's storage files"),
    "__init__.py:_panel_asset_bytes": io("reads the panel pages and catalogs to hash them (5.3 MB, 7.6 ms)"),
    # ---- api.py -------------------------------------------------------------------------------------------------
    "api.py:coordinator.get_chart_data": short_cpu(
        450,
        f"range=1y, {_MAX_LOG}, 60 h forecast, v4 archive 3 sensors x 7 leads x 400 days (quiet runs 404-450; one "
        "loaded run 541; 30d: 120, 7d: 56, 24h: 38). 5 sensors at 1y: 550-650 ms, OVER the budget",
        "dashboard chart: filter/bucket the chart log, v3 ODE curve, and the v4 block (build_chart_v4), which since "
        "Issue #1730 reads only the requested window of the archive and the first/last slot for past_span (before: "
        "1.5 s at 3 sensors for ANY range, 2.3 s at 5). NEAR BUDGET (90%): the 1y range is the whole archive, a Pi "
        "holds the GIL ~2.3 s. Tracked for moving to a worker",
        near_budget=True,
    ),
    "api.py:coordinator.learning.save_state": io(_LEARN, cpu_ms=85, at="max learning state"),
    "api.py:coordinator._save_investigation_reports": io("atomic JSON write of the report history"),
    "api.py:ClimateAdvisorPredictionAccuracyView.get._build": short_cpu(
        285,
        f"range=1y, {_MAX_LOG}, v4 archive 3 sensors x 7 leads x 400 days + v3 archive, _ca (a node sensor: 150 ms; "
        "30d: 75 ms, 24h: 48 ms; before Issue #1730: 270-460 ms for every range)",
        "prediction-accuracy payload; also reads the backtest file (io). Windows of the chart log are found by "
        "bisection (ChartStateLog sorted view) and each row's time is parsed once",
        calls=("dt_util.now", "prediction_accuracy.build_prediction_accuracy", "prediction_accuracy.load_backtest"),
    ),
    # ---- claude_api.py / config_flow.py: the SDK client builds an SSL context and reads the CA bundle ---------------
    "claude_api.py:AsyncAnthropic": io("Anthropic SDK client construction (SSL context, CA bundle read)"),
    "config_flow.py:ClaudeAPIClient": io("client construction for the connection test (SSL context)"),
    # ---- coordinator.py: persistence ---------------------------------------------------------------------------
    "coordinator.py:self._chart_log.load": io("chart log JSON read", cpu_ms=45, at=_MAX_LOG),
    "coordinator.py:self._chart_log.save": io("chart log atomic JSON write", cpu_ms=20, at=_MAX_LOG),
    "coordinator.py:self.learning.load_state": io("learning state JSON read"),
    "coordinator.py:self.learning.save_state": io(_LEARN, cpu_ms=85, at="max learning state"),
    "coordinator.py:self._state_persistence.load": io("small state JSON read"),
    "coordinator.py:self._state_persistence.save": io("small state JSON atomic write"),
    "coordinator.py:self._forecast_archive.load": io(
        "forecast archive JSON read", cpu_ms=55, at="8,760 hourly snapshots"
    ),
    "coordinator.py:self._forecast_archive.save": io(
        "forecast archive atomic JSON write", cpu_ms=30, at="8,760 hourly snapshots (365 days)"
    ),
    "coordinator.py:self._v4_store.load": io("v4 model store JSON read (one model dict)"),
    "coordinator.py:self._v4_store.save": io("v4 model store atomic JSON write (one model dict, 0.5 ms)"),
    "coordinator.py:self._v3_archive.load": io("v3 archive JSON read", cpu_ms=75, at="1 sensor x 4 leads x 400 days"),
    "coordinator.py:self._v3_archive.save": io(
        "v3 archive atomic JSON write", cpu_ms=105, at="1 sensor x 4 leads x 400 days"
    ),
    "coordinator.py:self._v4_archive.save": io(
        "v4 archive atomic JSON write (rebuilds the dict of every slot first, then one orjson call per slot; hourly at "
        "most). NEAR BUDGET (70%): a Pi holds the GIL ~1.8 s. Tracked for moving to a worker. 5 sensors: 480-520 ms "
        "(623 before Issue #1730), at the budget",
        cpu_ms=350,
        at="3 sensors x 7 leads x 400 days (2 sensors: 200-265 ms)",
        near_budget=True,
    ),
    "coordinator.py:self._cycle_store.load": io(
        "cycle store JSON read (a fast decode of the stored cycles; 1.4-1.5 s before Issue #1730). NEAR BUDGET (94%): "
        "a Pi holds the GIL ~2.3 s. Tracked for moving to a worker",
        cpu_ms=470,
        at="3,000 cycles x 400 samples = the store cap (medians 430-465, one loaded run 504; 72 samples: 100-110 ms)",
        near_budget=True,
    ),
    "coordinator.py:self._cycle_store.save": io(
        "cycle store atomic JSON write (compacts every cycle). NEAR BUDGET (80%, worst case): a Pi holds the GIL "
        "~2 s. Tracked for moving to a worker",
        cpu_ms=400,
        at="3,000 cycles x 400 samples = the store cap with every cycle at the sample cap (72 samples: 65-70 ms)",
        near_budget=True,
    ),
    "coordinator.py:store.save": io(
        "cycle store atomic JSON write at shutdown (see self._cycle_store.save). Tracked for moving to a worker",
        cpu_ms=400,
        at="store cap, 400 samples",
        near_budget=True,
    ),
    "coordinator.py:store.set_weights": io(
        "waits for the cycle store lock a running save holds during its write; 0.7 ms of work for 1,500 weights"
    ),
    "coordinator.py:self._load_investigation_reports": io("investigation report history JSON read"),
    "coordinator.py:self._save_investigation_reports": io("investigation report history atomic JSON write"),
    "coordinator.py:_ClaudeAPIClient": io("shared Anthropic client construction (SSL context, CA bundle read)"),
    # ---- coordinator.py: the findings -------------------------------------------------------------------------------
    "coordinator.py:self._v4_archive.load": io(
        "v4 archive JSON read at startup: one orjson call per slot, then a columnar build (no put_if_absent per "
        "value). Left the over_budget ratchet in Issue #1730 (626 ms before)",
        cpu_ms=175,
        at="3 sensors x 7 leads x 400 days (the archive's own retention; 2 sensors 100-120 ms, 5 sensors 250-315 ms)",
    ),
    # ---- coordinator.py: short CPU ---------------------------------------------------------------------------------
    "coordinator.py:_build_predicted_indoor_future": short_cpu(
        3.9,
        "168 h hourly forecast, physics ODE with a band schedule",
        "v3 ODE curve, linear in forecast hours (3 call sites)",
    ),
    "coordinator.py:build_timeline": short_cpu(
        80,
        "hot 8-day forecast, v4 house model, 193 hourly points (quiet runs 45-75 ms, 'hot' is the worst day type; warm "
        "7-day v3: 19-31 ms) plus the v3 path walk it calls (3.9 ms, 168 h)",
        "Issue #1727: the full-horizon outlook plan, built from a snapshot captured on the loop. Pure Python, no "
        "reads of coordinator state (path_fn and band_at close over copies). Runs at most once per zone at a time, "
        "every few plan builds. The loop-side build (to now + 60 h) stays on the loop at 8-9 ms",
    ),
    "coordinator.py:v4_shadow.run_cycle": short_cpu(
        2.6,
        "672 grid steps (168 h forecast), 1 sensor, display and baseline paths; whole shadow cycle 11.5 ms",
        "v4 predict; linear in sensors x steps (5 sensors would be about 15 ms)",
    ),
    "coordinator.py:v4_shadow.chart_extras": short_cpu(
        4.3, "672 grid steps, 1 sensor", "the two display-only extra lines of the Status chart"
    ),
    "coordinator.py:ClimateAdvisorCoordinator._cycle_judge._job": short_cpu(
        420,
        "3 just-closed cycles x 400 samples each (the sample cap): 7 runs on a quiet box 331-418 ms, median 357 (an "
        "earlier 227 ms was a lighter input; verifier 313-379 ms at 3 x 404). One realistic cycle of 65-73 samples is "
        "30 ms, 5 of them 160 ms",
        "one fit_cycles per just-closed cycle; the refit of the whole pool is the cycle_refit worker, not this. "
        "Linear in cycles x samples: NEAR BUDGET (84%, worst case): a Pi holds the GIL ~2 s. Tracked for moving to a "
        "worker",
        calls=("cycle_model.fit_cycles", "cycle_model.prior_from_cache"),
        near_budget=True,
    ),
    "coordinator.py:ClimateAdvisorCoordinator._cycle_refit._payload": short_cpu(
        195,
        "1,500 cycles x 400 samples (the per-mode store cap; GC off). With GC on a first call reached 380-730 ms: "
        "that is a GC pass over the allocation, a process-wide cost the budget does not charge here (module docstring)",
        "builds the public dict of every stored cycle of one mode for the cycle_refit worker; 36 ms at 72 samples",
        calls=("cycle_model.refit_job_payload", "store.cycles"),
    ),
    "coordinator.py:self.learning._commit_event_from_dict": short_cpu(
        0.9, "200 samples (THERMAL_MAX_OBS_SAMPLES), 2-parameter OLS", "one thermal observation's OLS"
    ),
    "coordinator.py:v4_shadow.fit_job_payload": short_cpu(
        3,
        f"train_days=30 (production), {_MAX_LOG}, 8,760 hourly forecast snapshots (was 114 ms)",
        "finds the chart rows by bisection (ChartStateLog.rows_between) and ships the forecast snapshots in their "
        "compact stored form; the v4_fit worker decodes them (forecast_archive.decode_snapshot; 0.1 s for the fit's "
        "744). NOTE train_days=None reads every row: production never passes it",
    ),
    "coordinator.py:v4_shadow.display_fit_job_payload": short_cpu(
        3, f"train_days=30, {_MAX_LOG} (was 30 ms)", "finds the chart rows by bisection for the v4_display_fit worker"
    ),
    "coordinator.py:night_replay.nightly_job_payload": short_cpu(
        9,
        f"105-day horizon (30 + 60 x 1.1 + 9), {_MAX_LOG}, 8,760 hourly snapshots (was 364 ms)",
        "finds the rows by bisection and ships the 2,544 snapshots of the horizon compact; the v4_night_replay worker "
        "decodes them (about 0.3 s there, off the GIL of Home Assistant). The payload is 3.2 MB, was 8.7 MB",
    ),
    "coordinator.py:process_job:v4_fit": worker("the v4 fit (21-24 s on x86)"),
    "coordinator.py:process_job:v4_display_fit": worker("the v4 display-only fit"),
    "coordinator.py:process_job:v4_night_replay": worker("the recorded-night replay (34 s on x86)"),
    "coordinator.py:process_job:cycle_refit": worker("the pooled cycle-model refit (1.2-1.5 s on x86)"),
    # ---- process_offload.py: the callables async_run_job hands to its ``executor_job`` parameter ---------------------
    "process_offload.py:_encode": short_cpu(
        150,
        "store_json.dumps of a 7 MB envelope: all 17,520 chart rows (more than any real job windows) + 8,760 forecast "
        "snapshots of 12 values (one orjson call per element: 60-80 ms as one json.dumps before Issue #1730; the real "
        "105-day night replay payload is 3.2 MB, 66 ms)",
        "serialises the worker payload for the child's stdin; each element is its own short GIL hold",
    ),
    "process_offload.py:_decode": short_cpu(
        85,
        "store_json.loads of a 7 MB result (the real results are small model dicts, a few KB)",
        "decodes the worker's stdout envelope",
    ),
    "process_offload.py:_thread_run": worker_fallback(
        "the whole worker job in an executor thread, with a JSON round-trip. Runs only when the worker process cannot "
        "be started (OSError, a degraded host) and when a test selects thread mode; it can exceed the budget by "
        "design (the v4 fit is 21-24 s on x86). A warning is logged each time"
    ),
    "coordinator.py:ClimateAdvisorCoordinator._executor_job(param fn)": forwarder("the wrapper itself: zone-tags fn"),
    "coordinator.py:ClimateAdvisorCoordinator._process_job(param payload_fn)": forwarder(
        "the wrapper itself: runs the caller's payload builder, classified at the _process_job call site"
    ),
    # ---- diagnostic_snapshot.py / fix_history.py / services.py -----------------------------------------------------
    "diagnostic_snapshot.py:_compute_control_quality": short_cpu(
        9,
        f"get_entries('3d') + ('7d') of a {_MAX_LOG}, 200 mode changes; build_control_quality alone is 25 ms at "
        "5,000 rows and 129 ms at 20,000 rows",
        "control-quality report; bounded by 3 raw days + 7 bucketed days, never the whole log",
    ),
    "fix_history.py:search_records": short_cpu(
        9, "the real fix_history.jsonl (727 records, 790 KB), with the file read", "term-count ranking + heapq"
    ),
    "fix_history.py:_recent_release_notes": short_cpu(
        6.4, "the real fix_history.jsonl (727 records), 5 versions, with the file read", "group and nlargest"
    ),
    "services.py:coordinator.learning.save_state": io(_LEARN, cpu_ms=85, at="max learning state"),
    "services.py:zone_coordinator.learning.save_state": io(_LEARN, cpu_ms=85, at="max learning state"),
    "services.py:zone_coordinator.learning.reset": io("clears the learning state and writes it"),
}

_CLASSES = {"io", "short_cpu", "worker", "forwarder", "over_budget", "worker_fallback"}
_BUDGET_MS = _EXECUTOR_CPU_BUDGET_S * 1000
_NEAR_BUDGET_FRACTION = 0.6  # a short_cpu (ms) or io (cpu_ms) entry above this share carries near_budget=True

# Pinned sets: adding to either is a reviewed decision in THIS file, never a side effect of adding a registry entry.
_ALLOWED_OVER_BUDGET: frozenset[str] = frozenset()  # empty since Issue #1730 (V4Archive.load); do not add to it
_ALLOWED_FALLBACK = frozenset({"process_offload.py:_thread_run"})
_EXPECTED_NEAR_BUDGET = frozenset(
    {
        "api.py:coordinator.get_chart_data",
        "coordinator.py:ClimateAdvisorCoordinator._cycle_judge._job",
        "coordinator.py:self._cycle_store.load",
        "coordinator.py:self._cycle_store.save",
        "coordinator.py:self._v4_archive.save",
        "coordinator.py:store.save",
    }
)


# --------------------------------------------------------------------------------------------------------------------
# Checks (functions, so the self-tests can run them on synthetic sources)
# --------------------------------------------------------------------------------------------------------------------


def unclassified(found: dict[str, Found], registry: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(i for i in found if i not in registry)


def stale(found: dict[str, Found], registry: dict[str, dict[str, Any]]) -> list[str]:
    problems = [f"{i}: no longer handed to an executor or worker" for i in sorted(registry) if i not in found]
    for ident, entry in sorted(registry.items()):
        if ident in found and found[ident].calls != entry.get("calls"):
            problems.append(
                f"{ident}: it now calls {found[ident].calls}, the entry says {entry.get('calls')}; re-read the job "
                "and re-measure"
            )
    return problems


def budget_problems(found: dict[str, Found], registry: dict[str, dict[str, Any]]) -> list[str]:
    problems = []
    for ident, entry in sorted(registry.items()):
        cls = entry.get("cls")
        if cls not in _CLASSES:
            problems.append(f"{ident}: class {cls!r} is not one of {sorted(_CLASSES)}")
            continue
        if not entry.get("reason"):
            problems.append(f"{ident}: a reason is required")
        if cls == "short_cpu":
            ms = entry.get("ms")
            if not isinstance(ms, int | float) or not entry.get("at"):
                problems.append(f"{ident}: short_cpu needs a measured ms and the input size it was measured on (at)")
            elif ms >= _BUDGET_MS:
                problems.append(
                    f"{ident}: measured {ms} ms is not under the {_BUDGET_MS:.0f} ms budget; a job that long must be a "
                    "worker job (claude.md Thread-Safety, third hazard)"
                )
        if cls == "io" and "cpu_ms" in entry and not (entry["cpu_ms"] < _BUDGET_MS and entry.get("at")):
            problems.append(f"{ident}: io cpu_ms must be under {_BUDGET_MS:.0f} ms and say what it was measured on")
        if cls == "over_budget" and not (isinstance(entry.get("ms"), int | float) and entry["ms"] >= _BUDGET_MS):
            problems.append(f"{ident}: over_budget but measured under the budget: reclassify it")
        cost = entry.get("ms") if cls == "short_cpu" else entry.get("cpu_ms") if cls == "io" else None
        if isinstance(cost, int | float):
            near = cost > _NEAR_BUDGET_FRACTION * _BUDGET_MS
            if near != bool(entry.get("near_budget")):
                problems.append(
                    f"{ident}: {cost} ms is {'above' if near else 'under'} {_NEAR_BUDGET_FRACTION:.0%} of the "
                    f"budget, so near_budget must be {near} (tracked for moving to a worker)"
                )
        elif entry.get("near_budget"):
            problems.append(f"{ident}: near_budget belongs on a costed short_cpu or io entry only")
        if cls == "worker":
            job = ident.partition(":process_job:")[2]
            if job not in process_offload.JOBS:
                problems.append(f"{ident}: {job!r} is not a process_offload.JOBS name {sorted(process_offload.JOBS)}")
        if ident in found:
            is_job = found[ident].kind == "process_job"
            if is_job != (cls == "worker"):
                problems.append(
                    f"{ident}: only a _process_job job is a worker, and every one is (kind={found[ident].kind})"
                )
            if (found[ident].kind == "forwarder") != (cls == "forwarder"):
                problems.append(f"{ident}: forwarder class and a bare-parameter callable go together")
    return problems


def _advice(missing: list[str], found: dict[str, Found]) -> str:
    lines = "\n".join(f"  {i}  (line {found[i].lines[0]})" for i in missing)
    return (
        f"\n{len(missing)} callable(s) are handed to an executor thread or worker process with no entry in REGISTRY of "
        f"tests/test_executor_job_registry_1728.py:\n{lines}\n"
        "Classify each one: 'io' (file, network, client construction), 'short_cpu' (pure-Python CPU: MEASURE its worst "
        f"case on x86 with time.thread_time on a realistic, at-the-cap input and record ms and the input; it must be "
        f"under {_BUDGET_MS:.0f} ms) or a worker job. If it can run longer than {_BUDGET_MS:.0f} ms on x86 it must NOT "
        "be an executor job: an executor thread holds the GIL and freezes Home Assistant on a Raspberry Pi (Issue "
        "#1722). Use coordinator._process_job (claude.md Thread-Safety, third hazard; docs/process-offload-brief.md)."
    )


# --------------------------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------------------------


def test_every_executor_callable_is_classified() -> None:
    found = scan_package()
    assert found, "the scanner found nothing: it is blind"
    missing = unclassified(found, REGISTRY)
    assert not missing, _advice(missing, found)


def test_no_stale_registry_entries() -> None:
    problems = stale(scan_package(), REGISTRY)
    assert not problems, "REGISTRY entries that no longer match the source:\n  " + "\n  ".join(problems)


def test_costs_are_under_the_budget_and_workers_are_real() -> None:
    problems = budget_problems(scan_package(), REGISTRY)
    assert not problems, "\n".join(problems)


def test_the_budget_is_named_once_and_matches_the_plan() -> None:
    assert _EXECUTOR_CPU_BUDGET_S == 0.5


def test_every_worker_job_name_is_registered_as_a_worker() -> None:
    """The reverse of the worker check: a JOBS name nobody calls through the guard is not silently an executor job."""
    called = {i.partition(":process_job:")[2] for i, f in scan_package().items() if f.kind == "process_job"}
    assert called <= set(process_offload.JOBS), called - set(process_offload.JOBS)


_PLANTED = """
import asyncio, functools
class Thing:
    async def run(self, hass, loop):
        await self._executor_job(plain_fn)
        await self.hass.async_add_executor_job(functools.partial(wrapped_fn, 1, key=2))
        await hass.async_add_executor_job(log_capture.bind_zone_for_executor(zoned.fn), 5)
        await self._executor_job(lambda: heavy(1))
        await asyncio.to_thread(threaded_fn)
        await loop.run_in_executor(None, pooled_fn)
        def _local():
            return crunch(3) + len([])
        await self._executor_job(_local)
        await self._process_job("v4_fit", functools.partial(payload_fn, 1), 30)
    async def fwd(self, fn):
        return await self._executor_job(fn)
"""


def test_the_scanner_finds_planted_calls() -> None:
    """Proof the scanner is not blind: every call form it claims to read, planted in a synthetic source."""
    found = scan_source(_PLANTED, "planted.py")
    assert set(found) == {
        "planted.py:plain_fn",
        "planted.py:wrapped_fn",
        "planted.py:zoned.fn",
        "planted.py:Thing.run.<lambda>",
        "planted.py:threaded_fn",
        "planted.py:pooled_fn",
        "planted.py:Thing.run._local",
        "planted.py:process_job:v4_fit",
        "planted.py:payload_fn",
        "planted.py:Thing.fwd(param fn)",
    }
    assert found["planted.py:Thing.run._local"].calls == ("crunch",)  # builtins (len) are not a call that matters
    assert found["planted.py:Thing.run.<lambda>"].calls == ("heavy",)
    assert found["planted.py:process_job:v4_fit"].kind == "process_job"
    assert found["planted.py:Thing.fwd(param fn)"].kind == "forwarder"
    assert unclassified(found, REGISTRY) == sorted(found), "planted ids must not be in the real registry"


def test_a_call_appended_to_the_real_source_is_reported() -> None:
    """The failure a reviewer would see: a new executor call in a real module with no registry entry."""
    source = (PACKAGE_DIR / "services.py").read_text(encoding="utf-8")
    assert unclassified(scan_source(source, "services.py"), REGISTRY) == []
    planted = source + "\n\nasync def _sneaky(hass):\n    return await hass.async_add_executor_job(some_new_fn)\n"
    found = scan_source(planted, "services.py")
    missing = unclassified(found, REGISTRY)
    assert missing == ["services.py:some_new_fn"]
    assert "worker job" in _advice(missing, found)


def test_a_removed_or_added_registry_entry_is_reported() -> None:
    found = scan_package()
    victim = "coordinator.py:self._chart_log.save"
    assert unclassified(found, {k: v for k, v in REGISTRY.items() if k != victim}) == [victim]
    assert stale(found, {**REGISTRY, "coordinator.py:gone_fn": io("x")}) == [
        "coordinator.py:gone_fn: no longer handed to an executor or worker"
    ]
    over = {**REGISTRY, "coordinator.py:self._chart_log.save": short_cpu(900, "x", "too slow")}
    assert any("worker job" in p for p in budget_problems(found, over))
    assert budget_problems(found, {**REGISTRY, "coordinator.py:process_job:nope": worker("x")}) != []


def test_over_budget_is_a_pinned_ratchet() -> None:
    """The over_budget set may only shrink. A new heavy job becomes a worker job; it cannot be parked here."""
    ids = {i for i, e in REGISTRY.items() if e["cls"] == "over_budget"}
    assert ids == _ALLOWED_OVER_BUDGET, (
        f"over_budget ids are pinned to {sorted(_ALLOWED_OVER_BUDGET)}, found {sorted(ids)}. A NEW heavy job must "
        "become a worker job (coordinator._process_job, process_offload.JOBS), not an over_budget entry; an id that "
        "left the set was fixed: reclassify it and shrink _ALLOWED_OVER_BUDGET"
    )


def test_the_over_budget_set_is_empty_and_a_new_entry_is_refused() -> None:
    assert frozenset() == _ALLOWED_OVER_BUDGET
    parked = {**REGISTRY, "coordinator.py:self._v4_archive.save": over_budget(900, "x", "parked")}
    ids = {i for i, e in parked.items() if e["cls"] == "over_budget"}
    assert ids != _ALLOWED_OVER_BUDGET  # test_over_budget_is_a_pinned_ratchet would fail on it


def test_an_over_budget_entry_that_is_under_budget_fails() -> None:
    found = scan_package()
    victim = "coordinator.py:self._v4_archive.load"
    assert budget_problems(found, REGISTRY) == []
    fixed = {**REGISTRY, victim: over_budget(120, "x", "cheaper now")}
    assert any("reclassify" in p for p in budget_problems(found, fixed))


def test_the_by_reference_callables_are_scanned_and_the_fallback_is_pinned() -> None:
    """``process_offload`` hands _encode/_decode/_thread_run to ``executor_job``: still scanned, still ours."""
    found = scan_package()
    by_ref = {"process_offload.py:_encode", "process_offload.py:_decode", "process_offload.py:_thread_run"}
    assert by_ref <= set(found), by_ref - set(found)
    assert {i for i in found if i.startswith("process_offload.py:")} == by_ref
    assert {i for i, e in REGISTRY.items() if e["cls"] == "worker_fallback"} == _ALLOWED_FALLBACK
    assert REGISTRY["process_offload.py:_thread_run"]["cls"] == "worker_fallback"


def test_ids_are_package_relative_paths_so_modules_cannot_collide() -> None:
    planted = "async def f(hass):\n    await hass.async_add_executor_job(helper)\n"
    assert set(scan_source(planted, "sub/a.py")) == {"sub/a.py:helper"}
    assert set(scan_source(planted, "a.py")) == {"a.py:helper"}
    assert all(i.partition(":")[0].endswith(".py") for i in scan_package())


def test_near_budget_entries_are_listed_and_marked_both_ways() -> None:
    """Every short_cpu (ms) or io (cpu_ms) entry above 60% of the budget carries near_budget, and says it is tracked."""
    near = {i for i, e in REGISTRY.items() if e.get("near_budget")}
    assert near == _EXPECTED_NEAR_BUDGET, sorted(near)
    assert all("tracked for moving to a worker" in REGISTRY[i]["reason"].lower() for i in near)
    assert budget_problems(scan_package(), REGISTRY) == []
    unmarked = {**REGISTRY, "coordinator.py:night_replay.nightly_job_payload": short_cpu(364, "x", "r")}  # 73%
    assert any("near_budget must be True" in p for p in budget_problems(scan_package(), unmarked))
    overmarked = {**REGISTRY, "fix_history.py:search_records": short_cpu(9, "x", "r", near_budget=True)}
    assert any("near_budget must be False" in p for p in budget_problems(scan_package(), overmarked))
    io_unmarked = {**REGISTRY, "coordinator.py:store.save": io("r", cpu_ms=464, at="x")}
    assert any("near_budget must be True" in p for p in budget_problems(scan_package(), io_unmarked))
    io_overmarked = {**REGISTRY, "coordinator.py:self._v3_archive.save": io("r", cpu_ms=70, at="x", near_budget=True)}
    assert any("near_budget must be False" in p for p in budget_problems(scan_package(), io_overmarked))
    uncosted = {**REGISTRY, "coordinator.py:self._v4_store.load": io("r", near_budget=True)}
    assert any("costed short_cpu or io" in p for p in budget_problems(scan_package(), uncosted))
