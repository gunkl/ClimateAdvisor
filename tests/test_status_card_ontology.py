"""Structural enforcement of CLAUDE.md's Status Card Ontology (Issue #527) for the
Next Automation card, `_compute_next_automation_action()` in `coordinator.py`.

## Why this test exists

Issue #847's follow-up investigation found that the nat-vent-cutoff candidate
(`f"Close windows around {t} {reason}"`) embeds a clock time directly in its action
string — a literal violation of the ontology table's "Next Automation... Must NOT
contain: Time-of-day phrasing" rule. Five-whys traced this to a real, deliberate
prior fix (Issue #534, commit 421af42) that folded the time in on purpose to resolve
a genuine present-tense-vs-future-tense ambiguity, landed *before* #527 hardened the
no-duplication rule and never reconciled against it afterward. The decision (see
`docs/plans` / the #847-followup plan) was Option B: keep #534's wording exactly as
it is, and formally amend the ontology rule with a narrow, registered exception
(`coordinator._ONTOLOGY_TIME_EXCEPTIONS`) plus an in-line
`# ontology-exception: <slug>` comment — instead of leaving the gap as silent prose
drift, which is exactly how it went unnoticed for this long (CLAUDE.md prose alone
already failed to catch it once).

This test is the structural backstop prose failed to be: it fails CI the moment a
new candidate embeds a time-of-day phrase without going through the same
register-and-tag ritual, and it fails if a registered slug's matching comment ever
goes missing (a stale/renamed registry entry).

## Detection mechanism and its tradeoffs

Everything here is driven by parsing `coordinator.py`'s own source via `ast`
(mirroring `test_nat_vent_plan_single_source.py` and `test_executor_offload.py`'s
AST-structural style rather than inventing a fourth methodology) — no import of the
coordinator module itself, since it drags in the full Home Assistant stub
environment for no benefit here; a source-level check doesn't need a live class.

**Scope decision — only `candidates.append(...)` arguments, not the whole function
body.** `_compute_next_automation_action()` has a handful of early-return guard
clauses ahead of the real candidate list (`"Startup coalescing"` when a coalesce
window is active, `"Waiting for classification..."` when there's no classification
yet, `"No more actions today"` when the candidate list ends up empty). These are
absence-of-schedule states, not schedule candidates being compared against each
other — they are exactly the "14 action-string candidates" the #847-followup plan
counted when it said "the other 13 do not" embed a time. Scoping the scan to
`candidates.append()` arguments only mirrors that plan's own accounting and stays
inside this test's write boundary (registry + one comment in `coordinator.py`; no
other production change). Tradeoff: a banned word landing in one of those guard-
clause returns instead of a real candidate would not be caught by this test. That is
a known, deliberate gap — not a silent one — and is out of scope for this fix (no
such word appears in any of those guard clauses as of this writing).

**Time-of-day detection — chases simple Name assignments back to their source
expression, not a flat text/regex pass over the whole function.** Every current
time-bearing candidate builds its display string via `<datetime>.strftime(...)`
assigned to a local variable, which is *then* interpolated into an f-string
(`_cutoff_t = ...strftime("%I:%M %p")...`, later `f"...{_cutoff_t}..."`) — the
literal `AM`/`PM`/`%I:%M` text never appears in the f-string's own constant parts,
only in the strftime format string several lines earlier. A flat text/regex scan
over each candidate string alone would miss this entirely. Instead,
`_find_time_source()` walks a candidate's description expression, and for every
`Name` node encountered resolves it (repeatedly, up to a small hop limit) against a
map of simple `<Name> = <expr>` assignments collected directly within
`_compute_next_automation_action()`'s own top-level statements (explicitly not
descending into its three nested helper functions — `_parse_time`, `_to_dt`,
`_nat_vent_gate_comparator` — none of which build candidate description text). A
match is either (a) a `.strftime(...)` call whose format string contains `%I`,
`%H`, or `%p`, or (b) a literal string constant containing `AM`/`PM` as whole words
or a literal `%I:%M` sequence (covers a hypothetical future candidate that embeds a
time-shaped literal directly rather than via strftime). Tradeoff: the Name-chase is
bounded (6 hops) and only follows plain `Name = <expr>` assignments — an
intermediate value built through attribute access, subscripting into a dict-typed
temp, or control flow (`x = a if cond else b`) is still walked structurally (`ast.walk`
covers all of those as sub-nodes), but a value laundered through a function call
that itself does the strftime *inside a helper* (rather than inline) would not be
followed across the call boundary. This matches every real candidate as of this
writing and keeps the mechanism simple; a future candidate with that shape would
need this test extended, not silently pass unnoticed (any new time-bearing text
that dodges detection would still read as an unregistered plain string to a human
reviewer diffing `coordinator.py`, and the exception-registration convention this
test enforces is itself the documented required step for a deliberate new one).

**Exception matching — nearest-comment-above the actual time-source node, not the
`candidates.append()` call site.** The `# ontology-exception:` comment sits directly
above the `.strftime(...)` assignment (where the time is actually produced), which
can be several lines above the eventual `candidates.append(...)` call once other
comments/statements sit in between (exactly the current nat-vent-cutoff shape:
comment -> `_cutoff_t = ...strftime(...)` -> unrelated `_cutoff_reason_fragment`
comment+assignment -> `candidates.append(...)`). Anchoring on the append-call site
instead would either need a much larger backward-search window (raising false-
positive risk of matching an unrelated comment) or would simply fail to find it.
Anchoring on the time-source node's own line number keeps the window tight (5 lines)
while still finding the real justification.

**Banned-word check — hard ban, no exception mechanism, by design.** "waiting",
"paused", "grace" (case-insensitive, whole-word) are checked directly against every
string constant inside each `candidates.append()` argument. Unlike the time-of-day
case, #534 never justified an exception for these words, so this test does not
invent a matching allow-list "for words nothing asked for" (the #847-followup plan's
own phrasing). Any hit is an unconditional failure.

**Symmetric check.** Every slug in `_ONTOLOGY_TIME_EXCEPTIONS` must have at least one
matching `# ontology-exception: <slug>` comment somewhere inside
`_compute_next_automation_action()`'s source — the same both-directions discipline
`test_nat_vent_plan_single_source.py`'s `_ALLOWED_CALL_SITES` uses, so this new
registry can't go stale (a renamed/removed candidate leaving a dead registry entry)
without the test noticing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_COORDINATOR_PY = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"

_TARGET_FN = "_compute_next_automation_action"
_REGISTRY_NAME = "_ONTOLOGY_TIME_EXCEPTIONS"

_TIME_FORMAT_CODES = ("%I", "%H", "%p")
_LITERAL_TIME_RE = re.compile(r"\b(AM|PM)\b|%I:%M")
_BANNED_WORDS = ("waiting", "paused", "grace")
_BANNED_WORD_RE = re.compile(r"\b(" + "|".join(_BANNED_WORDS) + r")\b", re.IGNORECASE)
_EXCEPTION_COMMENT_RE = re.compile(r"#\s*ontology-exception:\s*([A-Za-z0-9_]+)")
_COMMENT_SEARCH_WINDOW = 5  # lines to look upward from a time-source node for the tag


def _read_source() -> tuple[str, list[str], ast.Module]:
    text = _COORDINATOR_PY.read_text(encoding="utf-8")
    return text, text.splitlines(), ast.parse(text, filename=str(_COORDINATOR_PY))


def _find_registry(tree: ast.Module) -> set[str]:
    """Parse `_ONTOLOGY_TIME_EXCEPTIONS`'s literal value straight from the AST.

    Deliberately not importing coordinator.py — it requires the full Home
    Assistant stub environment to import cleanly, which this source-level
    structural check has no need for (same reasoning as
    test_nat_vent_plan_single_source.py and test_executor_offload.py).
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == _REGISTRY_NAME
            and node.value is not None
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, set), f"{_REGISTRY_NAME} must be a set literal, found {type(value)}"
            return value
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == _REGISTRY_NAME
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, set), f"{_REGISTRY_NAME} must be a set literal, found {type(value)}"
            return value
    raise AssertionError(
        f"{_REGISTRY_NAME} not found at module level in coordinator.py — "
        "it should never be removed while any ontology-exception comment references it."
    )


