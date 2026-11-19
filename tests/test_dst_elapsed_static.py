"""Issue #1244: a static guard so wall-clock elapsed time cannot quietly come back.

Occupant experience being guarded: on fall-back night ``dt_util.now() - self._started_at`` (both local, same
``ZoneInfo``) is wall-clock arithmetic and can read NEGATIVE, so an ``elapsed < window`` guard reads "recent" for up
to an hour -- the occupant's own thermostat change in the repeated hour is taken for Climate Advisor's echo, the fan
toggle floor and the nat-vent re-entry lockout last an hour too long. Across spring-forward the elapsed time is an
hour too long and those protections end early.

The rule (``time_utils`` module docstring): elapsed time since a stored start is ``seconds_since(start, now=...)``.
This test walks every module in the integration and fails on a subtraction whose left side is a "now" (a
``dt_util.now()``/``utcnow()`` call, or a name/attribute named ``now``/``_now``/``*_now``/``now_*``) that is used as
elapsed time (``.total_seconds()`` on it, or compared directly). The sites below are exempt, each with the reason it
is correct (or deliberately out of scope). A new site goes through ``seconds_since`` instead of onto this list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_NOW = re.compile(r"(^|_)now($|_)")

_UTC_OR_HA_STATE = "the start is an HA state timestamp (last_changed/last_reported, UTC) or a utcnow() value"
_ISO = "the start is parsed from an ISO string, which carries a fixed UTC offset: the subtraction is real time"
_MONOTONIC = "both sides are time.monotonic()/timestamp floats, not datetimes"
_LOG_THROTTLE = "a log/prune throttle: off by up to an hour once a year changes only how often a line is written"
_OUT_OF_SCOPE = "kept out of scope for #1244 (see the PR): diagnostics only, no decision reads it"

# (module, innermost function, source of the subtraction) -> why it is exempt
ALLOWED: dict[tuple[str, str, str], str] = {
    ("ai_skills_context.py", "_fetch_github_issues", "now - open_ts"): _MONOTONIC,
    ("ai_skills_context.py", "_fetch_github_issues", "now - closed_ts"): _MONOTONIC,
    ("ai_skills_context.py", "build_override_details_context", "now_local - override_dt"): _ISO,
    ("automation.py", "_whf_rule2_state_inputs", "now - changed"): _UTC_OR_HA_STATE,
    ("automation.py", "_log_fan_dry_run", "_now - _last"): _LOG_THROTTLE,
    ("automation.py", "_minutes_since_iso", "now - ts"): _ISO,
    ("automation.py", "minutes_since_mode_set", "now - ts"): _ISO + " (the mode-change ring buffer)",
    ("automation.py", "_track_comfort_hold_episode", 'now - datetime.fromisoformat(prev["end_ts"])'): _ISO,
    ("automation.py", "_recent", "now - ts"): _ISO,
    ("automation.py", "_get_fan_runtime_minutes", "now - on_since"): _ISO + " (_fan_on_since)",
    ("automation.py", "_decision_pass", "dt_util.now() - _wait_start"): _LOG_THROTTLE + " (lock wait, DEBUG log)",
    ("automation.py", "_decision_pass", "dt_util.now() - self._decision_lock_held_since"): _OUT_OF_SCOPE
    + " (lock hold time, DEBUG log)",
    ("automation.py", "remote_offset_state", "dt_util.utcnow() - ctx.reported_at"): _UTC_OR_HA_STATE,
    ("chart_log.py", "_maybe_prune", "now - self._last_prune"): _LOG_THROTTLE,
    ("claude_api.py", "async_list_models", "now - self._models_cache_ts"): _MONOTONIC,
    ("coordinator.py", "_async_thermostat_changed", "dt_util.now() - _active_start_ts"): _ISO,
    ("coordinator.py", "_async_thermostat_changed", "dt_util.now() - _last_evt_ts"): _ISO,
    ("coordinator.py", "_fan_remote_event_age_seconds", "dt_util.now() - stamp"): _ISO,
    ("coordinator.py", "_sample_all_observations", "now - active_start"): _ISO,
    ("coordinator.py", "_sample_all_observations", "now - start_ts"): _ISO,
    ("coordinator.py", "_sample_all_observations", "now - dt_util.parse_datetime(_last_s)"): _ISO,
    ("coordinator.py", "_sample_all_observations", "now - _first_ts"): _ISO,
    ("coordinator.py", "_obs_elapsed_minutes", "now - start"): _ISO,
    ("coordinator.py", "_cycle_refit_due_modes", "now_ts - last"): _MONOTONIC,
    ("coordinator.py", "_cycle_save_due", "now_ts - last"): _MONOTONIC,
    ("coordinator.py", "_abandon_observation", "dt_util.now() - _start_ts"): _ISO,
    ("coordinator.py", "_evaluate_rolling_window", "now - start_ts"): _ISO,
    ("coordinator.py", "_commit_rolling_window_obs", "dt_util.now() - _start_ts"): _ISO,
    ("coordinator.py", "_compute_fan_status", "_stale_now - _stale_last"): _LOG_THROTTLE,
    ("coordinator.py", "_compute_whf_status_parts", "_whf_stale_now - _whf_stale_last"): _LOG_THROTTLE,
    ("coordinator.py", "_compute_hvac_fan_status", "_hvac_stale_now - _hvac_stale_last"): _LOG_THROTTLE,
    ("coordinator.py", "get_chart_data", "now - anchor_dt"): _UTC_OR_HA_STATE + " (fromtimestamp(tz=UTC))",
    ("coordinator.py", "_build_thermal_pipeline_summary", "now - start_ts"): _ISO,
    ("coordinator.py", "_build_thermal_pipeline_summary", "now - last_ts"): _ISO,
    ("coordinator.py", "get_debug_state", "dt_util.now() - ae._decision_lock_held_since"): _OUT_OF_SCOPE,
    ("coordinator.py", "_interpolate_hourly_outdoor_temp", "now_utc - last_dt"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_interpolate_hourly_outdoor_temp", "now_utc - dt_before"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_whf_rule2_overlap_for_s", "now - since"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_whf_sensor_readings", "now - started"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_whf_sensor_readings", "now - changed"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_whf_sensor_readable_for_s", "now - readable_at"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_age", "now - changed"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_sensor_in_debounce", "dt_util.now() - last_changed"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_sensor_debounce_pending", "now - last_changed"): _UTC_OR_HA_STATE,
    ("coordinator.py", "_companion_temp_sensors", "now - cached[1]"): _MONOTONIC,
    ("coordinator.py", "_companion_sensors", "now - cached[1]"): _MONOTONIC,
    ("coordinator.py", "_v4_cycle_kwargs", "now - at"): _ISO,
    ("coordinator.py", "_v4_shadow_cycle_impl", "now - row_ts"): _ISO,
    ("coordinator.py", "_async_send_briefing", "dt_util.now() - _fetched_at"): _OUT_OF_SCOPE
    + " (_classification_fetched_at reuse window for the briefing; worst case one extra forecast fetch)",
    ("forecast_archive.py", "_maybe_prune", "now - self._last_prune"): _LOG_THROTTLE,
    ("prediction_engines.py", "control_path", "now - stamp"): _ISO,
    # swing.py is loaded standalone by tools/ and cannot import time_utils: its own stdlib helper (Issue #1607) keeps
    # the raw subtraction only for two naive (stub) clocks; aware values are compared in UTC.
    ("swing.py", "_coast_age_s", "now - off_dt"): "the naive-only branch of swing's stdlib UTC helper (#1607)",
    ("temperature.py", "is_sensor_stale", "now - last_seen"): _UTC_OR_HA_STATE,
    ("thermal_lead_time.py", "_warn_out_of_reach", "now - last"): _MONOTONIC,
    ("v4_archive.py", "_maybe_prune", "now_ts - self._last_prune"): _MONOTONIC,
    ("v4_shadow.py", "run_state_from_rows", "now - first_ts"): _ISO + " (chart-log rows)",
}


def _now_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("now", "utcnow"):
        return True
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
    return bool(name and _NOW.search(name))


def _is_timedelta(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "timedelta"


def _elapsed_subtractions(src: str):
    """Yield (innermost function, source) for every ``now - start`` used as elapsed time."""
    tree = ast.parse(src)

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            func = node.name
        sub = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "total_seconds"
            and isinstance(node.func.value, ast.BinOp)
            and isinstance(node.func.value.op, ast.Sub)
        ):
            sub = node.func.value
        elif isinstance(node, ast.Compare) and isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Sub):
            sub = node.left
        if sub is not None and _now_like(sub.left) and not _is_timedelta(sub.right):
            found.append((func, ast.get_source_segment(src, sub)))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    found: list[tuple[str, str]] = []
    visit(tree, "<module>")
    return found


def _all_sites() -> set[tuple[str, str, str]]:
    sites = set()
    for path in sorted(_PKG.glob("*.py")):
        for func, seg in _elapsed_subtractions(path.read_text(encoding="utf-8")):
            sites.add((path.name, func, seg))
    return sites


def test_no_raw_wall_clock_elapsed_time():
    new = sorted(_all_sites() - set(ALLOWED))
    assert not new, (
        "Elapsed time measured as `now - start` is wall-clock arithmetic for two local datetimes and is wrong by an "
        "hour (often negative) across a DST change (Issue #1244). Use time_utils.seconds_since(start, now=...):\n"
        + "\n".join(f"  {f} {fn}(): {seg}" for f, fn, seg in new)
    )


def test_allowlist_has_no_stale_entries():
    stale = sorted(set(ALLOWED) - _all_sites())
    assert not stale, "remove these ALLOWED entries (the site is gone or now uses seconds_since):\n" + "\n".join(
        f"  {f} {fn}(): {seg}" for f, fn, seg in stale
    )


def test_the_guard_catches_the_issue_1244_pattern():
    src = (
        "def f(self):\n"
        "    return (dt_util.now() - self._x).total_seconds() < 3\n"
        "def g(self, now):\n"
        "    return (now - self._y) < timedelta(minutes=5)\n"
        "def h(self, now):\n"
        "    return now - timedelta(minutes=5)\n"
    )
    assert _elapsed_subtractions(src) == [("f", "dt_util.now() - self._x"), ("g", "now - self._y")]
