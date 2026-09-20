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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan

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
    # (direct/standalone calls, tests passing raw prediction curves). All three
    # call sites live directly inside generate_briefing() itself (not separate
    # per-day-type helper functions) because the three day types need
    # independently-gated results, not because the computation itself differs —
    # ordinal 1 is the WARM-day plan, ordinal 2 is the MILD-day plan, ordinal 3
    # is the HOT-day plan (Issue #876 — extended the shared dynamic-crossing
    # mechanism to HOT's briefing text, which never used it before).
    ("briefing.py", "generate_briefing", 1),
    ("briefing.py", "generate_briefing", 2),
    ("briefing.py", "generate_briefing", 3),
    # coordinator.py: the ONE per-cycle computation, cached on self._nat_vent_plan.
    # Every other production consumer reads that cache — see
    # _compute_and_cache_nat_vent_plan()'s docstring. Two ordinals (Issue #876):
    # HOT days need a differently-bounded/gated call (window_opportunity_morning_start,
    # no comfort-floor scan) than WARM/MILD (window_open_time, comfort-floor scan) —
    # an if/else choosing between them, not two calls in sequence, but the AST
    # visitor still counts both branches as ordinals 1 and 2 within the same
    # enclosing function.
    ("coordinator.py", "_compute_and_cache_nat_vent_plan", 1),
    ("coordinator.py", "_compute_and_cache_nat_vent_plan", 2),
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


# ===========================================================================
# Issue #948 — evening_open_time's reopen scan has no trend/peak guard.
#
# compute_nat_vent_plan() finds evening_open_time as the first future point where
# outdoor drops back below comfort_cool - margin, with no requirement that outdoor's
# peak actually exceeded comfort_cool first. On a curve where outdoor dips briefly
# right after the morning close (long before the day's real peak), the reopen
# predicate is trivially satisfied at that early dip -- producing a nonsensical
# "reopen windows at noon" claim hours before the day has actually gotten hot. See
# the plan (despite-symmetrical-work-last-nested-spark.md, Root Cause 2) for the
# full root-cause writeup and the fix design (bound the reopen scan to start no
# earlier than max(nat_vent_cutoff, outdoor_ceiling_breach_time)).
#
# These tests are written against the CURRENT (pre-fix) code and are expected to
# FAIL until nat_vent_plan.py's evening_open_time scan is fixed -- that is the
# point of this TDD slice; the Craftsman lane fixes production code next.
# ===========================================================================

_BASE_TS = datetime(2026, 9, 20, 6, 0, 0, tzinfo=UTC)


def _curve(entries: list[tuple[int, float, float]]) -> tuple[list[dict], list[dict]]:
    """Build (predicted_indoor, predicted_outdoor) curves from
    (hour_offset, indoor_temp, outdoor_temp) triples, all sharing one ISO timestamp
    per hour offset from _BASE_TS -- matching the {"ts": ..., "temp": ...} shape
    find_temperature_crossing() expects."""
    indoor: list[dict] = []
    outdoor: list[dict] = []
    for hour_offset, indoor_temp, outdoor_temp in entries:
        ts = (_BASE_TS + timedelta(hours=hour_offset)).isoformat()
        indoor.append({"ts": ts, "temp": indoor_temp})
        outdoor.append({"ts": ts, "temp": outdoor_temp})
    return indoor, outdoor


# (day_type, comfort_cool) -- window_open_time is deliberately omitted (None) for
# all three: the _after_open() gate it introduces is orthogonal to the post-peak
# reopen-bound defect under test here, and a nonzero window_open_time would filter
# out this fixture's early-morning entries for Warm/Mild (whose real window_open_time
# is 10:00/10:00) for reasons that have nothing to do with the bug being tested.
# comfort_cool differs per day type to reflect each type's real classifier default,
# per the task's "cover all three day types" instruction.
_DAY_TYPE_PARAMS = [
    pytest.param("hot", 78.0, id="hot"),
    pytest.param("warm", 75.0, id="warm"),
    pytest.param("mild", 74.0, id="mild"),
]


