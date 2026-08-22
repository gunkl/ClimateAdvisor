"""Issue #709: generalized "exactly one writer" regression guard for the flags the
nat-vent/door-window/override-grace FSMs model.

This is the DRY-methodology deliverable from the #709 plan — a registry-driven,
AST-based static check in the style of ``tests/test_executor_offload.py``'s
``_BLOCKING_METHODS`` registry, applied to a different hazard: instead of catching
a blocking call bypassing an executor, it catches a flag being written by an
``_apply_*_fsm_state()`` method other than its documented owner(s).

**Root cause this generalizes from.** Prior to #709, ``_apply_door_window_fsm_state()``
also wrote ``_grace_active`` — a flag ``override_grace_fsm.py``'s own docstring already
claimed exclusive ownership of via ``_apply_override_grace_fsm_state()``. The two
writers could disagree for real: ``resume_from_pause()``/
``handle_all_doors_windows_closed()`` both called the door/window dispatcher (which
could land unconditionally on a GRACE-ish state) before the real grace-start action,
with genuine ``await`` points in between where a concurrently scheduled task could
observe the phantom flag. Nothing caught this by construction — it required reading
both methods side by side and knowing override/grace's docstring claim was actually
false. This test makes that comparison automatic and permanent: for every
``_apply_..._fsm_state()``-shaped method in ``automation.py``, every ``self.<flag> = ...``
assignment inside it must have that method listed as an allowed owner of ``<flag>`` in
``_FLAG_OWNERS`` below. Add an entry here whenever a new FSM-modeled flag is introduced.

**Why some flags have more than one allowed owner.** ``_paused_by_door`` is legitimately
co-owned by ``_apply_door_window_fsm_state()`` (door/window's own pause lifecycle) and
``_apply_nat_vent_fsm_state()`` (nat-vent reactivating while paused always means "no
longer paused by door" — a fact nat-vent's own transition genuinely determines, not a
race-prone speculative write racing against a real side effect the way the fixed
``_grace_active`` bug was). This is a deliberate cross-FSM composition
(Issue #631's "communicating automata" convention), not an oversight — unlike the
``_grace_active`` case, there is no separate real-timer side effect that could
disagree with this write. If a future flag needs the same multi-owner treatment,
add every legitimate owner to its ``_FLAG_OWNERS`` entry with a comment explaining why,
same as this one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

AUTOMATION_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "automation.py"

# Pattern identifying the "apply an FSM transition result to real instance flags"
# method family this test polices — matches _apply_door_window_fsm_state,
# _apply_override_grace_fsm_state, _apply_nat_vent_fsm_state, and any future
# sibling automatically, without needing this test file edited when one is added.
_APPLY_METHOD_PATTERN = re.compile(r"^_apply_.*_fsm_state$")

# Flag -> the method name(s) permitted to write it via `self.<flag> = ...` from
# inside one of the _apply_*_fsm_state() methods above. Every flag these three FSMs
# model must appear here, even ones with zero current apply-method writers (e.g.
# _manual_override_active, _override_confirm_pending's sibling raw flags) — an
# empty/absent entry means "no apply method may ever write this," so a future
# violation is still caught the moment it's introduced.
_FLAG_OWNERS: dict[str, frozenset[str]] = {
    # Issue #664/#709: override/grace's sole 3-flag derivation.
    "_grace_active": frozenset({"_apply_override_grace_fsm_state"}),
    "_grace_protects_override": frozenset({"_apply_override_grace_fsm_state"}),
    "_override_confirm_pending": frozenset({"_apply_override_grace_fsm_state"}),
    # Issue #594/#660: door/window's 2-flag derivation (Issue #709 removed
    # _grace_active from this method's derivation — see its own docstring).
    # _paused_by_door is legitimately co-owned by nat-vent's apply method too
    # (see module docstring above).
    "_paused_by_door": frozenset({"_apply_door_window_fsm_state", "_apply_nat_vent_fsm_state"}),
    "_paused_with_hvac_already_off": frozenset({"_apply_door_window_fsm_state"}),
    # Issue #411/#647: nat-vent's own lifecycle flags.
    "_natural_vent_active": frozenset({"_apply_nat_vent_fsm_state"}),
    "_nat_vent_soft_start": frozenset({"_apply_nat_vent_fsm_state"}),
    # Not currently written by any _apply_*_fsm_state() method — real writers are
    # handle_manual_override()/clear_manual_override()/_confirm_override(), none of
    # which are apply-method-shaped. Listed with an empty owner set so this test
    # immediately flags it if a future refactor starts writing it from an apply
    # method without an explicit decision to allow that.
    "_manual_override_active": frozenset(),
    # Issue #731 (Phase 4): fan/WHF's own 4-field derivation. _pre_fan_hvac_mode is
    # only ever None-cleared here (see _apply_fan_fsm_state()'s own docstring) — the
    # string value it's set TO on suppression entry remains a payload write owned by
    # the real _suppress_hvac_for_whf() caller.
    "_fan_active": frozenset({"_apply_fan_fsm_state"}),
    "_fan_override_active": frozenset({"_apply_fan_fsm_state"}),
    "_fan_min_runtime_active": frozenset({"_apply_fan_fsm_state"}),
    "_pre_fan_hvac_mode": frozenset({"_apply_fan_fsm_state"}),
    # Issue #731 Phase 7: NOT written by _apply_fan_fsm_state() — same "excluded field,
    # real-caller direct write" category _apply_fan_fsm_state()'s own docstring lists
    # (_fan_on_since, _fan_toggle_command_time, _fan_rate_limited_until/_direction,
    # _fan_drift_tick_count, ...), not the multi-owner category _paused_by_door above
    # uses. _activate_fan()/_deactivate_fan() write _fan_rate_limited_until/_direction
    # directly on the FSM-authoritative branch (mirroring _fan_toggle_rate_limited()'s own
    # DEFER_NEW-only write on the legacy branch) BEFORE/alongside calling
    # _resolve_fan_fsm_state() — never from inside _apply_fan_fsm_state() itself, which
    # this test's AST scan does not and should not see. Same empty-owner-set pattern as
    # _manual_override_active above: listed here so this test immediately flags it if a
    # future refactor starts writing either field from an apply method without an
    # explicit decision to allow that.
    "_fan_rate_limited_until": frozenset(),
    "_fan_rate_limited_direction": frozenset(),
    "_fan_on_since": frozenset(),
    "_fan_drift_tick_count": frozenset(),
    # Issue #746 (strangler-fig completion program, Phase 5 — final subsystem
    # extraction): economizer's own 2-flag derivation, same shape as nat-vent's
    # 2-flag derivation above. Sole owner is _apply_economizer_fsm_state();
    # _deactivate_economizer() also writes both fields directly on the legacy
    # branch (and on the FSM-authoritative branch's INACTIVE-transition path,
    # which calls it instead of _apply_economizer_fsm_state() — see
    # _check_window_cooling_opportunity_fsm()) but that's a real caller's direct
    # write, not a second _apply_*_fsm_state()-shaped method, so this test's AST
    # scan correctly does not see it.
    "_economizer_active": frozenset({"_apply_economizer_fsm_state"}),
    "_economizer_phase": frozenset({"_apply_economizer_fsm_state"}),
}


def _extract_apply_fsm_methods(tree: ast.AST) -> list[ast.FunctionDef]:
    """Every method in automation.py whose name matches the _apply_*_fsm_state
    naming convention — the method family this test polices."""
    methods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _APPLY_METHOD_PATTERN.match(node.name):
            methods.append(node)
    return methods


def _self_attr_assignments(fn_node: ast.FunctionDef) -> set[str]:
    """Every `self.<attr> = ...` (or `self.<attr>: T = ...`) assignment target
    directly inside fn_node, by attribute name."""
    attrs: set[str] = set()
    for node in ast.walk(fn_node):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                attrs.add(t.attr)
    return attrs


class TestFsmApplyMethodFlagOwnership:
    """Every _apply_*_fsm_state() method in automation.py may only write flags it
    is a documented, registered owner of in _FLAG_OWNERS."""

    def test_no_unauthorized_flag_writes(self):
        source = AUTOMATION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        apply_methods = _extract_apply_fsm_methods(tree)
        assert apply_methods, (
            "No _apply_*_fsm_state() methods found in automation.py — method(s) renamed? "
            "This test's whole mechanism depends on the naming convention holding."
        )

        violations: list[tuple[str, str]] = []
        for fn in apply_methods:
            written = _self_attr_assignments(fn)
            for attr in written:
                if attr not in _FLAG_OWNERS:
                    # Not a flag this test is registry-aware of. Fail loudly rather
                    # than silently ignoring it — every FSM-modeled flag an apply
                    # method writes must be a deliberate, registered decision, same
                    # as _BLOCKING_METHODS in test_executor_offload.py.
                    violations.append((fn.name, attr))
                    continue
                if fn.name not in _FLAG_OWNERS[attr]:
                    violations.append((fn.name, attr))

        assert not violations, (
            "Unauthorized/unregistered flag write(s) found in _apply_*_fsm_state() "
            "method(s): "
            + ", ".join(f"{method}() writes self.{attr}" for method, attr in violations)
            + ". Either this is a genuine second writer of a flag another FSM's apply "
            "method already owns (the exact #709 _grace_active bug shape — remove the "
            "write instead), or it's a legitimate new co-owner that needs an explicit "
            "entry (with rationale) added to _FLAG_OWNERS in this test file."
        )

    def test_grace_active_owned_solely_by_override_grace(self):
        """Direct regression test for the #709 bug shape: _apply_door_window_fsm_state()
        must not appear anywhere in _grace_active's registered owner set."""
        assert _FLAG_OWNERS["_grace_active"] == frozenset({"_apply_override_grace_fsm_state"})

    def test_registry_covers_every_flag_actually_written_by_apply_methods(self):
        """The inverse check: every flag an apply method actually writes today must
        have a _FLAG_OWNERS entry (not just the well-known 6) — otherwise a newly
        introduced flag could silently ship with no ownership check at all."""
        source = AUTOMATION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        all_written: set[str] = set()
        for fn in _extract_apply_fsm_methods(tree):
            all_written |= _self_attr_assignments(fn)

        unregistered = all_written - set(_FLAG_OWNERS)
        assert not unregistered, (
            f"Flag(s) written by an _apply_*_fsm_state() method with no _FLAG_OWNERS "
            f"registry entry at all: {sorted(unregistered)}. Add an entry (even if the "
            f"owner set is just the one method already writing it) so future dual-writes "
            f"are caught."
        )
