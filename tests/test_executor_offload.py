"""Regression tests for Issue #376: ODE/OLS computation executor offload.

Verifies that _build_predicted_indoor_future is never called directly from
async methods in coordinator.py (which would block the HA event loop).

The fix wraps all async callsites in await hass.async_add_executor_job(functools.partial(...)).
If someone removes the wrapper, the AST test catches it at test time rather than in prod.
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
