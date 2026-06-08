"""run_production — scenario adapter: feeds existing scenario JSON through the real AutomationEngine.

``run_production_scenario(scenario)`` is the single entry point.  It mirrors
``tools/simulate.py``'s event dispatch but invokes the REAL production engine
methods instead of the standalone simulator state machine.

Returned ``ProductionRunResult`` carries:
  - ``event_log``       — engine ``_emit_event_callback`` decisions
                          ``list[tuple[event_type, payload, datetime|None]]``
  - ``action_log``      — service calls captured by FakeHass
                          ``list[dict]``
  - ``engine_state``    — snapshot of engine flags after all events processed
                          ``dict[str, Any]``
  - ``callback_errors`` — exceptions recorded by FakeScheduler during advance
                          ``list[tuple[datetime, BaseException]]``

Event-type → production-method mapping (mirrors simulate.py process_event):

  temp_update             → engine.update_outdoor_temp(outdoor_f)
                            + inject indoor temp via climate entity attribute
                            + asyncio.run(engine.check_natural_vent_conditions())
  sensor_open             → asyncio.run(engine.handle_door_window_open(entity_id))
  sensor_close            → update engine._sensor_check_callback
                            + if no sensors open:
                              asyncio.run(engine.handle_all_doors_windows_closed())
  classification          → build DayClassification from event fields
                            + asyncio.run(engine.apply_classification(classification))
  occupancy_away          → engine.set_occupancy_mode("away")
                            + asyncio.run(engine.handle_occupancy_away())
  occupancy_home          → engine.set_occupancy_mode("home")
                            + asyncio.run(engine.handle_occupancy_home())
  occupancy_vacation      → engine.set_occupancy_mode("vacation")
                            + asyncio.run(engine.handle_occupancy_vacation())
  occupancy_change        → dispatches to away/home/vacation by event["mode"]
  occupancy_change_with_override
                          → sets _manual_override_active=True then dispatches
  bedtime                 → asyncio.run(engine.handle_bedtime())
  wakeup                  → asyncio.run(engine.handle_morning_wakeup())
  economizer_check        → asyncio.run(engine.check_window_cooling_opportunity(...))
  thermostat_state_changed
                          → inject thermostat state + engine.handle_manual_override(...)

No clean production entry points (FINDINGS):
  fan_cycle_on   — _fan_cycle_on() is private; production triggers it via
                   start_min_fan_runtime_cycles() + scheduler timers.  Injecting
                   a virtual `start_min_fan_runtime_cycles()` call at the event
                   time is the closest approximation but does not reproduce the
                   exact on-phase-start semantics of a mid-cycle event.
                   Marked as FINDING: no clean mapping; skip in adapter.
  fan_cycle_off  — same as fan_cycle_on; internal timer callback only.
                   FINDING: no clean mapping; skip in adapter.
  grace_start    — production grace periods start automatically in response to
                   sensor events; there is no external ``start_grace()`` method.
                   FINDING: no clean mapping; skip in adapter (grace state
                   already flows from sensor events).
  grace_end      — same; grace expiry fires via scheduler.  Advance_to() drives it.
                   FINDING: no clean mapping; skip in adapter (timers cover it).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Ensure project root on path (support running as a script)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness.build_engine import _DEFAULT_CONFIG, build_headless_engine  # noqa: E402
from tools.sim_harness.fake_hass import FakeHass, FakeState  # noqa: E402
from tools.sim_harness.fake_scheduler import FakeScheduler  # noqa: E402
from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

# Ensure stubs installed before any automation/classifier import
install_ha_stubs()


@dataclass
class ProductionRunResult:
    """Result of running a scenario through the production engine."""

    event_log: list[tuple[str, dict, datetime | None]] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)
    engine_state: dict[str, Any] = field(default_factory=dict)
    callback_errors: list[tuple[datetime, BaseException]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DayClassification builder
# ---------------------------------------------------------------------------


def _build_classification_from_event(event: dict) -> Any:
    """Build a real DayClassification from a scenario classification event.

    Bypasses ``__post_init__`` (which calls ``_compute_recommendations`` and
    would overwrite hvac_mode, windows_recommended etc. from the scenario JSON)
    and manually sets fields from the event, using sensible defaults for the
    required-but-not-scenario-relevant fields.

    Why object.__new__?  DayClassification.__post_init__ derives hvac_mode and
    window times from day_type — but scenario JSON specifies these explicitly.
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
        except (ValueError, AttributeError):
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


# ---------------------------------------------------------------------------
# Indoor temp injection helpers
# ---------------------------------------------------------------------------


