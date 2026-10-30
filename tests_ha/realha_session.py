"""RealHASession - the real-Home-Assistant adapter of the Session seam (Step 2 / Issue #1127, Increment 6).

One scenario, one world: a real Home Assistant core, a real Climate Advisor config entry and coordinator, the
dev thermostat sim in its ``scripted`` profile (indoor temperature comes from the scenario; ``hvac_action`` comes
from the sim's own compressor model), a frozen clock the adapter turns forward timer by timer, and a recorder
on the service-call bus.  It returns the SAME ``RunResult`` the FakeHass adapter returns, so the unchanged
outcomes layer (``tools/sim_harness/outcomes.py``) judges both worlds with identical rules.

This module imports no Home Assistant stub (``ha_stubs``) and installs none.

Protocol shape
--------------
Real Home Assistant is asynchronous, so the three Session calls that touch the world (``feed``, ``advance_to``,
``finish``) are coroutines here; ``now()`` and ``thermostat()`` stay synchronous.  ``make_session(...,
engine="realha")`` returns an unstarted session; ``async_drive_scenario`` starts it, runs the shared loop
(advance to each event's time, then feed it) and finishes.  Nothing here uses ``run_coro``: every dispatch is
awaited inside the HA loop (design R11).

Dispatch modes (design 8.2) - how many events used each is reported in ``RealHASession.dispatch_modes``:

* ``state-write``      the adapter writes a state or entity value; real listeners react
* ``service-occupant`` a real service call carrying a marked occupant Context
* ``time-travel``      the adapter only moves the clock; the production schedule fires
* ``real-lifecycle``   a real unload + setup (``simulate_restart``) or the real API view (``cancel_override``)
* ``direct-callback``  the same production method FakeHass calls, awaited inside the HA loop (declared fallback)
* ``no-op``            event types the FakeHass adapter also ignores (``grace_start`` ...)

Deliberate environment choices (reported, never hidden):

* ``periodic_refresh=False`` (default): after every set-up the coordinator's 30-minute refresh timer is
  cancelled, exactly as the FakeHass stub coordinator never schedules one.  Scenarios hand-feed the day
  classification; a real periodic cycle would re-derive it from synthetic weather and replace the scenario's
  premise.  ``coordinator_refresh`` events still run a real refresh.
* No weather entity exists (as in FakeHass), so Climate Advisor finds no forecast and never classifies the day
  on its own; scenarios hand-feed the classification.
* The sim thermostat reports tenths of a degree (HA's default is whole degrees in F; FakeHass passes every reading
  through unrounded).
* Outdoor temperature reaches Climate Advisor through a real entity read by its real listener
  (``outdoor_temp_source: input_number``, which unlike ``sensor`` is not stale-checked).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from tests_ha.house import CLIMATE_ENTITY, House, WholeHouseFan, build_house
from tests_ha.house.builder import OUTDOOR_F_SENSOR
from tools.sim_harness.scenario_model import (
    ProductionRunResult,
    build_classification_from_event,
    merged_scenario_config,
    parse_event_time,
    scenario_start_time,
    snapshot_engine_state,
    sorted_events,
)

__all__ = ["RealHASession", "async_drive_scenario"]

_SCENARIO_LOG = logging.getLogger("custom_components.climate_advisor.scenario")
_MONOTONIC_RESOLUTION = 1e-9
MIN_STEP = timedelta(seconds=1)
MAX_STEP = timedelta(seconds=30)  # the sim thermostat's own tick: the physics never sees a bigger jump
_SIM_MODES = {"off", "heat", "cool"}
# Behavioral settings the shared house seed carries but a FakeHass scenario config never had.  Left in, they would
# change what "no value configured" means (e.g. the seed's sleep_cool=78 replaces production's default 72), so the
# adapter removes them unless the scenario sets them (design: the scenario's config is the whole premise).
_SEED_ONLY_KEYS = (
    "sleep_heat",
    "sleep_cool",
    "max_setback_depth_f",
    "adaptive_preheat_enabled",
    "adaptive_setback_enabled",
    "weather_bias_enabled",
    "min_preheat_minutes",
    "max_preheat_minutes",
    "default_preheat_minutes",
    "preheat_safety_margin",
    "welcome_home_debounce",
)
_NOOP_EVENTS = frozenset({"fan_cycle_on", "fan_cycle_off", "grace_start", "grace_end"})

# event type -> dispatch mode, for the event types whose mode does not depend on the event's content
_MODE_BY_TYPE = {
    "sensor_open": "state-write",
    "sensor_close": "state-write",
    "sensor_unavailable": "state-write",
    "external_fan_state_change": "state-write",
    "external_entity_state_change": "state-write",
    "thermostat_state_changed": "service-occupant",
    "simulate_restart": "real-lifecycle",
    "cancel_override": "real-lifecycle",
    "coordinator_refresh": "real-lifecycle",
    "startup_coalesce": "direct-callback",
    "fan_backstop_tick": "direct-callback",
    "reconcile_fan_on_startup": "direct-callback",
    "economizer_check": "direct-callback",
    "nat_vent_temperature_check": "direct-callback",
    "activate_fan_min_runtime": "direct-callback",
    "classification": "direct-callback",
    "pre_cool": "direct-callback",
    "occupancy_away": "direct-callback",
    "occupancy_home": "direct-callback",
    "occupancy_vacation": "direct-callback",
    "occupancy_change": "direct-callback",
    "occupancy_change_with_override": "direct-callback",
}


class _ErrorCapture(logging.Handler):
    """Collects exceptions Climate Advisor logged (the real-HA analog of FakeScheduler.callback_errors)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.errors: list[tuple[datetime, BaseException]] = []

    def emit(self, record: logging.LogRecord) -> None:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            self.errors.append((dt_util.utcnow(), exc))


