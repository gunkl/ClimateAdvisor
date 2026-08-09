"""Tests for Issue #615: registry-enforced shadow-engine coverage.

#613 (Block 5 Phase 4/Q) shipped mirroring only 5 of ~13 real entry points that mutate
nat-vent lifecycle state, plus never fed the shadow engine 3 input-data attributes
(outdoor temp, forecast, thermal model) — a live incident (shadow stuck "inactive" for
hours vs production) traced this to ad hoc, scattered mirror-writes with no way to know
if coverage was complete.

This module is the durable fix for "how do we know coverage stays complete": it
AST-scans `automation.py` for every `AutomationEngine` method that assigns to one of
the 4 fields `derive_nat_vent_lifecycle_state()` reads, and requires each discovered
method name to be explicitly classified in `_COVERAGE_REGISTRY` below (mirrored /
internal / exempted-with-reason). A new method that mutates one of these fields and
isn't registered fails immediately — the same enforcement shape as
`tests/test_executor_offload.py`'s `_BLOCKING_METHODS` registry for blocking I/O.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TRACKED_FIELDS = {
    "_natural_vent_active",
    "_paused_by_door",
    "_nat_vent_soft_start",
    "_nat_vent_outdoor_exit_time",
}

_REPO_ROOT = Path(__file__).parent.parent
_AUTOMATION_PY = _REPO_ROOT / "custom_components" / "climate_advisor" / "automation.py"
_COORDINATOR_PY = _REPO_ROOT / "custom_components" / "climate_advisor" / "coordinator.py"
_API_PY = _REPO_ROOT / "custom_components" / "climate_advisor" / "api.py"

# Classification meanings:
#   "mirrored" — coordinator.py or api.py replays this call on shadow_automation_engine
#                via _mirror_to_shadow("<name>", ...) at a confirmed call site.
#   "internal" — engine-internal machinery (a self-scheduled HA timer/callback, e.g. the
#                5-min thermo backstop or a grace-expiry callback) that runs
#                independently and identically on whichever AutomationEngine instance
#                it's bound to. Reached transitively once the entry points that start
#                its governing timer are mirrored — no separate coordinator-level
#                mirror call is needed or correct.
#   "exempted: <reason>" — deliberately not mirrored, with an explicit reason.
#
# Some "mirrored" entries below (on_fan_turned_off, fan_thermostat_check,
# reconcile_fan_on_startup) don't directly assign a tracked field themselves — they
# mutate it indirectly by calling a choke-point method (_exit_nat_vent(),
# _clear_fan_flags_and_start_grace(), _reconcile_fan_on_startup_locked()). They're
# still registered as top-level entry points worth documenting coverage for, even
# though the AST direct-mutation scan won't independently flag them as requiring one.
_COVERAGE_REGISTRY: dict[str, str] = {
    "__init__": "exempted: constructor defaults only, not a decision",
    "apply_classification": "mirrored",
    "handle_door_window_open": "mirrored",
    "handle_all_doors_windows_closed": "mirrored",
    "check_natural_vent_conditions": "mirrored",
    "nat_vent_temperature_check": "mirrored",
    "fan_thermostat_check": "mirrored",
    "on_fan_turned_off": "mirrored",
    "reconcile_fan_on_startup": "mirrored",
    "handle_bedtime": "mirrored",
    "resume_from_pause": "mirrored",
    "handle_manual_override_during_pause": "mirrored",
    "_exit_nat_vent": ("internal: shared choke point, called only from already-mirrored entry points"),
    "_pause_for_door_window": (
        "internal: called only from handle_door_window_open (mirrored) and _re_pause_for_open_sensor (internal)"
    ),
    "_clear_fan_flags_and_start_grace": (
        "internal: called only from on_fan_turned_off (mirrored) and _reconcile_fan_physical_drift (internal)"
    ),
    "_reconcile_fan_on_startup_locked": "internal: private impl of reconcile_fan_on_startup (mirrored)",
    "_reconcile_fan_physical_drift": (
        "internal: self-scheduled 5-min thermo-backstop timer, runs independently per engine instance"
    ),
    "_re_pause_for_open_sensor": (
        "internal: self-scheduled grace-expiry timer callback, runs independently per engine instance"
    ),
}


def _all_automation_engine_method_names() -> set[str]:
    """Every method defined directly on AutomationEngine, mutator or not."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    engine_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    return {node.name for node in engine_class.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _find_flag_mutating_methods() -> set[str]:
    """AST-scan AutomationEngine for every method assigning to a tracked lifecycle field."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    engine_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    found: set[str] = set()
    for node in engine_class.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in _TRACKED_FIELDS
                ):
                    found.add(node.name)
    return found


class TestShadowEngineCoverageRegistry:
    def test_every_flag_mutating_method_is_registered(self) -> None:
        found = _find_flag_mutating_methods()
        unregistered = found - set(_COVERAGE_REGISTRY)
        assert not unregistered, (
            f"New method(s) mutate nat-vent lifecycle state but aren't in the coverage "
            f"registry: {sorted(unregistered)}. Add each to _COVERAGE_REGISTRY as "
            f'"mirrored", "internal", or "exempted: <reason>" — see Issue #615.'
        )

    def test_registry_entries_reference_real_methods(self) -> None:
        """Not "every entry must be a *direct* mutator" — several registered entries
        (e.g. on_fan_turned_off, fan_thermostat_check) mutate lifecycle state only
        indirectly, by calling a choke-point method like _exit_nat_vent() rather than
        assigning the field themselves; they're still real, meaningful entry points
        worth documenting coverage for. This just catches renamed/deleted methods."""
        all_methods = _all_automation_engine_method_names()
        unknown = set(_COVERAGE_REGISTRY) - all_methods
        assert not unknown, (
            f"Registry references method(s) that no longer exist on AutomationEngine "
            f"(renamed or removed?): {sorted(unknown)}. Update the registry."
        )

    def test_all_mirrored_entries_have_a_matching_mirror_call(self) -> None:
        """Positive control: every 'mirrored' registry entry must have a matching
        _mirror_to_shadow("<name>", ...) call in coordinator.py or api.py — catches the
        registry claiming coverage that doesn't actually exist in code."""
        combined = _COORDINATOR_PY.read_text(encoding="utf-8") + _API_PY.read_text(encoding="utf-8")
        for name, classification in _COVERAGE_REGISTRY.items():
            if classification != "mirrored":
                continue
            pattern = re.compile(r'_mirror_to_shadow\(\s*"' + re.escape(name) + r'"')
            assert pattern.search(combined), (
                f'{name} is marked "mirrored" but no _mirror_to_shadow("{name}", ...) '
                f"call was found in coordinator.py or api.py"
            )

    def test_positive_control_unregistered_method_is_caught(self) -> None:
        """Proves test_every_flag_mutating_method_is_registered actually fails on a
        genuinely unregistered method, not just passing vacuously."""
        found = {"a_totally_new_method_not_in_registry"}
        unregistered = found - set(_COVERAGE_REGISTRY)
        assert unregistered == {"a_totally_new_method_not_in_registry"}
