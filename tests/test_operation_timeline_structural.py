"""Who may read the operation timeline, and what the code that builds it may touch (Issue #1230, structural guard).

Occupant view: since R3b the card that says "Open windows at 10:00 AM", the Next Automation card, the briefing's steps
and the Analysis tomorrow column all read ONE timeline; in R3a (shadow) none of them did. The guard changed with the
job. It is checked on the source (AST), like ``test_v4_shadow_structural``:

* the timeline is READ only by the pure modules ``operation_timeline.py`` (builder) and ``timeline_surfaces.py`` (the
  surfaces' rules), and wired by ``coordinator.py`` (build + the surface readers), ``briefing.py`` (a ``timeline``
  argument, through ``timeline_surfaces`` only) and ``api.py`` (the REST view, through
  ``coordinator.timeline_payload``);
  no other module mentions it, so no decision (automation, classifier, learning) can start depending on it;
* the pure modules import no engine, classifier, briefing, coordinator or Home Assistant code;
* the coordinator methods that BUILD the timeline write only ``self._timeline_*`` and command nothing;
* the same-snapshot agreement of the surfaces is the invariant test (``tests/test_timeline_surface_invariants.py``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"

_NAMES = re.compile(
    r"operation_timeline|DetailedTimeline|^Timeline$|TimelineLatch|TimelineSnapshot|TimelineStep|TIMELINE_TRIGGER"
    r"|^timeline_(to|detail)_dict$"
    r"|^_timeline_(latch|last|failed_logged|dirty|note_event|tick|shadow_run|shadow_run_impl|snapshot)$"
    r"|^timeline_debug$|^build_timeline$"
)

_WIRING = {
    "__init__": "construct",
    "_async_update_data_impl": "call",
    "_async_occupancy_toggle_changed": "call",
    "_async_thermal_sample_tick": "call",
    "_emit_event": "call",
    "get_debug_state": "debug",
}

_READERS = {"operation_timeline.py", "timeline_surfaces.py", "coordinator.py", "briefing.py", "api.py"}
_FORBIDDEN_IMPORTS = {
    "automation",
    "classifier",
    "briefing",
    "coordinator",
    "desired_state",
    "nat_vent_fsm",
    "fan_fsm",
    "api",
    "sensor",
    "learning",
}


def _is_timeline(name: str) -> bool:
    return _NAMES.search(name) is not None


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _references(tree: ast.AST) -> list[tuple[ast.AST, str, list[str]]]:
    """Every timeline identifier (code only, not comments or prose) with its enclosing function/class names."""
    found: list[tuple[ast.AST, str, list[str]]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _note(self, node: ast.AST, name: str) -> None:
            if _is_timeline(name):
                found.append((node, name, list(self.stack)))

        def _scope(self, node: ast.AST, name: str) -> None:
            self.stack.append(name)
            self._note(node, name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scope(node, node.name)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scope(node, node.name)

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

        def visit_keyword(self, node: ast.keyword) -> None:
            if node.arg:
                self._note(node, node.arg)
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                self._note(node, node.value)

    Visitor().visit(tree)
    return found


def _coordinator_class() -> ast.ClassDef:
    return next(
        n
        for n in ast.walk(_tree(_PKG / "coordinator.py"))
        if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator"
    )


def _method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        n for n in _coordinator_class().body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    )


def test_no_other_module_mentions_the_timeline() -> None:
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _READERS:
            continue
        for node, name, where in _references(_tree(path)):
            offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')} {name} in {'.'.join(where) or '<module>'}")
    assert offenders == [], "a module outside the timeline's readers references the operation timeline:\n" + "\n".join(
        offenders
    )


def test_the_timeline_modules_import_only_pure_rule_modules() -> None:
    offenders = []
    for name in ("operation_timeline.py", "timeline_surfaces.py"):
        for node in ast.walk(_tree(_PKG / name)):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] == "homeassistant":
                    offenders.append(f"{name}: homeassistant import: {node.module}")
                if node.level == 1:
                    mods = {node.module} if node.module else {a.name for a in node.names}
                    offenders += [f"{name}: from .{m} import ..." for m in mods if m in _FORBIDDEN_IMPORTS]
            elif isinstance(node, ast.Import):
                offenders += [f"{name}: import {a.name}" for a in node.names if a.name.split(".")[0] == "homeassistant"]
    assert offenders == [], offenders


def test_briefing_and_api_reach_the_timeline_only_through_the_surface_module_or_the_coordinator() -> None:
    brief = _tree(_PKG / "briefing.py")
    imported = {
        (n.module or "")
        for n in ast.walk(brief)
        if isinstance(n, ast.ImportFrom) and n.level == 1 and "timeline" in (n.module or "")
    }
    assert imported == {"timeline_surfaces"}, imported
    api = _tree(_PKG / "api.py")
    assert not [n for n in ast.walk(api) if isinstance(n, ast.ImportFrom) and "timeline" in (n.module or "")]
    assert not [n for n in ast.walk(_tree(_PKG / "timeline_surfaces.py")) if isinstance(n, ast.AsyncFunctionDef)]


_SURFACE_READERS = {
    "_compute_next_action",
    "_compute_next_automation_action",
    "tomorrow_plan",
    "_build_briefing_text",
    "timeline_payload",
    "timeline_operations_lines",
    "_surface_timeline",
}


def test_coordinator_mentions_the_timeline_only_in_its_build_wiring_and_surface_readers() -> None:
    stray = []
    for node, name, where in _references(_tree(_PKG / "coordinator.py")):
        if not where:
            continue  # module-level import
        method = where[1] if where[0] == "ClimateAdvisorCoordinator" and len(where) > 1 else where[0]
        if (
            method.startswith("_timeline_")
            or method == "timeline_debug"
            or method in _WIRING
            or method in _SURFACE_READERS
        ):
            continue
        stray.append(f"line {getattr(node, 'lineno', '?')} {name} in {'.'.join(where)}")
    assert stray == [], "coordinator code outside the timeline wiring and surface readers references it:\n" + "\n".join(
        stray
    )


def test_each_wiring_point_is_one_discarded_call_and_nothing_uses_the_result() -> None:
    for method_name, kind in _WIRING.items():
        method = _method(method_name)
        mentions = [n for n in ast.walk(method) if isinstance(n, ast.Attribute) and _is_timeline(n.attr)]
        if kind == "construct":
            assert {m.attr for m in mentions} == {
                "_timeline_latch",
                "_timeline_last",
                "_timeline_failed_logged",
                "_timeline_dirty",
            }
            continue
        if kind == "debug":
            assert [m.attr for m in mentions] == ["timeline_debug"]
            # output-only: the call result is the VALUE of a dict entry under the "operation_timeline" key
            entry = [
                (k, v)
                for d in ast.walk(method)
                if isinstance(d, ast.Dict)
                for k, v in zip(d.keys, d.values, strict=False)
                if isinstance(k, ast.Constant) and k.value == "operation_timeline"
            ]
            assert len(entry) == 1 and isinstance(entry[0][1], ast.Call)
            continue
        assert len(mentions) == 1, f"{method_name}: {len(mentions)} timeline references"
        hooks = [s for s in method.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)]
        timeline_hooks = [
            s
            for s in hooks
            if isinstance(s.value.func, ast.Attribute) and _is_timeline(s.value.func.attr)  # type: ignore[union-attr]
        ]
        assert len(timeline_hooks) == 1, f"{method_name}: the timeline call must be a bare statement of the method"


def test_the_shadow_methods_write_only_timeline_state_and_never_command_anything() -> None:
    forbidden_calls = {
        "async_call",
        "_emit_event",
        "_emit_incident",
        "services",
        "set_hvac_mode",
        "set_temperature",
        "set_occupancy_mode",
        "apply_classification",
        "handle_occupancy_home",
        "handle_occupancy_away",
        "clear_manual_override",
        "_set_temperature",
        "_apply_comfort_band",
    }
    for name in (
        "_timeline_note_event",
        "_timeline_tick",
        "_timeline_shadow_run",
        "_timeline_shadow_run_impl",
        "_timeline_snapshot",
        "timeline_debug",
        "timeline_payload",
        "_surface_timeline",
        "timeline_operations_lines",
    ):
        method = _method(name)
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                base = node.value
                if isinstance(base, ast.Name) and base.id == "self":
                    assert node.attr.startswith("_timeline"), f"{name} writes self.{node.attr}"
                else:
                    raise AssertionError(f"{name} stores attribute {node.attr} on something other than self")
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_calls, f"{name} reaches {node.attr}"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    # no writes through the engine: `ae.something = ...`
                    assert not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in {"ae", "engine"}
                    ), f"{name} writes into the engine"


def test_the_notified_events_only_mark_the_plan_stale() -> None:
    """``_emit_event`` -> ``_timeline_note_event`` may set a flag; it must not schedule work or recompute inline."""
    method = _method("_timeline_note_event")
    names = {n.attr for n in ast.walk(method) if isinstance(n, ast.Attribute)}
    assert "async_create_task" not in names and "_timeline_shadow_run" not in names
