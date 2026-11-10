"""Static guard: nothing outside automation.py writes an AutomationEngine private (Issue #1443).

Occupant view: the decisions Climate Advisor makes for the house read the coordinator's forecast, thermal model,
today's outdoor peak and TOU window live, never a copy that one path (or one test harness) forgot to refresh. Before
#1443 the coordinator hand-pushed copies into engine privates; #1440 was one that drifted on the real-HA tier.

Rule: the coordinator is the single owner of those facts and the engine holds no copy. It reads them through the
read-only inputs provider (``engine_inputs``); engine-owned state is changed only through public engine methods. The
only remaining writes are installs of providers/observers/callbacks (production) and scenario-modelling writes in the
harness, each listed below with its justification. An entry no longer present also fails, so each list only shrinks
deliberately.

Flagged forms, where X is ``*.automation_engine``, ``*.engine``, a name in ``_BASE_NAMES``, or any local alias bound
from one of those (``ae = self.automation_engine``, ``_engine = getattr(self, "automation_engine", None)``):
``X._a = ...`` / ``X._a += ...`` / ``del X._a`` / ``X._a[k] = ...`` / ``X._a.b = ...`` / ``X._a.append(...)`` (and
other mutating methods) / ``setattr(X, ...)`` / ``delattr(X, ...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_ENGINE_MODULE = REPO / "custom_components" / "climate_advisor" / "automation.py"
_BASE_NAMES = {"engine", "automation_engine", "eng", "ae"}
_ENGINE_ATTRS = {"automation_engine", "engine"}
_MUTATORS = {
    "append",
    "extend",
    "update",
    "pop",
    "popitem",
    "clear",
    "add",
    "discard",
    "remove",
    "insert",
    "setdefault",
    "sort",
    "reverse",
}

# (path relative to the repo, attribute) -> justification. Production: installs only.
PRODUCTION_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "custom_components/climate_advisor/coordinator.py",
        "_control_model_provider",
    ): "read-only provider install, once in __init__ (#1230 R6b): the primary engine's model scalars",
    (
        "custom_components/climate_advisor/coordinator.py",
        "_command_observer",
    ): "observer install, once in __init__ (#1128 promises): sees every command, changes nothing",
    (
        "custom_components/climate_advisor/coordinator.py",
        "_occupancy_bank_write",
    ): "callback install (#1409): always the same bound method, no state; set before each occupancy handler",
}

# Harness (Tier A tools/sim_harness/ and the real-HA adapter): kept separately from production.
HARNESS_ALLOWLIST: dict[tuple[str, str], str] = {
    ("tools/sim_harness/build_engine.py", "_emit_event_callback"): "bare-engine callback install (event capture)",
    ("tools/sim_harness/build_engine.py", "_revisit_callback"): "bare-engine callback install (no-op revisit)",
    ("tools/sim_harness/build_engine.py", "_sensor_check_callback"): "bare-engine callback install (no sensors)",
    (
        "tools/sim_harness/build_coordinator.py",
        "_emit_event_callback",
    ): "observer around the coordinator's own emit callback (calls it unchanged, captures the event)",
    ("tools/sim_harness/run_production.py", "_sensor_check_callback"): "engine-only mode callback install (tracker)",
    ("tools/sim_harness/promise_probe.py", "_command_observer"): "probe observer install",
    (
        "tools/sim_harness/run_production.py",
        "_manual_override_active",
    ): "scenario event occupancy_change_with_override: an override already in force (engine-owned precondition)",
    (
        "tests_ha/realha_session.py",
        "_manual_override_active",
    ): "scenario event occupancy_change_with_override: an override already in force (engine-owned precondition)",
    **{
        ("tools/sim_harness/run_production.py", a): "simulate_restart on a reused engine: __init__ defaults (#645)"
        for a in (
            "_paused_by_door",
            "_pre_pause_mode",
            "_paused_with_hvac_already_off",
            "_paused_entity",
            "_paused_since",
            "_natural_vent_active",
        )
    },
    **{
        ("tools/sim_harness/run_production.py", a): "simulate_restart on a reused engine: un-persisted observe-only "
        "WHF notice flags back to __init__ defaults, so the restart notifies again as a new engine does (#1513)"
        for a in ("_whf_observe_notified", "_whf_observe_rule2_notified")
    },
    **{
        ("tools/sim_harness/_smoke_test.py", a): "infrastructure smoke-test fixture, not a scenario run"
        for a in ("_current_classification", "_paused_by_door", "_pre_pause_mode", "_revisit_callback")
    },
    **{
        ("tests_ha/realha_session.py", a): "pre-#1443 trees only (scripts/revert_proof.py), guarded by "
        "hasattr(engine, 'inputs'); the coordinator is seeded on this tree"
        for a in ("_thermal_model", "_hourly_forecast_temps")
    },
}


def _aliases(tree: ast.AST) -> set[str]:
    names = set(_BASE_NAMES)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr in _ENGINE_ATTRS
            or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "getattr"
                and len(value.args) >= 2
                and isinstance(value.args[1], ast.Constant)
                and value.args[1].value in _ENGINE_ATTRS
            )
        ):
            names.add(node.targets[0].id)
    return names


def scan_source(source: str) -> list[tuple[int, str, str]]:
    """(line, kind, attribute) for every write of an engine private in ``source``."""
    tree = ast.parse(source)
    aliases = _aliases(tree)

    def is_engine(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in aliases) or (
            isinstance(node, ast.Attribute) and node.attr in _ENGINE_ATTRS
        )

    def engine_private(node: ast.AST) -> ast.Attribute | None:
        while isinstance(node, (ast.Subscript, ast.Attribute)):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and is_engine(node.value)
            ):
                return node
            node = node.value
        return None

    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                for elt in target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]:
                    found = engine_private(elt)
                    if found is not None:
                        hits.append((node.lineno, "assign" if elt is found else "nested", found.attr))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id in ("setattr", "delattr")
                and node.args
                and is_engine(node.args[0])
            ):
                name = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else "?"
                hits.append((node.lineno, func.id, str(name)))
            elif isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                found = engine_private(func.value)
                if found is not None:
                    hits.append((node.lineno, f".{func.attr}()", found.attr))
    return hits


def _scan(paths: list[Path]) -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        if path.resolve() == _ENGINE_MODULE.resolve():
            continue
        rel = path.relative_to(REPO).as_posix()
        for line, kind, attr in scan_source(path.read_text(encoding="utf-8")):
            found.setdefault((rel, attr), []).append(f"{rel}:{line} {kind} {attr}")
    return found


def _check(found: dict[tuple[str, str], list[str]], allowlist: dict[tuple[str, str], str]) -> None:
    unlisted = [site for key, sites in sorted(found.items()) if key not in allowlist for site in sites]
    assert not unlisted, (
        "An AutomationEngine private is written outside automation.py (Issue #1443). The coordinator owns the fact: "
        "read it through engine.inputs (engine_inputs.py), pass it as an argument, or add a public engine method:\n  "
        + "\n  ".join(unlisted)
    )
    stale = sorted(set(allowlist) - set(found))
    assert not stale, f"Allowlist entries no longer present (remove them): {stale}"


def test_production_writes_no_engine_private():
    _check(_scan(sorted((REPO / "custom_components").rglob("*.py"))), PRODUCTION_ALLOWLIST)


def test_harness_writes_no_engine_private():
    paths = sorted((REPO / "tools" / "sim_harness").rglob("*.py")) + [REPO / "tests_ha" / "realha_session.py"]
    _check(_scan(paths), HARNESS_ALLOWLIST)


def test_every_allowlist_entry_is_justified():
    for key, why in {**PRODUCTION_ALLOWLIST, **HARNESS_ALLOWLIST}.items():
        assert isinstance(why, str) and len(why) > 15, key


def test_scanner_catches_every_form():
    """The scanner is not vacuous: one snippet per flagged form (and the read/public-method forms it allows)."""
    src = """
