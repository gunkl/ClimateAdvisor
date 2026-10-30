"""The house: one call builds a small real home inside real Home Assistant (design section 4).

Pieces: the simulated thermostat (``ca_dev_thermostat_sim``, which runs Climate Advisor's own heat-flow
math), a real WeatherEntity, contact sensors, a whole-house fan, a notification service, the unit system,
and a frozen clock the test turns forward in 30 s slices.

Lifecycle contract
------------------
* ``build_house`` sets up the sim first, then Climate Advisor.
* ``close`` (idempotent, always run by the ``make_house`` fixture) unloads Climate Advisor, then the sim.
* HA's autouse ``verify_cleanup`` fixture is the net: a timer, task or thread left alive fails the test.
  The house adds no waiver and no ``expected_lingering_timers``.
* ``reload_zone`` is the settings-save shape (update entry, HA reloads it); ``restart`` unloads and sets
  up Climate Advisor again on the same ``hass`` and config dir. The physical thermostat and fan keep
  running, as real devices do when HA restarts. It does NOT exercise HA's own boot ordering (a new
  process); persistence and clean-slate logic are exercised.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from .calls import CallRecorder
from .fans import WholeHouseFan
from .weather import HouseWeather

SIM_DOMAIN = "ca_dev_thermostat_sim"
CA_DOMAIN = "climate_advisor"
SIM_STEP = timedelta(seconds=30)
# Tier B harness seed: one known-good zone config. Read as data only (design R4: if Tier B is ever
# deleted, copy this file to tests_ha/house/config_seed.json first).
_SEED = Path(__file__).resolve().parents[2] / "tools" / "integration_harness" / "config-seed" / ".storage"
_SEED = _SEED / "core.config_entries"

DEFAULT_START = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
STARTUP_WINDOW_S = 300

CLIMATE_ENTITY = "climate.house_thermostat"
WEATHER_ENTITY = "weather.house"
OUTDOOR_F_SENSOR = "sensor.house_outdoor_f"


def _f_to_c(value_f: float) -> float:
    return (value_f - 32.0) * 5.0 / 9.0


def _seed_entry() -> dict[str, Any]:
    entries = json.loads(_SEED.read_text(encoding="utf-8"))["data"]["entries"]
    return next(e for e in entries if e["domain"] == CA_DOMAIN)


class ThermostatView:
    """The simulated thermostat as the occupant would read it, always in degrees Fahrenheit."""

    def __init__(self, house: House) -> None:
        self._house = house

    @property
    def _state(self):
        state = self._house.hass.states.get(CLIMATE_ENTITY)
        assert state is not None, f"{CLIMATE_ENTITY} has no state"
        return state

    def _to_f(self, value: float | None) -> float | None:
        if value is None:
            return None
        return value * 9.0 / 5.0 + 32.0 if self._house.units == "metric" else float(value)

    @property
    def mode(self) -> str:
        return self._state.state

    @property
    def setpoint_f(self) -> float | None:
        return self._to_f(self._state.attributes.get("temperature"))

    @property
    def indoor_f(self) -> float | None:
        return self._to_f(self._state.attributes.get("current_temperature"))

    @property
    def action(self) -> str | None:
        return self._state.attributes.get("hvac_action")

    @property
    def fan_mode(self) -> str | None:
        return self._state.attributes.get("fan_mode")


@dataclass
class House:
    """A built house. Construct with :func:`build_house`."""

    hass: HomeAssistant
    freezer: Any
    config_dir: Path
    units: str
    profile: str
    startup: str
    sim_entry: ConfigEntry | None = None
    ca_entry: ConfigEntry | None = None
    weather: HouseWeather | None = None
    fan: WholeHouseFan | None = None
    recorder: CallRecorder | None = None
    notifications: list = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    _closed: bool = False

    # ---------------------------------------------------------------- views
    @property
    def thermostat(self) -> ThermostatView:
        return ThermostatView(self)

    @property
    def calls(self) -> list:
        assert self.recorder is not None
        return self.recorder.calls

    @property
    def coordinator(self):
        """The live Climate Advisor coordinator, or None while unloaded."""
        assert self.ca_entry is not None
        return self.hass.data.get(CA_DOMAIN, {}).get(self.ca_entry.entry_id)

    @property
    def instances(self) -> list:
        """Every coordinator this house has set up, in order (a ghost copy stays listed)."""
        assert self.recorder is not None
        return self.recorder.instances

    def state(self, entity_id: str):
        return self.hass.states.get(entity_id)

    def sensor(self, key: str):
        """The Climate Advisor sensor whose unique id ends with ``key`` (e.g. ``"status"``)."""
        from homeassistant.helpers import entity_registry as er

        assert self.ca_entry is not None
        reg = er.async_get(self.hass)
        for ent in er.async_entries_for_config_entry(reg, self.ca_entry.entry_id):
            if ent.unique_id == f"{self.ca_entry.entry_id}_{key}":
                st = self.hass.states.get(ent.entity_id)
                assert st is not None, f"{ent.entity_id} has no state"
                return st
        raise KeyError(f"no Climate Advisor sensor with unique id suffix {key!r}")

    # -------------------------------------------------------------- the world
    def set_indoor(self, temp_f: float) -> None:
        """Jump the simulated indoor temperature (one-shot; physics resumes from it)."""
        ent = self.hass.data[SIM_DOMAIN][self.sim_entry.entry_id]["thermostat_entity"]
        ent.apply_manual_temperature_override(float(temp_f))

    def set_outdoor(
        self,
        temp_f: float,
        *,
        high_f: float | None = None,
        low_f: float | None = None,
        condition: str | None = None,
    ) -> None:
        if self.weather is not None:
            self.weather.set_conditions(temp_f=temp_f, high_f=high_f, low_f=low_f, condition=condition)
            self.weather.async_write_ha_state()
        self.hass.states.async_set(OUTDOOR_F_SENSOR, str(float(temp_f)))

    async def open(self, name: str = "window") -> None:
        self.hass.states.async_set(f"binary_sensor.house_{name}", "on")
        await self.hass.async_block_till_done()

    async def close_contact(self, name: str = "window") -> None:
        self.hass.states.async_set(f"binary_sensor.house_{name}", "off")
        await self.hass.async_block_till_done()

    def _occupant_context(self) -> Context:
        assert self.recorder is not None
        ctx = Context()
        self.recorder.occupant_context_ids.add(ctx.id)
        return ctx

    async def occupant_sets_thermostat(self, *, mode: str | None = None, setpoint_f: float | None = None) -> None:
        """The person uses the thermostat app: a real service call carrying a marked occupant Context."""
        ctx = self._occupant_context()
        if mode is not None:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode", {"entity_id": CLIMATE_ENTITY, "hvac_mode": mode}, blocking=True, context=ctx
            )
        if setpoint_f is not None:
            value = _f_to_c(setpoint_f) if self.units == "metric" else setpoint_f
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": CLIMATE_ENTITY, "temperature": value},
                blocking=True,
                context=ctx,
            )
        await self.hass.async_block_till_done()

    async def occupant_sets_fan_mode(self, fan_mode: str) -> None:
        """The person turns the thermostat blower on/off in its app."""
        await self.hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": CLIMATE_ENTITY, "fan_mode": fan_mode},
            blocking=True,
            context=self._occupant_context(),
        )
        await self.hass.async_block_till_done()

    async def occupant_presses_fan(self, on: bool) -> None:
        """The person at the wall switch or RF remote: the fan's physical state changes, no CA command."""
        assert self.fan is not None
        self.fan.press(on)
        await self.hass.async_block_till_done()

    async def set_automation(self, enabled: bool) -> None:
        """Flip Climate Advisor's own automation switch through the real switch service."""
        from homeassistant.helpers import entity_registry as er

        assert self.ca_entry is not None
        reg = er.async_get(self.hass)
        ent = next(
            e
            for e in er.async_entries_for_config_entry(reg, self.ca_entry.entry_id)
            if e.unique_id == f"{self.ca_entry.entry_id}_automation_enabled"
        )
        await self.hass.services.async_call(
            "switch", "turn_on" if enabled else "turn_off", {"entity_id": ent.entity_id}, blocking=True
        )
        await self.hass.async_block_till_done()

    # ------------------------------------------------------------------ time
    async def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        """Turn the clock forward in slices no larger than the sim tick, firing timers on time."""
        remaining = timedelta(seconds=seconds, minutes=minutes, hours=hours)
        while remaining > timedelta(0):
            step = min(SIM_STEP, remaining)
            self.freezer.tick(step)
            async_fire_time_changed(self.hass)
            await self._drain()
            remaining -= step

    async def advance_to(self, when: datetime) -> None:
        now = dt_util.utcnow()
        assert when >= now, f"cannot go back: {when} < {now}"
        await self.advance(seconds=(when - now).total_seconds())

    async def _drain(self) -> None:
        """Let everything runnable finish before the clock moves again.

        ``async_block_till_done`` waits for HA-tracked tasks only. Climate Advisor starts its thermostat calls
        with ``asyncio.ensure_future`` (so it can time them out), which HA does not track; if the clock jumps
        while one is in flight its 10 s timeout fires spuriously ("no answer from thermostat"), something a real
        clock never does because the call completes in milliseconds.
        """
        await self.hass.async_block_till_done()
        for _ in range(5):
            await asyncio.sleep(0)
        await self.hass.async_block_till_done()

    # ------------------------------------------------------------- lifecycle
    async def _setup_ca(self) -> None:
        assert self.ca_entry is not None
        assert await self.hass.config_entries.async_setup(self.ca_entry.entry_id)
        await self._drain()
        assert self.ca_entry.state is ConfigEntryState.LOADED, self.ca_entry.state
        assert self.recorder is not None
        self.recorder.instances.append(self.coordinator)
        self.recorder.reattribute()

    async def _settle(self) -> None:
        if self.startup == "settled":
            await self.advance(seconds=STARTUP_WINDOW_S + 1)

    async def reload_zone(self, **data_changes: Any) -> None:
        """The settings-save shape: optionally update the entry, then HA reloads it (unload + setup)."""
        assert self.ca_entry is not None
        if data_changes:
            self.hass.config_entries.async_update_entry(self.ca_entry, data={**self.ca_entry.data, **data_changes})
        assert await self.hass.config_entries.async_unload(self.ca_entry.entry_id)
        await self.hass.async_block_till_done()
        await self._setup_ca()

    async def restart(self, *, settle: bool = True, down_for_seconds: float = 0) -> None:
        """Unload and set up Climate Advisor again on the same hass and config dir (see module docstring).

        ``down_for_seconds`` keeps Climate Advisor unloaded for that long first; the thermostat and fan keep
        running, as real devices do while HA is down.
        """
        assert self.ca_entry is not None
        assert await self.hass.config_entries.async_unload(self.ca_entry.entry_id)
        await self.hass.async_block_till_done()
        if down_for_seconds:
            await self.advance(seconds=down_for_seconds)
        await self._setup_ca()
        if settle:
            await self.advance(seconds=STARTUP_WINDOW_S + 1)

    async def close(self) -> None:
        """Unload Climate Advisor, then the sim. Idempotent; always safe in a ``finally``."""
        if self._closed:
            return
        self._closed = True
        if self.recorder is not None:
            self.recorder.stop()
        for entry in (self.ca_entry, self.sim_entry):
            if entry is not None and entry.state is ConfigEntryState.LOADED:
                assert await self.hass.config_entries.async_unload(entry.entry_id)
                await self.hass.async_block_till_done()


