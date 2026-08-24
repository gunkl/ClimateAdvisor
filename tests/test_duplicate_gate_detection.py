"""Issue #737: AST-based structural-duplication ("DRY") checker for gate/threshold
conditions — flags 2+ functions independently reimplementing the SAME logical
comparison/boolean-gate condition, even when variable names and surrounding code
differ. Same registry-driven enforcement shape as the other 3 static AST checks in
this test suite (``test_fan_fsm_single_caller.py``, ``test_fsm_flag_ownership.py``,
``test_shadow_engine_coverage.py``): a scan finds candidate findings, every finding
must be explicitly classified in a registry, and an unclassified finding fails the
build. This one classifies *structural duplicate conditions* instead of call
counts/flag writers/coverage.

**Why textual/line diffing is not enough.** The known real bug pattern in this
codebase (CLAUDE.md's "sibling threshold drift", e.g. nat-vent Issues #400/#402/
#417/#456/#458) is a gate condition re-typed by hand at a second call site, with
different variable names and sometimes an extra null-guard, that silently drifts
from the original as one copy gets fixed and the other doesn't. A textual/line
diff tool would not flag ``current_temp <= _hard_floor`` as related to
``inputs.indoor is not None and inputs.indoor <= vent_floor`` — different names,
different token count. This checker instead extracts each ``if``/``elif`` boolean
test as an AST subtree, canonicalizes it (operands become positional ``V0``/``V1``
tokens assigned in order of first appearance — an alpha-renaming, so operand
*identity* doesn't matter, only *structural role* does), and additionally strips a
redundant ``X is not None`` guard clause when ``X`` is also compared for real in a
sibling clause of the same ``and`` (the single systematic textual difference the
None-guard convention introduces without changing the actual decision). Two
conditions with the same canonical form are the same logical gate, regardless of
naming or an added null-guard.

**Resolved (Issue #757 Phase 6 Step 5).** ``nat_vent_temperature_check()`` used
to have an ``if self._natvent_fsm_authoritative: ... else: ...`` split whose
legacy ``else`` branch hand-rolled a narrower 2-check subset of
``decide_nat_vent_exit()``'s own first two priority checks
(manual-override-conflict, then comfort-floor) instead of calling it directly —
a real, live duplication for as long as the legacy branch existed
(``_natvent_fsm_authoritative`` defaulted ``False`` and no production
coordinator ever flipped it). Step 5 deleted the legacy branch entirely once
the FSM path had been production-authoritative for weeks with zero corpus
divergence — the sole surviving branch now calls ``decide_nat_vent_exit()``
directly, unconditionally, closing this finding. The registry entry and its
dedicated regression test (``test_known_natvent_duplicate_is_detected``) were
removed accordingly, per this module's own "if this now fails... celebrate,
then remove" resolution note.

``docs/nat-vent-lifecycle-spec.md`` also names ``fan_thermostat_check()`` and
``check_natural_vent_conditions()`` as having the same duplication shape (citing
Issue #608). Direct reading of both (automation.py ~L5240, ~L3927) found this is
now STALE: ``fan_thermostat_check()`` was unified via ``decide_fan_thermostat_check()``
(Issue #435 architecture-reset — no inline re-check remains, FSM-authoritative or
not) and ``check_natural_vent_conditions()`` calls ``decide_nat_vent_exit()``
directly and unconditionally for its exit chain (no legacy hand-rolled duplicate
branch at all) — both already fully consolidated.

**Scope of the scan.** Every method on ``AutomationEngine`` (automation.py) plus
every module-level function in the "pure decision leaf" sibling modules — the
files this project's architecture-reset methodology (CLAUDE.md, Issue #441/#608)
already established as the class of code where a gate condition is meant to have
exactly one implementation. Scanning the whole repo (sensors, API views, config
flow, dashboard) would mostly find coincidental unrelated comparisons; scoping to
the decision-leaf family targets the actual hazard class without drowning in noise
— matches the narrow-scope convention the 3 precedent checkers already use
(``fan_fsm.py``/``automation.py`` only; ``AutomationEngine`` class only).
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_INTEGRATION_DIR = _REPO_ROOT / "custom_components" / "climate_advisor"
_AUTOMATION_PY = _INTEGRATION_DIR / "automation.py"

# Module-level "pure decision leaf" files scanned in full (every module-level
# function). These are the sibling modules the architecture-reset methodology
# (Issue #441 and successors) already extracts gate/threshold logic into — the
# class of file where "same condition, second implementation" is a real hazard,
# as opposed to incidental comparisons scattered across sensors/API/config-flow
# code that happen to use similar operators for unrelated reasons.
_LEAF_MODULES = [
    "door_window_fsm.py",
    "door_window_lifecycle.py",
    "fan_fsm.py",
    "fan_lifecycle.py",
    "fan_thermostat_decision.py",
    "nat_vent_exit.py",
    "nat_vent_fsm.py",
    "nat_vent_gate.py",
    "nat_vent_lifecycle.py",
    "nat_vent_reactivation_lockout.py",
    "override_grace_fsm.py",
    "override_grace_lifecycle.py",
    "setpoint_verify_decision.py",
]

# Value-comparison operator names that count as a "real" comparison for the
# has-real-comparison filter below. Deliberately excludes Is/IsNot (None-existence
# checks — ubiquitous null guards with no distinguishing structure) AND Eq/NotEq
# (validated empirically: bare `state_var == other_var`/`mode == "x"` equality
# checks are extremely common and structurally trivial in this codebase — string/
# enum/mode comparisons scattered everywhere for unrelated reasons — and an
# uncalibrated first pass including Eq produced dozens of unrelated matches, e.g.
# apply_classification() vs override_grace_fsm.py's transition helpers, none of
# which share any real gate logic). Threshold comparisons (Lt/LtE/Gt/GtE) and
# membership tests (In/NotIn) are the operators that actually carry the
# distinguishing "same reasoning" signal this checker targets.
_REAL_COMPARE_OPS = {"Lt", "LtE", "Gt", "GtE", "In", "NotIn"}


# ---------------------------------------------------------------------------
# AST canonicalization
# ---------------------------------------------------------------------------


def _identity_key(node: ast.expr) -> str | None:
    """Dotted-path identity for a Name/Attribute chain (e.g. "self._fan_active",
    "inputs.indoor"), or None if node isn't a simple identity reference. Used only
    to (a) assign consistent canonical variable indices within ONE expression and
    (b) detect a None-guard clause and the sibling clause it protects — never
    compared across functions directly (that would defeat the whole point of
    canonicalizing away naming differences)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _identity_key(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _referenced_identity_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Name, ast.Attribute)):
            key = _identity_key(sub)
            if key:
                keys.add(key)
    return keys