def _find_target_function(tree: ast.Module) -> ast.FunctionDef:
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == _TARGET_FN]
    assert len(matches) == 1, (
        f"Expected exactly one {_TARGET_FN}() definition, found {len(matches)}. "
        "This test's whole premise (a single Next Automation candidate list) depends on that."
    )
    return matches[0]


class _CandidateCollector(ast.NodeVisitor):
    """Collects `candidates.append(...)` calls and simple Name assignments made
    directly within the target function's own scope — explicitly NOT descending
    into nested helper functions defined inside it (`_parse_time`, `_to_dt`,
    `_nat_vent_gate_comparator`), none of which build candidate description text.
    """

    def __init__(self) -> None:
        self.assignments: dict[str, ast.expr] = {}
        self.append_calls: list[ast.Call] = []
        self._entered_target = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if not self._entered_target:
            self._entered_target = True
            self.generic_visit(node)
        # else: a nested function def inside the target function — skip its body.

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.assignments[node.targets[0].id] = node.value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "candidates"
        ):
            self.append_calls.append(node)
        self.generic_visit(node)


def _collect(fn_node: ast.FunctionDef) -> _CandidateCollector:
    collector = _CandidateCollector()
    collector.visit(fn_node)
    return collector


def _description_expr(call: ast.Call) -> ast.expr:
    assert len(call.args) == 1 and isinstance(call.args[0], ast.Tuple) and len(call.args[0].elts) == 2, (
        f"candidates.append() at line {call.lineno} doesn't match the expected "
        "(datetime, description) tuple shape this test assumes — update the test "
        "if this call's shape genuinely changed."
    )
    return call.args[0].elts[1]