def _first_value(events: list[dict], key: str, default: float | None) -> float | None:
    for e in events:
        if e.get(key) is not None:
            return float(e[key])
    return default


class RealHASession:
    """The real-HA adapter of the Session protocol.  Build with ``make_session(scenario, engine="realha", ...)``."""

    def __init__(
        self,
        scenario: dict,
        *,
        hass: HomeAssistant,
        freezer: Any,
        config_dir: Path,
        probe: Any = None,
        periodic_refresh: bool = False,
        classification_mode: str = "hand",
        use_coordinator: bool | None = None,  # accepted for make_session parity; real HA always has a coordinator
    ) -> None:
        if probe is not None:
            raise NotImplementedError("the promise probe has no real-HA source adapter yet")
        if classification_mode != "hand":
            raise NotImplementedError("classification_mode='derive' is not implemented (design 8.2 option)")
        self.scenario = scenario
        self.hass = hass
        self.freezer = freezer
        self.config_dir = config_dir
        self.periodic_refresh = periodic_refresh
        self.config: dict[str, Any] = merged_scenario_config(scenario)
        self.scenario_climate_entity: str = self.config.get("climate_entity", "climate.test_thermostat")
        self.dispatch_modes: Counter[str] = Counter()
        self.notes: list[str] = []
        self.house: House | None = None
        self.event_log: list[tuple[str, dict, datetime | None]] = []
        self._errors = _ErrorCapture()
        self._patch: Any = None
        self._event_index = 0
        self._result: ProductionRunResult | None = None
        self._closed = False
        self._events = sorted_events(scenario)
        self.leaked_timers: list[str] = []

    # ------------------------------------------------------------------ build
    @property
    def coordinator(self) -> Any:
        assert self.house is not None
        return self.house.coordinator

    @property
    def engine(self) -> Any:
        return self.coordinator.automation_engine

    def _contact_ids(self) -> list[str]:
        ids = [e for e in self.config.get("door_window_sensors", []) if isinstance(e, str)]
        for ev in self._events:
            if ev.get("type") in ("sensor_open", "sensor_close", "sensor_unavailable"):
                eid = ev.get("entity", "binary_sensor.window")
                if eid not in ids:
                    ids.append(eid)
        return ids

    async def start(self) -> None:
        """Build the house for this scenario (setup, optional settle) and install the event tee."""
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        scenario, cfg = self.scenario, self.config
        first = (
            parse_event_time(self._events[0]["time"], datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC))
            if self._events
            else None
        )
        start = scenario_start_time(self._events)
        # engine-only scenarios have no start-up window in FakeHass; coordinator scenarios keep it unless they opt out
        self._want_window = bool(scenario.get("use_coordinator", False)) and not scenario.get(
            "skip_startup_coalesce", False
        )
        settle = timedelta(0)
        if first is None:
            self.notes.append("scenario has no events")
        contacts = self._contact_ids()

        # --- Climate Advisor's view of the house: scenario config over harness defaults, entity ids remapped
        ca_config = {k: v for k, v in cfg.items() if k not in ("climate_entity",)}
        for key in _SEED_ONLY_KEYS:
            ca_config.setdefault(key, ...)  # a FakeHass scenario never had these; production's own default applies
        if not cfg.get("fan_entity"):
            ca_config[
                "fan_entity"
            ] = ...  # the flow never stores a fan entity it was not given (builder default is null)
        ca_config.update(
            {
                "door_window_sensors": contacts,
                "outdoor_temp_source": "input_number",  # a sensor source is stale-checked; scenarios do not re-report
                "outdoor_temp_entity": "sensor.house_outdoor_f",
            }
        )

        # --- the scenario's own devices keep their entity ids, so the event stream needs no translation
        init_states = {c: "off" for c in contacts}

        # the capture must see set-up events too, so the tee is a class-level patch for the session's lifetime
        original = ClimateAdvisorCoordinator._emit_event
        session = self

        def _emit_and_capture(coordinator_self: Any, event_type: str, data: dict) -> Any:
            result = original(coordinator_self, event_type, data)
            session.event_log.append((event_type, data, dt_util.utcnow()))
            return result

        self._patch = patch.object(ClimateAdvisorCoordinator, "_emit_event", _emit_and_capture)
        self._patch.start()
        logging.getLogger("custom_components.climate_advisor").addHandler(self._errors)

        # A FakeHass scenario that never reports a temperature leaves the reading None; do not invent one
        indoor0 = _first_value(self._events, "indoor_f", None)
        outdoor0 = _first_value(self._events, "outdoor_f", None)
        self._no_indoor = indoor0 is None and outdoor0 is None  # the sim needs a number once an outdoor reading exists
        if indoor0 is None and not self._no_indoor:
            indoor0 = 72.0
            self.notes.append(
                "scenario reports outdoor but never indoor temperature; the sim thermostat starts at 72 F"
            )
        self.house = await build_house(
            self.hass,
            self.freezer,
            self.config_dir,
            units="us",
            profile="scripted",
            fan=None,
            weather=False,
            contacts=[],
            startup="cold",
            start=start - settle,
            initial_indoor_f=indoor0 if indoor0 is not None else 70.0,
            outdoor_f=outdoor0,
            ca_config=ca_config,
            extra_states=init_states,
            initial_hvac_mode=cfg.get("initial_thermostat_mode", "off"),
            before_ca_setup=self._add_devices,
        )
        self._quiesce()
        self._apply_injected_inputs()
        if settle:
            await self._advance(settle)
        if not self._want_window:
            # what FakeHass does for skip_startup_coalesce: the window closes by flag, not by running a coalesce
            self.coordinator._startup_coalesce_active = False

    async def _add_devices(self, house: House) -> None:
        """Called by build_house between the sim and Climate Advisor set-up: the scenario's fan and notify service."""
        sim = self.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]
        # HA's climate entity reports whole degrees in F by default (72.5 -> 72), which several goldens' half-degree
        # readings do not survive.  Scenarios assume a thermostat that reports tenths (like the FakeHass one).
        sim._attr_precision = 0.1
        if self._no_indoor:
            sim._current_temp = None  # no reading at all, as the FakeHass thermostat has before its first temp_update
            sim.async_write_ha_state()
        notify = self.config.get("notify_service", "notify.test")
        if isinstance(notify, str) and "." in notify and notify != "notify.notify":
            domain, name = notify.split(".", 1)
            async_mock_service(self.hass, domain, name)
        fan_entity = self.config.get("fan_entity")
        if isinstance(fan_entity, str) and fan_entity:
            house.fan = WholeHouseFan(
                self.hass,
                command_entity=fan_entity,
                state_entity=self.config.get("fan_state_entity"),
                feedback=bool(self.config.get("fan_state_feedback", True)),
            )

    def _quiesce(self) -> None:
        """Cancel the coordinator's periodic refresh timer (see module docstring)."""
        if self.periodic_refresh:
            return
        coordinator = self.coordinator
        coordinator._unschedule_refresh()
        coordinator._schedule_refresh = lambda: None  # production reads update_interval as a timedelta; keep it set

    def _apply_injected_inputs(self) -> None:
        """Optional ODE inputs the scenario supplies (set on the live engine, as FakeHass does)."""
        if isinstance(self.scenario.get("thermal_model"), dict):
            self.engine._thermal_model = self.scenario["thermal_model"]
        if isinstance(self.scenario.get("hourly_forecast"), list):
            self.engine._hourly_forecast_temps = self.scenario["hourly_forecast"]

    # ------------------------------------------------------------- Session API
    def now(self) -> datetime:
        return dt_util.utcnow()

    def thermostat(self) -> Any:
        assert self.house is not None
        return self.house.thermostat

    def _next_due(self) -> float | None:
        """Seconds until the next live loop timer is due (negative/zero when one is already due)."""
        loop = self.hass.loop
        base = loop.time() + _MONOTONIC_RESOLUTION
        best: float | None = None
        for handle in get_scheduled_timer_handles(loop):
            if not isinstance(handle, asyncio.TimerHandle) or handle.cancelled():
                continue
            due = handle.when() - base
            if best is None or due < best:
                best = due
        return best

    async def _advance(self, span: timedelta) -> None:
        """Turn the clock forward by ``span``, stopping at each timer that comes due (1 s floor, 30 s cap)."""
        assert self.house is not None
        remaining = span
        while remaining > timedelta(0):
            step = min(MAX_STEP, remaining)
            due = self._next_due()
            if due is not None:
                step = min(step, max(timedelta(seconds=max(due, 0.0)), MIN_STEP))
            self.freezer.tick(step)
            async_fire_time_changed(self.hass)
            await self._drain()
            remaining -= step

    async def _drain(self) -> None:
        """Let everything runnable finish, without ever blocking on the frozen clock.

        ``hass.async_block_till_done`` waits for tracked tasks, including one that is merely sleeping; on a frozen
        clock that never ends.  So: yield until HA has no tracked task, and if one is left (a sleeper), move the
        clock a second so it can wake, up to 30 s of simulated time per drain.
        """
        hass = self.hass
        for _ in range(30):
            for _ in range(50):
                await asyncio.sleep(0)
                if not hass._tasks:
                    break
            if not hass._tasks:
                break
            self.freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
        for _ in range(5):
            await asyncio.sleep(0)

    async def advance_to(self, ts: datetime) -> None:
        now = dt_util.utcnow()
        if ts > now:
            await self._advance(ts - now)
        # FakeScheduler.advance_to(ts) fires every timer due AT ts before the event at ts is fed; real HA's
        # time-change timers sit up to 0.5 s past the whole second, so fire those now, before the event.
        await self._settle()

    async def _settle(self) -> None:
        """Fire anything already due at 'now' (zero-width), repeatedly, until quiet - FakeHass's advance_to(now)."""
        assert self.house is not None
        for _ in range(10):
            due = self._next_due()
            if due is None or due > 0.5:
                break
            async_fire_time_changed(self.hass)
            await self._drain()
        await self._drain()

    async def feed(self, event: dict) -> None:
        etype = event.get("type", "")
        _SCENARIO_LOG.info(
            "EVENT[%d] %s @ %s %s",
            self._event_index,
            etype,
            dt_util.utcnow().isoformat(),
            {k: v for k, v in event.items() if k not in ("note", "time", "type")},
        )
        mode = await self._dispatch(event, etype)
        self.dispatch_modes[mode] += 1
        await self._settle()
        self._event_index += 1

    def _set_indoor(self, temp_f: float) -> None:
        """The scripted sim thermostat reports a new indoor reading (its first one when it had none)."""
        assert self.house is not None
        sim = self.hass.data["ca_dev_thermostat_sim"][self.house.sim_entry.entry_id]["thermostat_entity"]
        if sim._current_temp is None:
            sim._current_temp = float(temp_f)  # the sim's own log line formats the previous reading
        self.house.set_indoor(temp_f)

    def _report_outdoor(self, temp_f: float) -> None:
        """The outdoor sensor reports a reading.  ``force_update`` makes an unchanged reading a state change
        too, so Climate Advisor's listener sees the first reading even when it equals the sensor's initial value."""
        assert self.house is not None
        self.house.set_outdoor(temp_f)
        self.hass.states.async_set(OUTDOOR_F_SENSOR, str(float(temp_f)), force_update=True)

    # --------------------------------------------------------------- dispatch
    async def _dispatch(self, event: dict, etype: str) -> str:  # noqa: PLR0912, PLR0915
        assert self.house is not None
        house, hass, cfg = self.house, self.hass, self.config
        engine = self.engine
        coordinator = self.coordinator

        if etype == "temp_update":
            outdoor_f, indoor_f = event.get("outdoor_f"), event.get("indoor_f")
            # FakeHass applies both readings, then evaluates once.  Real listeners see them one at a time, so
            # whichever is written first is evaluated against the other's OLD value (a real property of two
            # separate sensors).  Outdoor first: swapping to indoor-first fixes issue-392 and breaks
            # bedtime_natvent_continuation and 2026-03-28-overnight (see the ledger entry for issue-392).
            if outdoor_f is not None:
                self._report_outdoor(float(outdoor_f))
            if indoor_f is not None:
                self._set_indoor(float(indoor_f))
            await self._drain()
            # the periodic cycle's nat-vent re-evaluation: a real production trigger the scenario models per tick
            await engine.check_natural_vent_conditions()
            predicted = event.get("predicted_indoor")
            if predicted:
                # FakeHass hands the scenario's predicted curve to these two calls; the listener has no such curve
                new_indoor = float(indoor_f) if indoor_f is not None else None
                if new_indoor is not None:
                    await engine.comfort_family_temperature_check(new_indoor, predicted_indoor=predicted)
                if getattr(engine, "_current_classification", None) is not None:
                    await engine.apply_classification(
                        engine._current_classification, predicted_indoor=predicted, indoor_temp=new_indoor
                    )
                return "direct-callback"
            return "state-write"

        if etype == "sensor_open":
            hass.states.async_set(event.get("entity", "binary_sensor.window"), "on", {})
            await self._drain()
            if event.get("seed") or event.get("seed_fresh"):
                self.notes.append(
                    "seed/seed_fresh: FakeHass sets the state silently; real HA always notifies listeners"
                )
            return "state-write"
        if etype == "sensor_close":
            hass.states.async_set(event.get("entity", "binary_sensor.window"), "off", {})
            return "state-write"
        if etype == "sensor_unavailable":
            hass.states.async_set(event.get("entity", "binary_sensor.window"), "unavailable", {})
            return "state-write"
        if etype == "external_fan_state_change":
            hass.states.async_set(event.get("entity", cfg.get("fan_entity", "fan.whf")), event.get("state", "off"), {})
            return "state-write"
        if etype == "external_entity_state_change":
            if event.get("entity"):
                hass.states.async_set(event["entity"], event.get("state", "unknown"), event.get("attributes", {}))
            return "state-write"

        if etype == "thermostat_state_changed":
            return await self._thermostat_state_changed(event)

        if etype == "classification":
            classification = build_classification_from_event(event)
            coordinator._current_classification = classification
            coordinator._ensure_today_record(classification)
            await engine.apply_classification(classification, indoor_temp=house.thermostat.indoor_f)
            coordinator._resolve_tou_schedule_state()
            await coordinator._apply_tou_schedule()
            return "direct-callback"

        if etype in (
            "occupancy_away",
            "occupancy_home",
            "occupancy_vacation",
            "occupancy_change",
            "occupancy_change_with_override",
        ):
            if etype == "occupancy_change_with_override":
                engine._manual_override_active = True
            occ = event.get("mode", "home") if "change" in etype else etype.split("_", 1)[1]
            engine.set_occupancy_mode(occ)
            handler = {"away": engine.handle_occupancy_away, "vacation": engine.handle_occupancy_vacation}.get(
                occ, engine.handle_occupancy_home
            )
            await handler()
            return "direct-callback"

        if etype in ("bedtime", "wakeup"):
            return await self._scheduled_event(event, etype)
        if etype == "pre_cool":
            indoor_f = event.get("indoor_f")
            if indoor_f is not None:
                self._set_indoor(float(indoor_f))
                await self._drain()
            await engine.handle_pre_cool(
                indoor_temp=float(indoor_f) if indoor_f is not None else None,
                nat_vent_just_closed=bool(event.get("nat_vent_just_closed", False)),
            )
            return "direct-callback"

        if etype == "economizer_check":
            outdoor_temp = float(event.get("outdoor_temp", event.get("outdoor_f", 70.0)))
            indoor_temp = event.get("indoor_temp", event.get("indoor_f"))
            if indoor_temp is not None:
                indoor_temp = float(indoor_temp)
                self._set_indoor(indoor_temp)
            self._report_outdoor(outdoor_temp)
            await self._drain()
            await engine.check_window_cooling_opportunity(
                outdoor_temp, indoor_temp, bool(event.get("windows_open", False)), int(event.get("hour", -1))
            )
            return "direct-callback"

        if etype == "nat_vent_temperature_check":
            indoor_temp = float(event.get("indoor_temp", event.get("indoor_f", 70.0)))
            self._set_indoor(indoor_temp)
            await self._drain()
            await engine.nat_vent_temperature_check(indoor_temp, outdoor=engine._last_outdoor_temp)
            return "direct-callback"

        if etype == "reconcile_fan_on_startup":
            if event.get("outdoor_f") is not None:
                self._report_outdoor(float(event["outdoor_f"]))
            if event.get("indoor_f") is not None:
                self._set_indoor(float(event["indoor_f"]))
            await self._drain()
            await engine.reconcile_fan_on_startup(
                indoor=engine._get_indoor_temp_f(),
                outdoor=engine._last_outdoor_temp,
                thermostat_fan_running=bool(event.get("thermostat_fan_running", False)),
                any_sensor_open=bool(event.get("any_sensor_open", False)),
            )
            return "direct-callback"

        if etype == "startup_coalesce":
            await coordinator._do_startup_coalesce()
            return "direct-callback"

        if etype == "fan_backstop_tick":
            is_untracked = coordinator._compute_fan_status() == "running (untracked)"
            if coordinator._should_run_untracked_fan_backstop(is_untracked):
                st = hass.states.get(CLIMATE_ENTITY)
                fan_mode = str(st.attributes.get("fan_mode", "")) if st else ""
                action = str(st.attributes.get("hvac_action", "")).lower() if st else ""
                if action not in ("heating", "cooling"):
                    await engine.reconcile_fan_on_startup(
                        indoor=coordinator._get_indoor_temp(),
                        outdoor=coordinator._last_outdoor_temp,
                        thermostat_fan_running=coordinator._derive_thermostat_fan_running_for_reconcile(
                            fan_mode_attr=fan_mode, hvac_action_attr=action
                        ),
                        any_sensor_open=coordinator._any_sensor_open(),
                        trigger="backstop_30min",
                    )
            return "direct-callback"

        if etype == "activate_fan_min_runtime":
            await engine.start_min_fan_runtime_cycles()
            return "direct-callback"

        if etype == "coordinator_refresh":
            await coordinator.async_refresh()
            return "real-lifecycle"

        if etype == "cancel_override":
            return await self._cancel_override()

        if etype == "simulate_restart":
            await house.restart(settle=False)
            self._quiesce()
            self._apply_injected_inputs()
            return "real-lifecycle"

        if etype in _NOOP_EVENTS:
            return "no-op"
        self.notes.append(f"unknown event type ignored: {etype}")
        return "no-op"

    async def _scheduled_event(self, event: dict, etype: str) -> str:
        """bedtime / wakeup: time-travel when the configured time is the event time, else the direct callback."""
        assert self.house is not None
        key = "sleep_time" if etype == "bedtime" else "wake_time"
        configured = self.config.get(key)
        now = dt_util.utcnow()
        if isinstance(configured, str) and configured[:5] == f"{now.hour:02d}:{now.minute:02d}":
            return "time-travel"  # the production schedule already fired as the clock reached the event time
        handler = self.engine.handle_bedtime if etype == "bedtime" else self.engine.handle_morning_wakeup
        await handler()
        return "direct-callback"

    async def _thermostat_state_changed(self, event: dict) -> str:
        """The person changes the thermostat: a real service call on the sim with a marked occupant Context."""
        assert self.house is not None
        mode = event.get("hvac_mode", "off")
        if event.get("hvac_action") is not None:
            self.notes.append("hvac_action cannot be forced on the sim thermostat (its compressor model owns it)")
        if mode in _SIM_MODES:
            await self.house.occupant_sets_thermostat(mode=mode, setpoint_f=event.get("temperature"))
        else:
            # a mode the sim does not offer (e.g. heat_cool): write the state the way a bare state change would
            self.notes.append(f"hvac_mode {mode!r} is not offered by the sim thermostat; state written directly")
            st = self.hass.states.get(CLIMATE_ENTITY)
            attrs = dict(st.attributes) if st else {}
            if event.get("temperature") is not None:
                attrs["temperature"] = event["temperature"]
            self.hass.states.async_set(CLIMATE_ENTITY, mode, attrs)
        if event.get("thermostat_fan_mode") is not None:
            await self.house.occupant_sets_fan_mode(event["thermostat_fan_mode"])
        return "service-occupant"

    async def _cancel_override(self) -> str:
        """The dashboard's Cancel Override button: the real API view, not a copy of its logic."""
        from custom_components.climate_advisor.api import ClimateAdvisorCancelOverrideView

        request = MagicMock()
        request.app = {"hass": self.hass}
        request.query = {}
        await ClimateAdvisorCancelOverrideView().post(request)
        return "real-lifecycle"

    # ----------------------------------------------------------------- finish
    def _action_log(self) -> list[dict]:
        assert self.house is not None
        out: list[dict] = []
        for call in self.house.calls:
            if call.origin == "occupant" or call.domain == "weather":
                continue
            data = dict(call.data)
            if data.get("entity_id") == CLIMATE_ENTITY:
                data["entity_id"] = self.scenario_climate_entity
            out.append(
                {
                    "domain": call.domain,
                    "service": call.service,
                    "data": data,
                    "ts": call.at,
                    "context": call.context_id,
                }
            )
        return out

    async def finish(self) -> ProductionRunResult:
        if self._result is not None:
            return self._result
        try:
            engine_state = snapshot_engine_state(self.engine)
            engine_state["_thermostat_hvac_mode"] = self.house.thermostat.mode if self.house else None
            result = ProductionRunResult(
                event_log=list(self.event_log),
                action_log=self._action_log(),
                engine_state=engine_state,
                callback_errors=list(self._errors.errors),
            )
        finally:
            await self.close()
        self._result = result
        return result

    def _collect_leaked_timers(self) -> None:
        """After the unload, record (and cancel) any timer Climate Advisor left armed.

        HA's autouse ``verify_cleanup`` would fail the test for it with no way to ledger the finding, so the
        adapter turns it into data: ``leaked_timers`` is part of the run result's verdict (assertion index -2),
        and the timer is cancelled so the harness itself stays clean.
        """
        for handle in list(get_scheduled_timer_handles(self.hass.loop)):
            if not isinstance(handle, asyncio.TimerHandle) or handle.cancelled():
                continue
            text = f"{handle!r} {handle._args!r}"
            if "climate_advisor" in text or "AutomationEngine" in text or "ClimateAdvisor" in text:
                self.leaked_timers.append(text[:300])
                handle.cancel()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logging.getLogger("custom_components.climate_advisor").removeHandler(self._errors)
        if self.house is not None:
            await self.house.close()
            self._collect_leaked_timers()
        if self._patch is not None:
            with contextlib.suppress(RuntimeError):
                self._patch.stop()


async def async_drive_scenario(session: RealHASession, scenario: dict) -> ProductionRunResult:
    """The shared scenario loop (``session.drive_scenario``) for an async world: advance to each event, feed it."""
    try:
        await session.start()
        for event in sorted_events(scenario):
            await session.advance_to(parse_event_time(event.get("time", ""), session.now()))
            await session.feed(event)
        return await session.finish()
    finally:
        await session.close()
