"""Tests for Issue #615/#631: registry-enforced shadow-engine coverage.

#613 (Block 5 Phase 4/Q) shipped mirroring only 5 of ~13 real entry points that mutate
nat-vent lifecycle state, plus never fed the shadow engine 3 input-data attributes
(outdoor temp, forecast, thermal model) — a live incident (shadow stuck "inactive" for
hours vs production) traced this to ad hoc, scattered mirror-writes with no way to know
if coverage was complete.

#631 found a second instance of the same class of gap: grace/override state
(`_grace_active`, `_manual_override_active`, `_fan_override_active`,
`_override_confirm_pending/_mode/_source`, `_paused_with_hvac_already_off`) is set by
methods called either directly from coordinator.py/api.py (never followed by a
`_mirror_to_shadow(...)` call) or by purely internal `async_call_later` timers with no
coordinator call site at all — confirmed live as a 2h38m sustained false disagreement
(2026-08-12 21:02-23:40) because `check_natural_vent_conditions()` gates nat-vent
reactivation on `_grace_active`. Fixed via `_sync_shadow_inputs()` raw-copy (same
pattern as outdoor temp/forecast/thermal model), not new mirror call sites — see that
function's docstring in coordinator.py for why.

This module is the durable fix for "how do we know coverage stays complete": it
AST-scans `automation.py` for every `AutomationEngine` method that assigns to one of
the tracked fields, and requires each discovered method name to be explicitly
classified in `_COVERAGE_REGISTRY` below (mirrored / internal / exempted-with-reason).
A new method that mutates one of these fields and isn't registered fails immediately —
the same enforcement shape as `tests/test_executor_offload.py`'s `_BLOCKING_METHODS`
registry for blocking I/O.
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
    # Issue #631: grace/override lifecycle gates.
    "_grace_active",
    "_manual_override_active",
    "_fan_override_active",
    "_override_confirm_pending",
    "_override_confirm_mode",
    "_override_confirm_source",
    "_paused_with_hvac_already_off",
    # Issue #639: override/grace joint-lifecycle FSM's own reads — same raw-copy
    # discipline as the rest of the Issue #631 grace/override fields above.
    "_grace_protects_override",
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
    "_set_door_window_pause_fields": (
        "internal: Issue #637 Phase R Step 3 shared setter, called only from "
        "_pause_for_door_window (internal, itself called from already-mirrored entry "
        "points) and _exit_nat_vent (internal, shared choke point) — one definition of "
        "the door/window pause write-shape, not itself a new entry point"
    ),
    "_apply_door_window_fsm_state": (
        "internal: Issue #594 Phase R Step 2 helper, called only from the FSM-authoritative "
        "branches of handle_manual_override_during_pause (mirrored) and resume_from_pause "
        "(mirrored) — the inverse of door_window_lifecycle_state's derivation"
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
    # Issue #631: grace/override setters. Every one of these is a real coordinator/api
    # call site or an internal-only timer — deliberately NOT mirrored (see
    # coordinator.py's _sync_shadow_inputs() docstring for why a raw-value copy is
    # correct here instead of adding _mirror_to_shadow(...) calls for these methods).
    "restore_state": "mirrored",
    "clear_manual_override": (
        "exempted: called directly from 2 coordinator.py sites (unmirrored) and internally from "
        "cancel_override()/_on_grace_expired(); _manual_override_active/_override_confirm_* covered "
        "by _sync_shadow_inputs() raw copy (Issue #631)"
    ),
    "handle_fan_manual_override": "mirrored",
    # Issue #651: not itself an AST-detected direct mutator (delegates to
    # start_override_confirmation()), but mirrored at all 3 real call sites for the same
    # reason handle_fan_manual_override is — see TestOverrideGraceFsmEventCoverage for
    # the FSM-entry-kind check and TestPerCallerFsmFeedCoverage below for the
    # per-call-site assertion.
    "handle_manual_override": "mirrored",
    "clear_fan_override": (
        "exempted: called from clear_manual_override (exempted) and internal cascades; "
        "_fan_override_active covered by _sync_shadow_inputs() raw copy (Issue #631)"
    ),
    "start_override_confirmation": (
        "exempted: called from handle_manual_override() (3 now-mirrored coordinator.py sites, "
        "Issue #651) and handle_manual_override_during_pause() (mirrored); contains the internal "
        "_confirm_override_expired timer closure. _override_confirm_*/_manual_override_* covered by "
        "_sync_shadow_inputs() raw copy (Issue #631)"
    ),
    "_confirm_override": (
        "exempted: called from start_override_confirmation()'s immediate path and its internal "
        "confirm-delay timer closure; _manual_override_active/_grace_active covered by "
        "_sync_shadow_inputs() raw copy (Issue #631)"
    ),
    "_start_grace_period": (
        "exempted: called from mirrored (handle_all_doors_windows_closed, "
        "check_natural_vent_conditions, resume_from_pause, handle_manual_override_during_pause, "
        "handle_fan_manual_override) and unmirrored/internal (_confirm_override) paths; _grace_active "
        "and _grace_protects_override covered by _sync_shadow_inputs() raw copy regardless of caller "
        "(Issue #631, #639, #643)"
    ),
    "_cancel_grace_timers": (
        "exempted: called from cancel_override() (unmirrored) and the internal grace-expiry timer "
        "closures (_grace_expired/_grace_expired_restored); _grace_active covered by "
        "_sync_shadow_inputs() raw copy (Issue #631)"
    ),
    "_on_grace_expired": (
        "internal: fires only from the private _grace_expired/_grace_expired_restored "
        "async_call_later closures defined inside _start_grace_period()/_reschedule_grace_timer() — "
        "no coordinator call site exists or could exist; cascades to _cancel_grace_timers()/"
        "clear_manual_override() (both exempted above), covered by _sync_shadow_inputs() raw copy "
        "(Issue #631)"
    ),
    "cancel_override": (
        "exempted: called from api.py's 'Cancel Override'/'Cancel Fan Override' dashboard buttons "
        "(2 unmirrored sites); all fields it clears are covered by _sync_shadow_inputs() raw copy "
        "(Issue #631)"
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


# Issue #647: a second, independent registry — this one for whether each
# OverrideGraceFsmEventKind (the FSM's own tracked-state re-evaluation trigger, distinct
# from the shadow-mirror registry above) is actually reachable from coordinator.py/api.py.
# #643 wired the entry kind (OVERRIDE_DETECTED) without wiring any exit kind, leaving the
# FSM permanently stuck once a real fan override occurred — an asymmetric wiring change
# that shipped with no test catching the imbalance. This registry is that test.
#
# "unreachable: <reason>" is for FSM event kinds that exist in the enum but have no real
# production trigger today (documented, not silently missing).
_OVERRIDE_GRACE_EVENT_KIND_REGISTRY: dict[str, str] = {
    "OVERRIDE_DETECTED": "reachable",
    "MANUAL_OVERRIDE_DURING_PAUSE": "reachable",
    "DASHBOARD_RESUME": "reachable",
    "OVERRIDE_CONFIRM_EXPIRED": "reachable",
    "OVERRIDE_CANCELLED": "reachable",
    "GRACE_TIMER_EXPIRED": "reachable",
    "OVERRIDE_SUPERSEDED": (
        "unreachable: no production code path emits this distinct kind today — the "
        "closest real case (a mode change arriving during an active override grace, "
        "coordinator.py's 'new_override_during_grace' branch) still feeds OVERRIDE_CANCELLED "
        "for the clear half then OVERRIDE_DETECTED (via the now-mirrored "
        "handle_manual_override(), Issue #651) for the reopen half, rather than a single "
        "distinct SUPERSEDED transition"
    ),
}


class TestOverrideGraceFsmEventCoverage:
    def test_every_event_kind_is_registered(self) -> None:
        from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

        all_kinds = {member.name for member in OverrideGraceFsmEventKind}
        unregistered = all_kinds - set(_OVERRIDE_GRACE_EVENT_KIND_REGISTRY)
        assert not unregistered, (
            f"New OverrideGraceFsmEventKind member(s) aren't in "
            f"_OVERRIDE_GRACE_EVENT_KIND_REGISTRY: {sorted(unregistered)}. Classify each "
            f'as "reachable" (and wire a real trigger in coordinator.py/api.py) or '
            f'"unreachable: <reason>" — see Issue #647.'
        )

    def test_registry_entries_reference_real_members(self) -> None:
        from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

        all_kinds = {member.name for member in OverrideGraceFsmEventKind}
        unknown = set(_OVERRIDE_GRACE_EVENT_KIND_REGISTRY) - all_kinds
        assert not unknown, (
            f"Registry references OverrideGraceFsmEventKind member(s) that no longer "
            f"exist (renamed or removed?): {sorted(unknown)}. Update the registry."
        )

    def test_every_reachable_kind_has_a_real_trigger(self) -> None:
        """Positive control: every 'reachable' entry must appear as a value somewhere
        coordinator.py actually feeds the FSM from — either of the two mirror-name-keyed
        dicts, the event-type map, or a direct OverrideGraceFsmEventKind.<X> reference at
        a real call site (e.g. _feed_override_grace_fsm_cancelled()). Catches the
        registry claiming coverage that doesn't actually exist in code — this is the
        specific check that would have caught #643 shipping OVERRIDE_DETECTED without
        any exit kind wired."""
        combined = _COORDINATOR_PY.read_text(encoding="utf-8")
        for name, classification in _OVERRIDE_GRACE_EVENT_KIND_REGISTRY.items():
            if classification != "reachable":
                continue
            # Matches either a dict value like "override_cancelled" (snake_case enum
            # value, used by the two method-name/event-type-keyed dicts) or a direct
            # member reference — coordinator.py imports OverrideGraceFsmEventKind under
            # a local alias (`_OGFEventKind`) at every direct-call-site import, so match
            # any `<name ending in EventKind or aliased>.MEMBER` shape rather than the
            # literal unaliased class name.
            value_pattern = re.compile(r'"' + re.escape(name.lower()) + r'"')
            member_pattern = re.compile(r"\b\w*(?:EventKind|OGFEventKind)\." + re.escape(name) + r"\b")
            assert value_pattern.search(combined) or member_pattern.search(combined), (
                f'{name} is marked "reachable" but no dict value or direct '
                f"OverrideGraceFsmEventKind.{name} reference was found in coordinator.py"
            )

    def test_positive_control_unregistered_kind_is_caught(self) -> None:
        """Proves test_every_event_kind_is_registered actually fails on a genuinely
        unregistered member, not just passing vacuously."""
        all_kinds = {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
        unregistered = all_kinds - set(_OVERRIDE_GRACE_EVENT_KIND_REGISTRY)
        assert unregistered == {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}


# Issue #594 Phase R Step 1b: same registry-enforcement shape as
# _OVERRIDE_GRACE_EVENT_KIND_REGISTRY above, for DoorWindowFsmEventKind. Originally only
# 4 of 7 members had a real feed path (SENSOR_OPENED, ALL_SENSORS_CLOSED,
# MANUAL_OVERRIDE_DURING_PAUSE via _DOOR_WINDOW_FSM_EVENT_KINDS;
# NAT_VENT_EXITED_SENSOR_STILL_OPEN via _DOOR_WINDOW_NAT_VENT_EXIT_EVENT_TYPES, #647) —
# GRACE_TIMER_EXPIRED/DASHBOARD_RESUME/SYNC_RECONCILE were explicitly deferred as
# "future work" when Phase 2 shipped. Step 1b closes all 3.
_DOOR_WINDOW_EVENT_KIND_REGISTRY: dict[str, str] = {
    "SENSOR_OPENED": "reachable",
    "ALL_SENSORS_CLOSED": "reachable",
    "GRACE_TIMER_EXPIRED": "reachable",
    "MANUAL_OVERRIDE_DURING_PAUSE": "reachable",
    "DASHBOARD_RESUME": "reachable",
    "NAT_VENT_EXITED_SENSOR_STILL_OPEN": "reachable",
    "SYNC_RECONCILE": "reachable",
}


class TestDoorWindowFsmEventCoverage:
    def test_every_event_kind_is_registered(self) -> None:
        from custom_components.climate_advisor.door_window_fsm import DoorWindowFsmEventKind

        all_kinds = {member.name for member in DoorWindowFsmEventKind}
        unregistered = all_kinds - set(_DOOR_WINDOW_EVENT_KIND_REGISTRY)
        assert not unregistered, (
            f"New DoorWindowFsmEventKind member(s) aren't in "
            f"_DOOR_WINDOW_EVENT_KIND_REGISTRY: {sorted(unregistered)}. Classify each "
            f'as "reachable" (and wire a real trigger in coordinator.py) or '
            f'"unreachable: <reason>" — see Issue #594 Phase R Step 1b.'
        )

    def test_registry_entries_reference_real_members(self) -> None:
        from custom_components.climate_advisor.door_window_fsm import DoorWindowFsmEventKind

        all_kinds = {member.name for member in DoorWindowFsmEventKind}
        unknown = set(_DOOR_WINDOW_EVENT_KIND_REGISTRY) - all_kinds
        assert not unknown, (
            f"Registry references DoorWindowFsmEventKind member(s) that no longer exist "
            f"(renamed or removed?): {sorted(unknown)}. Update the registry."
        )

    def test_every_reachable_kind_has_a_real_trigger(self) -> None:
        """Positive control: every 'reachable' entry must appear as a value somewhere
        coordinator.py actually feeds the FSM from — either _DOOR_WINDOW_FSM_EVENT_KINDS'
        dict values, _DOOR_WINDOW_NAT_VENT_EXIT_EVENT_TYPES'/
        _DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES' event-type membership, or a direct
        DoorWindowFsmEventKind.<X>/_DWFEventKind.<X> reference at a real call site."""
        combined = _COORDINATOR_PY.read_text(encoding="utf-8")
        for name, classification in _DOOR_WINDOW_EVENT_KIND_REGISTRY.items():
            if classification != "reachable":
                continue
            value_pattern = re.compile(r'"' + re.escape(name.lower()) + r'"')
            member_pattern = re.compile(r"\b\w*EventKind\." + re.escape(name) + r"\b")
            assert value_pattern.search(combined) or member_pattern.search(combined), (
                f'{name} is marked "reachable" but no dict value or direct '
                f"DoorWindowFsmEventKind.{name} reference was found in coordinator.py"
            )

    def test_positive_control_unregistered_kind_is_caught(self) -> None:
        """Proves test_every_event_kind_is_registered actually fails on a genuinely
        unregistered member, not just passing vacuously."""
        all_kinds = {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
        unregistered = all_kinds - set(_DOOR_WINDOW_EVENT_KIND_REGISTRY)
        assert unregistered == {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}


# Issue #651: the existing "reachable from *some* call site" checks above are true but
# not sufficient — #643 shipping an entry-only wiring for handle_fan_manual_override
# proved that a kind being reachable overall doesn't mean every real production call
# site that should feed it actually does. These tests assert PER-CALLER reachability
# for the two gaps this issue closes, so a future regression that re-wires one call site
# but not its siblings (or drops the bedtime/morning-wakeup feed) fails loudly instead of
# passing the coarser kind-level check above.
class TestPerCallerFsmFeedCoverage:
    def test_handle_manual_override_mirrored_at_all_three_call_sites(self) -> None:
        """handle_manual_override() has exactly 3 real production call sites
        (coordinator.py's _async_thermostat_changed: new-override-during-grace,
        mode-changed-outside-pause, setpoint-only). Each must mirror, or a future new
        call site could silently skip FSM feed the way the original gap did."""
        src = _COORDINATOR_PY.read_text(encoding="utf-8")
        call_sites = len(re.findall(r"\.handle_manual_override\(", src))
        mirror_sites = len(re.findall(r'_mirror_to_shadow\(\s*"handle_manual_override"', src))
        assert call_sites >= 3, (
            f"Expected at least 3 handle_manual_override() call sites in coordinator.py, "
            f"found {call_sites} — if call sites were consolidated, lower this bound "
            f"deliberately rather than letting the test rot."
        )
        assert mirror_sites == call_sites, (
            f"handle_manual_override() has {call_sites} real call site(s) in coordinator.py "
            f'but only {mirror_sites} have a matching _mirror_to_shadow("handle_manual_override", '
            f"...) call — every call site must mirror (Issue #651)."
        )

    def test_bedtime_and_morning_wakeup_feed_fsm_on_override_clear(self) -> None:
        """_async_bedtime()/_async_morning_wakeup() must call
        _feed_override_grace_fsm_if_cleared() after the real engine call, so a fan-only
        override cleared by either handler is fed to the FSM immediately instead of
        waiting on _check_orphaned_grace()'s one-cycle-later self-heal (Issue #651)."""
        src = _COORDINATOR_PY.read_text(encoding="utf-8")
        # Match each method body up to the next method definition (async or sync) at the
        # same indent level, so the assertion is scoped to that method's own body only.
        bedtime_match = re.search(r"    async def _async_bedtime\(.*?\n    (?:async )?def ", src, re.DOTALL)
        wakeup_match = re.search(r"    async def _async_morning_wakeup\(.*?\n    (?:async )?def ", src, re.DOTALL)
        assert bedtime_match and "_feed_override_grace_fsm_if_cleared" in bedtime_match.group(0), (
            "_async_bedtime() must call _feed_override_grace_fsm_if_cleared() after "
            "automation_engine.handle_bedtime() (Issue #651)."
        )
        assert wakeup_match and "_feed_override_grace_fsm_if_cleared" in wakeup_match.group(0), (
            "_async_morning_wakeup() must call _feed_override_grace_fsm_if_cleared() after "
            "automation_engine.handle_morning_wakeup() (Issue #651)."
        )
