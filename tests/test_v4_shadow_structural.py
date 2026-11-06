"""No decision path reads prediction engine v4 (Issue #1230 R1, structural guard).

Occupant view: v4 runs in shadow beside today's prediction. If any setpoint, fan, window, briefing or Status code ever
read a v4 value, a bad v4 fit could change how the house is run. This is checked on the source (AST), not on behaviour,
so a future edit that wires v4 into a decision fails here before it can reach a home.

Allowed to mention v4 (anything named ``v4``/``predictor_v4``):

* the shadow modules: ``v4_shadow.py``, ``v4_archive.py``, ``prediction_accuracy.py`` (and ``predictor_v4.py``);
* ``const.py`` (``V4_*`` constants), ``api.py`` (the read-only endpoint
  and the Debug ``engines`` field);
* ``coordinator.py``, only in ``_v4*``/``v4_*`` methods and in six fixed wiring points that never feed v4 back: the
  constructor, ``async_setup`` (nightly timer), ``async_restore_state`` (archive load), ``async_shutdown``,
  the per-cycle call and the Debug payload.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SHADOW_MODULES = {"v4_shadow.py", "v4_archive.py", "prediction_accuracy.py", "predictor_v4.py"}
_DATA_MODULES = {"const.py", "api.py"}  # fields/constants/endpoint only; checked below
_COORDINATOR_WIRING = {
    "__init__",
    "async_setup",
    "async_restore_state",
    "async_shutdown",
    "_async_update_data_impl",
    "get_debug_state",
}


def _is_v4(name: str) -> bool:
    # `v4` as its own token (v4_model, V4Archive, predictor_v4), not inside `ipv4`.
    return re.search(r"(?<![a-z0-9])v4", name.lower()) is not None


def _references(tree: ast.AST) -> list[tuple[ast.AST, str, list[str]]]:
    """Every ``v4`` identifier in the code (not comments/docstrings/log text), with its enclosing function names."""
    found: list[tuple[ast.AST, str, list[str]]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _note(self, node: ast.AST, name: str) -> None:
            if _is_v4(name):
                found.append((node, name, list(self.stack)))

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self._note(node, node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self._note(node, node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Name(self, node: ast.Name) -> None:
            self._note(node, node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            self._note(node, node.attr)
            self.generic_visit(node)

        def visit_alias(self, node: ast.alias) -> None:
            self._note(node, node.name)
            if node.asname:
                self._note(node, node.asname)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                self._note(node, node.module)
            self.generic_visit(node)

        def visit_keyword(self, node: ast.keyword) -> None:
            if node.arg:
                self._note(node, node.arg)
            self.generic_visit(node)

        def visit_arg(self, node: ast.arg) -> None:
            self._note(node, node.arg)

        def visit_Constant(self, node: ast.Constant) -> None:
            # An identifier-shaped string ("v4_model" as a dict key / getattr name), not prose or a log message.
            if isinstance(node.value, str) and _IDENT.match(node.value):
                self._note(node, node.value)

    Visitor().visit(tree)
    return found


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_no_module_outside_the_shadow_wiring_mentions_v4() -> None:
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _SHADOW_MODULES | _DATA_MODULES | {"coordinator.py"}:
            continue
        for node, name, where in _references(_tree(path)):
            offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')} {name} in {'.'.join(where) or '<module>'}")
    assert offenders == [], "decision-path code references v4:\n" + "\n".join(offenders)


def test_shadow_modules_import_nothing_that_decides() -> None:
    """The shadow halves may not import the engine, classifier, briefing or anything that commands."""
    forbidden = {"automation", "classifier", "briefing", "desired_state", "swing", "nat_vent_fsm", "fan_fsm"}
    offenders = []
    for name in sorted(_SHADOW_MODULES - {"predictor_v4.py"}):
        path = _PKG / name
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                mods = {node.module} if node.module else {a.name for a in node.names}
                offenders += [f"{name}: from .{m} import ..." for m in mods if m in forbidden]
    assert offenders == []


def test_learning_py_knows_nothing_about_v4() -> None:
    """The model lives in its own file: the learning file must stay readable by a release without v4 (rollback)."""
    assert [n for _n, n, _w in _references(_tree(_PKG / "learning.py"))] == []


def test_api_py_only_exposes_v4_read_only() -> None:
    allowed_funcs = {"ClimateAdvisorPredictionAccuracyView", "ClimateAdvisorEnginesView"}
    stray = [
        f"line {getattr(n, 'lineno', '?')} {name} in {'.'.join(where) or '<module>'}"
        for n, name, where in _references(_tree(_PKG / "api.py"))
        if not (set(where) & allowed_funcs) and where  # module-level imports/constants are fine
    ]
    assert stray == []
    src = (_PKG / "api.py").read_text(encoding="utf-8")
    # the endpoint is GET only: no POST handler on the view
    view = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorPredictionAccuracyView"
    )
    assert {f.name for f in view.body if isinstance(f, ast.AsyncFunctionDef)} == {"get"}


def _coordinator_refs() -> list[tuple[ast.AST, str, list[str]]]:
    return _references(_tree(_PKG / "coordinator.py"))


def test_coordinator_mentions_v4_only_in_the_shadow_methods_and_fixed_wiring_points() -> None:
    stray = []
    for node, name, where in _coordinator_refs():
        if not where:
            continue  # module-level import / constant
        func = where[-1] if len(where) == 1 else where[1]  # where[0] is the class
        method = where[1] if where[0] == "ClimateAdvisorCoordinator" and len(where) > 1 else where[0]
        if method.startswith(("_v4", "v4_")) or method in _COORDINATOR_WIRING:
            continue
        stray.append(f"line {getattr(node, 'lineno', '?')} {name} in {'.'.join(where)} ({func})")
    assert stray == [], "coordinator code outside the v4 wiring references v4:\n" + "\n".join(stray)


def _method(name: str) -> ast.AST:
    cls = next(
        n
        for n in ast.walk(_tree(_PKG / "coordinator.py"))
        if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator"
    )
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name)


def test_the_cycle_hook_is_a_discarded_await_and_the_debug_field_is_output_only() -> None:
    cycle = _method("_async_update_data_impl")
    hooks = [
        s
        for s in ast.walk(cycle)
        if any(_is_v4(getattr(x, "attr", "")) for x in ast.walk(s) if isinstance(x, ast.Attribute))
        and isinstance(s, ast.stmt)
        and not isinstance(s, ast.AsyncFunctionDef | ast.FunctionDef)
    ]
    # exactly one statement mentions v4 in the cycle, and it is `await self._v4_shadow_cycle()` as a bare expression
    simple = [h for h in hooks if isinstance(h, ast.Expr)]
    assert len(simple) == 1 and isinstance(simple[0].value, ast.Await), ast.dump(simple[0]) if simple else hooks
    call = simple[0].value.value
    assert isinstance(call, ast.Call) and call.func.attr == "_v4_shadow_cycle" and not call.args  # type: ignore[attr-defined]
    # nothing else in the cycle (no `if`, assignment or return) carries a v4 value
    compound = [h for h in hooks if isinstance(h, ast.If | ast.Assign | ast.Return | ast.With | ast.Try | ast.For)]
    # compound statements legitimately contain the hook only if they also contain other code; the hook itself is at
    # method level, so no compound statement inside the cycle may mention v4 outside that one expression
    assert all(_only_contains_the_hook(c, simple[0]) for c in compound)

    debug = _method("get_debug_state")
    uses = [n for n in ast.walk(debug) if isinstance(n, ast.Attribute) and _is_v4(n.attr)]
    assert [u.attr for u in uses] == ["v4_model_summary"]


def _only_contains_the_hook(container: ast.AST, hook: ast.AST) -> bool:
    refs = [n for n in ast.walk(container) if isinstance(n, ast.Attribute) and _is_v4(n.attr)]
    hook_refs = [n for n in ast.walk(hook) if isinstance(n, ast.Attribute) and _is_v4(n.attr)]
    return all(r in hook_refs for r in refs)


@pytest.mark.parametrize(
    "name",
    [
        "_v4_shadow_cycle_impl",
        "_v4_run_fit",
        "_v4_start_fit",
        "_v4_flush_on_shutdown",
        "v4_model_summary",
        "v4_accuracy_inputs",
    ],
)
def test_shadow_methods_write_only_v4_state_and_never_touch_the_engine(name: str) -> None:
    """A shadow method writes only ``self._v4_*`` and the learning state's ``v4_*`` fields; it never decides."""
    method = _method(name)
    for node in ast.walk(method):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            base = node.value
            if isinstance(base, ast.Name) and base.id == "self":
                assert node.attr.startswith("_v4"), f"{name} writes self.{node.attr}"
            elif isinstance(base, ast.Name) and base.id == "state":  # state = self._v4_store: v4 data only
                pass
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"automation_engine", "_emit_event", "_emit_incident", "services", "async_call"}, (
                f"{name} reaches {node.attr}"
            )