def _inject_indoor_temp(fake_hass: FakeHass, climate_entity: str, indoor_f: float | None) -> None:
    """Update the current_temperature attribute on the climate entity.

    The engine reads indoor temperature via ``_get_indoor_temp_f()`` which
    falls back to ``hass.states.get(climate_entity).attributes["current_temperature"]``
    when no indoor_temp_entity is configured (the default).
    """
    if indoor_f is None:
        return
    existing = fake_hass.states.get(climate_entity)
    if existing is not None:
        attrs = dict(existing.attributes)
        attrs["current_temperature"] = indoor_f
        fake_hass.states.set(climate_entity, FakeState(state=existing.state, attributes=attrs))
    else:
        # No existing entity yet — create with "off" state to match SimState default
        fake_hass.states.set(
            climate_entity,
            FakeState(state="off", attributes={"current_temperature": indoor_f, "fan_mode": "auto"}),
        )


def _inject_thermostat_mode(fake_hass: FakeHass, climate_entity: str, hvac_mode: str) -> None:
    """Update the thermostat state string on the climate entity."""
    existing = fake_hass.states.get(climate_entity)
    attrs = dict(existing.attributes) if existing is not None else {}
    fake_hass.states.set(climate_entity, FakeState(state=hvac_mode, attributes=attrs))


# ---------------------------------------------------------------------------
# Open-sensor tracker
# ---------------------------------------------------------------------------


class _SensorTracker:
    """Track which sensors are currently open; provides the engine callback."""

    def __init__(self) -> None:
        self._open: set[str] = set()

    def open(self, entity_id: str) -> None:
        self._open.add(entity_id)

    def close(self, entity_id: str) -> None:
        self._open.discard(entity_id)

    def any_open(self) -> bool:
        return bool(self._open)

    def all_closed(self) -> bool:
        return not self._open


# ---------------------------------------------------------------------------
# Core adapter: parse event time
# ---------------------------------------------------------------------------


