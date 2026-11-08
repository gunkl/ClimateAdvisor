"""No decision path reads prediction engine v4 (Issue #1230 R1, structural guard).

Occupant view: v4 runs in shadow beside today's prediction. If any setpoint, fan, window, briefing or Status code ever
read a v4 value, a bad v4 fit could change how the house is run. This is checked on the source (AST), not on behaviour,
so a future edit that wires v4 into a decision fails here before it can reach a home.

Allowed to mention v4 (anything named ``v4``/``predictor_v4``):

* the shadow modules: ``v4_shadow.py``, ``v4_archive.py``, ``prediction_accuracy.py`` (and ``predictor_v4.py``);
* the ADVISORY module ``night_strategy.py`` (Issue #1230 R4): it reads v4 to rank bedroom options for the occupant
  to read, never to command anything; ``test_advisory_modules_command_nothing`` pins that it imports nothing that
  decides or calls a service;
* ``const.py`` (``V4_*`` constants), ``api.py`` (the read-only endpoint
  and the Debug ``engines`` field);
* ``coordinator.py``, only in ``_v4*``/``v4_*`` methods and in fixed wiring points that never feed v4 back: the
  constructor, ``async_setup`` (nightly timer), ``async_restore_state`` (archive load), ``async_shutdown`` and the
  HA-stop write task ``_persist_on_ha_stop`` (archive flush, Issue #1423),
  the per-cycle call and the Debug payload.

Issue #1230 R6b: the PRIMARY engine now drives the prediction-dependent decisions, through the coordinator's control
feed (``control_thermal_model``, ``control_predicted_indoor``, ``_refresh_control_prediction``, and the
``_v4_control_cycle`` orchestration). The guards that remain: no decision MODULE names v4, the registry or the
settings; every ``predicted_indoor=`` handed to a decision is ``control_predicted_indoor()``; the shadow engine is never
read by a decision path (``test_every_predicted_indoor_handed_to_control_comes_from_the_registry_primary_path``,
``test_the_shadow_is_never_read_by_a_decision_path``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SHADOW_MODULES = {"v4_shadow.py", "v4_archive.py", "prediction_accuracy.py", "predictor_v4.py", "night_replay.py"}
# The DISPLAY registry (Issue #1230): the primary/shadow engines feed the chart, the Next 24 hours path, the tiles.
_DISPLAY_MODULES = {"prediction_engines.py"}
_ADVISORY_MODULES = {"night_strategy.py"}  # advise only (DP-7): see test_advisory_modules_command_nothing
_DATA_MODULES = {"const.py", "api.py"}  # fields/constants/endpoint only; checked below
_COORDINATOR_WIRING = {
    "__init__",
    "async_setup",
    "async_restore_state",
    "async_shutdown",
    # Issue #1423/#1438: the EVENT_HOMEASSISTANT_STOP write task flushes the v4 archive like async_shutdown does.
    "_persist_on_ha_stop",
    "_async_update_data_impl",
    "get_debug_state",
    # Issue #1230 R2: the Status chart DISPLAYS v4 (when v4 has data or the occupant selected it). Display only:
    # see test_get_chart_data_only_displays_v4 for how narrowly it may touch v4.
    "get_chart_data",
    # Issue #1459: the chart key's capabilities block says whether v4 runs, so a v4 chip is locked only when v4 is
    # switched off. Display only, called only by get_chart_data (pinned in tests/test_chart_capabilities_1459.py).
    "chart_capabilities",
    # Issue #1230 R6: the night strategy ADVISES (DP-7: nothing commands a fan, window or setpoint). These two read the
    # v4 model/state to plan tonight's bedroom advice and publish it read-only; they never feed v4 back
    # (test_night_wiring_is_advisory_only pins what they may write and call).
    "_timeline_night_apply",
    "_timeline_night_plan",
    "night_strategy_payload",
    # Issue #1230: the DISPLAY reads which engine is primary/shadow (the Next 24 hours path source) and the engine
    # status block; control never does (test_no_decision_path_reads_the_primary_or_shadow_engine_setting).
    "_timeline_snapshot",
    "primary_engine_id",
    "shadow_engine_id",
    "engine_running",
    "prediction_engines_payload",
}


def _is_v4(name: str) -> bool:
    # `v4` as its own token (v4_model, V4Archive, predictor_v4), not inside `ipv4`.
    low = name.lower()
    if low.startswith("prediction_model"):  # the setting's own constants (PREDICTION_MODEL_V4), checked separately
        return False
    return re.search(r"(?<![a-z0-9])v4", low) is not None


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
        if path.name in _SHADOW_MODULES | _ADVISORY_MODULES | _DATA_MODULES | _DISPLAY_MODULES | {"coordinator.py"}:
            continue
        for node, name, where in _references(_tree(path)):
            offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')} {name} in {'.'.join(where) or '<module>'}")
    assert offenders == [], "decision-path code references v4:\n" + "\n".join(offenders)


def test_shadow_modules_import_nothing_that_decides() -> None:
    """The shadow halves may not import the engine, classifier, briefing or anything that commands."""
    forbidden = {"automation", "classifier", "briefing", "desired_state", "swing", "nat_vent_fsm", "fan_fsm"}
    offenders = []
    for name in sorted((_SHADOW_MODULES | _DISPLAY_MODULES) - {"predictor_v4.py"}):
        path = _PKG / name
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                mods = {node.module} if node.module else {a.name for a in node.names}
                offenders += [f"{name}: from .{m} import ..." for m in mods if m in forbidden]
    assert offenders == []


def test_advisory_modules_command_nothing() -> None:
    """R4 night strategy reads v4 but only advises: no engine/coordinator/api import, no service call, no hass."""
    forbidden = {"automation", "classifier", "briefing", "desired_state", "swing", "nat_vent_fsm", "fan_fsm"}
    forbidden |= {"coordinator", "api"}
    offenders = []
    for name in sorted(_ADVISORY_MODULES):
        tree = _tree(_PKG / name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                mods = {node.module} if node.module else {a.name for a in node.names}
                offenders += [f"{name}: from .{m} import ..." for m in mods if m in forbidden]
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("homeassistant"):
                offenders.append(f"{name}: imports Home Assistant ({node.module})")
            if isinstance(node, ast.Attribute) and node.attr in {"async_call", "async_create_task", "services"}:
                offenders.append(f"{name}:{node.lineno} .{node.attr}")
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


def test_get_chart_data_only_displays_v4() -> None:
    """Issue #1230 R2: the Status chart may show v4; get_chart_data touches it in two calls and one payload key.

    The result of ``v4_chart_series`` may only flow into the returned dict (never into a band, target, activity or any
    other series), so a v4 value can be drawn but can never steer what the chart says Climate Advisor will do.
    """
    method = _method("get_chart_data")
    names = {name for _node, name, where in _coordinator_refs() if "get_chart_data" in where}
    assert names <= {"v4_chart_series", "v4_indoor_sensors_meta", "_v4_block", "v4"}, names
    block_uses = [n for n in ast.walk(method) if isinstance(n, ast.Name) and n.id == "_v4_block"]
    stores = [n for n in block_uses if isinstance(n.ctx, ast.Store)]
    loads = [n for n in block_uses if isinstance(n.ctx, ast.Load)]
    assert len(stores) == 1 and len(loads) == 1
    ret = next(n for n in ast.walk(method) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
    ret_dict = ret.value
    assert isinstance(ret_dict, ast.Dict)
    pairs = {k.value: v for k, v in zip(ret_dict.keys, ret_dict.values, strict=True) if isinstance(k, ast.Constant)}
    assert isinstance(pairs["v4"], ast.Name) and pairs["v4"].id == "_v4_block"


# ---------------------------------------------------------------------------------------------------------------
# Issue #1230: the primary/shadow engine SETTINGS. Display, v4 orchestration and (R6b) the CONTROL FEED read the
# primary through the registry; the shadow is never read by a decision path.
# ---------------------------------------------------------------------------------------------------------------

_SELECTION_NAMES = {
    "prediction_engines",
    "CONF_PRIMARY_ENGINE",
    "CONF_SHADOW_ENGINE",
    "primary_engine",
    "shadow_engine",
    "primary_engine_id",
    "shadow_engine_id",
    "engine_running",
    "prediction_engines_payload",
}
# Modules that may name the selection: the registry itself, the setting's definition and form, nothing else.
_SELECTION_MODULES = {"prediction_engines.py", "prediction_accuracy.py", "const.py", "config_flow.py", "coordinator.py"}
# Coordinator methods that may read it. Display (chart payloads, the accuracy tiles' input, the Next 24 hours path),
# orchestration of v4 itself (is v4 the primary or shadow? then fit/predict/archive it), and (Issue #1230 R6b) the
# two CONTROL FEED methods that hand the PRIMARY's prediction and rates to the decisions as plain values.
_SELECTION_COORDINATOR_METHODS = {
    "_timeline_snapshot",
    "_timeline_night_apply",
    "get_chart_data",
    "chart_capabilities",  # Issue #1459: display only (the chart key's v4_running flag)
    "decision_rates_summary",  # Issue #1468: Debug tab only (which engine each decision rate comes from)
    "primary_engine_id",
    "shadow_engine_id",
    "engine_running",
    "prediction_engines_payload",
    "control_thermal_model",
    "control_predicted_indoor",
    "_refresh_control_prediction",
    "_v3_curve_wanted",
}
# The control feed: the only coordinator methods that decide which engine's numbers the decisions read.
_CONTROL_FEED_METHODS = {"control_thermal_model", "control_predicted_indoor", "_refresh_control_prediction"}
_SHADOW_NAMES = {"shadow_id", "shadow_engine_id", "shadow_engine", "CONF_SHADOW_ENGINE", "DEFAULT_SHADOW_ENGINE"}


def _selection_refs(tree: ast.AST) -> list[tuple[int, str, list[str]]]:
    out: list[tuple[int, str, list[str]]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _f(self, node: ast.AST) -> None:
            self.stack.append(node.name)  # type: ignore[attr-defined]
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _f  # type: ignore[assignment]

        def _note(self, node: ast.AST, name: str) -> None:
            if name in _SELECTION_NAMES:
                out.append((getattr(node, "lineno", 0), name, list(self.stack)))

        def visit_Name(self, node: ast.Name) -> None:
            self._note(node, node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            self._note(node, node.attr)
            self.generic_visit(node)

        def visit_alias(self, node: ast.alias) -> None:
            self._note(node, node.name)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                self._note(node, node.module)
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str):
                self._note(node, node.value)

    V().visit(tree)
    return out


def test_no_decision_path_reads_the_primary_or_shadow_engine_setting_or_the_registry() -> None:
    """No decision MODULE names the engines or the registry: the coordinator hands them plain values (R6b)."""
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _SELECTION_MODULES:
            continue
        for line, name, where in _selection_refs(_tree(path)):
            offenders.append(f"{path.name}:{line} {name} in {'.'.join(where) or '<module>'}")
    assert offenders == [], "a decision-path module reads the display selection:\n" + "\n".join(offenders)


def test_the_coordinator_reads_the_engine_setting_only_in_display_and_v4_orchestration_methods() -> None:
    stray = []
    for line, name, where in _selection_refs(_tree(_PKG / "coordinator.py")):
        if not where:
            continue  # module-level import
        method = where[1] if where[0] == "ClimateAdvisorCoordinator" and len(where) > 1 else where[0]
        if method not in _SELECTION_COORDINATOR_METHODS and not method.startswith(("_v4", "v4_")):
            stray.append(f"line {line} {name} in {'.'.join(where)}")
    assert stray == [], "coordinator code outside the display methods reads the selection:\n" + "\n".join(stray)


def test_every_predicted_indoor_handed_to_control_comes_from_the_registry_primary_path() -> None:
    """Issue #1230 R6b: every ``predicted_indoor=`` handed to a decision is ``control_predicted_indoor()``.

    That method reads either v3's own curve (v3 primary) or the control path ``_refresh_control_prediction`` built from
    the registry (``prediction_engines.control_path`` -> ``primary_path``), never a v4/shadow name directly.
    """
    tree = _tree(_PKG / "coordinator.py")
    seen = 0
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "predicted_indoor":
                continue
            src = ast.unparse(kw.value)
            seen += 1
            # ``_remaining_predicted_indoor`` is ``_walk_forward_regime``'s own slice of the curve it was handed (the
            # chart's walk; get_chart_data gives it the control path when the primary is not v3).
            if src not in ("self.control_predicted_indoor()", "_remaining_predicted_indoor"):
                bad.append(f"line {node.lineno}: predicted_indoor={src}")
    assert seen >= 6, "the control call sites disappeared"
    assert bad == [], "a decision gets a curve that did not come through the control feed:\n" + "\n".join(bad)


def test_the_shadow_is_never_read_by_a_decision_path() -> None:
    """Issue #1230 R6b: the comparison (shadow) engine never feeds a decision.

    (1) No decision module names it (``test_no_decision_path_reads_...``). (2) In the coordinator the shadow's id is
    read only by the display/orchestration methods, never by a control-feed method; (3) the feed methods name no shadow
    identifier at all; (4) the registry's control functions never call ``shadow_id``.
    """
    tree = _tree(_PKG / "coordinator.py")
    in_feed = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator"):
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name in _CONTROL_FEED_METHODS:
                for n in ast.walk(fn):
                    names = []
                    if isinstance(n, ast.Name):
                        names.append(n.id)
                    if isinstance(n, ast.Attribute):
                        names.append(n.attr)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        names.append(n.value)
                    in_feed += [f"{fn.name}:{n.lineno} {x}" for x in names if x in _SHADOW_NAMES]
    assert in_feed == [], "the control feed reads the shadow engine:\n" + "\n".join(in_feed)
    for line, name, where in _selection_refs(tree):
        if name in _SHADOW_NAMES and where:
            method = where[1] if where[0] == "ClimateAdvisorCoordinator" and len(where) > 1 else where[0]
            assert method in {"shadow_engine_id", "prediction_engines_payload", "get_chart_data"} or method.startswith(
                ("_v4", "v4_")
            ), f"line {line}: shadow name {name} in {method}"
    registry = ast.parse((_PKG / "prediction_engines.py").read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(registry) if isinstance(n, ast.FunctionDef)):
        if fn.name in {
            "control_model",
            "control_path",
            "control_ready",
            "control_capable",
            "_control_engine",
            "resample_path",
        }:
            called = {
                (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
            }
            assert not (called & {"shadow_id", "running_ids"}), f"{fn.name} reads the shadow ({called})"


def test_the_registry_and_the_timeline_selection_are_display_inputs_only() -> None:
    """``TimelineSnapshot.selected_path`` is read only by ``_choose_path`` (the path the occupant sees)."""
    src = (_PKG / "operation_timeline.py").read_text(encoding="utf-8")
    assert len(re.findall(r"snap\.selected_path\b", src)) == 2 and "def _choose_path" in src
    for name in ("automation.py", "desired_state.py", "ode_ceiling_guard.py", "nat_vent_plan.py", "swing.py"):
        text = (_PKG / name).read_text(encoding="utf-8")
        assert "prediction_engines" not in text and "primary_engine" not in text and "selected_path" not in text, name
