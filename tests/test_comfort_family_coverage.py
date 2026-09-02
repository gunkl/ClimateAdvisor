"""Issue #827 prevention: registry-enforced coverage of every ``AutomationEngine``
method that touches ``self._comfort_mode_family``.

The comfort-family consolidation (Issue #827) replaced three previously
inconsistent authorities (``select_comfort_band()``'s day-type edge picker, the
confidence-gated ``_resolve_comfort_family_mode()``, and the separately-armed
``_family_switch_locked_out()`` dwell timer) with a single FSM
(``comfort_family_fsm.transition()``), reached through
``_resolve_comfort_family_via_fsm()``. ``self._comfort_mode_family`` survives as a
**compatibility attribute** (Design §2 preserved contract) so
``tools/sim_harness/outcomes.py``'s ``"comfort_family"`` assertion type and the 7
out-of-scope legacy callers of ``_arm_comfort_family()`` (nat-vent/WHF activation,
``_exit_nat_vent()``, the away-ceiling exit branches) keep working unchanged.

This module is the durable fix for "how do we know every method touching that
compatibility attribute stays intentionally classified as new code is added": it
AST-scans ``automation.py`` for every ``AutomationEngine`` method that reads or
writes ``self._comfort_mode_family`` (a direct attribute access, OR a
``getattr``/``setattr`` call keyed by that literal string — this codebase's
getattr-defensive convention for methods reachable via ``object.__new__()``
partial-instantiation in tests, see CLAUDE.md's "Coordinator methods" testing
note) and requires each to be explicitly classified in ``_COVERAGE_REGISTRY`` as
``"routed-through-fsm"``, ``"internal"``, or ``"exempted: <reason>"`` — the same
enforcement shape as ``tests/test_nat_vent_exit_lockout_coverage.py``'s
``_COVERAGE_REGISTRY``. A new method touching the field without being classified
here fails immediately, instead of silently drifting the compatibility contract
out of sync with the FSM.

Classified at method granularity (not call-site ordinal, unlike the nat-vent
precedent) — this field is a plain attribute, not a function call with its own
argument shape to distinguish multiple call sites within one method.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_AUTOMATION_PY = _REPO_ROOT / "custom_components" / "climate_advisor" / "automation.py"
_FIELD = "_comfort_mode_family"

# Classification meanings:
#   "routed-through-fsm" — reads/writes the compatibility attribute as part of the
#                           Issue #827 FSM wiring (either the FSM adapter itself, or
#                           the shared compatibility writer both the FSM and the 7
#                           out-of-scope legacy callers funnel through).
#   "internal"            — plain attribute declaration/initialization, no
#                            decision logic.
#   "exempted: <reason>"  — deliberately touches the field outside the FSM's own
#                            call graph, with a specific reason (never a bare
#                            "not needed").
_COVERAGE_REGISTRY: dict[str, str] = {
    "__init__": "internal — declares self._comfort_mode_family = None at engine construction",
    "_arm_comfort_family": (
        "routed-through-fsm — the single compatibility writer: called by "
        "_resolve_comfort_family_via_fsm() on every non-locked-out FSM transition, "
        "AND by the 7 out-of-scope legacy callers (nat-vent/WHF activation, "
        "_exit_nat_vent() and its bypass branches, the away-ceiling exit branches) "
        "left untouched by Issue #827 per that issue's own explicit scope note"
    ),
    "_resolve_comfort_family_via_fsm": (
        "routed-through-fsm — reads the compatibility attribute to seed the FSM's "
        "current_state on cold start (no prior family recorded), before handing off "
        "to comfort_family_fsm.transition()"
    ),
}


def _class_node(tree: ast.AST) -> ast.ClassDef:
    return next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine")


def _methods_touching_field(tree: ast.AST) -> list[str]:
    """AST-scan the ``AutomationEngine`` class body in ``tree`` for every method
    (direct child of the class — matches the nat-vent precedent's own scoping) that
    reads or writes ``self.<_FIELD>``, either via a direct ``ast.Attribute`` access
    or a ``getattr``/``setattr`` call whose string-literal argument equals
    ``_FIELD``. Returns method names, sorted, de-duplicated."""
    engine_class = _class_node(tree)
    methods: set[str] = set()
    for node in engine_class.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == _FIELD:
                methods.add(node.name)
                break
            if isinstance(sub, ast.Constant) and sub.value == _FIELD:
                methods.add(node.name)
                break
    return sorted(methods)


def _find_methods_touching_field_in_file() -> list[str]:
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    return _methods_touching_field(tree)


class TestComfortFamilyCoverageRegistry:
    def test_every_method_is_registered(self) -> None:
        found = set(_find_methods_touching_field_in_file())
        unregistered = found - set(_COVERAGE_REGISTRY)
        assert not unregistered, (
            f"New AutomationEngine method(s) found touching self.{_FIELD} but not "
            f"classified in _COVERAGE_REGISTRY: {sorted(unregistered)}. Add each as "
            f'"routed-through-fsm", "internal", or "exempted: <reason>" — see Issue '
            f"#827 Design §2's preserved-contract requirement for "
            f"self._comfort_mode_family."
        )

    def test_registry_has_no_stale_entries(self) -> None:
        """Catches a registry entry left behind after its method was removed or
        renamed — a stale entry would otherwise silently hide a real coverage gap
        forever (mirrors test_nat_vent_exit_lockout_coverage.py's own stale-entry
        check)."""
        found = set(_find_methods_touching_field_in_file())
        stale = set(_COVERAGE_REGISTRY) - found
        assert not stale, f"Registry references method(s) that no longer touch self.{_FIELD}: {sorted(stale)}."


class TestComfortFamilyCoverageScannerPositiveControl:
    """Proves the scanner actually catches an unregistered method, rather than
    trusting the real registry happens to be complete (CLAUDE.md's own instruction
    for this test shape: "add a temporary fake method in the positive-control test,
    don't rely on the real registry happening to be complete")."""

    def test_scanner_detects_unregistered_direct_attribute_access(self) -> None:
        source = """
class AutomationEngine:
    def __init__(self):
        self._comfort_mode_family = None

    def _totally_new_unregistered_method(self):
        return self._comfort_mode_family
"""
        tree = ast.parse(source)
        found = set(_methods_touching_field(tree))
        assert "_totally_new_unregistered_method" in found

        fake_registry = {"__init__": "internal"}
        unregistered = found - set(fake_registry)
        assert unregistered == {"_totally_new_unregistered_method"}, (
            "Scanner must flag a method touching self._comfort_mode_family that isn't in the registry"
        )

    def test_scanner_detects_unregistered_getattr_access(self) -> None:
        """getattr-defensive access (this codebase's convention for
        object.__new__()-partial-instantiated test doubles) must be caught too, not
        just a literal ``self._comfort_mode_family`` attribute node."""
        source = """
class AutomationEngine:
    def _another_new_method(self):
        return getattr(self, "_comfort_mode_family", None)
"""
        tree = ast.parse(source)
        found = set(_methods_touching_field(tree))
        assert found == {"_another_new_method"}

    def test_scanner_ignores_unrelated_methods(self) -> None:
        """A method that never touches the field must not be flagged — proves the
        scanner isn't just returning every method name unconditionally."""
        source = """
class AutomationEngine:
    def _unrelated_method(self):
        return self._some_other_field
"""
        tree = ast.parse(source)
        found = set(_methods_touching_field(tree))
        assert found == set()


class TestComfortFamilyRegistryClassificationsAreMeaningful:
    def test_no_bare_not_needed_exemptions(self) -> None:
        """Every 'exempted' entry must carry a specific reason, never a bare
        'not needed' (matches the registry's own documented convention above)."""
        for method, classification in _COVERAGE_REGISTRY.items():
            if classification.startswith("exempted"):
                assert classification != "exempted: not needed", (
                    f"{method}'s exemption reason must be specific, not a bare 'not needed'"
                )
                assert ":" in classification and len(classification.split(":", 1)[1].strip()) > 0, (
                    f"{method}'s exemption must include a reason after 'exempted:'"
                )

    def test_all_classifications_use_known_prefixes(self) -> None:
        _KNOWN_PREFIXES = ("routed-through-fsm", "internal", "exempted:")
        for method, classification in _COVERAGE_REGISTRY.items():
            assert classification.startswith(_KNOWN_PREFIXES), (
                f"{method}'s classification {classification!r} must start with one of {_KNOWN_PREFIXES}"
            )
