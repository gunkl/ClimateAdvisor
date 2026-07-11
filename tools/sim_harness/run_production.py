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
                            + IF indoor changed: run_coro(engine.nat_vent_temperature_check())
                              (if nat-vent active) + run_coro(engine.fan_thermostat_check())
                              (if any CA fan active) — mirrors _async_thermostat_changed's
                              state-listener dispatch (coordinator.py:2837-2862)
                            + run_coro(engine.check_natural_vent_conditions())
  sensor_open             → (use_coordinator=True) states.async_set(entity_id, "on") — fires
                              the real coordinator._async_door_window_changed listener
                              (requires entity_id in config["door_window_sensors"]; owns
                              debounce, pause, and starting the real grace timer)
                            → (use_coordinator=False) run_coro(engine.handle_door_window_open(...))
  sensor_close            → (use_coordinator=True) states.async_set(entity_id, "off") — same
                              real listener, owns resume + all-closed grace-start
                            → (use_coordinator=False) update engine._sensor_check_callback
                              + if no sensors open: run_coro(engine.handle_all_doors_windows_closed())
  cancel_override         → (use_coordinator=True only) replicates api.py's
                              ClimateAdvisorCancelOverrideView.post(): clear_manual_override()
                              + _cancel_grace_timers() synchronously, then a real 10s
                              async_call_later before re-applying coordinator._current_classification
  classification          → build DayClassification from event fields
                            + run_coro(engine.apply_classification(classification))
  occupancy_away          → engine.set_occupancy_mode("away")
                            + run_coro(engine.handle_occupancy_away())
  occupancy_home          → engine.set_occupancy_mode("home")
                            + run_coro(engine.handle_occupancy_home())
  occupancy_vacation      → engine.set_occupancy_mode("vacation")
                            + run_coro(engine.handle_occupancy_vacation())
  occupancy_change        → dispatches to away/home/vacation by event["mode"]
  occupancy_change_with_override
                          → sets _manual_override_active=True then dispatches
  bedtime                 → run_coro(engine.handle_bedtime())
  wakeup                  → run_coro(engine.handle_morning_wakeup())
  economizer_check        → run_coro(engine.check_window_cooling_opportunity(...))
  thermostat_state_changed
                          → (use_coordinator=True) states.async_set() — fires the real
                            coordinator._async_thermostat_changed listener (full
                            override-detection state machine, no approximation)
                          → (use_coordinator=False) raw state injection only; no
                            override-detection equivalent exists at the engine level
  activate_fan_min_runtime
                          → run_coro(engine.start_min_fan_runtime_cycles()) — real public
                            entry point, activates the fan without a nat-vent session
  reconcile_fan_on_startup
                          → inject indoor/outdoor + run_coro(engine.reconcile_fan_on_startup(
                            thermostat_fan_running=, any_sensor_open=)) — mirrors the
                            coordinator's startup-coalesce call

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

from tools.sim_harness._loop import run_coro  # noqa: E402
from tools.sim_harness.build_coordinator import build_headless_coordinator  # noqa: E402
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


def _inject_indoor_temp(
    fake_hass: FakeHass, climate_entity: str, indoor_f: float | None, *, dispatch: bool = False
) -> None:
    """Update the current_temperature attribute on the climate entity.

    The engine reads indoor temperature via ``_get_indoor_temp_f()`` which
    falls back to ``hass.states.get(climate_entity).attributes["current_temperature"]``
    when no indoor_temp_entity is configured (the default).

    Args:
        dispatch: When True (a coordinator is present — Issue #474), use
            ``states.async_set()`` so the change reaches the coordinator's real
            ``_async_thermostat_changed`` listener. When False (engine-only
            scenarios), use the silent ``states.set()`` — no listeners are
            registered anyway, since no coordinator's ``async_setup()`` ran.
    """
    if indoor_f is None:
        return
    existing = fake_hass.states.get(climate_entity)
    if existing is not None:
        attrs = dict(existing.attributes)
        attrs["current_temperature"] = indoor_f
        state_str = existing.state
    else:
        # No existing entity yet — create with "off" state to match SimState default
        attrs = {"current_temperature": indoor_f, "fan_mode": "auto"}
        state_str = "off"
    if dispatch:
        fake_hass.states.async_set(climate_entity, state_str, attrs)
    else:
        fake_hass.states.set(climate_entity, FakeState(state=state_str, attributes=attrs))


