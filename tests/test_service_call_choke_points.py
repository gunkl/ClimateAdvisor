"""Static guard: every climate/fan/switch service call goes through a known choke point (Issue #1128, I7).

Why: promise 3 (``observe_only_silence``) and the command journal can only see what flows
through the choke points. A third, unguarded call site would let Climate Advisor command the
thermostat or fan without the shut-down latch, the observe-only gate, a recorded command
``Context`` or the promise journal observing it (the shape of Issues #589, #1102, #1108, #1109).

The scan is an AST walk, so it also catches a call whose domain is a variable (``domain`` is
derived from an entity id), which a text search for ``"climate"`` would miss. Adding a call site
fails this test; the fix is to route it through ``_call_service_with_context`` or to add a named,
justified entry to ``ALLOWED_SITES`` after review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import custom_components.climate_advisor as pkg

PKG_DIR = Path(pkg.__file__).parent

# Domains that are plainly not equipment commands may be called from anywhere.
HARMLESS_LITERAL_DOMAINS = frozenset({"notify", "weather", "persistent_notification", "logbook"})

# (file name, enclosing function) -> why this site may call a climate/fan/switch service directly.
ALLOWED_SITES: dict[tuple[str, str], str] = {
    ("automation.py", "_call_service_with_context"): (
        "THE choke point: shut-down latch, observe-only gate, fresh Context recorded per command kind, "
        "and the single place the command journal hook is added (Step 3 / I8)."
    ),
    ("coordinator.py", "_async_command_fan_entity"): (
        "Second command path, deliberately allow-listed (Step 3 finding M3, verdict on Issue #1129). It honors "
        "the shut-down latch and the automation-enabled gate itself (Issue #589), reports to the promise journal "
        "itself, and sends NO Context. That is safe only because every caller sits behind the command-only "
        "(fan_state_feedback off) gate, where fan-entity echoes are never read as overrides; "
        "tests/test_promise_wiring.py::TestIssue1129FanReconcileEcho proves both halves."
    ),
}

# What each allowed site is expected to contain (keeps the allow-list from silently growing).
EXPECTED_CALLS_PER_SITE = {
    ("automation.py", "_call_service_with_context"): 2,  # blocking and non-blocking branch
    ("coordinator.py", "_async_command_fan_entity"): 1,
}


def _is_services_call(node: ast.Call) -> bool:
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in ("async_call", "call")):
        return False
    recv = f.value
    return isinstance(recv, ast.Attribute) and recv.attr == "services"


def _domain_of(node: ast.Call) -> str | None:
    """Literal domain string, or None when the domain is not a literal."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def find_equipment_call_sites(source: str, filename: str) -> list[tuple[str, int, str]]:
    """Return (file, line, enclosing function) for each service call that may command equipment."""
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_services_call(node)):
            continue
        domain = _domain_of(node)
        if domain is not None and domain in HARMLESS_LITERAL_DOMAINS:
            continue
        func = "<module>"
        cur: ast.AST | None = node
        while cur is not None:
            cur = parents.get(cur)
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = cur.name
                break
        sites.append((filename, node.lineno, func))
    return sites


def _scan_package() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in sorted(PKG_DIR.rglob("*.py")):
        sites.extend(find_equipment_call_sites(path.read_text(encoding="utf-8"), path.name))
    return sites


class TestScannerItself:
    """The guard must be able to fail (negative controls on synthetic source)."""

    def test_literal_climate_call_outside_a_choke_point_is_found(self):
        src = "async def sneaky(self):\n    await self.hass.services.async_call('climate', 'set_hvac_mode', {})\n"
        assert find_equipment_call_sites(src, "x.py") == [("x.py", 2, "sneaky")]

    def test_variable_domain_is_found(self):
        src = "async def sneaky(self, domain):\n    await hass.services.async_call(domain, 'turn_off', {})\n"
        assert len(find_equipment_call_sites(src, "x.py")) == 1

    def test_notify_is_not_equipment(self):
        src = "async def ok(self):\n    await self.hass.services.async_call('notify', 'mobile', {})\n"
        assert find_equipment_call_sites(src, "x.py") == []

    def test_call_in_a_nested_closure_reports_the_closure(self):
        src = (
            "async def outer(self):\n"
            "    async def inner():\n"
            "        await hass.services.async_call('fan', 'turn_on', {})\n"
        )
        assert find_equipment_call_sites(src, "x.py")[0][2] == "inner"

    def test_unrelated_async_call_attribute_is_ignored(self):
        src = "async def f(self):\n    await self.client.async_call('climate', 'x')\n"
        assert find_equipment_call_sites(src, "x.py") == []


class TestProductionCallSites:
    def test_every_equipment_service_call_is_at_an_allow_listed_choke_point(self):
        stray = [s for s in _scan_package() if (s[0], s[2]) not in ALLOWED_SITES]
        assert stray == [], (
            "Service call(s) that may command a climate/fan/switch entity outside the known choke points: "
            f"{stray}. Route them through AutomationEngine._call_service_with_context, or add a named, "
            "justified entry to ALLOWED_SITES after review."
        )

    def test_allow_list_has_no_stale_or_widened_entries(self):
        counts: dict[tuple[str, str], int] = {}
        for file, _line, func in _scan_package():
            counts[(file, func)] = counts.get((file, func), 0) + 1
        assert counts == EXPECTED_CALLS_PER_SITE

    def test_every_allow_list_entry_is_justified(self):
        assert all(len(why) > 40 for why in ALLOWED_SITES.values())
        assert set(ALLOWED_SITES) == set(EXPECTED_CALLS_PER_SITE)