def _is_none_guard(node: ast.expr) -> str | None:
    """If node is exactly `X is None` or `X is not None` (single-op Compare against
    a bare None constant), return X's identity key. Else None."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    if not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
        return None
    comparator = node.comparators[0]
    if isinstance(comparator, ast.Constant) and comparator.value is None:
        return _identity_key(node.left)
    if isinstance(node.left, ast.Constant) and node.left.value is None:
        return _identity_key(comparator)
    return None


class _Canon:
    """Canonicalizes one boolean test expression into a hashable tuple, alpha-
    renaming operand identities to positional V0/V1/... tokens (assigned in order
    of first appearance within THIS expression only) so that two structurally
    identical conditions compare equal regardless of the actual variable/attribute
    names used at each call site."""

    def __init__(self) -> None:
        self._var_map: dict[str, int] = {}

    def _var_token(self, node: ast.expr) -> tuple:
        key = _identity_key(node)
        if key is None:
            return ("VAR", "?")
        idx = self._var_map.setdefault(key, len(self._var_map))
        return ("VAR", idx)

    def canon(self, node: ast.expr) -> tuple:
        if isinstance(node, ast.BoolOp):
            values = list(node.values)
            # Strip a redundant `X is (not) None` guard when X is also referenced
            # (for real) in a sibling clause of the same and/or — the single
            # systematic textual difference the null-guard convention introduces
            # without changing the actual decision (see module docstring).
            other_refs: set[str] = set()
            for v in values:
                if _is_none_guard(v) is None:
                    other_refs |= _referenced_identity_keys(v)
            kept = []
            for v in values:
                guard_key = _is_none_guard(v)
                if guard_key is not None and guard_key in other_refs:
                    continue
                kept.append(v)
            if not kept:
                kept = values  # degenerate: everything was a guard, keep as-is
            if len(kept) == 1:
                return self.canon(kept[0])
            return ("BOOLOP", type(node.op).__name__, tuple(self.canon(v) for v in kept))

        if isinstance(node, ast.UnaryOp):
            return ("UNARY", type(node.op).__name__, self.canon(node.operand))

        if isinstance(node, ast.Compare):
            ops = tuple(type(op).__name__ for op in node.ops)
            left_c = self.canon(node.left)
            comps_c = tuple(self.canon(c) for c in node.comparators)
            return ("CMP", ops, left_c, comps_c)

        if isinstance(node, ast.Constant):
            if node.value is None:
                return ("NONE",)
            if isinstance(node.value, bool):
                return ("BOOL",)
            if isinstance(node.value, (int, float)):
                return ("NUM",)
            if isinstance(node.value, str):
                return ("STR",)
            return ("CONST",)

        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return (type(node).__name__.upper(), tuple(self.canon(e) for e in node.elts))

        if isinstance(node, ast.Call):
            func_name = "?"
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            return ("CALL", func_name, tuple(self.canon(a) for a in node.args))

        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._var_token(node)

        # Fallback for anything not modeled above (Subscript, IfExp, ...) — kept
        # distinguishable but not expected to match across functions.
        return ("OTHER", ast.dump(node))


def _canon_test(node: ast.expr) -> tuple:
    return _Canon().canon(node)


def _has_real_comparison(canon: tuple) -> bool:
    """True if canon contains at least one CMP node whose operator(s) are not
    purely Is/IsNot null-existence checks — the minimum-complexity bar that keeps
    this checker from flagging ubiquitous generic guards (`if x:`, `if not x:`,
    `if x is None:`) that carry no distinguishing structure of their own."""
    if not isinstance(canon, tuple) or not canon:
        return False
    if canon[0] == "CMP":
        ops = canon[1]
        if any(o in _REAL_COMPARE_OPS for o in ops):
            return True
    return any(_has_real_comparison(c) for c in canon if isinstance(c, tuple))


def _has_membership_test(canon: tuple) -> bool:
    """True if canon contains a CMP with an In/NotIn op — see _extract_function_gates'
    Group A docstring for why standalone (non-paired) matching is restricted to this
    rarer, more specific idiom."""
    if not isinstance(canon, tuple) or not canon:
        return False
    if canon[0] == "CMP" and any(o in ("In", "NotIn") for o in canon[1]):
        return True
    return any(_has_membership_test(c) for c in canon if isinstance(c, tuple))


# ---------------------------------------------------------------------------
# Per-function extraction: standalone compound conditions + adjacent-pair chains
# ---------------------------------------------------------------------------


def _block_children(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Nested statement-list ("block") fields of stmt that should be scanned as
    their own independent chain contexts (excluding an If's elif orelse, which
    _collect_chains flattens into the SAME chain rather than a new one)."""
    blocks: list[list[ast.stmt]] = []
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        blocks.append(stmt.body)
        if stmt.orelse:
            blocks.append(stmt.orelse)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        blocks.append(stmt.body)
    elif isinstance(stmt, ast.Try):
        blocks.append(stmt.body)
        for h in stmt.handlers:
            blocks.append(h.body)
        if stmt.orelse:
            blocks.append(stmt.orelse)
        if stmt.finalbody:
            blocks.append(stmt.finalbody)
    return blocks