def _find_time_source(expr: ast.expr, assignments: dict[str, ast.expr]) -> ast.AST | None:
    """Return the AST node (a `.strftime(...)` Call or a literal time-shaped
    Constant) responsible for embedding a clock time in `expr`, chasing simple
    Name references back to their assignment (bounded hops — see module docstring).
    Returns None if no time-of-day content is found.
    """
    seen_names: set[str] = set()
    stack: list[ast.AST] = [expr]
    hops = 0
    while stack and hops < 200:  # generous structural-node budget, not a "hop count" of Names
        node = stack.pop()
        hops += 1
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "strftime"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and isinstance(sub.args[0].value, str)
                and any(code in sub.args[0].value for code in _TIME_FORMAT_CODES)
            ):
                return sub
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and _LITERAL_TIME_RE.search(sub.value):
                return sub
            if isinstance(sub, ast.Name) and sub.id in assignments and sub.id not in seen_names:
                seen_names.add(sub.id)
                stack.append(assignments[sub.id])
    return None


def _banned_word_hits(expr: ast.expr) -> list[str]:
    hits = []
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found = _BANNED_WORD_RE.findall(sub.value)
            hits.extend(w.lower() for w in found)
    return hits


def _nearest_exception_slug(node: ast.AST, source_lines: list[str]) -> str | None:
    lineno = getattr(node, "lineno", None)
    if lineno is None:
        return None
    start = max(0, lineno - 1 - _COMMENT_SEARCH_WINDOW)
    for i in range(lineno - 1, start - 1, -1):
        match = _EXCEPTION_COMMENT_RE.search(source_lines[i])
        if match:
            return match.group(1)
    return None


def test_next_automation_candidates_have_no_unregistered_time_phrasing():
    _source_text, source_lines, tree = _read_source()
    registry = _find_registry(tree)
    fn_node = _find_target_function(tree)
    collector = _collect(fn_node)

    assert collector.append_calls, (
        f"No candidates.append() calls found inside {_TARGET_FN}() — this test's whole "
        "detection mechanism depends on that shape; something structural changed."
    )

    matched_slugs: set[str] = set()
    violations: list[str] = []

    for call in collector.append_calls:
        description = _description_expr(call)

        banned_hits = _banned_word_hits(description)
        if banned_hits:
            snippet = ast.get_source_segment(_source_text, description) or "<unavailable>"
            violations.append(
                f"line {call.lineno}: candidate contains banned mechanism word(s) "
                f"{sorted(set(banned_hits))} with no exception mechanism (CLAUDE.md Status "
                f"Card Ontology — Next Automation must never say these): {snippet!r}"
            )

        time_source = _find_time_source(description, collector.assignments)
        if time_source is None:
            continue

        slug = _nearest_exception_slug(time_source, source_lines)
        if slug is None:
            snippet = ast.get_source_segment(_source_text, description) or "<unavailable>"
            violations.append(
                f"line {call.lineno} (time embedded at line {time_source.lineno}): candidate embeds "
                f"a clock time with no `# ontology-exception: <slug>` comment within "
                f"{_COMMENT_SEARCH_WINDOW} lines above it. Per CLAUDE.md's Status Card Ontology, "
                f"Next Automation must not contain time-of-day phrasing unless registered as a "
                f"narrow, justified exception (see #534/#847-followup). Candidate text: {snippet!r}"
            )
            continue

        matched_slugs.add(slug)
        if slug not in registry:
            violations.append(
                f"line {call.lineno} (time embedded at line {time_source.lineno}): comment tags "
                f"exception slug {slug!r}, but it is not present in coordinator.py's "
                f"{_REGISTRY_NAME} set. Register it there, or this is an unauthorized exception."
            )

    assert not violations, (
        "Status Card Ontology violation(s) in " + _TARGET_FN + "():\n" + "\n".join(f"  - {v}" for v in violations)
    )

    # Symmetric check (mirrors _ALLOWED_CALL_SITES' both-directions discipline): every
    # registered slug must actually correspond to a found, matched exception comment —
    # catches a stale entry left behind after a candidate is renamed/removed.
    stale = sorted(registry - matched_slugs)
    assert not stale, (
        f"{_REGISTRY_NAME} contains slug(s) with no matching, reachable "
        f"`# ontology-exception: <slug>` comment in {_TARGET_FN}(): {stale}. Either the "
        "candidate was removed/renamed (delete the stale registry entry) or its comment "
        "was accidentally removed (restore it)."
    )
