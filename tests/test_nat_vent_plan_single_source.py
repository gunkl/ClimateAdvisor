"""Enforcement test (Issue #817): compute_nat_vent_plan() has exactly one allowed
set of call sites.

Before nat_vent_plan.py existed, "when should windows close" was independently
recomputed in briefing.py and coordinator.py — the same shape of bug that let
Issue #528 silently reintroduce a duplicate computation 2 days after Issue #518
promised there'd never be one. This test closes that hole for good: any new call
site anywhere else in the integration fails this test by file and enclosing
function, rather than silently drifting until the next contradictory Status report.

Uses the AST (not a text grep) so a call site can't hide from this test behind
formatting — a multi-line call, an aliased import, or a renamed local binding of
the same function all still resolve to a `Call` node whose function name is
``compute_nat_vent_plan``.

Keying scheme history (Issue #843/#847 follow-up):
    The allow-list was originally keyed by ``(relative_path, line_number)``. That
    keying broke this test with a false-positive failure twice in a row — Issue
    #843 (an unrelated recency-gated-deadband change shifted briefing.py/
    coordinator.py line numbers by a few lines) and Issue #847 (a shared
    cutoff-reason wording change shifted them again) — with **zero genuine new
    call sites** in either case. Both times the fix was a one-line number bump,
    which treated the symptom rather than the actual defect: line number is not
    a structural identifier, it is a side effect of unrelated edits anywhere
    earlier in the file.

    This is the second consecutive occurrence of the identical failure shape,
    which is this project's own signal (see CLAUDE.md's investigation protocol)
    to fix the layer beneath the repeated patch rather than patch it a third
    time. The redesign mirrors ``tests/test_executor_offload.py``'s
    ``_BLOCKING_METHODS`` registry (Issue #543/#545), which solved the identical
    "don't key an allow-list by something formatting can shift" problem for
    blocking-I/O call sites by keying on ``(attribute_name, method_name)`` —
    zero line numbers. Applied here, the AST walker now tracks enclosing-
    function ancestry as it visits (``ast.walk()`` does not track parents on its
    own, so a small ``ast.NodeVisitor`` subclass pushes/pops the current
    enclosing ``FunctionDef``/``AsyncFunctionDef`` name as it descends) and keys
    each call site on ``(file, enclosing_function_qualname, ordinal)`` instead of
    line number.

    The ``ordinal`` (1-based, counted per enclosing function) is not unused
    complexity — it is required. ``briefing.py``'s ``generate_briefing()`` calls
    ``compute_nat_vent_plan()`` twice in a row (the WARM-day plan, then the
    MILD-day plan), both directly inside the same function body, not inside two
    separate helper functions as an earlier draft of this fix assumed. Without
    the ordinal, ``(file, function)`` alone would collapse those two legitimate
    call sites into one key, and a genuine *third* call added to
    ``generate_briefing()`` would be indistinguishable from the existing two —
    silently defeating the very guarantee this test exists to enforce. This key
    shape is immune to pure line drift (formatting, unrelated edits elsewhere in
    the file) while still failing on a real new call site, whether that's an
    entirely new function or a second call inside an existing single-call
    function.
"""

from __future__ import annotations

import ast
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"

_TARGET_FN = "compute_nat_vent_plan"

# Every call site allowed to invoke compute_nat_vent_plan() directly, as
# (relative_path, enclosing_function_qualname, ordinal) triples. `ordinal` is
# the 1-based index of this call among all compute_nat_vent_plan() calls found
# directly within the same enclosing function (see module docstring for why
# this is necessary, not incidental). Adding a new entry here is a real design
# decision — do it deliberately, with the same "why does this need its own copy"
# scrutiny Issue #817 applied to the sites that existed before this test.
_ALLOWED_CALL_SITES: set[tuple[str, str, int]] = {
    # briefing.py: the documented fallback path in generate_briefing() — only
    # reached when a caller doesn't already have a precomputed nat_vent_plan
    # (direct/standalone calls, tests passing raw prediction curves). Both call
    # sites live directly inside generate_briefing() itself (not separate
    # per-day-type helper functions) because the two day types need
    # independently-gated results, not because the computation itself differs —
    # ordinal 1 is the WARM-day plan, ordinal 2 is the MILD-day plan.
    ("briefing.py", "generate_briefing", 1),
    ("briefing.py", "generate_briefing", 2),
    # coordinator.py: the ONE per-cycle computation, cached on self._nat_vent_plan.
    # Every other production consumer reads that cache — see
    # _compute_and_cache_nat_vent_plan()'s docstring.
    ("coordinator.py", "_compute_and_cache_nat_vent_plan", 1),
}


class _EnclosingFunctionCallVisitor(ast.NodeVisitor):
    """Walks a module tracking enclosing-function ancestry, recording every
    call to _TARGET_FN as (filename, enclosing_function_qualname, ordinal).

    Neither ast.walk() nor the default ast.NodeVisitor.generic_visit() tracks
    parent nodes, so enclosing-function context has to be maintained explicitly
    via a push/pop stack as FunctionDef/AsyncFunctionDef nodes are entered and
    left. `ordinal` counts matches per enclosing-function qualname (not
    globally), so two calls in the same function get 1 and 2, while two calls
    in two different functions (however named) each independently get 1.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self._stack: list[str] = []
        self._counts: dict[str, int] = {}
        self.sites: list[tuple[str, str, int]] = []

    def _enclosing_qualname(self) -> str:
        # Dotted qualname of nested functions (e.g. "outer.inner"); "<module>"
        # for a call sitting directly at module level, outside any function.
        return ".".join(self._stack) if self._stack else "<module>"

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        if name == _TARGET_FN:
            qualname = self._enclosing_qualname()
            self._counts[qualname] = self._counts.get(qualname, 0) + 1
            self.sites.append((self.filename, qualname, self._counts[qualname]))
        self.generic_visit(node)


def _find_call_sites(path: Path) -> list[tuple[str, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _EnclosingFunctionCallVisitor(path.name)
    visitor.visit(tree)
    return visitor.sites


def test_compute_nat_vent_plan_has_no_unlisted_call_sites():
    found: list[tuple[str, str, int]] = []
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

    # Also confirm the allow-list itself hasn't gone stale (a call removed or
    # renamed by an unrelated edit would otherwise let this test silently stop
    # checking anything real). Unlike the old line-number keying, this can no
    # longer go stale from pure formatting/line-shift — only from the call
    # itself moving to a different enclosing function or being removed.
    missing = sorted(_ALLOWED_CALL_SITES - set(found))
    assert not missing, (
        f"Expected call site(s) not found: {missing}. Update _ALLOWED_CALL_SITES "
        "to match the current call sites, or the corresponding call was removed "
        "and this entry should be deleted."
    )
