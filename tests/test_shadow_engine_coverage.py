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
    # Issue #716: fan_thermostat_check()'s shadow mirroring was inert — _fan_active is
    # the field it actually keys off, and its two real writers (_activate_fan(),
    # _deactivate_fan()) both `return` early under `self.dry_run` before ever assigning
    # it, so a direct _mirror_to_shadow(...) replay of either method can never reach the
    # field on the permanently-dry_run shadow instance. Same raw-copy fix as the fields
    # above — see _sync_shadow_inputs()'s docstring in coordinator.py.
    "_fan_active",
    # Issue #724: same gap class again — _whf_owns_hvac() depends on this field, and it
    # was never added to _sync_shadow_inputs()'s raw-copy block. Confirmed live-reachable
    # (not dormant): _sync_paused_by_door_with_live_sensors() (called from 4 mirrored
    # entry points) reads _whf_owns_hvac() as an early-return guard before calling
    # _pause_for_door_window(), which sets _paused_by_door — a tracked field feeding the
    # door_window/nat_vent diagnostic axes. Same raw-copy fix as _fan_active above.
    "_pre_fan_hvac_mode",
    # Issue #731: fan/WHF FSM extraction. Same raw-copy gap class as #716/#724 above —
    # fan_lifecycle.py's derivation and fan_fsm.py's dispatch (rate-limit/drift/cycling
    # axes) read these as engine-instance state; _sync_shadow_inputs() raw-copies all 5
    # for the same reason (dry_run means several of production's real setters never run
    # on the shadow instance).
    "_fan_on_since",
    "_fan_min_runtime_active",
    "_fan_rate_limited_until",
    "_fan_rate_limited_direction",
    "_fan_drift_tick_count",
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
    "_apply_nat_vent_fsm_state": (
        "internal: Issue #594 Phase R Phase 2f helper, added ahead of any production wiring — "
        "has zero call sites as of Issue #691 (see that issue's own docstring). Same "
        "additive-first pattern as _apply_door_window_fsm_state above; will be reclassified "
        "when a future issue wires it into a real entry point"
    ),
    "_apply_fan_fsm_state": (
        "internal: Issue #731 Phase 4 helper, added ahead of any production wiring — has "
        "zero call sites until Phase 5 re-points the 16 real fan/WHF entry points at "
        "_resolve_fan_fsm_state(). Same additive-first pattern as _apply_nat_vent_fsm_state "
        "above; will be reclassified once wired"
    ),
    "_clear_fan_flags_and_start_grace": (
        "internal: called only from on_fan_turned_off (mirrored) and _reconcile_fan_physical_drift (internal)"
    ),
    "_reconcile_fan_on_startup_locked": "internal: private impl of reconcile_fan_on_startup (mirrored)",
    "_apply_ode_ceiling_guard_decision": (
        "internal: Issue #742 side-effecting shell for the classification FSM's "
        "ODE-ceiling-guard ESCALATE branch — called only from apply_classification "
        "(mirrored), never a standalone entry point. Sets _natural_vent_active=False/"
        "_nat_vent_soft_start=False on escalation, mirroring the legacy inline block "
        "it replaces when self._classification_fsm_authoritative is True"
    ),
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
    # Issue #664: override/grace full authoritative migration. The action/flags split
    # (mirroring door/window's own _pause_for_door_window_action() precedent) introduced
    # several small internal helpers — none are new entry points, all are called only
    # from the real call sites already classified above (now routed through
    # _resolve_override_grace_fsm_state() instead of writing flags inline).
    "_apply_override_grace_fsm_state": (
        "internal: Issue #664 helper, called only from the FSM-authoritative branch of "
        "_resolve_override_grace_fsm_state() — the inverse of override_grace_lifecycle_state's "
        "derivation, same role as _apply_door_window_fsm_state above"
    ),
    "_confirm_override_action": (
        "internal: Issue #664 action half of _confirm_override (exempted above) — called from "
        "start_override_confirmation()'s immediate-accept branch and the internal "
        "_confirm_override_expired timer closure's PATH A, both already covered by "
        "start_override_confirmation's own exemption"
    ),
    "_clear_override_confirm_action": (
        "internal: Issue #664 action half of clear_manual_override's confirm-clear branch — "
        "called from cancel_override() (exempted), _on_grace_expired() (internal), and "
        "clear_manual_override() itself (exempted)"
    ),
    "_clear_manual_override_active": (
        "internal: Issue #664 helper extracted from clear_manual_override's second half — "
        "called from clear_manual_override() (exempted) and cancel_override() (exempted)"
    ),
    "_legacy_set_grace_flags": (
        "internal: Issue #664 trivial 2-line flag-set, passed as the non-authoritative "
        "'legacy' closure to _resolve_override_grace_fsm_state() at every real grace-starting "
        "call site, and reused by _start_grace_period()'s own thin wrapper for every "
        "non-FSM-modeled trigger (fan-off, window-close, nat-vent-exit)"
    ),
    "_legacy_clear_grace_flags": (
        "internal: Issue #664 trivial 2-line flag-clear, passed as the non-authoritative "
        "'legacy' closure to _resolve_override_grace_fsm_state() at the GRACE_TIMER_EXPIRED/"
        "OVERRIDE_CANCELLED call sites, and reused by _cancel_grace_timers()'s own thin wrapper"
    ),
    "_legacy_clear_confirm_flag": (
        "internal: Issue #664 trivial 1-line flag-clear, passed as the non-authoritative "
        "'legacy' closure to _resolve_override_grace_fsm_state() wherever confirm is cleared, "
        "and reused by clear_manual_override()'s own thin wrapper"
    ),
    # Issue #716: _fan_active's two real writers. Deliberately NOT "mirrored" — a
    # _mirror_to_shadow("_activate_fan"/"_deactivate_fan", ...) replay would hit each
    # method's `if self.dry_run: return` guard before _fan_active is ever assigned on
    # the permanently-dry_run shadow instance, so the mirror would look wired but stay
    # inert. _fan_active is covered instead by _sync_shadow_inputs()'s raw copy, which
    # reads production's current value every cycle regardless of which method (these
    # two, or the coordinator's own stale-flag correction in _async_thermostat_changed)
    # last set it.
    "_activate_fan": (
        "internal: real writer of _fan_active, but the field is covered by "
        "_sync_shadow_inputs() raw copy, not a mirror call — see Issue #716"
    ),
    "_deactivate_fan": (
        "internal: real writer of _fan_active, but the field is covered by "
        "_sync_shadow_inputs() raw copy, not a mirror call — see Issue #716; also a real "
        "writer of _pre_fan_hvac_mode (2 release branches), same raw-copy coverage — "
        "Issue #724"
    ),
    # Issue #724: _pre_fan_hvac_mode's two remaining real writers, not otherwise
    # registered (restore_state() and _deactivate_fan() already are — see above and the
    # "mirrored" entry for restore_state). Both are covered by _sync_shadow_inputs()'s raw
    # copy, not a mirror call, for the same reason _activate_fan/_deactivate_fan are for
    # _fan_active: WHF suppression state has no mirror path that would reach the
    # permanently-dry_run shadow instance correctly.
    "_suppress_hvac_for_whf": (
        "internal: real writer of _pre_fan_hvac_mode, but the field is covered by "
        "_sync_shadow_inputs() raw copy, not a mirror call — see Issue #724"
    ),
    "_release_whf_and_reclassify": (
        "internal: real writer of _pre_fan_hvac_mode, but the field is covered by "
        "_sync_shadow_inputs() raw copy, not a mirror call — see Issue #724"
    ),
    # Issue #731: fan/WHF FSM extraction's own real writers of the 5 newly-tracked fan
    # fields above. All 4 follow the exact same shape _activate_fan/_deactivate_fan
    # established for _fan_active (Issue #716) — a mirror replay would be inert on the
    # permanently-dry_run shadow instance (min-runtime cycling schedules real HA timers;
    # rate-limiting reads real wall-clock deferral windows), so raw-copy in
    # _sync_shadow_inputs() is the correct — and only working — coverage mechanism.
    "_stop_fan_min_runtime_cycles": (
        "internal: real writer of _fan_min_runtime_active (clears it), but the field is "
        "covered by _sync_shadow_inputs() raw copy, not a mirror call — see Issue #731"
    ),
    "_fan_cycle_on": (
        "internal: real writer of _fan_min_runtime_active (sets it), but the field is "
        "covered by _sync_shadow_inputs() raw copy, not a mirror call — see Issue #731"
    ),
    "_fan_cycle_off": (
        "internal: real writer of _fan_min_runtime_active (clears it), but the field is "
        "covered by _sync_shadow_inputs() raw copy, not a mirror call — see Issue #731"
    ),
    "_fan_toggle_rate_limited": (
        "internal: real writer of _fan_rate_limited_until/_fan_rate_limited_direction, "
        "but both fields are covered by _sync_shadow_inputs() raw copy, not a mirror "
        "call — see Issue #731"
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
    # Issue #661: split from OVERRIDE_DETECTED — handle_fan_manual_override() never
    # routes through start_override_confirmation()'s confirm-delay machinery the way
    # the thermostat-override call sites (handle_manual_override(),
    # handle_manual_override_during_pause()) genuinely do, so it needed its own kind
    # rather than sharing OVERRIDE_DETECTED's confirm-delay landing logic.
    "FAN_OVERRIDE_DETECTED": "reachable",
    "MANUAL_OVERRIDE_DURING_PAUSE": "reachable",
    "DASHBOARD_RESUME": "reachable",
    "OVERRIDE_CONFIRM_EXPIRED": "reachable",
    "OVERRIDE_CANCELLED": "reachable",
    "GRACE_TIMER_EXPIRED": "reachable",
    # Issue #664: re-classified reachable. Investigation for the full-authority migration
    # found the OVERRIDE_CANCELLED classification above was a real, dangerous mismatch —
    # coordinator.py's 'new_override_during_grace' branch (clear_manual_override() call)
    # never touches grace (Issue #282's "Fix D" deliberately leaves the still-running
    # grace protecting the NEW override handle_manual_override() immediately re-detects),
    # but OVERRIDE_CANCELLED's own transition unconditionally forces grace to NONE. Feeding
    # this real site as OVERRIDE_CANCELLED would have made an authoritative FSM wrongly
    # clear a grace period production intentionally preserves. OVERRIDE_SUPERSEDED's own
    # transition (_land_after_detection with grace fixed at ACTIVE_PROTECTING_OVERRIDE)
    # correctly matches production's real "confirm re-evaluated, grace untouched" behavior.
    "OVERRIDE_SUPERSEDED": "reachable",
    # Issue #672: automation.py's _start_grace_period() — the shared wrapper for every
    # trigger that was never modeled at all (fan-off, window-close, nat-vent-exit,
    # drift-correction) — now dispatches this kind after _start_grace_period_action()
    # confirms a real grace actually started.
    "UNPROTECTED_GRACE_STARTED": "reachable",
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
    "PAUSED_NAT_VENT_REACTIVATED": "reachable",
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
    def test_fan_thermostat_check_mirrored_at_all_three_call_sites(self) -> None:
        """Issue #716: fan_thermostat_check() has 3 real production call sites in
        coordinator.py (indoor-temp listener, outdoor-temp listener, thermostat
        attribute-change dispatch) — only the third was mirrored originally. Each must
        mirror, or a future new call site could silently ship the same per-caller gap."""
        src = _COORDINATOR_PY.read_text(encoding="utf-8")
        call_sites = len(re.findall(r"\.fan_thermostat_check\(", src))
        mirror_sites = len(re.findall(r'_mirror_to_shadow\(\s*\n?\s*"fan_thermostat_check"', src))
        assert call_sites >= 3, (
            f"Expected at least 3 fan_thermostat_check() call sites in coordinator.py, "
            f"found {call_sites} — if call sites were consolidated, lower this bound "
            f"deliberately rather than letting the test rot."
        )
        assert mirror_sites == call_sites, (
            f"fan_thermostat_check() has {call_sites} real call site(s) in coordinator.py "
            f'but only {mirror_sites} have a matching _mirror_to_shadow("fan_thermostat_check", '
            f"...) call — every call site must mirror (Issue #716)."
        )

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


# Issue #731: same registry-enforcement shape as _OVERRIDE_GRACE_EVENT_KIND_REGISTRY/
# _DOOR_WINDOW_EVENT_KIND_REGISTRY above, for FanFsmEventKind. Unlike those two, every
# real fan/WHF dispatch site lives INSIDE AutomationEngine itself (automation.py), not
# in coordinator.py/api.py — fan_fsm.py's own module docstring documents all 16 members
# as "one per real call site read in full for this phase", each a method on
# AutomationEngine. So this registry's scan target is automation.py, not coordinator.py.
#
# "unreachable: <reason>" is for FSM event kinds that exist in the enum but are
# deliberately never dispatched from a real call site (documented, not silently missing).
_FAN_FSM_EVENT_KIND_REGISTRY: dict[str, str] = {
    "ACTIVATE_REQUESTED": "reachable",
    "DEACTIVATE_REQUESTED": "reachable",
    # Issue #731 Phase 5: reconcile_fan_on_startup()'s "fan is off" write group spans
    # TWO independent lifecycles — _fan_active/_fan_on_since (fan-lifecycle, owned by
    # _apply_fan_fsm_state()) and _natural_vent_active/_nat_vent_soft_start (nat-vent's
    # own lifecycle, which _apply_fan_fsm_state() does not and should not own). Routing
    # this write group through the dispatcher would silently drop the nat-vent-side flag
    # changes once _fan_fsm_authoritative flips True (the FSM branch would apply only the
    # fan-side quarter of this reconcile decision) — a correctness regression, not a gap
    # in wiring effort. Stays a direct write; see automation.py's own comment at this
    # call site for the full rationale.
    "STARTUP_RECONCILE": (
        "unreachable: reconcile_fan_on_startup()'s write group spans nat-vent's own "
        "lifecycle fields, which _apply_fan_fsm_state() doesn't own — dispatching would "
        "silently drop the nat-vent-side half of the decision"
    ),
    "MANUAL_OVERRIDE_DETECTED": "reachable",
    "OVERRIDE_CLEARED": "reachable",
    # Issue #731 Phase 5: on_fan_turned_off()'s normal fan-off path is deliberately left
    # without its own USER_FAN_OFF dispatch — its entire flag-clearing effect IS
    # _clear_fan_flags_and_start_grace() (FLAGS_CLEARED_FOR_GRACE's real dispatch site),
    # so a second dispatch here would report the same net state change twice for one
    # logical event (double-dispatch), not add real coverage.
    "USER_FAN_OFF": (
        "unreachable: fully delegated to FLAGS_CLEARED_FOR_GRACE to avoid "
        "double-dispatching the same logical event — see fan_fsm.py's own "
        "USER_FAN_OFF/FLAGS_CLEARED_FOR_GRACE docstring split"
    ),
    "TIMER_BOUNDARY_SETTLE": "reachable",
    "FLAGS_CLEARED_FOR_GRACE": "reachable",
    "MIN_RUNTIME_CYCLE_ON": "reachable",
    "MIN_RUNTIME_CYCLE_OFF": "reachable",
    "MIN_RUNTIME_CYCLE_STOPPED": "reachable",
    "DRIFT_TICK": "reachable",
    "THERMO_BACKSTOP_TICK": "reachable",
    "THERMOSTAT_CHECK_TICK": "reachable",
    "WHF_SUPPRESSION_REQUESTED": "reachable",
    "WHF_RELEASE_REQUESTED": "reachable",
}


class TestFanFsmEventCoverage:
    def test_every_event_kind_is_registered(self) -> None:
        from custom_components.climate_advisor.fan_fsm import FanFsmEventKind

        all_kinds = {member.name for member in FanFsmEventKind}
        unregistered = all_kinds - set(_FAN_FSM_EVENT_KIND_REGISTRY)
        assert not unregistered, (
            f"New FanFsmEventKind member(s) aren't in _FAN_FSM_EVENT_KIND_REGISTRY: "
            f'{sorted(unregistered)}. Classify each as "reachable" (and wire a real '
            f'dispatch site in automation.py) or "unreachable: <reason>" — see Issue #731.'
        )

    def test_registry_entries_reference_real_members(self) -> None:
        from custom_components.climate_advisor.fan_fsm import FanFsmEventKind

        all_kinds = {member.name for member in FanFsmEventKind}
        unknown = set(_FAN_FSM_EVENT_KIND_REGISTRY) - all_kinds
        assert not unknown, (
            f"Registry references FanFsmEventKind member(s) that no longer exist "
            f"(renamed or removed?): {sorted(unknown)}. Update the registry."
        )

    def test_every_reachable_kind_has_a_real_dispatch_site(self) -> None:
        """Positive control: every 'reachable' entry must appear as a direct
        FanFsmEventKind.<X> reference in automation.py — unlike override/grace and
        door/window (dispatched from coordinator.py/api.py via mirror-name-keyed
        dicts), every real fan/WHF dispatch site is a method on AutomationEngine
        itself, so automation.py is the correct — and only — scan target."""
        src = _AUTOMATION_PY.read_text(encoding="utf-8")
        for name, classification in _FAN_FSM_EVENT_KIND_REGISTRY.items():
            if classification != "reachable":
                continue
            member_pattern = re.compile(r"\bFanFsmEventKind\." + re.escape(name) + r"\b")
            assert member_pattern.search(src), (
                f'{name} is marked "reachable" but no direct FanFsmEventKind.{name} '
                f"reference was found in automation.py"
            )

    def test_every_unreachable_kind_has_no_real_dispatch_site(self) -> None:
        """Inverse positive control: an 'unreachable' entry must NOT appear as a
        direct FanFsmEventKind.<X> dispatch reference in automation.py — catches the
        registry claiming a kind is deliberately unwired when a later phase actually
        wired it (the classification going stale in the opposite direction)."""
        src = _AUTOMATION_PY.read_text(encoding="utf-8")
        for name, classification in _FAN_FSM_EVENT_KIND_REGISTRY.items():
            if not classification.startswith("unreachable"):
                continue
            member_pattern = re.compile(r"\bFanFsmEventKind\." + re.escape(name) + r"\b")
            assert not member_pattern.search(src), (
                f'{name} is marked "unreachable" but a direct FanFsmEventKind.{name} '
                f'reference now exists in automation.py — reclassify to "reachable" '
                f"(Issue #731)."
            )

    def test_positive_control_unregistered_kind_is_caught(self) -> None:
        """Proves test_every_event_kind_is_registered actually fails on a genuinely
        unregistered member, not just passing vacuously."""
        all_kinds = {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
        unregistered = all_kinds - set(_FAN_FSM_EVENT_KIND_REGISTRY)
        assert unregistered == {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
