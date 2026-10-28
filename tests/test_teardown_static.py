"""Issue #1102: static guards so the reload leak cannot quietly come back.

Occupant experience being guarded: a timer or listener CA forgets to release keeps
a replaced ("dead") instance alive after a settings save, and that instance turns
the whole-house fan back on after the occupant switched it off.

- T7: no handle from ``async_call_later`` / ``async_track_*`` / ``async_at_start(ed)``
  / ``bus.async_listen(_once)`` is discarded anywhere in the integration. A discarded
  handle can never be cancelled on unload (that is how F1/F2/F6/F8 leaked).
- T7b: every ``hass.services.async_call`` in automation.py and coordinator.py sits in a
  function that itself checks the shut-down latch (``_refuse_if_shut_down``), except
  the read-only ``weather.get_forecasts`` fetches. A new command site added without
  the latch fails here instead of reaching a dead instance in production.
- T7c (Issue #1108): the same service-call sites must each honour observe-only mode
  (reference ``dry_run`` or ``_automation_enabled``), so a new command site cannot act
  while the automation switch is off.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"

_HANDLE_RETURNING = {"async_call_later", "async_at_start", "async_at_started", "async_listen", "async_listen_once"}

# Read-only data fetches (weather.get_forecasts): no device, no notification.
_SERVICE_CALL_ALLOWLIST = {
    ("coordinator.py", "_get_forecast_data"),
    ("coordinator.py", "_get_hourly_forecast_data"),
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _returns_handle(name: str | None) -> bool:
    return name is not None and (name in _HANDLE_RETURNING or name.startswith("async_track_"))


def test_no_discarded_listener_or_timer_handle():
    offenders = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A bare expression statement throws the returned cancel handle away.
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and _returns_handle(_call_name(node.value))
            ):
                offenders.append(f"{path.relative_to(_PKG)}:{node.lineno} {_call_name(node.value)}(...)")
    assert offenders == [], "discarded handles (store them so unload can cancel them):\n" + "\n".join(offenders)


def _is_services_async_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "async_call"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "services"
    )


def _calls_latch(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and _call_name(n) == "_refuse_if_shut_down" for n in ast.walk(fn))


def _service_call_sites(filename: str):
    tree = ast.parse((_PKG / filename).read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # Direct body only: nested functions are visited as their own FunctionDef.
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(node, ast.Call) and _is_services_async_call(node):
                yield fn, node
            stack.extend(ast.iter_child_nodes(node))


def test_every_service_call_is_behind_the_shutdown_latch():
    unlatched = []
    seen = 0
    for filename in ("automation.py", "coordinator.py"):
        for fn, call in _service_call_sites(filename):
            seen += 1
            if (filename, fn.name) in _SERVICE_CALL_ALLOWLIST:
                continue
            if not _calls_latch(fn):
                unlatched.append(f"{filename}:{call.lineno} in {fn.name}()")
    assert seen > 0, "no hass.services.async_call found — the AST matcher is broken"
    assert unlatched == [], (
        "service calls in functions that do not check the #1102 shut-down latch "
        "(add `if _refuse_if_shut_down(self, ...): return <no-op result>` before the call):\n" + "\n".join(unlatched)
    )


# --- T7c (Issue #1108): every command site honours observe-only mode -----------------------

_OBSERVE_ONLY_MARKERS = {"dry_run", "_automation_enabled"}


def _checks_observe_only(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and n.attr in _OBSERVE_ONLY_MARKERS:
            return True
        if isinstance(n, ast.Name) and n.id in _OBSERVE_ONLY_MARKERS:
            return True
    return False


def test_every_service_call_honours_observe_only_mode():
    """T7c: with the automation switch OFF, no command may reach a device or notifier.

    Occupant experience being guarded: the person switched Climate Advisor to observe-only
    and expects it to be hands-off; a command site added without the dry_run gate (the class
    behind #589 and #1108) turns their fan or thermostat anyway. Every function in
    automation.py / coordinator.py that calls ``hass.services.async_call`` must itself
    reference ``dry_run`` or ``_automation_enabled``, except the read-only forecast fetches.
    """
    ungated = []
    seen = 0
    for filename in ("automation.py", "coordinator.py"):
        for fn, call in _service_call_sites(filename):
            seen += 1
            if (filename, fn.name) in _SERVICE_CALL_ALLOWLIST:
                continue
            if not _checks_observe_only(fn):
                ungated.append(f"{filename}:{call.lineno} in {fn.name}()")
    assert seen > 0, "no hass.services.async_call found — the AST matcher is broken"
    assert ungated == [], (
        "service calls in functions that do not honour observe-only mode "
        "(add `if self.dry_run is True: log '[DRY RUN] Would ...'; return <no-op result>` before the call):\n"
        + "\n".join(ungated)
    )
