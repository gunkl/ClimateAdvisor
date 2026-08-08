"""Static guard for the Issue #584/#591 duplicate-decision bug class (Block 2 Step I).

Mirrors tests/test_executor_offload.py's AST-walk pattern: some reachability
properties (here, "this event type is emitted from more than one call site,
so an overlapping trigger can double-emit it") aren't visible to a line-local
Ruff rule, but are cheap to check with a direct AST walk over automation.py.

Scope: Issue #591/#590's Delta 2 audit examined exactly 10 event types reachable
from apply_classification()'s or handle_bedtime()'s confirmed multi-trigger call
graph (the ones NOT already covered by the Activity Record's _NO_DEDUP
display-collapse list) and found them vulnerable to the #584 bug shape. This test
covers only those 10 — it is NOT a general "every multi-site event in
automation.py must be deduped" rule. Other multi-site event types in the file
(fan_cancel, sensor_opened, override_cleared, incident_detected, etc.) were
Delta-2-audited and found already safe via state-flip guards or genuine one-shot
triggers, or were never in scope for this investigation — auditing them is
tracked separately, not re-litigated here (a blanket rule produces false-positive
noise for sites nobody has actually verified are unsafe).

Rule, scoped to DELTA_2_AUDITED_EVENT_TYPES: each such event_type emitted from 2+
call sites must either (a) have every call site guarded by a
`not self._recent_duplicate("<event_type>", ...)` check, or (b) appear in
MULTI_SITE_EVENT_ALLOWLIST with a comment explaining why its repeats are
legitimate (mirrors conftest.LEGITIMATELY_REPEATING_EVENT_TYPES, the same idea
applied to actual runtime event streams instead of source call sites).
"""

from __future__ import annotations

import ast
from pathlib import Path

AUTOMATION_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "automation.py"

# Issue #591: Delta 2's audit found occupancy_setback and hvac_write_blocked_whf_active
# emitted from 2 call sites each. Both were tried against _recent_duplicate() and reverted
# after golden/pending scenarios (wakeup_preserves_whf_manual_override,
# away_morning_wakeup_skipped_assertion, morning_wakeup_skipped_away_occupancy,
# cancel_override_then_resume, vacation_occupancy_override_cleared) proved each repeat is a
# distinct, meaningful re-confirmation at a new decision point, not an accidental echo of the
# same decision — see the comments at their call sites in automation.py.
MULTI_SITE_EVENT_ALLOWLIST = {
    "occupancy_setback",
    "hvac_write_blocked_whf_active",
}

# Issue #591/#590 Delta 2's full audited list — the only event types this test checks.
# classification_applied and comfort_band_applied are single-named-mechanism sites (the
# pre-existing #96/#444 pattern, migrated onto the same shared helper) rather than literal
# multiple `self._emit_event_callback("classification_applied", ...)` call sites, so they
# aren't part of the multi-site AST check below, but ARE covered by
# tests/test_recent_duplicate_helper.py and the golden-level check in
# tests/test_no_duplicate_decisions.py.
DELTA_2_AUDITED_EVENT_TYPES = {
    "classification_suppressed_paused",
    "occupancy_setback_suppressed_paused",
    "occupancy_setback",
    "hvac_write_blocked_whf_active",
    "nat_vent_ac_assist_armed",
    "bedtime_setback_skipped",
    "nat_vent_bedtime_continue",
}


def _emit_call_sites(tree: ast.AST) -> dict[str, list[int]]:
    """Map event_type (string literal) -> list of line numbers where
    self._emit_event_callback(event_type, ...) is called."""
    sites: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "_emit_event_callback"):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                sites.setdefault(first_arg.value, []).append(node.lineno)
    return sites


def _recent_duplicate_guarded_lines(tree: ast.AST) -> set[int]:
    """Return the set of line numbers of every ast.If node whose test contains a
    `not self._recent_duplicate(...)` (or equivalent) call — used as a coarse proxy
    for "this emit call is inside a dedup-guarded branch." We collect the full line
    range of each such If's body so an emit call anywhere inside counts as guarded."""
    guarded_lines: set[int] = set()

    def _contains_recent_duplicate_call(expr: ast.AST) -> bool:
        for sub in ast.walk(expr):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "_recent_duplicate"
            ):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _contains_recent_duplicate_call(node.test):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    guarded_lines.add(sub.lineno)
        # BoolOp (e.g. `if self._emit_event_callback and not self._recent_duplicate(...)`)
        # attached directly to an If.test also needs to be caught — already covered above
        # since node.test IS the BoolOp in that shape. Also catch the "compute a local dup
        # flag, then `if ... and not _dup_flag:`" shape used by a couple of call sites.
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and _contains_recent_duplicate_call(node.value)
        ):
            # Find the enclosing If that reads this assigned name, conservatively:
            # mark every line in the rest of the enclosing function as guarded-eligible
            # is too broad, so instead we just mark this assignment's own line — the
            # per-site test below checks proximity, not exact scope, since a false
            # negative here just means a legitimately-guarded site needs its own
            # explicit allowlist entry rather than silently passing.
            guarded_lines.add(node.lineno)

    return guarded_lines


def test_multi_site_events_are_deduped_or_allowlisted():
    source = AUTOMATION_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    sites = _emit_call_sites(tree)
    guarded_lines = _recent_duplicate_guarded_lines(tree)

    unguarded_multi_site: dict[str, list[int]] = {}
    for event_type, lines in sites.items():
        if event_type not in DELTA_2_AUDITED_EVENT_TYPES:
            continue
        if len(lines) < 2:
            continue
        if event_type in MULTI_SITE_EVENT_ALLOWLIST:
            continue
        # A site counts as guarded if a _recent_duplicate() call appears within a small
        # window of lines before it (covers both the `if not self._recent_duplicate(...)`
        # inline shape and the `_dup = self._recent_duplicate(...)` local-variable shape).
        unguarded = [ln for ln in lines if not any(abs(ln - g) <= 3 for g in guarded_lines)]
        if unguarded:
            unguarded_multi_site[event_type] = unguarded

    assert not unguarded_multi_site, (
        "Event type(s) emitted from 2+ call sites in automation.py without a nearby "
        "_recent_duplicate() guard, and not in MULTI_SITE_EVENT_ALLOWLIST — this is the "
        "Issue #584/#591 duplicate-decision bug shape (a function reachable from multiple "
        "independent trigger paths can double-emit the same decision). Wrap the emit in "
        "`if self._emit_event_callback and not self._recent_duplicate(<event_type>, <signature>): "
        "...` or, if every repeat is a legitimate distinct re-confirmation (not an accidental "
        "echo — verify against real scenario behavior before assuming this), add the event "
        f"type to MULTI_SITE_EVENT_ALLOWLIST with a reason. Found: {unguarded_multi_site}"
    )
