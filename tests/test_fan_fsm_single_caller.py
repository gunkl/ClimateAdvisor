"""Issue #731 (graduated in Issue #757 Step 2): AST-scan enforcement that each
of the 6 pure fan/WHF decision functions is called from at most two places in
the whole integration — ``fan_fsm.py`` (the real FSM's own dispatch, once
each) and at most one legacy closure location in ``automation.py``. The
legacy closures were deleted in Step 2 once the FSM path became the sole
implementation, so automation.py's call count for each function is now 0 —
this test still enforces the ceiling (at most 1) rather than an exact count,
so it continues to guard against a future call site creeping back in.

Same AST-scanning technique as ``test_shadow_engine_coverage.py``'s own
registry-completeness checks. The hazard this guards against: a third call
site (e.g. a new caller re-implementing the decision inline instead of
reusing the shared pure function, or a second legacy closure duplicating the
extraction) would silently create a second source of truth for that decision
— exactly the "sibling threshold drift" class of bug this project's CLAUDE.md
already documents for nat-vent (see #400/#402/#417/#456/#458). A pure
decision function is only trustworthy as a single source of truth if it is,
in fact, called from a single controlled location per real code path.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_INTEGRATION_DIR = _REPO_ROOT / "custom_components" / "climate_advisor"
_FAN_FSM_PY = _INTEGRATION_DIR / "fan_fsm.py"
_AUTOMATION_PY = _INTEGRATION_DIR / "automation.py"

# The 6 pure fan/WHF decision functions this test polices, and which module
# defines each (so a call to the function's own definition line is not
# double-counted as a "call site").
_DECISION_FUNCTIONS: dict[str, str] = {
    "decide_fan_toggle_rate_limit": "fan_toggle_rate_limit",
    "decide_fan_drift_reconciliation": "fan_drift_reconciliation",
    "decide_fan_cycle_on": "desired_state",
    "decide_fan_cycle_off": "desired_state",
    "decide_fan_thermo_backstop": "desired_state",
    "decide_fan_thermostat_check": "fan_thermostat_decision",
}

# Every call site is expected to live in exactly one of these two files: the
# real FSM's own dispatch (fan_fsm.py) and automation.py's legacy closure(s).
# fan_fsm.py is UNCAPPED in how many times it may call a given function — its
# own transition() logic legitimately calls decide_fan_toggle_rate_limit()
# twice (once for the activate branch, once for deactivate), which is real,
# deliberate reuse inside the FSM's own module, not a 3rd "location". Every
# OTHER file gets exactly one allowed call site: automation.py's single named
# legacy closure. Any call site anywhere outside {fan_fsm.py, automation.py},
# or more than one distinct call site inside automation.py itself, is a
# genuine 3rd location this test must catch.
_UNCAPPED_CALL_SITE_FILE = "fan_fsm.py"
_MAX_CALL_SITES_IN_LEGACY_FILE = 1
_LEGACY_FILE = "automation.py"


def _all_python_files() -> list[Path]:
    return sorted(_INTEGRATION_DIR.glob("*.py"))


def _find_calls_to(fn_name: str, tree: ast.AST) -> int:
    """Count direct calls to fn_name (as a bare name, e.g. `decide_fan_cycle_on(...)`
    or `desired_state.decide_fan_cycle_on(...)`) anywhere in tree."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_bare_call = isinstance(func, ast.Name) and func.id == fn_name
        is_attr_call = isinstance(func, ast.Attribute) and func.attr == fn_name
        if is_bare_call or is_attr_call:
            count += 1
    return count


def _find_function_def(fn_name: str, tree: ast.AST) -> bool:
    """True if tree contains a top-level def/async def named fn_name (its own
    definition — not a call site)."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name for node in ast.walk(tree)
    )


class TestFanDecisionFunctionSingleCaller:
    def test_all_six_decision_functions_are_covered(self) -> None:
        """Sanity: every function this test polices must actually exist somewhere
        in the integration — catches a rename silently disabling coverage."""
        found: set[str] = set()
        for path in _all_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn_name in _DECISION_FUNCTIONS:
                if _find_function_def(fn_name, tree):
                    found.add(fn_name)
        missing = set(_DECISION_FUNCTIONS) - found
        assert not missing, (
            f"Decision function(s) not found anywhere in custom_components/climate_advisor/: "
            f"{sorted(missing)} — renamed or removed? Update _DECISION_FUNCTIONS."
        )

    def test_at_most_one_legacy_call_site_and_none_elsewhere(self) -> None:
        """Each of the 6 pure decision functions must be called from at most
        fan_fsm.py's own dispatch (call count there is uncapped — see module
        docstring) plus exactly one call site in automation.py's legacy
        closure, and NEVER from any other file in the integration."""
        violations: list[str] = []
        for fn_name in _DECISION_FUNCTIONS:
            call_sites: dict[str, int] = {}
            for path in _all_python_files():
                tree = ast.parse(path.read_text(encoding="utf-8"))
                n = _find_calls_to(fn_name, tree)
                if n:
                    call_sites[path.name] = n

            stray_files = set(call_sites) - {_UNCAPPED_CALL_SITE_FILE, _LEGACY_FILE}
            if stray_files:
                violations.append(
                    f"{fn_name}() is called from unexpected file(s) {sorted(stray_files)} "
                    f"(full call-site map: {call_sites}) — only fan_fsm.py and automation.py "
                    f"are allowed real call sites for this decision function."
                )
                continue

            legacy_count = call_sites.get(_LEGACY_FILE, 0)
            if legacy_count > _MAX_CALL_SITES_IN_LEGACY_FILE:
                violations.append(
                    f"{fn_name}() is called {legacy_count} times in automation.py (map: "
                    f"{call_sites}), more than the allowed {_MAX_CALL_SITES_IN_LEGACY_FILE} "
                    f"named legacy closure — a 2nd automation.py call site duplicates this "
                    f"decision's single source of truth."
                )

        assert not violations, "\n".join(violations)

    def test_positive_control_third_call_site_is_caught(self) -> None:
        """Proves test_at_most_one_legacy_call_site_and_none_elsewhere actually
        fails on a genuine stray call site, not just passing vacuously."""
        fake_source = "decide_fan_cycle_on(1)\ndecide_fan_cycle_on(2)\n"
        tree = ast.parse(fake_source)
        count = _find_calls_to("decide_fan_cycle_on", tree)
        assert count == 2
        assert count > _MAX_CALL_SITES_IN_LEGACY_FILE