def _inject_thermostat_mode(
    fake_hass: FakeHass, climate_entity: str, hvac_mode: str, *, dispatch: bool = False
) -> None:
    """Update the thermostat state string on the climate entity.

    Args:
        dispatch: see ``_inject_indoor_temp``.
    """
    existing = fake_hass.states.get(climate_entity)
    attrs = dict(existing.attributes) if existing is not None else {}
    if dispatch:
        fake_hass.states.async_set(climate_entity, hvac_mode, attrs)
    else:
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


def run_production_scenario(scenario: dict, *, use_coordinator: bool = False) -> ProductionRunResult:
    """Run a scenario dict through the REAL AutomationEngine and return results.

    Args:
        scenario: Parsed scenario JSON dict (as returned by json.load).
        use_coordinator: When True (Issue #474), build a real
            ``ClimateAdvisorCoordinator`` via ``build_headless_coordinator()``
            instead of a bare engine via ``build_headless_engine()``. Event
            types that need coordinator-listener fidelity
            (``thermostat_state_changed``, indoor-temp ticks inside
            ``temp_update``) dispatch through ``fake_hass.states.async_set()``
            so the real ``_async_thermostat_changed`` listener runs, instead
            of the old hand-approximated mirror. Required for any scenario
            previously tagged ``track: "integration"`` /
            ``simulator_support: false``.

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

    # --- Build headless engine (or real coordinator — Issue #474) ---
    coordinator: Any | None = None
    if use_coordinator:
        # skip_startup_coalesce (scenario field, default False — the honest default,
        # see build_headless_coordinator's docstring): a freshly built coordinator has
        # its real 5-minute post-restart override-detection suppression window active,
        # same as production. A scenario testing steady-state behavior (not startup
        # itself) must opt out via "skip_startup_coalesce": true, or every dispatched
        # event vacuously early-returns before reaching any override-detection guard —
        # this was found and fixed for the #474 proving slice.
        coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
            config=merged_config,
            climate_entity=climate_entity,
            climate_state=initial_thermostat_mode,
            start_time=start_dt,
            skip_startup_coalesce=bool(scenario.get("skip_startup_coalesce", False)),
        )
        engine = coordinator.automation_engine
    else:
        engine, fake_hass, scheduler, event_log = build_headless_engine(
            config=merged_config,
            climate_entity=climate_entity,
            climate_state=initial_thermostat_mode,
            start_time=start_dt,
        )

    # --- Sensor tracker — drives engine._sensor_check_callback (engine-only mode) ---
    # Issue #476: when a coordinator is present, its real __init__ already wired
    # engine._sensor_check_callback = coordinator._any_sensor_open (production
    # wiring, reads real fake_hass entity state). Overwriting it here with the
    # _SensorTracker stub — which coordinator-mode sensor_open/sensor_close
    # dispatch never updates (it dispatches via states.async_set() through the
    # real listener instead) — silently breaks any check that depends on "is a
    # sensor still open" via the engine's callback (e.g. _on_grace_expired's
    # re-pause-on-expiry-with-sensor-open decision: found returning re_paused=False
    # even with a sensor genuinely left open, because the callback always reported
    # False). Only wire the tracker for engine-only scenarios.
    tracker = _SensorTracker()
    if coordinator is None:
        engine._sensor_check_callback = tracker.any_open

    # --- Optional ODE inputs (Issue #236 D — scenario schema extension) ---
    # A scenario may supply a learned thermal model and/or an hourly forecast so
    # the production ODE ceiling guard (and the forecast/floor-imminence nat-vent
    # guards) can be exercised. The LEGACY simulator has no ODE and ignores these
    # keys entirely, so adding them to a scenario only affects the production run.
    #   "thermal_model":   dict with keys the engine reads, e.g.
    #                      {"confidence": "high", "k_passive": -0.18,
    #                       "k_active_heat": ..., "k_active_cool": ..., "k_vent": ...,
    #                       "k_solar": ...}
    #   "hourly_forecast": list of {"datetime": ISO8601, "temperature": float}
    if "thermal_model" in scenario and isinstance(scenario["thermal_model"], dict):
        engine._thermal_model = scenario["thermal_model"]
    if "hourly_forecast" in scenario and isinstance(scenario["hourly_forecast"], list):
        engine._hourly_forecast_temps = scenario["hourly_forecast"]

    # --- Process events under scheduler.installed() ---
    with scheduler.installed():
        for event in events:
            etype = event.get("type", "")
            ts_str = event.get("time", "")
            event_dt = _parse_event_time(ts_str, scheduler.now())

            # 1. Advance clock to event time (fires any due timers)
            scheduler.advance_to(event_dt)

            # 2. Dispatch event to production entry point
            _dispatch_event(
                event, etype, engine, fake_hass, scheduler, tracker, climate_entity, merged_config, coordinator
            )

            # 3. Settle (Issue #476 — supersedes the #474 trailing-only fix, which
            # only caught the LAST event). A coordinator-mode dispatch
            # (fake_hass.states.async_set(...) in _dispatch_event) enqueues the
            # real listener (e.g. _async_door_window_changed) as a task via
            # async_create_task — it does NOT run synchronously. Without settling
            # here, that task sits queued until the NEXT event's own
            # advance_to(event_dt) call, which only drains it as an incidental
            # side effect of firing whatever UNRELATED heap entry happens to be
            # due first (found via a real scenario: a periodic 5-min thermal-
            # sampler tick happened to be due mid-way to the next event, so the
            # queued door/window listener ran at ITS fire time, not the actual
            # dispatch time — sensor_opened was logged ~5 minutes late). Worse,
            # if that listener itself schedules a NEW async_call_later() heap
            # entry (e.g. the debounce timer), that entry is pushed AFTER
            # advance_to()'s own heap-loop already exited, so it silently sits
            # unfired for yet another event before it can fire.
            # advance_to(scheduler.now()) re-enters the same heap-fire ->
            # drain-tasks -> recheck-heap loop advance_to() already implements,
            # settling arbitrarily deep chains to a fixed point at the ACTUAL
            # dispatch time, before any more wall-clock time is allowed to pass.
            scheduler.advance_to(scheduler.now())

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
    coordinator: Any | None = None,
) -> None:
    """Dispatch a single scenario event to the correct production engine method.

    ``coordinator`` (Issue #474) is non-None only when
    ``run_production_scenario(..., use_coordinator=True)`` built a real
    ``ClimateAdvisorCoordinator``. Event types that need coordinator-listener
    fidelity check for it explicitly.
    """

    if etype == "temp_update":
        _handle_temp_update(event, engine, fake_hass, climate_entity, coordinator)

    elif etype == "sensor_open":
        entity_id = event.get("entity", "binary_sensor.window")
        if coordinator is not None:
            # Issue #476: real state-change dispatch through the coordinator's
            # actual _async_door_window_changed listener (registered via
            # _subscribe_door_window_listeners() in async_setup(), requires the
            # entity_id to be in the scenario's config["door_window_sensors"]).
            # This owns debounce (harness default sensor_debounce_seconds=0, so
            # effectively instant), pause/resume, and starting the real grace
            # timer — none of that is approximated here.
            fake_hass.states.async_set(entity_id, "on", {})
        else:
            tracker.open(entity_id)
            run_coro(engine.handle_door_window_open(entity_id))

    elif etype == "sensor_close":
        entity_id = event.get("entity", "binary_sensor.window")
        if coordinator is not None:
            fake_hass.states.async_set(entity_id, "off", {})
        else:
            tracker.close(entity_id)
            if tracker.all_closed():
                run_coro(engine.handle_all_doors_windows_closed())
            # If sensors still open, production does nothing on partial close —
            # it waits for all_closed() to be called.

    elif etype == "classification":
        classification = _build_classification_from_event(event)
        # Issue #295: pass the current injected indoor temp so apply_classification
        # can evaluate the pre-cool achievement gate — mirrors coordinator behaviour
        # where _get_indoor_temp() is always passed on each cycle.
        _cls_indoor_f = None
        _climate_st = fake_hass.states.get(climate_entity)
        if _climate_st is not None:
            _cls_indoor_f = _climate_st.attributes.get("current_temperature")
            if _cls_indoor_f is not None:
                try:
                    _cls_indoor_f = float(_cls_indoor_f)
                except (TypeError, ValueError):
                    _cls_indoor_f = None
        # Issue #474: when a real coordinator is present, also set its own
        # _current_classification — _async_thermostat_changed's override-detection
        # branches read self._current_classification (coordinator's own copy, not
        # engine._current_classification) to know what mode CA expects.
        if coordinator is not None:
            coordinator._current_classification = classification
        run_coro(engine.apply_classification(classification, indoor_temp=_cls_indoor_f))

    elif etype == "occupancy_away":
        engine.set_occupancy_mode("away")
        run_coro(engine.handle_occupancy_away())

    elif etype == "occupancy_home":
        engine.set_occupancy_mode("home")
        run_coro(engine.handle_occupancy_home())

    elif etype == "occupancy_vacation":
        engine.set_occupancy_mode("vacation")
        run_coro(engine.handle_occupancy_vacation())

    elif etype == "occupancy_change":
        mode = event.get("mode", "home")
        engine.set_occupancy_mode(mode)
        if mode == "away":
            run_coro(engine.handle_occupancy_away())
        elif mode == "vacation":
            run_coro(engine.handle_occupancy_vacation())
        else:
            run_coro(engine.handle_occupancy_home())

    elif etype == "occupancy_change_with_override":
        # Mirrors simulate.py: set override active, then dispatch occupancy
        engine._manual_override_active = True
        mode = event.get("mode", "home")
        engine.set_occupancy_mode(mode)
        if mode == "away":
            run_coro(engine.handle_occupancy_away())
        elif mode == "vacation":
            run_coro(engine.handle_occupancy_vacation())
        else:
            run_coro(engine.handle_occupancy_home())

    elif etype == "bedtime":
        run_coro(engine.handle_bedtime())

    elif etype == "wakeup":
        run_coro(engine.handle_morning_wakeup())

    elif etype == "economizer_check":
        outdoor_temp = float(event.get("outdoor_temp", event.get("outdoor_f", 70.0)))
        indoor_temp = event.get("indoor_temp", event.get("indoor_f"))
        if indoor_temp is not None:
            indoor_temp = float(indoor_temp)
            _inject_indoor_temp(fake_hass, climate_entity, indoor_temp)
        engine.update_outdoor_temp(outdoor_temp)
        windows_open = bool(event.get("windows_open", False))
        hour = int(event.get("hour", -1))
        run_coro(engine.check_window_cooling_opportunity(outdoor_temp, indoor_temp, windows_open, hour))

    elif etype == "reconcile_fan_on_startup":
        # Step-1 blind-spot closure: no golden previously exercised this real, public
        # engine entry point at all. In production, coordinator._do_startup_coalesce()
        # calls this with an archetype-resolved thermostat_fan_running signal (real
        # thermostat attrs for FAN_MODE_HVAC, the physical WHF entity state for
        # FAN_MODE_WHOLE_HOUSE, per Issue #423) — the scenario supplies that already-
        # resolved boolean directly, since resolving it from raw entity state is the
        # coordinator's job, not this engine method's. This is NOT a coordinator-dispatch
        # approximation (contrast the deleted thermostat_state_changed override-detection
        # mirror, Issue #474) — it's a direct call to a real public AutomationEngine
        # method with externally-supplied inputs, the same legitimate pattern as
        # "classification"/"economizer_check"/"pre_cool" events.
        outdoor_f = event.get("outdoor_f")
        if outdoor_f is not None:
            engine.update_outdoor_temp(float(outdoor_f))
        indoor_f = event.get("indoor_f")
        if indoor_f is not None:
            _inject_indoor_temp(fake_hass, climate_entity, float(indoor_f))
        run_coro(
            engine.reconcile_fan_on_startup(
                indoor=engine._get_indoor_temp_f(),
                outdoor=engine._last_outdoor_temp,
                thermostat_fan_running=bool(event.get("thermostat_fan_running", False)),
                any_sensor_open=bool(event.get("any_sensor_open", False)),
            )
        )

    elif etype == "thermostat_state_changed":
        # Issue #474: when a real coordinator is present, dispatch via
        # states.async_set() — this fires the coordinator's actual
        # _async_thermostat_changed listener (coordinator.py:2910-3461), which
        # owns the full 3-branch override-detection state machine
        # (door-pause / re-override-during-grace / normal), startup-coalesce
        # suppression, the expected-confirmation guard, and HVAC-session
        # tracking. There is no longer a hand-approximated substitute for any
        # of that here — the 18-line override-detection mirror that used to
        # live in this branch covered only the "normal" case and had already
        # gone stale post-#249 (see override_detection_and_confirmation.json).
        #
        # When no coordinator is present (engine-only scenarios), only inject
        # the raw state — there is no coordinator-equivalent override
        # detection to approximate at the engine level; a scenario that needs
        # override-detection fidelity must run with use_coordinator=True.
        new_hvac_mode = event.get("hvac_mode", "off")
        if coordinator is not None:
            _inject_thermostat_mode(fake_hass, climate_entity, new_hvac_mode, dispatch=True)
        else:
            _inject_thermostat_mode(fake_hass, climate_entity, new_hvac_mode)

    elif etype == "cancel_override":
        # Issue #476: replicates api.py's ClimateAdvisorCancelOverrideView.post()
        # exactly — clear_manual_override() + _cancel_grace_timers() synchronously,
        # then a real 10-second async_call_later before re-applying the current
        # classification. This is the production code path for the dashboard's
        # "Cancel Override" button (cancel_override_then_resume.json). Requires a
        # real coordinator — there is no engine-only equivalent since the delayed
        # re-apply is coordinator-owned state (coordinator._current_classification).
        if coordinator is not None:
            import custom_components.climate_advisor.coordinator as _coord_mod  # noqa: PLC0415

            ae = coordinator.automation_engine
            if ae._manual_override_active:
                ae.clear_manual_override()
                ae._cancel_grace_timers()

                @_coord_mod.callback
                def _apply_after_delay(_now: Any, _coordinator: Any = coordinator, _ae: Any = ae) -> None:
                    if _coordinator._current_classification:
                        _coordinator.hass.async_create_task(
                            _ae.apply_classification(_coordinator._current_classification)
                        )

                _coord_mod.async_call_later(coordinator.hass, 10, _apply_after_delay)

    elif etype in ("fan_cycle_on", "fan_cycle_off", "grace_start", "grace_end"):
        # FINDINGS: no clean production entry point — see module docstring.
        # These event types are driven internally by the scheduler (timers) in
        # production; the adapter skips them as the scheduler already drives them
        # via advance_to() before each event.
        pass

    elif etype == "activate_fan_min_runtime":
        # Real, public production entry point (start_min_fan_runtime_cycles(), called
        # at coordinator startup / after override-clear) — NOT the internal timer-driven
        # mid-cycle re-trigger the module docstring's fan_cycle_on/off FINDING refers to.
        # Added for Step 2 fan_thermostat_check two-phase synthetic coverage: activates
        # the fan (self._fan_active=True) WITHOUT a nat-vent session
        # (self._natural_vent_active stays False), the precondition needed to reach
        # Check 1's STOP_DEACTIVATE branch and Check 2's non-nat-vent floor stop.
        run_coro(engine.start_min_fan_runtime_cycles())

    elif etype == "nat_vent_temperature_check":
        # Bug 3 (Issue #321): Dispatch midpoint-cycling re-evaluation to the production engine.
        # Used by nat_vent_thermostat_cycling pending scenario to assert cycling behavior.
        indoor_temp = float(event.get("indoor_temp", event.get("indoor_f", 70.0)))
        _inject_indoor_temp(fake_hass, climate_entity, indoor_temp)
        run_coro(engine.nat_vent_temperature_check(indoor_temp))

    elif etype == "pre_cool":
        # Issue #258: Dispatch the pre-cool trigger to the production engine.
        # nat_vent_just_closed=True when the event marks the post-nat-vent trigger;
        # False when using the wake_time-4h fallback path.
        indoor_f = event.get("indoor_f")
        indoor_temp = float(indoor_f) if indoor_f is not None else None
        if indoor_temp is not None:
            _inject_indoor_temp(fake_hass, climate_entity, indoor_temp)
        nat_vent_just_closed = bool(event.get("nat_vent_just_closed", False))
        run_coro(engine.handle_pre_cool(indoor_temp=indoor_temp, nat_vent_just_closed=nat_vent_just_closed))

    elif etype == "coordinator_refresh" and coordinator is not None:
        # Issue #481: dispatches the real periodic coordinator cycle
        # (DataUpdateCoordinator.async_request_refresh() -> _async_update_data()) at a
        # specific simulated time. Requires use_coordinator=True — there is no
        # engine-only equivalent since _async_update_data() (and the incident detection
        # it runs at its tail, _detect_and_emit_incidents()) is coordinator-owned.
        # Production calls the equivalent (async_refresh()) every 30 minutes via
        # update_interval; the harness's _MockDataUpdateCoordinator only auto-runs
        # _async_update_data() once, at async_config_entry_first_refresh() during setup
        # (tools/sim_harness/ha_stubs.py — the stub does not implement async_refresh()
        # at all, only async_request_refresh()/async_config_entry_first_refresh()), so
        # scenarios that need to exercise a LATER cycle (e.g. incident detection
        # evaluated against a later-injected indoor temp) must dispatch it explicitly.
        # Purely additive — no existing event type's semantics changed.
        run_coro(coordinator.async_request_refresh())

    # All other unknown types are silently ignored (mirrors simulate.py's final `return None`)


def _handle_temp_update(
    event: dict,
    engine: Any,
    fake_hass: FakeHass,
    climate_entity: str,
    coordinator: Any | None = None,
) -> None:
    """Handle temp_update: inject temperatures, then re-evaluate nat-vent conditions.

    Reflects TWO distinct real production triggers, not one:
      1. ``_async_thermostat_changed`` (coordinator.py:2910-3461) — a state-listener
         that fires on every indoor current_temperature ATTRIBUTE change and calls
         ``nat_vent_temperature_check()`` (if nat-vent active) and
         ``fan_thermostat_check()`` (if any CA fan active). Issue #474: when a real
         coordinator is present, the indoor-temp injection dispatches via
         ``states.async_set()``, so the real listener runs this logic itself —
         no approximation needed. When no coordinator is present (engine-only
         scenarios — the majority of the existing golden suite), this function
         still calls ``nat_vent_temperature_check()``/``fan_thermostat_check()``
         directly: unlike the old ``thermostat_state_changed`` override-detection
         mirror (deleted — see that branch), this one isn't approximating a
         multi-branch state machine prone to drift, it's a direct, unconditional
         call guarded by the same two flags production reads — legitimate
         engine-level Tier-A fidelity, not coordinator-dispatch duplication, and
         it's what most existing golden scenarios rely on.
      2. ``check_natural_vent_conditions()`` — the periodic ``_async_update_data``
         cycle re-evaluation, dispatched unconditionally below regardless of mode
         (it's a real, distinct production trigger, not a coordinator-listener
         approximation).
    """
    outdoor_f = event.get("outdoor_f")
    indoor_f = event.get("indoor_f")

    existing = fake_hass.states.get(climate_entity)
    old_indoor = existing.attributes.get("current_temperature") if existing is not None else None

    if outdoor_f is not None:
        engine.update_outdoor_temp(float(outdoor_f))

    if indoor_f is not None:
        _inject_indoor_temp(fake_hass, climate_entity, float(indoor_f), dispatch=coordinator is not None)

    new_indoor = float(indoor_f) if indoor_f is not None else None
    if coordinator is None and new_indoor is not None and new_indoor != old_indoor:
        if engine._natural_vent_active:
            run_coro(engine.nat_vent_temperature_check(new_indoor))
        if engine._fan_active or engine._natural_vent_active:
            run_coro(
                engine.fan_thermostat_check(
                    indoor=engine._get_indoor_temp_f(),
                    outdoor=engine._last_outdoor_temp,
                    trigger="tick",
                )
            )

    # Production: coordinator calls check_natural_vent_conditions() on each update.
    # This is the correct re-evaluation entry point for temperature changes.
    run_coro(engine.check_natural_vent_conditions())

    # Issue #236 (ceiling-D): the ODE ceiling guard lives inside apply_classification,
    # which production re-runs every coordinator cycle. The harness models classification
    # as a one-shot, so a temp_update may carry a `predicted_indoor` curve to re-invoke
    # apply_classification (with the current classification) — faithfully reproducing the
    # periodic ceiling-guard re-evaluation. Legacy ignores this field.
    predicted = event.get("predicted_indoor")
    if predicted and getattr(engine, "_current_classification", None) is not None:
        # Issue #295: pass current indoor temp so achievement gate is evaluated on cycle.
        _tu_indoor_f = float(indoor_f) if indoor_f is not None else None
        run_coro(
            engine.apply_classification(
                engine._current_classification,
                predicted_indoor=predicted,
                indoor_temp=_tu_indoor_f,
            )
        )


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
        "_economizer_phase",
        "_pre_condition_achieved",  # Issue #295
        "_pre_condition_achieved_date",  # Issue #295
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
