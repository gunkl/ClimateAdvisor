"""Guards for the shared ``homeassistant.util.dt`` stub (Issue #1186).

The stub used to be two unrelated MagicMocks (``homeassistant.util`` and ``homeassistant.util.dt``),
so ``from homeassistant.util import dt`` (what production binds) was not the object tests assigned
``now`` on, ``parse_datetime`` returned MagicMocks, and ~40 test files overwrote one shared clock
at import time (last importer wins). These tests keep that from coming back:

* a fresh interpreter proves the stub's defaults are real and that both access paths are one object;
* an AST scan fails if any test module assigns ``homeassistant.util.dt`` attributes at import time
  (use the ``frozen_now`` fixture from tests/conftest.py).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TESTS = _REPO / "tests"

_FRESH_INTERPRETER_CHECK = textwrap.dedent(
    """
    import sys
    from datetime import datetime
    from unittest.mock import MagicMock

    sys.path.insert(0, {repo!r})
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()
    install_ha_stubs()  # idempotent

    from homeassistant.util import dt as bound

    mod = sys.modules["homeassistant.util.dt"]
    assert bound is mod, "util.dt attribute and sys.modules entry are different objects"
    assert sys.modules["homeassistant"].util is sys.modules["homeassistant.util"]

    for fn in ("now", "utcnow", "as_local", "as_utc", "parse_datetime", "start_of_local_day", "get_time_zone"):
        assert not isinstance(getattr(bound, fn), MagicMock), fn

    now = bound.now()
    assert isinstance(now, datetime) and now.tzinfo is not None, "now() must be an aware datetime"
    utc = bound.utcnow()
    assert isinstance(utc, datetime) and utc.utcoffset().total_seconds() == 0
    assert bound.as_local(datetime(2026, 1, 1, 12)).tzinfo is not None, "as_local must return aware"
    parsed = bound.parse_datetime("2026-01-01T00:00:00+00:00")
    assert isinstance(parsed, datetime) and parsed.tzinfo is not None
    assert bound.parse_datetime(None) is None and bound.parse_datetime("garbage") is None
    assert bound.DEFAULT_TIME_ZONE is not None and not isinstance(bound.DEFAULT_TIME_ZONE, MagicMock)

    # production binds the same object
    from custom_components.climate_advisor import automation, chart_log, coordinator

    for m in (automation, chart_log, coordinator):
        assert m.dt_util is bound, m.__name__

    # reset() undoes a stray assignment
    bound.now = lambda: None
    bound.reset()
    assert bound.now() is not None
    print("OK")
    """
)


def test_dt_stub_defaults_are_real_and_one_object_in_a_fresh_interpreter() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH_INTERPRETER_CHECK.format(repo=str(_REPO))],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0 and proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr


def _target_text(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return ""


def _is_dt_target(target: ast.expr) -> bool:
    """True for ``X.attr = ...`` where X is the HA dt stub under any spelling."""
    if not isinstance(target, ast.Attribute):
        return False
    base = _target_text(target.value)
    return (
        "homeassistant.util" in base
        or base.endswith("util.dt")
        or base.split(".")[-1] in {"dt_util", "dt_mod", "_ha_util_dt", "ha_util_dt"}
        or base in {"_ha_util", "ha_util"}
    )


def _module_level_statements(body: list[ast.stmt]):
    """Yield statements that run at import time (descends if/try/with/for, not def/class)."""
    for node in body:
        yield node
        if isinstance(node, ast.If | ast.For | ast.While | ast.With | ast.Try):
            for field in ("body", "orelse", "finalbody"):
                yield from _module_level_statements(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _module_level_statements(handler.body)


def _dt_aliases(tree: ast.Module) -> set[str]:
    """Names bound to the HA dt module (``import ...util.dt as X`` / ``from ...util import dt as X``)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "homeassistant.util.dt" and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "homeassistant.util":
            for a in node.names:
                if a.name == "dt":
                    aliases.add(a.asname or "dt")
    return aliases


def _is_ha_modules_subscript(target: ast.expr) -> bool:
    """True for ``sys.modules["homeassistant.util..."] = ...``."""
    if not isinstance(target, ast.Subscript) or _target_text(target.value) != "sys.modules":
        return False
    key = target.slice
    return isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.startswith("homeassistant.util")


def find_import_time_dt_assignments(source: str) -> list[int]:
    tree = ast.parse(source)
    aliases = _dt_aliases(tree)

    def is_target(t: ast.expr) -> bool:
        if _is_ha_modules_subscript(t) or _is_dt_target(t):
            return True
        return isinstance(t, ast.Attribute) and _target_text(t.value) in aliases

    lines: list[int] = []
    for node in _module_level_statements(tree.body):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (
                isinstance(func, ast.Name)
                and func.id == "setattr"
                and node.value.args
                and is_target(ast.Attribute(value=node.value.args[0], attr="x", ctx=ast.Store()))
            ):
                lines.append(node.lineno)
        if any(is_target(t) for t in targets):
            lines.append(node.lineno)
    return lines


def test_scanner_flags_import_time_assignment() -> None:
    bad = 'import sys\nsys.modules["homeassistant.util.dt"].now = lambda: 1\n'
    assert find_import_time_dt_assignments(bad) == [2]
    bad_attr = "from homeassistant.util import dt as dt_util\ndt_util.now = lambda: 1\n"
    assert find_import_time_dt_assignments(bad_attr) == [2]
    bad_sub = 'import sys\nsys.modules["homeassistant.util.dt"] = object()\n'
    assert find_import_time_dt_assignments(bad_sub) == [2]
    bad_sub_pkg = 'import sys\nsys.modules["homeassistant.util"] = object()\n'
    assert find_import_time_dt_assignments(bad_sub_pkg) == [2]
    bad_alias = "import homeassistant.util.dt as hdt\nhdt.now = lambda: 1\n"
    assert find_import_time_dt_assignments(bad_alias) == [2]
    bad_alias2 = "from homeassistant.util import dt as clock\nsetattr(clock, 'now', lambda: 1)\n"
    assert find_import_time_dt_assignments(bad_alias2) == [2]
    ok_other = 'import sys\nsys.modules["other.mod"] = object()\n'
    assert find_import_time_dt_assignments(ok_other) == []
    ok = 'def f():\n    import sys\n    sys.modules["homeassistant.util.dt"].now = lambda: 1\n'
    assert find_import_time_dt_assignments(ok) == []


def test_no_test_module_assigns_ha_dt_at_import_time() -> None:
    offenders: dict[str, list[int]] = {}
    for path in sorted(_TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        found = find_import_time_dt_assignments(path.read_text(encoding="utf-8"))
        if found:
            offenders[path.name] = found
    assert not offenders, (
        "Test modules must not assign homeassistant.util.dt attributes at import time "
        f"(a shared clock leaks across files); use the frozen_now fixture instead: {offenders}"
    )
