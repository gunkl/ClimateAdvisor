"""Regression tests for Issue #376 and Issue #543/#545: executor offload for
blocking work called from async methods in coordinator.py.

TestODEExecutorOffload (Issue #376) verifies that _build_predicted_indoor_future
is never called directly from specific async methods — CPU-bound work.

TestBlockingIOExecutorOffload (Issue #543/#545) is registry-driven and generalizes
the same idea to blocking I/O: known blocking sub-component methods (chart_log,
state persistence, learning) must never be called directly from ANY async method
in coordinator.py, not just a hardcoded list of call sites.

If someone removes an executor wrapper, these AST tests catch it at test time
rather than in prod.
"""

from __future__ import annotations

import ast
from pathlib import Path

COORDINATOR_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"
API_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "api.py"

_TARGET_FN = "_build_predicted_indoor_future"


def _is_direct_call(node: ast.AST) -> bool:
    """Return True if node is a direct call to _TARGET_FN (not inside functools.partial)."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _TARGET_FN


def _extract_async_methods(tree: ast.AST) -> list[tuple[str, ast.AsyncFunctionDef]]:
    """Return (name, node) for all async methods in the module."""
    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            results.append((node.name, node))
    return results


def _find_direct_calls_in_node(fn_node: ast.AST) -> list[ast.Call]:
    """Walk fn_node and return any direct (non-partial) calls to _TARGET_FN.

    A call is "direct" if _TARGET_FN appears as the immediate callee, not as
    an argument inside functools.partial(). We skip nodes that are arguments
    to a functools.partial call.
    """
    direct_calls = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            # Check if this is functools.partial(...) — if so, don't recurse
            # into its arguments (those calls are fine; they're the wrapped fn).
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "partial"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "functools"
            ):
                # Don't descend into partial() arguments.
                return

            # Check if this call itself is a direct invocation of _TARGET_FN
            if _is_direct_call(node):
                direct_calls.append(node)

            # Recurse into sub-expressions (but we already skipped partial args above)
            self.generic_visit(node)

    _Visitor().visit(fn_node)
    return direct_calls


class TestODEExecutorOffload:
    """_build_predicted_indoor_future must not be called directly from async methods."""

    def test_no_direct_calls_in_async_update_data(self):
        """_async_update_data must use async_add_executor_job for the ODE call.

        Regression for Issue #376: direct call blocked the event loop on every
        coordinator refresh (every 30 min).
        """
        source = COORDINATOR_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        async_methods = {name: node for name, node in _extract_async_methods(tree)}
        assert "_async_update_data" in async_methods, (
            "_async_update_data not found in coordinator.py — method was renamed?"
        )

        direct = _find_direct_calls_in_node(async_methods["_async_update_data"])
        assert not direct, (
            f"_build_predicted_indoor_future called directly (not via functools.partial) "
            f"in _async_update_data at line(s): {[c.lineno for c in direct]}. "
            "Wrap in await hass.async_add_executor_job(functools.partial(...)) to avoid "
            "blocking the HA event loop."
        )

    def test_no_direct_calls_in_async_send_briefing(self):
        """_async_send_briefing must use async_add_executor_job for the ODE call.

        Regression for Issue #376: direct call blocked the event loop on each
        morning briefing.
        """
        source = COORDINATOR_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        async_methods = {name: node for name, node in _extract_async_methods(tree)}
        assert "_async_send_briefing" in async_methods, (
            "_async_send_briefing not found in coordinator.py — method was renamed?"
        )

        direct = _find_direct_calls_in_node(async_methods["_async_send_briefing"])
        assert not direct, (
            f"_build_predicted_indoor_future called directly (not via functools.partial) "
            f"in _async_send_briefing at line(s): {[c.lineno for c in direct]}. "
            "Wrap in await hass.async_add_executor_job(functools.partial(...)) to avoid "
            "blocking the HA event loop."
        )

    def test_chart_data_view_uses_executor(self):
        """ClimateAdvisorChartDataView.get() must offload get_chart_data to executor.

        get_chart_data() runs _build_predicted_indoor_future inline (ODE+OLS math).
        Calling it directly from the async aiohttp handler blocks the event loop.
        Fix: await hass.async_add_executor_job(functools.partial(coordinator.get_chart_data, ...))
        """
        source = API_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find ClimateAdvisorChartDataView class
        chart_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ClimateAdvisorChartDataView":
                chart_class = node
                break
        assert chart_class is not None, "ClimateAdvisorChartDataView not found in api.py"

        # Find the get() method inside it
        get_method = None
        for node in ast.walk(chart_class):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "get":
                get_method = node
                break
        assert get_method is not None, "ClimateAdvisorChartDataView.get() not found"

        # Verify get_chart_data is NOT called directly (i.e., not as a plain Call outside partial)
        direct_chart_calls = []

        class _ChartVisitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "partial"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "functools"
                ):
                    return
                if isinstance(node.func, ast.Attribute) and node.func.attr == "get_chart_data":
                    direct_chart_calls.append(node)
                self.generic_visit(node)

        _ChartVisitor().visit(get_method)

        assert not direct_chart_calls, (
            f"coordinator.get_chart_data() called directly (not via executor) in "
            f"ClimateAdvisorChartDataView.get() at line(s): {[c.lineno for c in direct_chart_calls]}. "
            "Offload via await hass.async_add_executor_job(functools.partial(coordinator.get_chart_data, ...))."
        )

        # Also verify async_add_executor_job IS referenced in the get() method
        executor_calls = []

        class _ExecutorVisitor(ast.NodeVisitor):
            def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
                if node.attr == "async_add_executor_job":
                    executor_calls.append(node)
                self.generic_visit(node)

        _ExecutorVisitor().visit(get_method)
        assert executor_calls, (
            "async_add_executor_job not found in ClimateAdvisorChartDataView.get(). "
            "The chart endpoint must offload get_chart_data to the thread pool."
        )


# ---------------------------------------------------------------------------
# Issue #543 / #545: generalized check for blocking sub-component methods
# called unwrapped from ANY async method in coordinator.py.
# ---------------------------------------------------------------------------

# Known (attribute_name, method_name) pairs that do blocking I/O and must never
# be called directly from an async method — only passed by reference to
# await hass.async_add_executor_job(self.<attr>.<method>, ...).
# Add an entry here whenever a new I/O sub-component is introduced (chart_log.py,
# state.py, learning.py are the existing ones as of Issue #543/#545).
_BLOCKING_METHODS: set[tuple[str, str]] = {
    ("_chart_log", "load"),
    ("_chart_log", "save"),
    ("_forecast_archive", "load"),
    ("_forecast_archive", "save"),
    ("_v4_archive", "load"),
    ("_v4_archive", "save"),
    ("_v4_store", "load"),
    ("_v4_store", "save"),
    ("_state_persistence", "load"),
    ("_state_persistence", "save"),
    ("learning", "load_state"),
    ("learning", "save_state"),
}


def _find_blocking_calls_in_node(fn_node: ast.AST) -> list[ast.Call]:
    """Walk fn_node and return any direct calls shaped like self.<attr>.<method>(...)
    where (<attr>, <method>) is a known-blocking pair from _BLOCKING_METHODS.

    Correctly-offloaded code never produces this Call shape at all — the fix passes
    self.<attr>.<method> by reference to hass.async_add_executor_job(...) without
    invoking it (e.g. `await self.hass.async_add_executor_job(self._chart_log.save)`).
    So unlike _find_direct_calls_in_node above (which must skip functools.partial's
    arguments, since those ARE real calls to the wrapped function), no exclusion
    logic is needed here: any matching Call node is, by construction, a direct
    blocking call that bypassed the executor.
    """
    blocking_calls = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "self"
                and (func.value.attr, func.attr) in _BLOCKING_METHODS
            ):
                blocking_calls.append(node)
            self.generic_visit(node)

    _Visitor().visit(fn_node)
    return blocking_calls


class TestBlockingIOExecutorOffload:
    """Known blocking sub-component methods must never be called directly from
    any async method in coordinator.py.

    Regression for Issue #543: ChartStateLog.load()/save() did blocking file I/O
    (tempfile write, os.replace, os.chmod, Path.read_text) but were called directly
    from async coordinator methods without going through
    await hass.async_add_executor_job(...), stalling the HA event loop for the
    entire Home Assistant instance. Unlike TestODEExecutorOffload above (hardcoded
    to 3 call sites for one function), this check is registry-driven and applies to
    EVERY async method in coordinator.py — add new (attribute, method) pairs to
    _BLOCKING_METHODS as new I/O sub-components are introduced (see
    claude.md's Thread-Safety Requirements section).
    """

    def test_no_blocking_calls_in_any_async_method(self):
        source = COORDINATOR_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders: list[tuple[str, ast.Call]] = []
        for name, node in _extract_async_methods(tree):
            for call in _find_blocking_calls_in_node(node):
                offenders.append((name, call))

        assert not offenders, (
            "Blocking sub-component method(s) called directly (not via "
            "hass.async_add_executor_job) inside async method(s): "
            + ", ".join(f"{name}() at line {call.lineno}" for name, call in offenders)
            + ". Wrap in await self.hass.async_add_executor_job(self.<attr>.<method>) "
            "to avoid blocking the HA event loop."
        )


# ---------------------------------------------------------------------------
# Issue #702: fix_history.py's blocking file reads must only be reached via its
# own async_search_fix_history/async_recent_release_notes wrappers, never by
# calling the sync search_records/_recent_release_notes functions directly from
# an async context provider. These are plain module-level functions (not
# self.<attr>.<method> bound methods), so the coordinator.py-specific registry
# above doesn't apply — this is a small, targeted check of its own.
# ---------------------------------------------------------------------------

AI_SKILLS_CONTEXT_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "ai_skills_context.py"

_FIX_HISTORY_BLOCKING_FUNCS = {"search_records", "_recent_release_notes", "_iter_records"}


def _find_direct_fix_history_calls(fn_node: ast.AST) -> list[ast.Call]:
    direct_calls = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in _FIX_HISTORY_BLOCKING_FUNCS:
                direct_calls.append(node)
            self.generic_visit(node)

    _Visitor().visit(fn_node)
    return direct_calls


class TestFixHistoryExecutorOffload:
    """fix_history.py does blocking file I/O — its sync functions must never be
    called directly from an async context provider in ai_skills_context.py, only
    through the async_search_fix_history/async_recent_release_notes wrappers that
    offload via hass.async_add_executor_job (Issue #702)."""

    def test_no_direct_calls_in_ai_skills_context_async_methods(self):
        source = AI_SKILLS_CONTEXT_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders: list[tuple[str, ast.Call]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for call in _find_direct_fix_history_calls(node):
                    offenders.append((node.name, call))

        assert not offenders, (
            "fix_history's blocking sync function(s) called directly (not via "
            "async_search_fix_history/async_recent_release_notes) inside: "
            + ", ".join(f"{name}() at line {call.lineno}" for name, call in offenders)
        )


# ---------------------------------------------------------------------------
# Issue #1722: the heavy fits run in a worker PROCESS (a thread would hold the GIL and stall the event loop).
# No coordinator method may reference the in-process fit functions at all - called directly, handed to
# ``_executor_job`` / ``hass.async_add_executor_job`` or wrapped in ``functools.partial``. The coordinator builds the
# JSON payload (``*_job_payload``) and runs the job through ``self._process_job``.
# ---------------------------------------------------------------------------

_HEAVY_FIT_FUNCS: dict[str, set[str]] = {
    "v4_shadow": {"run_fit", "run_display_fit"},
    "night_replay": {"run_nightly"},
    "cycle_model": {"fit_cycles"},
}
# (enclosing method, function) -> why it may stay in the executor
_HEAVY_FIT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("_cycle_judge", "fit_cycles"): (
        "judges ONE just-closed cycle (about 26 ms, per new cycle, not the pooled refit): a worker process would cost "
        "more to start than the work takes"
    ),
}


def _find_heavy_fit_references(tree: ast.AST) -> list[tuple[str, str, int]]:
    """``(enclosing method, function, line)`` for every reference to a heavy in-process fit function: the attribute
    shape (``v4_shadow.run_fit``) and the from-import shape (``from .v4_shadow import run_fit`` / a bare name)."""
    found: list[tuple[str, str, int]] = []
    bare = {fn for fns in _HEAVY_FIT_FUNCS.values() for fn in fns}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _enter(self, node: ast.AST) -> None:
            self.stack.append(node.name)  # type: ignore[attr-defined]
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _enter  # noqa: N815
        visit_AsyncFunctionDef = _enter  # noqa: N815

        def _scope(self) -> str:
            return self.stack[0] if self.stack else "<module>"

        def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in _HEAVY_FIT_FUNCS
                and node.attr in _HEAVY_FIT_FUNCS[node.value.id]
            ):
                found.append((self._scope(), node.attr, node.lineno))
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if node.id in bare:
                found.append((self._scope(), node.id, node.lineno))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            if (node.module or "").split(".")[-1] in _HEAVY_FIT_FUNCS:
                for alias in node.names:
                    if alias.name in _HEAVY_FIT_FUNCS[(node.module or "").split(".")[-1]]:
                        found.append((self._scope(), alias.name, node.lineno))

    _Visitor().visit(tree)
    return found


class TestHeavyFitsRunInWorkerProcess:
    """Issue #1722: ``v4_shadow.run_fit`` / ``run_display_fit``, ``night_replay.run_nightly`` and the pooled
    ``cycle_model.fit_cycles`` refit must go through ``process_offload`` (``self._process_job``), never run in the
    process - not on the loop, and not in an executor thread either (the GIL)."""

    def test_no_coordinator_method_runs_a_heavy_fit_in_process(self):
        tree = ast.parse(COORDINATOR_PY.read_text(encoding="utf-8"))
        offenders = [
            (scope, fn, line)
            for scope, fn, line in _find_heavy_fit_references(tree)
            if (scope, fn) not in _HEAVY_FIT_ALLOWLIST
        ]
        assert not offenders, (
            "In-process heavy fit(s) in coordinator.py: "
            + ", ".join(f"{scope}() uses {fn} at line {line}" for scope, fn, line in offenders)
            + ". Build the payload with v4_shadow.fit_job_payload / display_fit_job_payload / "
            "night_replay.nightly_job_payload / cycle_model.refit_job_payload and run it with self._process_job(...) "
            "(a thread still holds the GIL and stalls Home Assistant)."
        )

    def test_the_allowlist_has_no_stale_entries(self):
        tree = ast.parse(COORDINATOR_PY.read_text(encoding="utf-8"))
        used = {(scope, fn) for scope, fn, _line in _find_heavy_fit_references(tree)}
        stale = set(_HEAVY_FIT_ALLOWLIST) - used
        assert not stale, f"allowlist entries no longer matched in coordinator.py: {sorted(stale)}"

    def test_the_guard_sees_every_way_of_running_one(self):
        """The checker itself: direct call, executor hand-over, functools.partial and a from-import all count."""
        src = (
            "from .v4_shadow import run_fit\n"
            "class C:\n"
            "    async def a(self):\n"
            "        return v4_shadow.run_fit(x=1)\n"
            "    async def b(self):\n"
            "        return await self._executor_job(functools.partial(night_replay.run_nightly, x=1))\n"
            "    async def c(self):\n"
            "        def _job():\n"
            "            return cycle_model.fit_cycles([], 'heat', None)\n"
            "        return await self._executor_job(_job)\n"
            "    async def d(self):\n"
            "        return await self.hass.async_add_executor_job(v4_shadow.run_display_fit)\n"
        )
        hits = {(scope, fn) for scope, fn, _l in _find_heavy_fit_references(ast.parse(src))}
        assert hits == {
            ("<module>", "run_fit"),
            ("a", "run_fit"),
            ("b", "run_nightly"),
            ("c", "fit_cycles"),
            ("d", "run_display_fit"),
        }
