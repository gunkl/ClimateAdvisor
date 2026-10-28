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


# ---------------------------------------------------------------------------
# T7d (Issue #1113): no new bare ``hass.async_create_task`` — ratcheting allowlist.
#
# Occupant experience being guarded: during a settings save (zone reload), a state
# save or thermal-observation commit spawned as a bare HA task can land AFTER the new
# zone instance has loaded, so a learned observation is lost or the state file is
# briefly stale. Coordinator work that must finish before the new instance starts goes
# through ``ClimateAdvisorCoordinator._spawn`` (config-entry task: unload waits for
# it). Every remaining bare call is counted per (file, enclosing function). A NEW bare
# call fails here; converting one means LOWERING its count below (the ratchet).
# ---------------------------------------------------------------------------

# Permanent: these must stay bare hass tasks.
_BARE_TASK_PERMANENT = {
    # The EVENT_HOMEASSISTANT_STOP listener: restart-only path. HA never unloads entries
    # on stop, and an entry/background task would gain nothing (background = cancelled).
    ("coordinator.py", "_async_homeassistant_stop"): 1,
    # _spawn's mandatory fallback when there is no config entry (tests, sim harness).
    ("coordinator.py", "_spawn"): 1,
}

# Ratchet: groups B-D of Issue #1113 (engine/timer dispatch, refresh requests,
# notifications), tolerated at their current counts. Lower a count when a site is
# converted to an entry-tied task; never raise one.
_BARE_TASK_RATCHET = {
    ("api.py", "_apply_after_delay"): 1,
    ("automation.py", "_clear_fan_flags_and_start_grace"): 1,
    ("automation.py", "_confirm_override_expired"): 1,
    ("automation.py", "_grace_expired"): 1,
    ("automation.py", "_notify_indoor_blind"): 1,
    ("automation.py", "_on_grace_expired"): 2,
    ("automation.py", "_reconcile_fan_physical_drift"): 1,
    ("automation.py", "_retry"): 1,
    ("automation.py", "_revisit_fired"): 1,
    ("automation.py", "_schedule_check"): 1,
    ("automation.py", "_schedule_real_target"): 1,
    ("automation.py", "_schedule_retry"): 1,
    ("automation.py", "_thermo_tick"): 1,
    ("automation.py", "_turn_off"): 1,
    ("automation.py", "_turn_on"): 1,
    ("automation.py", "_verify_setpoint_after_fan_off"): 1,
    ("automation.py", "_verify_setpoint_after_fan_on"): 1,
    ("automation.py", "clear_fan_override"): 1,
    ("automation.py", "handle_fan_manual_override"): 1,
    ("automation.py", "on_fan_turned_off"): 1,
    ("automation.py", "start_override_confirmation"): 1,
    ("coordinator.py", "_async_door_window_changed"): 2,
    ("coordinator.py", "_async_fan_entity_changed"): 1,
    ("coordinator.py", "_async_indoor_temp_changed"): 1,
    ("coordinator.py", "_async_outdoor_temp_changed"): 1,
    ("coordinator.py", "_async_thermostat_changed"): 1,
    ("coordinator.py", "_build_production_automation_callbacks"): 1,
    ("coordinator.py", "_burst_window_elapsed"): 1,
    ("coordinator.py", "_debounce_expired"): 1,
    ("coordinator.py", "_do_debounce"): 1,
    ("coordinator.py", "_do_startup_coalesce"): 1,
    ("coordinator.py", "_notify_in_background"): 1,
    ("coordinator.py", "_occupancy_away_timer_expired"): 1,
    ("coordinator.py", "_on_post_grace_fan_check"): 1,
    ("coordinator.py", "_on_startup_coalesce_timer"): 1,
    ("coordinator.py", "_on_whf_release_reclassify"): 1,
    ("coordinator.py", "_schedule_retry"): 1,
    ("coordinator.py", "_tou_av_window_start"): 1,
    ("coordinator.py", "_update_comfort_stall"): 2,
}


def _bare_hass_task_counts() -> dict[tuple[str, str], int]:
    """Count ``hass.async_create_task(...)`` / ``<x>.hass.async_create_task(...)`` calls
    per (package-relative file, nearest enclosing function)."""
    counts: dict[tuple[str, str], int] = {}

    def visit(node: ast.AST, rel: str, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_fn = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "async_create_task"
            ):
                owner = child.func.value
                if (isinstance(owner, ast.Name) and owner.id == "hass") or (
                    isinstance(owner, ast.Attribute) and owner.attr == "hass"
                ):
                    key = (rel, fn)
                    counts[key] = counts.get(key, 0) + 1
            visit(child, rel, child_fn)

    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        visit(ast.parse(path.read_text(encoding="utf-8")), rel, "<module>")
    return counts


def test_no_new_bare_hass_async_create_task():
    allowed = dict(_BARE_TASK_RATCHET)
    for key, n in _BARE_TASK_PERMANENT.items():
        allowed[key] = allowed.get(key, 0) + n
    actual = _bare_hass_task_counts()
    problems = []
    for key in sorted(set(actual) | set(allowed)):
        have, may = actual.get(key, 0), allowed.get(key, 0)
        if have > may:
            problems.append(
                f"{key[0]}:{key[1]} has {have} bare hass.async_create_task call(s), allowlist {may} "
                "— use coordinator._spawn (config-entry task) instead"
            )
        elif have < may:
            problems.append(
                f"{key[0]}:{key[1]} has {have} bare call(s) but the allowlist says {may} "
                "— lower the ratchet entry (converted sites must not be re-addable)"
            )
    assert problems == [], "\n".join(problems)
