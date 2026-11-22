"""Scenario humidity for both simulation adapters (Issue #1690, plan #1507 H0).

A ``temp_update`` event may carry ``humidity`` (indoor RH %, the thermostat's own humidity sensor) and
``outdoor_humidity`` (outdoor RH %, the outdoor temperature sensor's own humidity sensor). Production pairs each
humidity with the temperature of the SAME device, found through the entity registry. Neither harness registers
devices, so this module models the registry's answer the one way production asks it: the coordinator's device-lookup
cache (``_companion_sensors_cache``), seeded with a never-expiring entry that says "this humidity sensor is on the
same device as the thermostat / the outdoor sensor". Everything after the lookup (reading, unit, pairing, glitch and
freshness checks, the median) is production's own code.

Stamping: only the humidity entities written here carry ``last_updated`` / ``last_reported`` (the virtual clock in
FakeHass, wall clock in real HA), per event, as ``remote_sensor_report`` does. Stamping every write would switch on
the existing staleness checks in the coordinator-mode goldens (Issue #1690 plan, CHECK-G).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

INDOOR_HUMIDITY_ENTITY = "sensor.sim_indoor_humidity"
OUTDOOR_HUMIDITY_ENTITY = "sensor.sim_outdoor_humidity"
HUMIDITY_ATTRS = {"device_class": "humidity", "unit_of_measurement": "%"}


def seed_same_device(coordinator: Any, entity_id: str, humidity_entity: str, *, outdoor: bool) -> None:
    """Make the coordinator's device lookup find ``humidity_entity`` on ``entity_id``'s device (never refreshed)."""
    cache = getattr(coordinator, "_companion_sensors_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        coordinator._companion_sensors_cache = cache
    cache[(entity_id, "humidity", not outdoor)] = ((humidity_entity,), math.inf)


def outdoor_sensor_entity(config: dict) -> str | None:
    """The scenario's dedicated outdoor temperature sensor, or None (weather-service outdoor source)."""
    if config.get("outdoor_temp_source") in ("sensor", "input_number"):
        entity = config.get("outdoor_temp_entity")
        return entity if isinstance(entity, str) and entity else None
    return None


def require_outdoor_sensor(config: dict) -> str:
    """``outdoor_humidity`` needs a dedicated outdoor sensor to pair with (the harness models no weather entity)."""
    entity = outdoor_sensor_entity(config)
    if entity is None:
        raise ValueError(
            "temp_update.outdoor_humidity needs outdoor_temp_source 'sensor' with an outdoor_temp_entity: the "
            "outdoor humidity is paired with that sensor's temperature (Issue #1690)"
        )
    return entity


def report_scenario_humidity(
    coordinator: Any, climate_entity: str, event: dict, report: Callable[[str, float], None]
) -> bool:
    """The shared seed-then-report step of both adapters for a ``temp_update``'s ``humidity`` / ``outdoor_humidity``.

    ``report(entity_id, rh)`` writes the humidity state the adapter's way (FakeHass stamps it on the virtual clock,
    real HA stamps it itself). Returns True when the event carried ``outdoor_humidity``.
    """
    humidity, outdoor_humidity = event.get("humidity"), event.get("outdoor_humidity")
    if humidity is not None:
        seed_same_device(coordinator, climate_entity, INDOOR_HUMIDITY_ENTITY, outdoor=False)
        report(INDOOR_HUMIDITY_ENTITY, float(humidity))
    if outdoor_humidity is not None:
        sensor = require_outdoor_sensor(coordinator.config)
        seed_same_device(coordinator, sensor, OUTDOOR_HUMIDITY_ENTITY, outdoor=True)
        report(OUTDOOR_HUMIDITY_ENTITY, float(outdoor_humidity))
        return True
    return False
