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


# A forecast typical of each day type, used ONLY for classification events that do not state their own
# ``today_high`` / ``today_low``. (Issue #1211) These replace the old blanket 75 / 60 default, which was a
# "warm" high for every day type. Hot days get a warm night (low above the window-opportunity cutoff) so a
# scenario that never mentions the economizer does not silently gain one.
_REPRESENTATIVE_LOW_F = {"hot": 82.0, "warm": 60.0, "mild": 50.0, "cool": 40.0, "cold": 25.0}


def _representative_high_f(day_type: str, comfort_heat: float) -> float:
    """A high that classifies as ``day_type`` (default thresholds); a mild one also reaches the comfort floor."""
    from custom_components.climate_advisor.const import (  # noqa: PLC0415
        THRESHOLD_COOL,
        THRESHOLD_HOT,
        THRESHOLD_MILD,
        THRESHOLD_WARM,
    )

    if day_type == "hot":
        return float(THRESHOLD_HOT + 5)
    if day_type == "warm":
        return float(THRESHOLD_WARM + 5)
    if day_type == "mild":
        return float(min(max(comfort_heat + 2.0, THRESHOLD_MILD), THRESHOLD_WARM - 1))
    if day_type == "cool":
        return float(THRESHOLD_COOL + 7)
    return float(THRESHOLD_COOL - 10)


def build_classification_from_event(event: dict, config: dict | None = None) -> Any:
    """Build a real DayClassification from a scenario classification event.

    Issue #1211 (C6): every recommendation field the scenario does NOT state is filled by the REAL
    classifier -- ``DayClassification.__post_init__`` -- from the scenario's day type, forecast and comfort
    band (``config``), instead of the old hand-built defaults (high 75 / low 60, hvac_mode "off",
    windows_recommended False, no window times). A field the scenario DOES state still wins, so every
    hand-authored classification keeps meaning exactly what its author wrote.

    Why the old default was wrong (semantic justification, per the test-infrastructure rule): it made an
    event that said ``day_type: warm`` and nothing else describe a day production's classifier would never
    produce (a warm day whose low is 60 gets windows recommended and window times; a mild day whose high
    is 75 is a warm day). Scenarios therefore exercised a state production cannot reach, and could not
    notice when the classifier changed. It cannot make a real failure pass silently: it only makes unstated
    inputs match the classifier; every stated field is applied after, unchanged.
    """
    from datetime import time as dt_time  # noqa: PLC0415

    from custom_components.climate_advisor.classifier import DayClassification  # noqa: PLC0415
    from custom_components.climate_advisor.const import DEFAULT_COMFORT_COOL, DEFAULT_COMFORT_HEAT  # noqa: PLC0415

    cfg = config or {}
    comfort_heat = float(cfg.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    comfort_cool = float(cfg.get("comfort_cool", DEFAULT_COMFORT_COOL))
    day_type = event.get("day_type", "mild")
    today_high = float(event.get("today_high", _representative_high_f(day_type, comfort_heat)))
    today_low = float(event.get("today_low", _REPRESENTATIVE_LOW_F.get(day_type, 50.0)))

    c = DayClassification(
        day_type=day_type,
        trend_direction=event.get("trend_direction", "stable"),
        trend_magnitude=float(event.get("trend_magnitude", 0.0)),
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=float(event.get("tomorrow_high", today_high)),
        tomorrow_low=float(event.get("tomorrow_low", today_low)),
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
    )

    # Scenario JSON uses "HH:MM" strings; DayClassification stores datetime.time objects.
    def _parse_time(val: str | None) -> dt_time | None:
        if not val:
            return None
        try:
            h, m = val.split(":")
            return dt_time(int(h), int(m))
        except ValueError, AttributeError:
            return None

    # --- Stated fields win over the classifier's ---
    if "hvac_mode" in event:
        c.hvac_mode = event["hvac_mode"]
    if "windows_recommended" in event:
        c.windows_recommended = bool(event["windows_recommended"])
        c.forecast_windows_recommended = c.windows_recommended
    if "setback_modifier" in event:
        c.setback_modifier = float(event["setback_modifier"])
    if "window_open_time" in event:
        c.window_open_time = _parse_time(event.get("window_open_time"))
    if "window_close_time" in event:
        c.window_close_time = _parse_time(event.get("window_close_time"))
    if "pre_condition" in event:
        c.pre_condition = bool(event["pre_condition"])
    if "pre_condition_target" in event:
        c.pre_condition_target = event.get("pre_condition_target")
    for flag in ("window_opportunity_morning", "window_opportunity_evening"):
        if flag in event:
            setattr(c, flag, bool(event[flag]))
    for key in (
        "window_opportunity_morning_start",
        "window_opportunity_morning_end",
        "window_opportunity_evening_start",
        "window_opportunity_evening_end",
    ):
        if key in event:
            setattr(c, key, _parse_time(event.get(key)))

    return c


def pin_scenario_thermal_model(coordinator: Any, model: dict) -> None:
    """Make a coordinator-mode run use the scenario's ``thermal_model`` through every refresh (Issue #1211).

    A scenario's ``thermal_model`` used to be written only to ``engine._thermal_model``. In production that
    attribute is whatever ``learning.get_thermal_model()`` last returned, and ``_async_update_data_impl``
    rewrites it from that call every 30-minute cycle -- so in coordinator mode the first refresh silently
    replaced the scenario's model with the harness's empty learning store, and the two runner modes diverged.
    The scenario's model IS the learned model for that run, so the coordinator's learning store answers with it.

    Why the old semantics were wrong (test-infrastructure rule): a scenario asserting a decision that depends
    on the ODE model passed or failed depending on whether a refresh had run yet. This cannot let a real
    failure pass silently: the engine sees the same model as before, now also after the first refresh.
    """
    learning = getattr(coordinator, "learning", None)
    if learning is not None:
        learning.get_thermal_model = lambda *args, **kwargs: model


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
