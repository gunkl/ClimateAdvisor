"""scenario_model - pure, stub-free helpers shared by every scenario runner adapter.

Issue #1127 Increment 5 / #1128 I2 (the Session split).  Nothing in this module installs
Home Assistant stubs or imports ``homeassistant``; the one production type it builds
(``DayClassification``) is imported lazily inside the function that needs it.  That is what lets
a real-Home-Assistant runner import the scenario model without the FakeHass stub layer.

Lifted verbatim from ``run_production.py`` (no behavior change): the result dataclass, event time
parsing, the scenario config merge / start-time rule, the hand-built ``DayClassification`` and the
final engine-state snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tools.sim_harness.build_engine import _DEFAULT_CONFIG

_FALLBACK_START = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)


@dataclass
class ProductionRunResult:
    """Result of running a scenario through the production engine."""

    event_log: list[tuple[str, dict, datetime | None]] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)
    engine_state: dict[str, Any] = field(default_factory=dict)
    callback_errors: list[tuple[datetime, BaseException]] = field(default_factory=list)
    # Optional: a PromiseReport when a PromiseProbe was attached (never read by outcomes.py).
    promise_report: Any = None


RunResult = ProductionRunResult


def parse_event_time(time_str: str, default: datetime) -> datetime:
    """Parse ISO timestamp string to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError, AttributeError:
        return default


def merged_scenario_config(scenario: dict) -> dict[str, Any]:
    """Scenario ``config`` laid over the harness defaults."""
    scenario_config: dict[str, Any] = scenario.get("config", {})
    return {**_DEFAULT_CONFIG, **scenario_config}


def sorted_events(scenario: dict) -> list[dict]:
    return sorted(scenario.get("events", []), key=lambda e: e["time"])


def scenario_start_time(events: list[dict]) -> datetime:
    """One second before the first event (or a fixed fallback for an event-less scenario)."""
    if events:
        return parse_event_time(events[0]["time"], _FALLBACK_START) - timedelta(seconds=1)
    return _FALLBACK_START


def build_classification_from_event(event: dict) -> Any:
    """Build a real DayClassification from a scenario classification event.

    Bypasses ``__post_init__`` (which calls ``_compute_recommendations`` and
    would overwrite hvac_mode, windows_recommended etc. from the scenario JSON)
    and manually sets fields from the event, using sensible defaults for the
    required-but-not-scenario-relevant fields.

    Why object.__new__?  DayClassification.__post_init__ derives hvac_mode and
    window times from day_type - but scenario JSON specifies these explicitly.
    Using __new__ lets us honour what the scenario author wrote.
    """
    from custom_components.climate_advisor.classifier import DayClassification  # noqa: PLC0415

    c = object.__new__(DayClassification)

    # --- Required structural fields (not in scenarios; use sensible defaults) ---
    c.trend_direction = event.get("trend_direction", "stable")
    c.trend_magnitude = float(event.get("trend_magnitude", 0.0))
    c.today_high = float(event.get("today_high", 75.0))
    c.today_low = float(event.get("today_low", 60.0))
    c.tomorrow_high = float(event.get("tomorrow_high", 75.0))
    c.tomorrow_low = float(event.get("tomorrow_low", 60.0))

    # --- Classification fields that scenarios DO specify ---
    c.day_type = event.get("day_type", "mild")
    c.hvac_mode = event.get("hvac_mode", "off")
    c.windows_recommended = bool(event.get("windows_recommended", False))
    c.setback_modifier = float(event.get("setback_modifier", 0.0))

    # Window times: stored as datetime.time objects in DayClassification
    # Scenario JSON uses "HH:MM" strings (same as SimClassification)
    from datetime import time as dt_time  # noqa: PLC0415

    def _parse_time(val: str | None) -> dt_time | None:
        if not val:
            return None
        try:
            h, m = val.split(":")
            return dt_time(int(h), int(m))
        except ValueError, AttributeError:
            return None

    c.window_open_time = _parse_time(event.get("window_open_time"))
    c.window_close_time = _parse_time(event.get("window_close_time"))

    # Optional pre-condition fields
    c.pre_condition = bool(event.get("pre_condition", False))
    c.pre_condition_target = event.get("pre_condition_target")

    # Window opportunity fields (hot-day economizer)
    c.window_opportunity_morning = bool(event.get("window_opportunity_morning", False))
    c.window_opportunity_evening = bool(event.get("window_opportunity_evening", False))
    c.window_opportunity_morning_start = _parse_time(event.get("window_opportunity_morning_start"))
    c.window_opportunity_morning_end = _parse_time(event.get("window_opportunity_morning_end"))
    c.window_opportunity_evening_start = _parse_time(event.get("window_opportunity_evening_start"))
    c.window_opportunity_evening_end = _parse_time(event.get("window_opportunity_evening_end"))

    return c


def snapshot_engine_state(engine: Any) -> dict[str, Any]:
    """Read key engine flag attributes and return a plain dict snapshot."""
    snap: dict[str, Any] = {}

    for attr in (
        "_natural_vent_active",
        "_manual_override_active",
        "_grace_active",
        "_paused_by_door",
        "_fan_active",
        "_fan_override_active",
        "_occupancy_mode",
        "_override_confirm_pending",
        "_economizer_active",
        "_economizer_phase",
        "_pre_condition_achieved",  # Issue #295
        "_pre_condition_achieved_date",  # Issue #295
        "_nat_vent_soft_start",  # Issue #606
        "_nat_vent_outdoor_exit_time",  # Issue #606
        "_last_outdoor_temp",  # Issue #633
        "_outdoor_temp_today_peak",  # Issue #633
        "_outdoor_temp_today_sample_count",  # Issue #633
        "_comfort_mode_family",  # Issue #821 - "heating"/"cooling"/None
        "_fan_manual_origin_unclosed",  # Issue #829
    ):
        snap[attr] = getattr(engine, attr, None)

    # Issue #633: indoor temp is never cached on the engine itself (always a
    # live hass state read) - snapshot it explicitly the same way any other
    # post-run inspection would, using the engine's own reader.
    get_indoor = getattr(engine, "_get_indoor_temp_f", None)
    snap["_last_indoor_temp"] = get_indoor() if callable(get_indoor) else None

    # Issue #1048: the thermostat's ACTUAL mode (ground truth, not what CA commanded).
    _hass = getattr(engine, "hass", None)
    _st = _hass.states.get(engine.climate_entity) if _hass is not None else None
    snap["_thermostat_hvac_mode"] = getattr(_st, "state", None)

    # Include classification summary if available
    c = getattr(engine, "_current_classification", None)
    if c is not None:
        snap["_current_classification"] = {
            "day_type": getattr(c, "day_type", None),
            "hvac_mode": getattr(c, "hvac_mode", None),
            "windows_recommended": getattr(c, "windows_recommended", None),
        }
    else:
        snap["_current_classification"] = None

    return snap