def _collect_chains(stmts: list[ast.stmt], chains: list[list[ast.expr]]) -> None:
    """Walks a statement list, building one ordered "chain" of if/elif tests (and
    sequential sibling early-return-style ifs interleaved with other statements)
    per block, appending each chain of length >= 2 to `chains`. Recurses into
    every nested block (if-true-branch, else-branch, with/for/while/try bodies) as
    its own independent chain context."""
    current: list[ast.expr] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            node: ast.If = stmt
            while True:
                current.append(node.test)
                _collect_chains(node.body, chains)
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    node = node.orelse[0]
                    continue
                if node.orelse:
                    _collect_chains(node.orelse, chains)
                break
        else:
            for blk in _block_children(stmt):
                _collect_chains(blk, chains)
    if len(current) >= 2:
        chains.append(current)


class _FunctionGates:
    __slots__ = ("standalone", "pairs")

    def __init__(self) -> None:
        self.standalone: set[tuple] = set()
        self.pairs: set[tuple[tuple, tuple]] = set()


def _extract_function_gates(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> _FunctionGates:
    result = _FunctionGates()

    # Group A: standalone compound (BoolOp) conditions anywhere in the function.
    # Restricted to BOOLOPs containing an In/NotIn membership test (not a bare
    # Lt/LtE/Gt/GtE threshold) — calibration finding: a 2-clause "flag AND
    # threshold-compare" shape (e.g. `active and temp <= floor`) is common enough
    # on its own, for entirely unrelated thresholds, that matching it standalone
    # produced a false positive (handle_pre_cool()'s pre-cool-already-achieved
    # check vs nat_vent_temperature_check()'s fan-cycling off_threshold — same
    # shape, unrelated reasoning). An In/NotIn-against-a-literal-set clause (e.g.
    # `mode not in (None, "off")`) is a much rarer, more specific idiom; bare
    # threshold compares are still caught, but only when they show up as part of
    # a matching ADJACENT PAIR (Group B) — a much stronger signal since it
    # requires two conditions to line up in the same relative order, not one.
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            c = _canon_test(node.test)
            if c[0] == "BOOLOP" and _has_real_comparison(c) and _has_membership_test(c):
                result.standalone.add(c)

    # Group B: adjacent-pair signatures from if/elif and sequential-guard chains.
    # Skips a degenerate pair whose two conditions are themselves the SAME
    # canonical shape (a != b required) — calibration finding: an elif ladder
    # branching on the same variable against different literal sets (e.g.
    # `fan_mode in (WHOLE_HOUSE, BOTH)` then `fan_mode in (HVAC, BOTH)`) produces
    # two adjacent conditions with an identical generic shape purely because
    # they're sibling branches of ONE decision, not because two DIFFERENT
    # functions independently reimplemented the same two-step reasoning (the
    # real hazard this checker targets, where step 1's shape differs from step
    # 2's — e.g. a compound override-conflict check followed by a threshold
    # compare, per the confirmed nat-vent proof case).
    chains: list[list[ast.expr]] = []
    _collect_chains(fn.body, chains)
    for chain in chains:
        canon_chain = [_canon_test(t) for t in chain]
        for a, b in itertools.pairwise(canon_chain):
            if a != b and _has_real_comparison(a) and _has_real_comparison(b):
                result.pairs.add((a, b))

    return result


# ---------------------------------------------------------------------------
# File/function universe
# ---------------------------------------------------------------------------


def _automation_engine_methods() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    engine_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    out = []
    for node in engine_class.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((f"automation.py:AutomationEngine.{node.name}", node))
    return out


def _leaf_module_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out = []
    for filename in _LEAF_MODULES:
        path = _INTEGRATION_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{filename}:{node.name}", node))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append((f"{filename}:{node.name}.{sub.name}", sub))
    return out


def _all_scanned_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    return _automation_engine_methods() + _leaf_module_functions()


def _find_duplicate_gate_pairs() -> dict[frozenset[str], list[str]]:
    """Returns {frozenset({label_a, label_b}): [evidence, ...]} for every pair of
    scanned functions sharing a structurally-identical gate condition (standalone
    compound, or an adjacent same-order pair within an if/elif or sequential-guard
    chain)."""
    per_function: dict[str, _FunctionGates] = {}
    for label, node in _all_scanned_functions():
        per_function[label] = _extract_function_gates(node)

    standalone_map: dict[tuple, set[str]] = {}
    pair_map: dict[tuple, set[str]] = {}
    for label, gates in per_function.items():
        for sig in gates.standalone:
            standalone_map.setdefault(sig, set()).add(label)
        for sig in gates.pairs:
            pair_map.setdefault(sig, set()).add(label)

    found: dict[frozenset[str], list[str]] = {}
    for sig, labels in standalone_map.items():
        if len(labels) < 2:
            continue
        for a, b in itertools.combinations(sorted(labels), 2):
            found.setdefault(frozenset((a, b)), []).append(f"standalone compound condition: {sig}")
    for sig, labels in pair_map.items():
        if len(labels) < 2:
            continue
        for a, b in itertools.combinations(sorted(labels), 2):
            found.setdefault(frozenset((a, b)), []).append(f"adjacent guard-pair: {sig}")

    return found


# ---------------------------------------------------------------------------
# Registry — every real finding must be explicitly acknowledged, same shape as
# _COVERAGE_REGISTRY / _FLAG_OWNERS in the precedent checkers.
#
# Each entry's key is the full set of functions sharing one gate condition (not
# necessarily just 2 — see the manual-override-conflict entry below, which
# covers all C(4,2)=6 pairwise findings among its 4 members with one entry).
# Each entry's value is (classification, rationale):
#   "real_duplicate_debt"    — a genuine second (or third/fourth) hand-written
#                               copy of the same gate condition; tracked as
#                               technical debt per CLAUDE.md's "sibling
#                               threshold drift" concern, not yet consolidated.
#   "reviewed_not_a_duplicate" — manually investigated and confirmed to be a
#                               coincidental structural match (two genuinely
#                               different decisions that happen to reduce to
#                               the same canonical AST shape), not the same
#                               reasoning duplicated. Documented rather than
#                               silently tuned away, so a future reviewer can
#                               see why it's here and re-open the question if
#                               the surrounding code changes.
# A found pair is acknowledged if some registry key's member set is a superset
# of that pair (not just an exact-pair key match) — this is what lets the
# 4-member manual-override group collapse into one entry.
# ---------------------------------------------------------------------------

_ACKNOWLEDGED_DUPLICATE_GATES: dict[frozenset[str], tuple[str, str]] = {
    frozenset(
        {
            "automation.py:AutomationEngine.nat_vent_temperature_check",
            "automation.py:AutomationEngine._activate_fan",
            "fan_thermostat_decision.py:decide_fan_thermostat_check",
            "nat_vent_exit.py:decide_nat_vent_exit",
        }
    ): (
        "real_duplicate_debt",
        "Issue #705/#714/#737: the manual-override-conflict check "
        "(`X_active and X_mode not in (None, 'off')`) is independently "
        "hand-typed in FOUR places. _activate_fan()'s own comment (automation.py "
        "~L9028-9034) explicitly documents this as deliberate — it says it "
        "'[m]irrors the _fan_override_active guard immediately above' and is 'the "
        "entry-side half of the #705 bug; the exit-side half is handled by the new "
        "MANUAL_OVERRIDE_CONFLICT checks in nat_vent_exit.py/"
        "fan_thermostat_decision.py' — i.e. this is a known, intentional "
        "entry-gate-vs-exit-gate split, not an accident, but it IS the same "
        "underlying condition typed 4 times by hand with no shared source of "
        "truth. If the manual-override-mode exclusion set (currently `(None, "
        "'off')`) or the flag names ever change, all 4 copies must be updated "
        "together or they will silently drift — exactly the hazard class this "
        "checker exists to surface. A future consolidation (e.g. a shared "
        "`is_manual_override_conflict(active, mode)` pure helper in "
        "fan_thermostat_decision.py, imported by all 4 sites) would let this "
        "entry be removed.",
    ),
    frozenset(
        {
            "automation.py:AutomationEngine._build_fan_fsm_inputs",
            "automation.py:AutomationEngine._reconcile_fan_physical_drift",
        }
    ): (
        "real_duplicate_debt",
        "Issue #731/#737: both methods hand-type the IDENTICAL 4-clause physical-"
        "state-read laziness guard (`self._fan_active and fan_mode in "
        "(FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and not recent_fan_command and "
        "physical_state_available`), using the same variable names, with "
        "_build_fan_fsm_inputs()'s own docstring explicitly stating '[p]hysical_on "
        "is read LAZILY, replicating _reconcile_fan_physical_drift()'s own "
        "laziness guard exactly.' This is a self-documented literal duplicate, not "
        "a coincidence — real (currently dormant, per that docstring: "
        "'[n]ot yet consulted by any production call site') tracked debt. If "
        "either copy's threshold-30s or fan-mode-tuple changes without the other, "
        "they will silently diverge once Phase 5 wires _build_fan_fsm_inputs() "
        "into production. Resolve by extracting the guard into one shared helper "
        "both call, then remove this registry entry.",
    ),
    frozenset(
        {
            "automation.py:AutomationEngine._build_override_grace_fsm_inputs",
            "automation.py:AutomationEngine.handle_occupancy_home",
        }
    ): (
        "reviewed_not_a_duplicate",
        "Issue #737 calibration: both reduce to the generic canonical shape "
        "AND(IsNot(V0, None), In(V1, (STR, STR))), but manual read confirms they "
        "check UNRELATED operand pairs — "
        "_build_override_grace_fsm_inputs() checks `classification is not None "
        "and classification_mode in ('heat', 'cool')` (classification nullness "
        "gating classification's OWN mode field), while handle_occupancy_home() "
        "checks `indoor_temp is not None and c.hvac_mode in ('heat', 'cool')` "
        "(indoor_temp availability gating a DIFFERENT object's hvac_mode field). "
        "The None-guard in each doesn't protect the same value the In-check reads "
        "(unlike the real None-guard-stripping case this checker's own docstring "
        "describes), so this is coincidental reuse of the common "
        "'guard-not-None AND mode-in-(heat,cool)' idiom, not the same reasoning "
        "duplicated. Kept as a documented exemption rather than silently "
        "tightening the detector's threshold further, since 'mode in "
        "(heat, cool)' guarded by an adjacent not-None check is a common enough "
        "idiom in this codebase that over-fitting the filter to exclude it here "
        "risks losing real future findings elsewhere.",
    ),
}


def _acknowledgment_for(pair: frozenset[str]) -> tuple[str, str] | None:
    """A found pair is covered if some registry key's member set is a superset of
    it (lets one registry entry cover every pairwise combination within a larger
    N-way group, e.g. the 4-member manual-override-conflict entry)."""
    for members, classification in _ACKNOWLEDGED_DUPLICATE_GATES.items():
        if pair <= members:
            return classification
    return None


class TestDuplicateGateDetection:
    def test_no_unacknowledged_duplicate_gates(self) -> None:
        """Every duplicate-gate finding in the scanned decision-leaf universe must
        be explicitly registered in _ACKNOWLEDGED_DUPLICATE_GATES (either
        "real_duplicate_debt" or "reviewed_not_a_duplicate"). A brand-new,
        unregistered finding means a gate condition was just re-implemented a
        second time instead of reusing the existing pure function — exactly the
        sibling-threshold-drift bug class this checker exists to catch."""
        found = _find_duplicate_gate_pairs()
        unacknowledged = {pair: evidence for pair, evidence in found.items() if _acknowledgment_for(pair) is None}
        assert not unacknowledged, (
            "New structurally-duplicate gate condition(s) found that aren't in "
            "_ACKNOWLEDGED_DUPLICATE_GATES:\n"
            + "\n".join(f"  {sorted(pair)}: {ev}" for pair, ev in unacknowledged.items())
            + "\nEither reuse the existing implementation instead of re-typing the "
            "condition, or — if this is genuinely intentional/unavoidable, or a "
            "reviewed coincidental structural match — add an entry to "
            "_ACKNOWLEDGED_DUPLICATE_GATES with a classification and rationale "
            "(Issue #737)."
        )

    def test_registry_entries_reference_real_functions(self) -> None:
        """Catches a registry entry going stale after a rename/removal — every
        label in _ACKNOWLEDGED_DUPLICATE_GATES must still resolve to a real scanned
        function."""
        all_labels = {lbl for lbl, _ in _all_scanned_functions()}
        for pair in _ACKNOWLEDGED_DUPLICATE_GATES:
            for label in pair:
                assert label in all_labels, (
                    f"_ACKNOWLEDGED_DUPLICATE_GATES references {label!r}, which no "
                    f"longer resolves to a scanned function (renamed or removed?) — "
                    f"update the registry."
                )

    def test_positive_control_unacknowledged_finding_fails_enforcement(self) -> None:
        """Proves test_no_unacknowledged_duplicate_gates' own enforcement logic
        actually fails on a genuinely unacknowledged pair, not just passing
        vacuously — same shape as
        test_shadow_engine_coverage.py::test_positive_control_unregistered_method_is_caught."""
        fake_pair = frozenset({"totally_new_module.py:brand_new_fn_a", "totally_new_module.py:brand_new_fn_b"})
        assert _acknowledgment_for(fake_pair) is None

    def test_positive_control_synthetic_duplicate_is_caught(self) -> None:
        """Proves the extraction+matching pipeline actually fires on a genuine
        duplicate, not just that today's registry happens to line up — same
        methodology as the other 3 checkers' own positive-control tests. Two
        independent synthetic functions reimplement the identical gate with
        different names/thresholds; the detector must still match them."""
        src_a = """
def check_a(current_temp, floor_a):
    if current_temp <= floor_a:
        return "exit"
    return "continue"
"""
        src_b = """
def check_b(indoor, vent_floor):
    if indoor is not None and indoor <= vent_floor:
        return "exit"
    return "continue"
"""
        fn_a = ast.parse(src_a).body[0]
        fn_b = ast.parse(src_b).body[0]
        gates_a = _extract_function_gates(fn_a)
        gates_b = _extract_function_gates(fn_b)

        # Each function's sole condition, standing alone (not part of a >=2 chain),
        # only shows up in `standalone` if it's a BOOLOP — check_a's bare CMP isn't
        # captured there, so assert via a direct canon-equality check instead, which
        # is what the pairwise chain matcher relies on when these conditions DO
        # appear adjacent to a sibling gate (the real nat-vent case has exactly this
        # shape — see test_known_natvent_duplicate_is_detected).
        canon_a = next(c for c in ast.walk(fn_a) if isinstance(c, ast.If))
        canon_b = next(c for c in ast.walk(fn_b) if isinstance(c, ast.If))
        assert _canon_test(canon_a.test) == _canon_test(canon_b.test), (
            "Synthetic duplicate conditions (bare comfort-floor compare vs the same "
            "compare wrapped in a redundant None-guard) failed to canonicalize to "
            "the same signature — the detector would not catch a real instance of "
            "this exact pattern."
        )
        assert not gates_a.standalone and not gates_b.standalone  # bare CMP, not BOOLOP — correct non-match here

        # Now prove the full chain-pair path: pair each condition with an identical
        # sibling "manual override conflict"-shaped compound guard immediately
        # before it, exactly like the real nat-vent case's elif-chain / sequential-
        # if shape, and confirm the two functions' pair signatures collide.
        src_a2 = """
def check_a2(active, mode, current_temp, floor_a):
    if active and mode not in (None, "off"):
        return "conflict"
    elif current_temp <= floor_a:
        return "exit"
    return "continue"
"""
        src_b2 = """
def check_b2(manual_active, manual_mode, indoor, vent_floor):
    if manual_active and manual_mode not in (None, "off"):
        return "conflict"
    if indoor is not None and indoor <= vent_floor:
        return "exit"
    return "continue"
"""
        fn_a2 = ast.parse(src_a2).body[0]
        fn_b2 = ast.parse(src_b2).body[0]
        gates_a2 = _extract_function_gates(fn_a2)
        gates_b2 = _extract_function_gates(fn_b2)
        assert gates_a2.pairs & gates_b2.pairs, (
            "Synthetic elif-chain (check_a2) vs sequential-if (check_b2) reimplementing "
            "the identical (manual-override-conflict, comfort-floor) pair failed to "
            "produce a matching pair signature — the detector would not catch the real "
            "nat_vent_temperature_check() vs decide_nat_vent_exit() duplication."
        )
        # And the compound manual-override clause alone also matches standalone,
        # since it's a BOOLOP with a real (NotIn) comparison.
        assert gates_a2.standalone & gates_b2.standalone