ae = self.automation_engine
_ae_x = self.automation_engine
_eng = getattr(self, "automation_engine", None)
self.automation_engine._a = 1
coordinator.engine._b = 2
ae._c += 1
_ae_x._d = 3
_eng._e = 4
del engine._f
engine._g["k"] = 5
engine._h.sub = 6
engine._i.append(1)
self.automation_engine._j.update({})
setattr(engine, "_k", 1)
delattr(ae, "_l")
x = engine._m            # a read: allowed
engine.note_equipment_ran("heat", now)   # public method: allowed
engine.public = 1        # not private: allowed
self._own = 1            # not the engine: allowed
"""
    attrs = sorted(attr for _, _, attr in scan_source(src))
    assert attrs == ["_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_i", "_j", "_k", "_l"]


# --------------------------------------------------------------------------- the facts are passed in (owner decision)
def _decision_methods() -> set[str]:
    """Every AutomationEngine method that reaches ``_facts`` (reads a coordinator-owned fact) on some path."""
    tree = ast.parse(_ENGINE_MODULE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AutomationEngine")
    calls: dict[str, set[str]] = {}
    for fn in cls.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls[fn.name] = {
                n.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
            }
    reach = {"_facts"}
    changed = True
    while changed:
        changed = False
        for name, used in calls.items():
            if name not in reach and used & reach:
                reach.add(name)
                changed = True
    return reach - {"_facts", "decision_facts"}


def test_every_coordinator_decision_call_passes_its_facts_in():
    """Owner decision (#1443, "number 2"): values passed in at the decision call, nothing looked up live.

    A direct call into an engine method that reaches a coordinator-owned fact would make the engine build the fact
    itself; every such call outside automation.py goes through ``engine_inputs.decide`` / ``decide_sync``, which pass
    a frozen snapshot computed at the call. (Engine-internal timers and the read-only readers inside automation.py
    are the documented exception: they have no coordinator call frame.)
    """
    methods = _decision_methods()
    assert {"handle_door_window_open", "check_natural_vent_conditions", "handle_occupancy_away"} <= methods
    direct: list[str] = []
    for path in sorted((REPO / "custom_components").rglob("*.py")):
        if path.resolve() == _ENGINE_MODULE.resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in methods:
                continue
            owner = node.func.value
            if (isinstance(owner, ast.Name) and owner.id in aliases) or (
                isinstance(owner, ast.Attribute) and owner.attr in _ENGINE_ATTRS
            ):
                direct.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} {node.func.attr}")
    assert not direct, "engine decision called without passing the coordinator's facts in:\n  " + "\n  ".join(direct)