def _parse_event_time(time_str: str, default: datetime) -> datetime:
    """Parse ISO timestamp string to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_production_scenario(scenario: dict) -> ProductionRunResult:
    """Run a scenario dict through the REAL AutomationEngine and return results.

    Args:
        scenario: Parsed scenario JSON dict (as returned by json.load).

    Returns:
        ProductionRunResult with event_log, action_log, engine_state, callback_errors.
    """
    # --- Build merged config ---
    scenario_config: dict[str, Any] = scenario.get("config", {})
    merged_config: dict[str, Any] = {**_DEFAULT_CONFIG, **scenario_config}

    climate_entity: str = merged_config.get("climate_entity", "climate.test_thermostat")

    # --- Determine start time from first event ---
    events = sorted(scenario.get("events", []), key=lambda e: e["time"])
    if events:
        start_dt = _parse_event_time(events[0]["time"], datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC))
        # Start the clock a tiny bit before the first event
        from datetime import timedelta  # noqa: PLC0415

        start_dt = start_dt - timedelta(seconds=1)
    else:
        start_dt = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)

    # --- Determine initial thermostat mode (mirrors SimState default: "off") ---
    # Legacy simulator honours config["initial_thermostat_mode"] if present (simulate.py ~line 144).
    initial_thermostat_mode: str = merged_config.get("initial_thermostat_mode", "off")

    # --- Build headless engine ---
    engine, fake_hass, scheduler, event_log = build_headless_engine(
        config=merged_config,
        climate_entity=climate_entity,
        climate_state=initial_thermostat_mode,
        start_time=start_dt,
    )

    # --- Sensor tracker — drives engine._sensor_check_callback ---
    tracker = _SensorTracker()
    engine._sensor_check_callback = tracker.any_open

    # --- Process events under scheduler.installed() ---
    with scheduler.installed():
        for event in events:
            etype = event.get("type", "")
            ts_str = event.get("time", "")
            event_dt = _parse_event_time(ts_str, scheduler.now())

            # 1. Advance clock to event time (fires any due timers)
            scheduler.advance_to(event_dt)

            # 2. Dispatch event to production entry point
            _dispatch_event(event, etype, engine, fake_hass, scheduler, tracker, climate_entity, merged_config)

    # --- Capture final engine state snapshot ---
    engine_state = _snapshot_engine_state(engine)

    return ProductionRunResult(
        event_log=list(event_log),
        action_log=list(fake_hass.action_log),
        engine_state=engine_state,
        callback_errors=list(scheduler.callback_errors),
    )


# ---------------------------------------------------------------------------
# Event dispatcher
# ---------------------------------------------------------------------------


def _dispatch_event(
    event: dict,
    etype: str,
    engine: Any,
    fake_hass: FakeHass,
    scheduler: FakeScheduler,
    tracker: _SensorTracker,
    climate_entity: str,
    config: dict[str, Any],
) -> None:
    """Dispatch a single scenario event to the correct production engine method."""

    if etype == "temp_update":
        _handle_temp_update(event, engine, fake_hass, climate_entity)

    elif etype == "sensor_open":
        entity_id = event.get("entity", "binary_sensor.window")
        tracker.open(entity_id)
        asyncio.run(engine.handle_door_window_open(entity_id))

    elif etype == "sensor_close":
        entity_id = event.get("entity", "binary_sensor.window")
        tracker.close(entity_id)
        if tracker.all_closed():
            asyncio.run(engine.handle_all_doors_windows_closed())
        # If sensors still open, production does nothing on partial close —
        # it waits for all_closed() to be called.

    elif etype == "classification":
        classification = _build_classification_from_event(event)
        asyncio.run(engine.apply_classification(classification))

    elif etype == "occupancy_away":
        engine.set_occupancy_mode("away")
        asyncio.run(engine.handle_occupancy_away())

    elif etype == "occupancy_home":
        engine.set_occupancy_mode("home")
        asyncio.run(engine.handle_occupancy_home())

    elif etype == "occupancy_vacation":
        engine.set_occupancy_mode("vacation")
        asyncio.run(engine.handle_occupancy_vacation())

    elif etype == "occupancy_change":
        mode = event.get("mode", "home")
        engine.set_occupancy_mode(mode)
        if mode == "away":
            asyncio.run(engine.handle_occupancy_away())
        elif mode == "vacation":
            asyncio.run(engine.handle_occupancy_vacation())
        else:
            asyncio.run(engine.handle_occupancy_home())

    elif etype == "occupancy_change_with_override":
        # Mirrors simulate.py: set override active, then dispatch occupancy
        engine._manual_override_active = True
        mode = event.get("mode", "home")
        engine.set_occupancy_mode(mode)
        if mode == "away":
            asyncio.run(engine.handle_occupancy_away())
        elif mode == "vacation":
            asyncio.run(engine.handle_occupancy_vacation())
        else:
            asyncio.run(engine.handle_occupancy_home())

    elif etype == "bedtime":
        asyncio.run(engine.handle_bedtime())

    elif etype == "wakeup":
        asyncio.run(engine.handle_morning_wakeup())

    elif etype == "economizer_check":
        outdoor_temp = float(event.get("outdoor_temp", event.get("outdoor_f", 70.0)))
        indoor_temp = event.get("indoor_temp", event.get("indoor_f"))
        if indoor_temp is not None:
            indoor_temp = float(indoor_temp)
            _inject_indoor_temp(fake_hass, climate_entity, indoor_temp)
        engine.update_outdoor_temp(outdoor_temp)
        windows_open = bool(event.get("windows_open", False))
        hour = int(event.get("hour", -1))
        asyncio.run(engine.check_window_cooling_opportunity(outdoor_temp, indoor_temp, windows_open, hour))

    elif etype == "thermostat_state_changed":
        # Mirrors coordinator._async_thermostat_changed stale-clear + override detection.
        # Production path: coordinator calls handle_manual_override() when it detects
        # divergence between thermostat state and classification.
        new_hvac_mode = event.get("hvac_mode", "off")
        _inject_thermostat_mode(fake_hass, climate_entity, new_hvac_mode)
        c = engine._current_classification
        classification_mode = c.hvac_mode if c else None
        if (
            classification_mode is not None
            and not engine._manual_override_active
            and not engine._override_confirm_pending
            and new_hvac_mode != classification_mode
            and new_hvac_mode not in ("off",)
        ):
            engine.handle_manual_override(
                source="normal",
                old_mode=classification_mode,
                new_mode=new_hvac_mode,
                classification_mode=classification_mode,
            )
        # else: no override action needed (consistent with classification, or already pending)

    elif etype in ("fan_cycle_on", "fan_cycle_off", "grace_start", "grace_end"):
        # FINDINGS: no clean production entry point — see module docstring.
        # These event types are driven internally by the scheduler (timers) in
        # production; the adapter skips them as the scheduler already drives them
        # via advance_to() before each event.
        pass

    # All other unknown types are silently ignored (mirrors simulate.py's final `return None`)


def _handle_temp_update(
    event: dict,
    engine: Any,
    fake_hass: FakeHass,
    climate_entity: str,
) -> None:
    """Handle temp_update: inject temperatures, then re-evaluate nat-vent conditions."""
    outdoor_f = event.get("outdoor_f")
    indoor_f = event.get("indoor_f")

    if outdoor_f is not None:
        engine.update_outdoor_temp(float(outdoor_f))

    if indoor_f is not None:
        _inject_indoor_temp(fake_hass, climate_entity, float(indoor_f))

    # Production: coordinator calls check_natural_vent_conditions() on each update.
    # This is the correct re-evaluation entry point for temperature changes.
    asyncio.run(engine.check_natural_vent_conditions())


# ---------------------------------------------------------------------------
# Engine state snapshot
# ---------------------------------------------------------------------------


def _snapshot_engine_state(engine: Any) -> dict[str, Any]:
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
    ):
        snap[attr] = getattr(engine, attr, None)

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