class TestEveningOpenTimeRequiresGenuinePeakBreach:
    """Issue #948: evening_open_time must never fire before outdoor has genuinely
    breached comfort_cool at least once that day."""

    @pytest.mark.parametrize("day_type,comfort_cool", _DAY_TYPE_PARAMS)
    def test_no_genuine_peak_evening_open_time_is_none(self, day_type: str, comfort_cool: float) -> None:
        """Outdoor rises just past the (low) overnight indoor value early (hour 2,
        triggering nat_vent_cutoff), dips again (hour 3 -- the exact pre-peak dip
        the reported Mild-day bug reopened on), but the true peak (hour 5, 72.0F)
        never reaches comfort_cool for ANY of the three day types' comfort_cool
        values (74/75/78). With no genuine ceiling breach anywhere in the curve,
        there is no real "got too hot, will cool down later" event to narrate --
        evening_open_time must be None.

        Against current (pre-fix) code, this FAILS: the reopen scan finds the
        early hour-3 dip (60.0F <= comfort_cool - 1.0) and reports it as
        evening_open_time, even though outdoor never actually got hot.
        """
        indoor, outdoor = _curve(
            [
                (0, 68.0, 55.0),
                (1, 68.0, 62.0),
                (2, 68.0, 69.0),  # nat_vent_cutoff: 69 >= indoor(68) - 1
                (3, 68.0, 60.0),  # pre-peak dip: old buggy code reopens HERE
                (4, 68.0, 65.0),
                (5, 68.0, 72.0),  # the day's actual peak -- still < comfort_cool for all 3 types
                (6, 68.0, 58.0),
            ]
        )
        result = compute_nat_vent_plan(indoor, outdoor, comfort_cool=comfort_cool, window_open_time=None)

        assert result["nat_vent_cutoff"] is not None
        assert result["nat_vent_cutoff_reason"] == "outdoor_rise"
        assert result["evening_open_time"] is None, (
            f"[{day_type}] evening_open_time={result['evening_open_time']} but outdoor never exceeded"
            f" comfort_cool={comfort_cool} anywhere in the curve -- there is no genuine reopen event"
            " to report. This is the exact 'reopen windows at noon' bug from Issue #948: an early"
            " pre-peak dip (hour 3, 60.0F) was mistaken for a real post-peak cooldown."
        )

    @pytest.mark.parametrize("day_type,comfort_cool", _DAY_TYPE_PARAMS)
    def test_genuine_peak_breach_evening_open_time_lands_after_breach(self, day_type: str, comfort_cool: float) -> None:
        """Same pre-peak dip shape as above (hour 3), but this time outdoor DOES
        genuinely exceed comfort_cool later (hours 5-6, 85/90F) before declining
        (hour 7, 60F). evening_open_time must land at the genuine post-peak decline
        (hour 7), not the earlier pre-peak dip (hour 3) -- and must be strictly
        after the real ceiling breach (hour 5).

        Against current (pre-fix) code, this FAILS: the reopen scan is unbounded
        and returns the first qualifying point after nat_vent_cutoff, which is the
        pre-peak dip at hour 3 -- hours before the real peak even happens.
        """
        indoor, outdoor = _curve(
            [
                (0, 68.0, 55.0),
                (1, 68.0, 62.0),
                (2, 68.0, 69.0),  # nat_vent_cutoff
                (3, 68.0, 60.0),  # pre-peak dip: old buggy code reopens HERE
                (4, 68.0, 65.0),
                (5, 68.0, 85.0),  # genuine ceiling breach (> comfort_cool for all 3 types)
                (6, 68.0, 90.0),  # peak continues
                (7, 68.0, 60.0),  # genuine post-peak decline -- the real reopen event
            ]
        )
        result = compute_nat_vent_plan(indoor, outdoor, comfort_cool=comfort_cool, window_open_time=None)

        cutoff_hour3 = _BASE_TS + timedelta(hours=3)
        breach_hour5 = _BASE_TS + timedelta(hours=5)
        expected_hour7 = _BASE_TS + timedelta(hours=7)

        assert result["nat_vent_cutoff"] is not None
        assert result["evening_open_time"] is not None
        assert result["evening_open_time"] != cutoff_hour3, (
            f"[{day_type}] evening_open_time landed on the pre-peak dip (hour 3) instead of after"
            " the genuine peak breach (hour 5) -- this is the Issue #948 bug."
        )
        assert result["evening_open_time"] > breach_hour5, (
            f"[{day_type}] evening_open_time={result['evening_open_time']} must be strictly after"
            f" the genuine ceiling breach at {breach_hour5}."
        )
        assert result["evening_open_time"] == expected_hour7
