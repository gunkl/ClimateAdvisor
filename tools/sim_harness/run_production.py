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

  temp_update             → _feed_outdoor_temp(): coordinator._apply_outdoor_temp(outdoor_f)
                              in coordinator mode, else engine.update_outdoor_temp(outdoor_f)
                            + inject indoor temp via climate entity attribute
                            + IF indoor changed: run_coro(engine.nat_vent_temperature_check())
                              (if nat-vent active) + run_coro(engine.fan_thermostat_check())
                              (if any CA fan active) + run_coro(engine.comfort_family_temperature_check())
                              (Issue #858, unconditional — the method's own internal gating
                              matches production) — mirrors _async_thermostat_changed's
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
  sensor_unavailable      → (use_coordinator=True only) states.set_simple(entity_id,
                              "unavailable") — silent, no listener dispatch. Sets up old_state
                              for a SUBSEQUENT plain sensor_open event to correctly compute
                              old_state.state == "unavailable" and exercise the real
                              _async_door_window_changed() reconnect-blip path (Issue #645).
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
  fan_backstop_tick      → (use_coordinator=True) real coordinator._compute_fan_status() +
                            coordinator._should_run_untracked_fan_backstop() +, if that
                            gate passes, run_coro(automation_engine.reconcile_fan_on_startup(
                            trigger="backstop_30min")) — the real periodic untracked-fan
                            backstop call site (Issue #627), previously not dispatchable at all
  simulate_restart        → (use_coordinator=True) engine.restore_state(engine.
                            get_serializable_state()) + coordinator._startup_coalesce_active
                            = True — the real continuity boundary an HA restart crosses
                            (Issue #263/#327 clean-slate + fresh coalescing window), not a
                            mirror of what restart does to engine state

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

import contextlib
import os
import sys
from datetime import datetime, timedelta
from typing import Any

# Ensure project root on path (support running as a script)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness._loop import run_coro  # noqa: E402
from tools.sim_harness.build_coordinator import build_headless_coordinator  # noqa: E402
from tools.sim_harness.build_engine import build_headless_engine  # noqa: E402
from tools.sim_harness.fake_hass import FakeHass, FakeState  # noqa: E402
from tools.sim_harness.fake_scheduler import FakeScheduler  # noqa: E402
from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402
from tools.sim_harness.scenario_model import (  # noqa: E402
    ProductionRunResult,
    build_classification_from_event,
    merged_scenario_config,
    parse_event_time,
    scenario_start_time,
    snapshot_engine_state,
    sorted_events,
)
from tools.sim_harness.session import drive_scenario  # noqa: E402

# Issue #1127 Increment 5: the Home Assistant stub install is a property of the FakeHass ADAPTER
# (FakeHassSession.__init__), not of importing this module, so a real-HA environment can import
# the runner seam without the stub layer.  The pure helpers moved to scenario_model.py; the old
# private names stay importable from here.
_build_classification_from_event = build_classification_from_event
_snapshot_engine_state = snapshot_engine_state
_parse_event_time = parse_event_time

__all__ = ["FakeHassSession", "ProductionRunResult", "run_production_scenario"]


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
        # Issue #968: fan_modes default matches build_engine.py's rationale.
        attrs = {"current_temperature": indoor_f, "fan_mode": "auto", "fan_modes": ["auto", "on"]}
        state_str = "off"
    if dispatch:
        fake_hass.states.async_set(climate_entity, state_str, attrs)
    else:
        fake_hass.states.set(climate_entity, FakeState(state=state_str, attributes=attrs))


def _inject_thermostat_mode(
    fake_hass: FakeHass,
    climate_entity: str,
    hvac_mode: str,
    *,
    dispatch: bool = False,
    temperature: float | None = None,
    hvac_action: str | None = None,
    fan_mode_attr: str | None = None,
) -> None:
    """Update the thermostat state string on the climate entity.

    Args:
        dispatch: see ``_inject_indoor_temp``.
        temperature: Issue #483 — optional setpoint to set alongside the mode change,
            for scenarios where the user changes mode AND setpoint in a single
            thermostat interaction (e.g. a Nest/Ecobee "heat at 70" UI action).
            When omitted (the default, all pre-existing scenarios), the entity's
            existing ``temperature`` attribute is left untouched, matching the
            prior behavior exactly.
        hvac_action: Issue #790 — optional ``hvac_action`` attribute to set alongside
            the mode change, for scenarios that need to drive the real
            ``_async_thermostat_changed`` listener's ``old_action``/``new_action``
            transition detection (e.g. a transition into ``"fan"`` that fires
            ``reconcile_fan_on_startup(trigger="thermostat_state_change")``). When
            omitted (the default, all pre-existing scenarios), the entity's existing
            ``hvac_action`` attribute is left untouched, matching the prior behavior
            exactly.
        fan_mode_attr: Issue #1103 — optional ``fan_mode`` attribute, so a scenario can state
            whether the thermostat's running blower is its own schedule (``"auto"``) or a fan
            someone set on (``"on"``). Omitted = attribute left untouched (prior behavior).
    """
    existing = fake_hass.states.get(climate_entity)
    attrs = dict(existing.attributes) if existing is not None else {}
    if temperature is not None:
        attrs["temperature"] = temperature
    if hvac_action is not None:
        attrs["hvac_action"] = hvac_action
    if fan_mode_attr is not None:
        attrs["fan_mode"] = fan_mode_attr
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
# FakeHass adapter: the Session implementation
# ---------------------------------------------------------------------------


class _FakeThermostatView:
    """Read-only ThermostatView over the FakeHass climate entity."""

    def __init__(self, fake_hass: FakeHass, climate_entity: str) -> None:
        self._hass = fake_hass
        self._entity = climate_entity

    def _state(self) -> Any:
        return self._hass.states.get(self._entity)

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except TypeError, ValueError:
            return None

    @property
    def mode(self) -> str | None:
        return getattr(self._state(), "state", None)

    @property
    def setpoint_f(self) -> float | None:
        st = self._state()
        return self._num(st.attributes.get("temperature")) if st is not None else None

    @property
    def action(self) -> str | None:
        st = self._state()
        return st.attributes.get("hvac_action") if st is not None else None

    @property
    def indoor_f(self) -> float | None:
        st = self._state()
        return self._num(st.attributes.get("current_temperature")) if st is not None else None


class FakeHassSession:
    """The FakeHass adapter of the Session protocol (``tools/sim_harness/session.py``).

    Behavior-preserving extraction of the original ``run_production_scenario`` body: the same
    build, the same dispatch table, the same settle, the same final snapshot taken inside the
    patched clock.  ``install_ha_stubs()`` runs here, and nowhere at import time.

    ``probe`` (optional, ``promise_probe.PromiseProbe``) is called after every settled step and,
    when attached, time is advanced in slices of at most five minutes so the probe also sees the
    quiet stretches between events.  Slicing is behavior-neutral (the heap fires callbacks in
    time order either way); ``tests/test_goldens_promises.py`` proves the logs are identical.
    """

    PROBE_SLICE = timedelta(minutes=5)

    def __init__(
        self,
        scenario: dict,
        *,
        use_coordinator: bool = False,
        role: str = "production",
        dry_run: bool = False,
        probe: Any = None,
    ) -> None:
        install_ha_stubs()
        self.scenario = scenario
        self.probe = probe
        self._event_index = 0
        self._closed = False
        self._result: ProductionRunResult | None = None

        # --- Build merged config ---
        merged_config: dict[str, Any] = merged_scenario_config(scenario)
        climate_entity: str = merged_config.get("climate_entity", "climate.test_thermostat")
        self.config = merged_config
        self.climate_entity = climate_entity

        # --- Determine start time from first event ---
        events = sorted_events(scenario)
        start_dt = scenario_start_time(events)

        # --- Determine initial thermostat mode (mirrors SimState default: "off") ---
        # Legacy simulator honours config["initial_thermostat_mode"] if present (simulate.py ~line 144).
        initial_thermostat_mode: str = merged_config.get("initial_thermostat_mode", "off")

        # --- Build headless engine (or real coordinator - Issue #474) ---
        coordinator: Any | None = None
        if use_coordinator:
            # skip_startup_coalesce (scenario field, default False - the honest default,
            # see build_headless_coordinator's docstring): a freshly built coordinator has
            # its real 5-minute post-restart override-detection suppression window active,
            # same as production. A scenario testing steady-state behavior (not startup
            # itself) must opt out via "skip_startup_coalesce": true, or every dispatched
            # event vacuously early-returns before reaching any override-detection guard -
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
                role=role,
                dry_run=dry_run,
            )

        # --- Issue #1048: opt-in thermostat quirk (scenario config "thermostat_quirk") ---
        # "sensi_like": set_temperature ignores the hvac_mode kwarg and is rejected while
        # the thermostat is Off. Absent by default, so all existing scenarios are unchanged.
        _quirk = merged_config.get("thermostat_quirk")
        if _quirk:
            fake_hass.set_entity_quirk(climate_entity, _quirk)

        # --- Sensor tracker - drives engine._sensor_check_callback (engine-only mode) ---
        # Issue #476: when a coordinator is present, its real __init__ already wired
        # engine._sensor_check_callback = coordinator._any_sensor_open (production
        # wiring, reads real fake_hass entity state). Overwriting it here with the
        # _SensorTracker stub - which coordinator-mode sensor_open/sensor_close
        # dispatch never updates (it dispatches via states.async_set() through the
        # real listener instead) - silently breaks any check that depends on "is a
        # sensor still open" via the engine's callback (e.g. _on_grace_expired's
        # re-pause-on-expiry-with-sensor-open decision: found returning re_paused=False
        # even with a sensor genuinely left open, because the callback always reported
        # False). Only wire the tracker for engine-only scenarios.
        tracker = _SensorTracker()
        if coordinator is None:
            engine._sensor_check_callback = tracker.any_open

        # --- Optional ODE inputs (Issue #236 D - scenario schema extension) ---
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
            if coordinator is not None:
                # Production's coordinator owns the forecast and pushes it to the engine
                # (_refresh_hourly_forecast); keep both copies identical.
                coordinator._hourly_forecast_temps = scenario["hourly_forecast"]

        self.coordinator = coordinator
        self.engine = engine
        self.fake_hass = fake_hass
        self.scheduler = scheduler
        self.event_log = event_log
        self._tracker = tracker
        self._thermostat = _FakeThermostatView(fake_hass, climate_entity)

        # --- Everything from here runs under scheduler.installed() (patched clock + timers) ---
        self._stack = contextlib.ExitStack()
        try:
            self._stack.enter_context(scheduler.installed())
            if probe is not None:
                probe.attach(self)
        except BaseException:
            self._stack.close()
            raise

    # -- Session protocol -----------------------------------------------------

    def now(self) -> datetime:
        return self.scheduler.now()

    def thermostat(self) -> _FakeThermostatView:
        return self._thermostat

    def advance_to(self, ts: datetime) -> None:
        """Advance the clock to ``ts`` (step 1 of the original loop), firing due timers."""
        if self.probe is None:
            self.scheduler.advance_to(ts)
            return
        # With a probe: the same advance, in slices of at most PROBE_SLICE, probe after each.
        while self.scheduler.now() + self.PROBE_SLICE < ts:
            self.scheduler.advance_to(self.scheduler.now() + self.PROBE_SLICE)
            self.probe.step("tick")
        self.scheduler.advance_to(ts)
        self.probe.step("tick")

    def feed(self, event: dict) -> None:
        """Dispatch one event to its production entry point (step 2), then settle (step 3)."""
        etype = event.get("type", "")
        _dispatch_event(
            event,
            etype,
            self.engine,
            self.fake_hass,
            self.scheduler,
            self._tracker,
            self.climate_entity,
            self.config,
            self.coordinator,
        )

        # 3. Settle (Issue #476 - supersedes the #474 trailing-only fix, which
        # only caught the LAST event). A coordinator-mode dispatch
        # (fake_hass.states.async_set(...) in _dispatch_event) enqueues the
        # real listener (e.g. _async_door_window_changed) as a task via
        # async_create_task - it does NOT run synchronously. Without settling
        # here, that task sits queued until the NEXT event's own
        # advance_to(event_dt) call, which only drains it as an incidental
        # side effect of firing whatever UNRELATED heap entry happens to be
        # due first (found via a real scenario: a periodic 5-min thermal-
        # sampler tick happened to be due mid-way to the next event, so the
        # queued door/window listener ran at ITS fire time, not the actual
        # dispatch time - sensor_opened was logged ~5 minutes late). Worse,
        # if that listener itself schedules a NEW async_call_later() heap
        # entry (e.g. the debounce timer), that entry is pushed AFTER
        # advance_to()'s own heap-loop already exited, so it silently sits
        # unfired for yet another event before it can fire.
        # advance_to(scheduler.now()) re-enters the same heap-fire ->
        # drain-tasks -> recheck-heap loop advance_to() already implements,
        # settling arbitrarily deep chains to a fixed point at the ACTUAL
        # dispatch time, before any more wall-clock time is allowed to pass.
        self.scheduler.advance_to(self.scheduler.now())

        if self.probe is not None:
            if etype == "simulate_restart" and self.coordinator is not None:
                self.probe.notify_restart(self.scheduler.now())
            self.probe.step(f"event[{self._event_index}]:{etype}")
        self._event_index += 1

    def finish(self) -> ProductionRunResult:
        """Capture the final snapshot (inside the patched clock), close, and return the result."""
        if self._result is not None:
            return self._result
        # Issue #895: must stay inside scheduler.installed() - engine._get_indoor_temp_f()
        # now evaluates _in_sleep_window(dt_util.now(), ...) for the sleep-sensor swap, so
        # snapshotting outside the patched clock context hits an unconfigured dt_util.now()
        # (a bare MagicMock) instead of the scenario's virtual clock. This also makes the
        # snapshot semantically correct: "final state" should reflect the scenario's own
        # simulated end time, not real wall-clock time.
        try:
            engine_state = snapshot_engine_state(self.engine)
        finally:
            self.close()
        result = ProductionRunResult(
            event_log=list(self.event_log),
            action_log=list(self.fake_hass.action_log),
            engine_state=engine_state,
            callback_errors=list(self.scheduler.callback_errors),
        )
        if self.probe is not None:
            result.promise_report = self.probe.report()
        self._result = result
        return result

    def close(self) -> None:
        """Leave the patched clock/timer context (idempotent)."""
        if not self._closed:
            self._closed = True
            self._stack.close()

    def __enter__(self) -> FakeHassSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_production_scenario(
    scenario: dict,
    *,
    use_coordinator: bool = False,
    role: str = "production",
    dry_run: bool = False,
    probe: Any = None,
) -> ProductionRunResult:
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
        role: Issue #611 (Block 5, subtask O) - forwarded to
            ``build_headless_engine(role=...)``. Only meaningful with
            ``use_coordinator=False``; ``build_headless_coordinator()`` always
            builds its engine as the production role (a coordinator-driven
            shadow engine is Phase 4/Q's scope, not this offline harness's).
        dry_run: Issue #611 - forwarded to ``build_headless_engine(dry_run=...)``.
            Same coordinator-mode restriction as ``role`` above.
        probe: Optional ``PromiseProbe`` (Issue #1128 I2). Default None is byte-identical
            to the pre-seam behavior.

    Returns:
        ProductionRunResult with event_log, action_log, engine_state, callback_errors.
    """
    session = FakeHassSession(scenario, use_coordinator=use_coordinator, role=role, dry_run=dry_run, probe=probe)
    try:
        return drive_scenario(session, scenario)
    finally:
        session.close()


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
            if event.get("seed_fresh"):
                # Issue #623: models the exact race that caused the transient-open
                # regression — a sensor's raw HA state already reads "open" (as
                # apply_classification()'s _any_monitored_sensor_open() would see
                # it), but the debounce-timer registration that
                # _async_door_window_changed() would normally have performed
                # hasn't run yet (in production: an unrelated coordinator refresh
                # cycle won the race against the event-loop scheduling that
                # listener). Silent set_simple() (no listener dispatch, so
                # _door_open_timers stays empty — same as the real race) but
                # stamped with a fresh last_changed so _sensor_debounce_pending()
                # must still recognize it as pending via the timestamp check.
                fake_hass.states.set_simple(entity_id, "on", {}, last_changed=scheduler.now())
            elif event.get("seed"):
                # Issue #523: models a window that was ALREADY open before HA
                # restarted — no fresh on/off transition ever occurs (HA's
                # _async_door_window_changed listener only fires on a state
                # CHANGE event, not on the value already in place when it
                # subscribes), so the debounce-driven pause path never engages.
                # Only _do_startup_coalesce()'s own raw _is_sensor_open() read
                # sees this sensor open. Uses the silent states.set() (no
                # listener dispatch) instead of async_set(). last_changed stays
                # None ("not tracked") — this sensor is genuinely long-settled,
                # unlike the seed_fresh variant above (Issue #623).
                fake_hass.states.set_simple(entity_id, "on", {})
            else:
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

    elif etype == "sensor_unavailable" and coordinator is not None:
        # Issue #1181: real state-change dispatch through the coordinator's actual
        # _async_door_window_changed() listener, like sensor_open / sensor_close -- so a window sensor that
        # goes dark mid-pause exercises the real "dark sensor holds a pause it caused" path. It still leaves
        # the entity "unavailable" with old_state populated, which is what Issue #645's scenario needs for the
        # SUBSEQUENT plain sensor_open (a group/helper sensor re-registering "on" after an HA restart).
        # Coordinator-only (no engine-only equivalent).
        entity_id = event.get("entity", "binary_sensor.window")
        fake_hass.states.async_set(entity_id, "unavailable", {})

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
                except TypeError, ValueError:
                    _cls_indoor_f = None
        # Issue #474: when a real coordinator is present, also set its own
        # _current_classification — _async_thermostat_changed's override-detection
        # branches read self._current_classification (coordinator's own copy, not
        # engine._current_classification) to know what mode CA expects.
        if coordinator is not None:
            coordinator._current_classification = classification
            # Issue #602: mirrors the real production hook (coordinator.py's regular
            # classification cycle) that ensures today's DailyRecord exists — without
            # this, self._today_record stays None for the whole scenario (it's
            # otherwise only created by the once-daily _async_send_briefing() trigger,
            # which this harness never fires unless a scenario explicitly seeds
            # weather), silently blocking setpoint-only override detection.
            coordinator._ensure_today_record(classification)
        run_coro(engine.apply_classification(classification, indoor_temp=_cls_indoor_f))
        # Issue #786: TOU scheduler — mirrors real production's per-cycle sequence
        # (coordinator.py's _async_update_data_impl(): apply_classification() then
        # _resolve_tou_schedule_state() then _apply_tou_schedule()). Purely additive: every
        # existing scenario has no "schedules" key in its config, and
        # _resolve_tou_schedule_state() no-ops immediately when config.get("schedules") is
        # empty — so this changes nothing for any scenario that doesn't opt in.
        if coordinator is not None:
            coordinator._resolve_tou_schedule_state()
            run_coro(coordinator._apply_tou_schedule())

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
        _feed_outdoor_temp(engine, coordinator, outdoor_temp)
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
            _feed_outdoor_temp(engine, coordinator, float(outdoor_f))
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

    elif etype == "startup_coalesce":
        # Issue #523: direct call to the real coordinator's _do_startup_coalesce()
        # entry point — the same legitimate pattern as reconcile_fan_on_startup
        # above, not a coordinator-dispatch approximation. In production this
        # method runs inside _async_update_data_impl(), gated on
        # (_startup_timer_fired and _startup_coalesce_active and
        # _current_classification) once the 300s post-restart timer fires and a
        # subsequent refresh cycle runs. Driving that full periodic-refresh/
        # weather-retry machinery through the fake scheduler just to reach this
        # one method is unnecessary indirection — this calls it directly so a
        # scenario can assert its real, current behavior (sensor pause vs.
        # nat-vent vs. classification) against real sensor/temp/classification
        # state, exactly as it runs in production once reached.
        if coordinator is not None:
            run_coro(coordinator._do_startup_coalesce())

    elif etype == "fan_backstop_tick":
        # Issue #627: direct call to the real coordinator's periodic backstop_30min
        # untracked-fan reconcile — the same legitimate "real entry point, externally-
        # supplied trigger" pattern as reconcile_fan_on_startup/startup_coalesce above,
        # not a mirror of the decision logic. Before Issue #627 there was no dispatchable
        # event for this call site at all — it runs inline inside
        # _async_update_data_impl(), and driving that whole method through the fake
        # scheduler just to reach it is unnecessary indirection. This calls the same two
        # real methods coordinator.py itself calls, in the same order:
        # _compute_fan_status() (real ground-truth derivation) to get _is_untracked, then
        # _should_run_untracked_fan_backstop() (the exact gate Issue #627 fixed) to decide
        # whether to fire reconcile_fan_on_startup(trigger="backstop_30min") — the same
        # method reconcile_fan_on_startup calls with the same trigger label production
        # uses for this call site.
        if coordinator is not None:
            _is_untracked_tick = coordinator._compute_fan_status() == "running (untracked)"
            if coordinator._should_run_untracked_fan_backstop(_is_untracked_tick):
                _cs_tick = fake_hass.states.get(climate_entity)
                _tick_fan_mode = str(_cs_tick.attributes.get("fan_mode", "")) if _cs_tick else ""
                _tick_hvac_action = str(_cs_tick.attributes.get("hvac_action", "")).lower() if _cs_tick else ""
                if _tick_hvac_action not in ("heating", "cooling"):
                    run_coro(
                        coordinator.automation_engine.reconcile_fan_on_startup(
                            indoor=coordinator._get_indoor_temp(),
                            outdoor=coordinator._last_outdoor_temp,
                            thermostat_fan_running=coordinator._derive_thermostat_fan_running_for_reconcile(
                                fan_mode_attr=_tick_fan_mode,
                                hvac_action_attr=_tick_hvac_action,
                            ),
                            any_sensor_open=coordinator._any_sensor_open(),
                            trigger="backstop_30min",
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
        new_temperature = event.get("temperature")  # optional (Issue #483)
        new_hvac_action = event.get("hvac_action")  # optional (Issue #790)
        new_fan_mode_attr = event.get("thermostat_fan_mode")  # optional (Issue #1103)
        if coordinator is not None:
            _inject_thermostat_mode(
                fake_hass,
                climate_entity,
                new_hvac_mode,
                dispatch=True,
                temperature=new_temperature,
                hvac_action=new_hvac_action,
                fan_mode_attr=new_fan_mode_attr,
            )
        else:
            _inject_thermostat_mode(
                fake_hass,
                climate_entity,
                new_hvac_mode,
                temperature=new_temperature,
                hvac_action=new_hvac_action,
                fan_mode_attr=new_fan_mode_attr,
            )

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
        run_coro(engine.nat_vent_temperature_check(indoor_temp, outdoor=engine._last_outdoor_temp))

    elif etype == "external_fan_state_change":
        # Issue #482: models a genuinely external actor changing the configured WHF
        # fan_entity's HA state directly (e.g. someone flips a physical switch, or
        # calls the fan.turn_on/off service from the HA UI outside of CA). Unlike
        # CA's OWN commands (which route through automation.py's
        # _call_fan_service_with_context via services.async_call and so carry a
        # real Context — see _handle_temp_update-adjacent CA-driven paths above),
        # this dispatches states.async_set() DIRECTLY with no context, exactly
        # modeling "no CA attribution available" for a real external change. Only
        # meaningful with use_coordinator=True — reaches the real
        # _async_fan_entity_changed listener the same way sensor_open/thermostat_
        # state_changed do.
        entity_id = event.get("entity", config.get("fan_entity", "fan.whf"))
        new_state = event.get("state", "off")
        if coordinator is not None:
            fake_hass.states.async_set(entity_id, new_state, {})
        # No engine-only equivalent — command-only/bare-engine mode has no
        # _async_fan_entity_changed listener to dispatch to at all.

    elif etype == "external_entity_state_change":
        # Issue #677: generalizes external_fan_state_change above to set an arbitrary
        # entity's state AND attributes — needed to model the QuietCool RF remote's
        # event.* entity (fan_remote_entity) re-announcing a still-valid timer_* token
        # after a restart, which coordinator._read_live_remote_timer_provenance() reads
        # live from hass.states.get() during startup coalescing. Unlike
        # external_fan_state_change (hardcoded empty attributes dict, sufficient for a
        # plain on/off fan entity), this entity's decoded command lives in
        # attributes["event_type"] and its state field IS the press timestamp (see
        # docs/fan-remote-spec.md § Firmware Event Contract) — both need to be settable.
        # Only meaningful with use_coordinator=True — same rationale as
        # external_fan_state_change.
        entity_id = event.get("entity")
        new_state = event.get("state", "unknown")
        attributes = event.get("attributes", {})
        if entity_id and coordinator is not None:
            fake_hass.states.async_set(entity_id, new_state, attributes)

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

    elif etype == "simulate_restart" and coordinator is not None:
        # Issue #627: models an HA restart mid-scenario using the REAL production
        # continuity boundary — engine.get_serializable_state() (the real save-path
        # method) followed by engine.restore_state() (the real load-path method,
        # including its Issue #263/#327 clean-slate wipe of _fan_override_active/
        # _fan_remote_timer_hours while preserving _pre_fan_hvac_mode/_fan_active) — not
        # a hand-rolled mirror of what restart does. Also resets
        # coordinator._startup_coalesce_active to True, its real __init__ default,
        # since a fresh coordinator/process is exactly what a restart produces. This is
        # the only way to reach the exact post-restart state (Issue #627's premature
        # backstop_30min misfire) without spinning up a second full coordinator.
        engine.restore_state(engine.get_serializable_state())
        coordinator._startup_coalesce_active = True
        # Issue #645: restore_state() intentionally does NOT reset _paused_by_door/
        # _pre_pause_mode/_paused_with_hvac_already_off/_paused_entity/_paused_since or
        # _natural_vent_active — per its own docstring, it relies on AutomationEngine.
        # __init__ having already set them to their clean defaults, since in real
        # production a restart constructs a brand-new engine instance. This harness reuses
        # the SAME live engine object across simulate_restart (there is no second
        # construction to rely on), so those fields would otherwise silently leak across
        # the simulated restart — masking exactly the kind of post-restart bug
        # (Issue #645) this event type exists to reproduce. Match __init__'s literal
        # defaults here so simulate_restart is faithful to the real clean-slate guarantee.
        engine._paused_by_door = False
        engine._pre_pause_mode = None
        engine._paused_with_hvac_already_off = False
        engine._paused_entity = None
        engine._paused_since = None
        engine._natural_vent_active = False

    elif etype == "automation_toggle":
        # Issue #1128 I3 (additive): the occupant flips the "automation" switch.  Coordinator mode
        # goes through the real ``set_automation_enabled()`` (which also sets ``engine.dry_run``,
        # exactly what the switch entity calls); engine-only mode sets the same ``dry_run`` the
        # observe-only gate in ``_call_service_with_context`` reads.  ``enabled`` defaults to False.
        enabled = bool(event.get("enabled", False))
        if coordinator is not None:
            coordinator.set_automation_enabled(enabled)
        else:
            engine.dry_run = not enabled

    elif etype == "indoor_unreadable":
        # Issue #1128 I3 (additive): the indoor temperature source drops out (``unreadable`` true,
        # the default) or comes back (``unreadable`` false, optionally with ``indoor_f``).  An outage
        # clears ``current_temperature`` on the climate entity -- the very attribute the engine
        # resolves indoor from -- so every engine read returns "no value", as in production.
        existing = fake_hass.states.get(climate_entity)
        attrs = dict(existing.attributes) if existing is not None else {}
        state_str = existing.state if existing is not None else "off"
        if bool(event.get("unreadable", True)):
            attrs["current_temperature"] = None
        elif event.get("indoor_f") is not None:
            attrs["current_temperature"] = float(event["indoor_f"])
        else:
            return  # recovery without a value: the next temp_update restores it
        if coordinator is not None:
            fake_hass.states.async_set(climate_entity, state_str, attrs)
        else:
            fake_hass.states.set(climate_entity, FakeState(state=state_str, attributes=attrs))

    # All other unknown types are silently ignored (mirrors simulate.py's final `return None`)


def _feed_outdoor_temp(engine: Any, coordinator: Any | None, outdoor_f: float) -> None:
    """Deliver an outdoor reading the way production does.

    Issue #1193 (C7, test-infrastructure change) — semantic justification: the
    previous semantics (engine.update_outdoor_temp() only) were WRONG in coordinator
    mode because they modelled a house where the engine knows the outdoor temperature
    but the coordinator's _last_outdoor_temp is still None. Production never produces
    that state: the outdoor sensor / weather listener reaches
    coordinator._apply_outdoor_temp(), which sets coordinator._last_outdoor_temp AND
    calls engine.update_outdoor_temp() from the same reading. With the split state, the
    coordinator's indoor-change checks (_async_run_temp_reactive_checks ->
    nat_vent_temperature_check(indoor, outdoor=None)) cleared an armed OUTDOOR_RISE
    nat-vent exit that production fires, and fan_backstop_tick read a stale
    coordinator outdoor after economizer_check / reconcile_fan_on_startup. Could this
    let a real failure pass silently? No: the engine still receives the identical
    reading (via the same call production makes). The coordinator now sees it too,
    which also runs production's outdoor windows gate (_apply_outdoor_windows_gate may
    clear windows_recommended on the shared classification) and updates its data
    attribute, exactly as the real listener does. Engine-only scenarios are unchanged.
    """
    if coordinator is not None:
        coordinator._apply_outdoor_temp(outdoor_f, record_history=False)
    else:
        engine.update_outdoor_temp(outdoor_f)


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
        _feed_outdoor_temp(engine, coordinator, float(outdoor_f))

    # Issue #1193 (same class as _feed_outdoor_temp): in production the coordinator
    # owns the predicted-indoor curve (_last_predicted_indoor, rebuilt each cycle) and
    # its indoor-change listener passes it to comfort_family_temperature_check(). The
    # old harness gave the event's curve only to the engine, so in coordinator mode the
    # listener ran with an empty curve, a state production never has once a curve
    # exists. Give the coordinator the same curve the engine gets.
    if coordinator is not None and event.get("predicted_indoor"):
        coordinator._last_predicted_indoor = event["predicted_indoor"]

    if indoor_f is not None:
        _inject_indoor_temp(fake_hass, climate_entity, float(indoor_f), dispatch=coordinator is not None)

    new_indoor = float(indoor_f) if indoor_f is not None else None
    if coordinator is None and new_indoor is not None and new_indoor != old_indoor:
        if engine._natural_vent_active:
            run_coro(engine.nat_vent_temperature_check(new_indoor, outdoor=engine._last_outdoor_temp))
        if engine._fan_active or engine._natural_vent_active:
            run_coro(
                engine.fan_thermostat_check(
                    indoor=engine._get_indoor_temp_f(),
                    outdoor=engine._last_outdoor_temp,
                    trigger="tick",
                )
            )
        # Issue #858: third sibling call, same "direct, unconditional call guarded
        # by the same flags production reads" fidelity as the two above —
        # coordinator._async_thermostat_changed's real dispatch to
        # comfort_family_temperature_check() on every current_temperature change.
        run_coro(engine.comfort_family_temperature_check(new_indoor, predicted_indoor=event.get("predicted_indoor")))

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