async def build_house(
    hass: HomeAssistant,
    freezer: Any,
    config_dir: Path,
    *,
    units: str = "us",
    profile: str = "closed_loop",
    fan: str | None = None,
    contacts: list[str] | None = None,
    startup: str = "settled",
    start: datetime = DEFAULT_START,
    initial_indoor_f: float = 75.0,
    outdoor_f: float | None = 70.0,
    high_f: float = 78.0,
    low_f: float = 58.0,
    time_zone: str = "UTC",
    ca_config: dict[str, Any] | None = None,
    extra_states: dict[str, str] | None = None,
    initial_hvac_mode: str | None = None,
    before_ca_setup: Any = None,
    weather: bool = True,
) -> House:
    """Build the house and return it with Climate Advisor LOADED.

    ``units`` is ``"us"`` (HA US customary + CA ``fahrenheit``) or ``"metric"`` (HA metric + CA ``celsius``); the
    pair is set together because a mismatch makes HA reject every setpoint (finding F4).
    ``profile``: ``"closed_loop"`` (physics runs) or ``"scripted"`` (all k values zero; the test sets indoor).
    ``fan``: ``None``, ``"whf"`` (single entity, feedback), ``"whf_dual"`` (command + power entity),
    ``"hvac_fan"`` (the thermostat blower), or ``"whf_command_only"`` (no feedback).
    ``ca_config``: overlaid on the entry data; a value of ``...`` removes that key (a config the flow never stored).
    ``extra_states``: entity id -> state written before Climate Advisor sets up (e.g. an occupancy toggle).
    ``initial_hvac_mode``: the thermostat's mode before Climate Advisor sets up (``"off"`` by default); set on the
    sim entity directly, so it is not a service call and nothing records it.
    ``weather``: False builds no weather entity at all (the outdoor sensor still exists), for a scenario whose
    world has none: Climate Advisor then finds no forecast and does not classify the day on its own.
    ``before_ca_setup``: optional ``async def (house)`` run after the sim is loaded and before Climate Advisor
    sets up (the real-HA scenario adapter adds the scenario's own devices here).
    ``startup``: ``"cold"`` (setup only; the 300 s override-suppression window is open) or ``"settled"``.
    """
    assert units in ("us", "metric"), units
    assert profile in ("closed_loop", "scripted"), profile
    assert startup in ("cold", "settled"), startup
    contacts = list(contacts or [])

    hass.config.config_dir = str(config_dir)  # CA writes its state and diagnostics files here
    await hass.config.async_set_time_zone(time_zone)
    hass.config.units = METRIC_SYSTEM if units == "metric" else US_CUSTOMARY_SYSTEM
    freezer.move_to(start)

    house = House(hass=hass, freezer=freezer, config_dir=config_dir, units=units, profile=profile, startup=startup)
    house.contacts = contacts
    house.recorder = CallRecorder(hass)
    house.recorder.start()
    house.notifications = async_mock_service(hass, "notify", "notify")

    # --- weather (real entity) and the outdoor mirror the sim reads in degrees F
    if weather:
        assert await async_setup_component(hass, "weather", {})
        from homeassistant.components.weather.const import DATA_COMPONENT

        house.weather = HouseWeather(WEATHER_ENTITY)
        await hass.data[DATA_COMPONENT].async_add_entities([house.weather])
    if outdoor_f is not None:  # None: no outdoor reading exists yet (the sim then skips its ticks)
        house.set_outdoor(outdoor_f, high_f=high_f, low_f=low_f)

    for entity_id, value in (extra_states or {}).items():
        hass.states.async_set(entity_id, value)

    # --- contacts and fan
    for name in contacts:
        hass.states.async_set(f"binary_sensor.house_{name}", "off")
    ca_fan: dict[str, Any] = {"fan_mode": "disabled", "fan_entity": None}
    if fan in ("whf", "whf_dual", "whf_command_only"):
        house.fan = WholeHouseFan(
            hass,
            state_entity="binary_sensor.house_whf_power" if fan == "whf_dual" else None,
            feedback=fan != "whf_command_only",
        )
        ca_fan = {
            "fan_mode": "whole_house_fan",
            "fan_entity": house.fan.command_entity,
            "fan_state_entity": house.fan.state_entity,
            "fan_state_feedback": fan != "whf_command_only",
        }
    elif fan == "hvac_fan":
        ca_fan = {"fan_mode": "hvac_fan", "fan_entity": None}
    else:
        assert fan is None, fan

    # --- the simulated thermostat
    k = (-0.15, 6.0, -6.0) if profile == "closed_loop" else (0.0, 0.0, 0.0)
    house.sim_entry = MockConfigEntry(
        domain=SIM_DOMAIN,
        title="House Thermostat",
        data={
            "name": "House Thermostat",
            "initial_temp_f": float(initial_indoor_f),
            "k_passive": k[0],
            "k_active_heat": k[1],
            "k_active_cool": k[2],
            "comfort_heat": 70.0,
            "comfort_cool": 75.0,
            "outdoor_source": OUTDOOR_F_SENSOR,
            "tick_seconds": int(SIM_STEP.total_seconds()),
        },
    )
    house.sim_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(house.sim_entry.entry_id)
    await hass.async_block_till_done()
    assert house.sim_entry.state is ConfigEntryState.LOADED, house.sim_entry.state
    assert hass.states.get(CLIMATE_ENTITY) is not None, "sim climate entity id changed"
    if initial_hvac_mode is not None:
        from homeassistant.components.climate import HVACMode

        sim = hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]
        sim._hvac_mode = HVACMode(initial_hvac_mode)
        sim.async_write_ha_state()

    if before_ca_setup is not None:
        await before_ca_setup(house)

    # --- Climate Advisor
    seed = _seed_entry()
    data = dict(seed["data"])
    data.update(
        {
            "climate_entity": CLIMATE_ENTITY,
            "weather_entity": WEATHER_ENTITY,
            "notify_service": "notify.notify",
            "temp_unit": "celsius" if units == "metric" else "fahrenheit",
            "door_window_sensors": [f"binary_sensor.house_{n}" for n in contacts],
            **ca_fan,
        }
    )
    data.update(ca_config or {})
    data = {k: v for k, v in data.items() if v is not ...}  # an Ellipsis value in ca_config removes the key
    house.ca_entry = MockConfigEntry(
        domain=CA_DOMAIN,
        title="House",
        version=seed["version"],
        minor_version=seed.get("minor_version", 1),
        data=data,
        options=seed.get("options", {}),
    )
    house.ca_entry.add_to_hass(hass)
    await house._setup_ca()
    await house._settle()
    return house
