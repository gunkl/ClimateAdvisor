"""Enforcement test (Issue #817): compute_nat_vent_plan() has exactly one allowed
set of call sites.

Before nat_vent_plan.py existed, "when should windows close" was independently
recomputed in briefing.py and coordinator.py — the same shape of bug that let
Issue #528 silently reintroduce a duplicate computation 2 days after Issue #518
promised there'd never be one. This test closes that hole for good: any new call
site anywhere else in the integration fails this test by file and line number,
rather than silently drifting until the next contradictory Status report.

Uses the AST (not a text grep) so a call site can't hide from this test behind
formatting — a multi-line call, an aliased import, or a renamed local binding of
the same function all still resolve to a `Call` node whose function name is
``compute_nat_vent_plan``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"

# Every call site allowed to invoke compute_nat_vent_plan() directly, as
# (relative_path, line_number) pairs. Adding a new one here is a real design
# decision — do it deliberately, with the same "why does this need its own copy"
# scrutiny Issue #817 applied to the sites that existed before this test.
_ALLOWED_CALL_SITES: set[tuple[str, int]] = {
    # briefing.py: the documented fallback path in generate_briefing() — only
    # reached when a caller doesn't already have a precomputed nat_vent_plan
    # (direct/standalone calls, tests passing raw prediction curves). Two call
    # sites (WARM, MILD) because the two day types need independently-gated
    # results, not because the computation itself differs.
    ("briefing.py", 157),
    ("briefing.py", 177),
    # coordinator.py: the ONE per-cycle computation, cached on self._nat_vent_plan.
    # Every other production consumer reads that cache — see
    # _compute_and_cache_nat_vent_plan()'s docstring.
    ("coordinator.py", 3985),
}


def _find_call_sites(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == "compute_nat_vent_plan":
                sites.append((path.name, node.lineno))
    return sites


def test_compute_nat_vent_plan_has_no_unlisted_call_sites():
    found: list[tuple[str, int]] = []
    for py_file in _COMPONENT_DIR.glob("*.py"):
        found.extend(_find_call_sites(py_file))

    unlisted = sorted(set(found) - _ALLOWED_CALL_SITES)
    assert not unlisted, (
        f"New, unreviewed call site(s) of compute_nat_vent_plan() found: {unlisted}. "
        "If this is a deliberate new consumer, read _compute_and_cache_nat_vent_plan()'s "
        "docstring first — the coordinator already computes this once per cycle on "
        "self._nat_vent_plan; a new caller should almost always read that cache instead "
        "of calling compute_nat_vent_plan() directly. If a direct call is genuinely "
        "warranted, add it to _ALLOWED_CALL_SITES with a comment explaining why."
    )

    # Also confirm the allow-list itself hasn't gone stale (a line renumbered by
    # an unrelated edit would otherwise let this test silently stop checking
    # anything real).
    missing = sorted(_ALLOWED_CALL_SITES - set(found))
    assert not missing, (
        f"Expected call site(s) not found: {missing}. Update _ALLOWED_CALL_SITES "
        "to match the current line numbers, or the corresponding call was removed "
        "and this entry should be deleted."
    )
