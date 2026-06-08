"""build_engine — construct a headless AutomationEngine with FakeHass + FakeScheduler.

``build_headless_engine()`` is the single entry point for harness consumers.
It installs HA stubs, imports AutomationEngine, wires up FakeHass and
FakeScheduler, and returns all four handles the caller needs to drive the
engine and inspect its decisions.

Returned tuple: ``(engine, fake_hass, scheduler, event_log)``

  - ``engine``    — the live ``AutomationEngine`` instance
  - ``fake_hass`` — ``FakeHass``; read ``fake_hass.action_log`` for service calls
  - ``scheduler`` — ``FakeScheduler``; call ``scheduler.advance_to/by(...)``
                    inside the ``scheduler.installed()`` context manager
  - ``event_log`` — ``list[tuple[str, dict, datetime]]``; appended by
                    ``engine._emit_event_callback``

Minimal config defaults are supplied so the engine constructs without error.
Pass a ``config`` dict to override any field.

Coordinator proxy:
  The engine never calls back into the coordinator directly — the three
  coordinator-facing attributes are wired separately:
    - ``engine._revisit_callback``      set to an async no-op by default
    - ``engine._sensor_check_callback`` set to ``lambda: False`` (no open sensors)
    - ``engine._emit_event_callback``   appends to event_log

  The engine does read ``self._thermal_model`` and ``self._hourly_forecast_temps``
  directly as instance attributes, and ``self._current_classification`` — these
  are set on the engine instance directly.  Use ``engine._thermal_model = {...}``
  etc. after construction to inject test values.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from tools.sim_harness.fake_hass import FakeHass, FakeState
from tools.sim_harness.fake_scheduler import FakeScheduler
from tools.sim_harness.ha_stubs import install_ha_stubs

# Default config values that satisfy the engine's internal guards.
# Keys use the exact string values from const.py (CONF_* = "...") so that
# engine config.get(CONF_FOO, default) lookups resolve correctly.
_DEFAULT_CONFIG: dict[str, Any] = {
    "climate_entity": "climate.test_thermostat",
    "weather_entity": "weather.test_forecast",
    "door_window_sensors": [],
    "notify_service": "notify.test",
    "comfort_heat": 70.0,
    "comfort_cool": 76.0,
    "setback_heat": 60.0,
    "setback_cool": 82.0,
    # Grace periods (CONF_AUTOMATION_GRACE_PERIOD = "automation_grace_seconds", etc.)
    "automation_grace_seconds": 300,
    "manual_grace_seconds": 900,
    "automation_grace_notify": False,
    "manual_grace_notify": False,
    # Override confirmation (CONF_OVERRIDE_CONFIRM_PERIOD = "override_confirm_seconds")
    "override_confirm_seconds": 30,
    # Fan — disabled by default so fan cycles don't interfere unless requested
    # CONF_FAN_MODE = "fan_mode", CONF_FAN_MIN_RUNTIME_PER_HOUR = "fan_min_runtime_per_hour"
    "fan_mode": "disabled",
    "fan_min_runtime_per_hour": 0,
    # Misc (CONF_SENSOR_DEBOUNCE = "sensor_debounce_seconds", etc.)
    "sensor_debounce_seconds": 0,
    "nat_vent_hysteresis_f": 2.0,
    "nat_vent_reactivation_lockout_s": 300,
    "natural_vent_delta": 3.0,
}


def build_headless_engine(
    config: dict[str, Any] | None = None,
    *,
    climate_entity: str = "climate.test_thermostat",
    climate_state: str = "off",
    climate_attributes: dict[str, Any] | None = None,
    start_time: datetime | None = None,
    sensor_polarity_inverted: bool = False,
) -> tuple[Any, FakeHass, FakeScheduler, list[tuple[str, dict, datetime | None]]]:
    """Build and return a headless AutomationEngine.

    Args:
        config: Runtime config dict.  Merged over ``_DEFAULT_CONFIG``; pass only
                the keys you want to differ from the defaults.
        climate_entity: Entity ID for the climate device.
        climate_state: Initial thermostat state string (e.g. ``"off"``, ``"heat"``).
                       Defaults to ``"off"`` to match the legacy SimState default.
        climate_attributes: Initial thermostat attributes dict.
        start_time: Virtual clock start time.  Defaults to 2024-01-15 08:00 UTC.
        sensor_polarity_inverted: Passed to AutomationEngine constructor.

    Returns:
        ``(engine, fake_hass, scheduler, event_log)``
    """
    # 1. Install HA stubs — idempotent, safe to call multiple times
    install_ha_stubs()

    # 2. Import AutomationEngine AFTER stubs are installed
    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415

    # 3. Build merged config
    merged_config: dict[str, Any] = {**_DEFAULT_CONFIG}
    merged_config["climate_entity"] = climate_entity
    if config:
        merged_config.update(config)

    # 4. Virtual clock + FakeHass
    if start_time is None:
        start_time = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)

    scheduler = FakeScheduler(start=start_time)
    fake_hass = FakeHass(clock_fn=scheduler.now)
    fake_hass.set_scheduler(scheduler)

    # 5. Inject initial climate entity state so the engine can read it.
    #    Default matches legacy SimState: mode="off", fan_mode="auto", no target temp,
    #    no hvac_action (engine should not fire mode-inconsistency checks at startup).
    attrs = {"fan_mode": "auto"}
    if climate_attributes:
        attrs.update(climate_attributes)
    fake_hass.states.set(climate_entity, FakeState(state=climate_state, attributes=attrs))

    # 6. Event log — shared list that the engine's callback appends to
    event_log: list[tuple[str, dict, datetime | None]] = []

    def _emit_event(event_type: str, payload: dict) -> None:
        ts: datetime | None = None
        with contextlib.suppress(Exception):
            ts = scheduler.now()
        event_log.append((event_type, payload, ts))

    # 7. Construct AutomationEngine
    engine = AutomationEngine(
        hass=fake_hass,
        climate_entity=climate_entity,
        weather_entity=merged_config.get("weather_entity", "weather.test"),
        door_window_sensors=merged_config.get("door_window_sensors", []),
        notify_service=merged_config.get("notify_service", "notify.test"),
        config=merged_config,
        sensor_polarity_inverted=sensor_polarity_inverted,
    )

    # 8. Wire coordinator-facing callbacks
    engine._emit_event_callback = _emit_event

    async def _noop_revisit() -> None:
        pass

    engine._revisit_callback = _noop_revisit
    engine._sensor_check_callback = lambda: False  # no open sensors by default

    return engine, fake_hass, scheduler, event_log
