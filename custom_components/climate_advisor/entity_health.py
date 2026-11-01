"""Generic entity-availability health sweep (Issue #805).

Climate Advisor depends on ~11 user-configured Home Assistant entities
(thermostat, weather source, temperature sensors, door/window sensors, fan
entities, occupancy toggles, notify service). Before this module existed,
every read site for every one of these entities degraded silently when the
entity was removed or went unavailable — no log above DEBUG, no user-facing
signal, in most cases. Issue #805 was filed because a removed thermostat
entity left the automation running on stale/empty data for hours with no
indication anything was wrong.

**Unlike ``invariant_watchdog.py``, this is deliberately a declarative
registry + one generic sweep function, not a flat list of bespoke check
functions.** ``invariant_watchdog.py``'s own docstring is explicit that its
flat-list shape is right for physically distinct *safety invariants*, each
requiring individual human sign-off. Entity health is structurally
different: all ~11 checks are the *same* operation ("does this configured
entity currently resolve?") applied to different config keys. Writing 11
near-identical hand-rolled functions would be exactly the kind of
copy-pasted-per-entity duplication this module exists to avoid.

This module detects only — it never changes automation/control-logic
behavior (e.g. it does not change what ``_is_sensor_open()`` does when a
door sensor is missing). It returns a plain list of issues; the coordinator
decides what to do with them (log, notify, surface on the status card).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_FAN_REMOTE_ENTITY,
    CONF_FAN_STATE_ENTITY,
    CONF_GUEST_TOGGLE,
    CONF_HOME_TOGGLE,
    CONF_VACATION_TOGGLE,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_WHOLE_HOUSE,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    UNAVAILABLE_STATES,
)

_UNAVAILABLE_STATES = UNAVAILABLE_STATES  # back-compat alias; rule lives in const.py

# The one list-valued config key. Swept separately from the scalar registry
# below since each list member gets its own independent health check.
DOOR_WINDOW_SENSORS_CONFIG_KEY = "door_window_sensors"
DOOR_WINDOW_SENSOR_FRIENDLY_NAME = "Door/window sensor"
DOOR_WINDOW_SENSOR_CRITICALITY = "degraded"
THERMOSTAT_CONFIG_KEY = "climate_entity"


@dataclass(frozen=True)
class EntityHealthIssue:
    """A single configured entity that is currently missing or unavailable."""

    config_key: str
    entity_id: str
    friendly_name: str
    criticality: str  # "critical" | "degraded" | "optional"
    status: str  # "missing" (state is None) | "unavailable" (state exists but unavailable/unknown)
    # What Climate Advisor is doing about it, in the occupant's terms (Issue #1181). Filled in by the
    # coordinator (``entity_health_consequence``); empty keeps the generic "may not control your HVAC" text.
    detail: str = ""

    @property
    def state_key(self) -> str:
        """Key of this issue's transition/debounce slot: one per entity, not one per config key.

        ``door_window_sensors`` is a list, so every sensor needs its own slot -- otherwise a second sensor that
        fails during the first one's outage is never announced (Issue #1181).
        """
        if self.config_key == DOOR_WINDOW_SENSORS_CONFIG_KEY:
            return f"{self.config_key}:{self.entity_id}"
        return self.config_key


def entity_health_consequence(
    issue: EntityHealthIssue, *, door_last_reading: str | None = None, whf_running: bool = False
) -> str:
    """The one place that words what Climate Advisor does while an entity is unreadable (Issue #1181).

    Door/window sensors (owner rule 3, decision D1): a sensor that last read open keeps heating and cooling
    paused; one that last read closed (or never reported) counts as closed. Thermostat (rule 2, decision D5):
    while the whole-house fan runs, "fan and heating/cooling never together" cannot be checked. Anything else
    returns "" and keeps the generic text. This module still never changes what automation does.
    """
    if issue.config_key == DOOR_WINDOW_SENSORS_CONFIG_KEY:
        if door_last_reading == "open":
            return (
                "It last read open, so heating and cooling stay paused until it reports again "
                "or you change the thermostat."
            )
        if door_last_reading == "closed":
            return (
                "It last read closed, so Climate Advisor treats it as closed. "
                "If that door or window is actually open, heating or cooling may run."
            )
        return (
            "It hasn't reported since Climate Advisor started, so Climate Advisor treats it as closed. "
            "If that door or window is actually open, heating or cooling may run."
        )
    if issue.config_key == THERMOSTAT_CONFIG_KEY and whf_running:
        return (
            "The whole-house fan is running, and Climate Advisor can't check that the fan and heating/cooling "
            "are never on together until the thermostat responds. Turn the heating or cooling off yourself "
            "if it is on."
        )
    return ""


def _always_relevant(_config: dict) -> bool:
    return True


def _fan_entities_relevant(config: dict) -> bool:
    """fan_entity/fan_state_entity only matter when fan_mode actually uses a whole-house fan.

    Mirrors invariant_watchdog.py's own fan_mode scoping for check_ac_whf_mutex —
    FAN_MODE_HVAC coexists with the compressor by design and has no separate WHF
    entity to monitor; FAN_MODE_DISABLED means fan control isn't configured at all.
    """
    return config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH)


def _outdoor_temp_entity_relevant(config: dict) -> bool:
    """outdoor_temp_entity only matters when outdoor_temp_source is a dedicated sensor."""
    return config.get("outdoor_temp_source") in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)


def _indoor_temp_entity_relevant(config: dict) -> bool:
    """indoor_temp_entity only matters when indoor_temp_source is a dedicated sensor."""
    return config.get("indoor_temp_source") in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)


def _check_scalar_entity(hass: Any, config: dict, config_key: str, meta: dict) -> EntityHealthIssue | None:
    """Default checker: config_key's value is an HA entity_id, resolved via hass.states.get()."""
    if not meta["relevant"](config):
        return None
    entity_id = config.get(config_key)
    if not entity_id:
        # Blank/unset is optional-by-design (e.g. fan_remote_entity intentionally
        # disabled) — never flagged. This is how "optional" is enforced
        # structurally rather than special-cased per key.
        return None
    state = hass.states.get(entity_id)
    if state is None:
        status = "missing"
    elif state.state in _UNAVAILABLE_STATES:
        status = "unavailable"
    else:
        return None
    return EntityHealthIssue(
        config_key=config_key,
        entity_id=entity_id,
        friendly_name=meta["friendly_name"],
        criticality=meta["criticality"],
        status=status,
    )


def _check_notify_service(hass: Any, config: dict, config_key: str, meta: dict) -> EntityHealthIssue | None:
    """notify_service is a service name (e.g. "notify.mobile_app_x"), not a state entity —
    resolved via hass.services.has_service(), not hass.states.get().
    """
    service_value = config.get(config_key)
    if not service_value:
        return None
    service_name = service_value.split(".")[-1] if "." in service_value else service_value
    if hass.services.has_service("notify", service_name):
        return None
    return EntityHealthIssue(
        config_key=config_key,
        entity_id=service_value,
        friendly_name=meta["friendly_name"],
        criticality=meta["criticality"],
        status="missing",
    )


# Declarative registry — the single source of truth for what gets swept, how
# urgent it is, and whether it applies given the rest of the current config.
# Adding a new monitored entity means adding one row here, not a new function.
ENTITY_HEALTH_REGISTRY: dict[str, dict[str, Any]] = {
    "climate_entity": {
        "friendly_name": "Thermostat",
        "criticality": "critical",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
    "weather_entity": {
        "friendly_name": "Weather source",
        "criticality": "critical",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
    "notify_service": {
        "friendly_name": "Notification service",
        "criticality": "critical",
        "relevant": _always_relevant,
        "checker": _check_notify_service,
    },
    "outdoor_temp_entity": {
        "friendly_name": "Outdoor temperature sensor",
        "criticality": "degraded",
        "relevant": _outdoor_temp_entity_relevant,
        "checker": _check_scalar_entity,
    },
    "indoor_temp_entity": {
        "friendly_name": "Indoor temperature sensor",
        "criticality": "degraded",
        "relevant": _indoor_temp_entity_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_FAN_ENTITY: {
        "friendly_name": "Whole-house fan",
        "criticality": "degraded",
        "relevant": _fan_entities_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_FAN_STATE_ENTITY: {
        "friendly_name": "Fan state sensor",
        "criticality": "degraded",
        "relevant": _fan_entities_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_FAN_REMOTE_ENTITY: {
        "friendly_name": "Fan RF remote",
        "criticality": "optional",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_HOME_TOGGLE: {
        "friendly_name": "Home/away toggle",
        "criticality": "degraded",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_VACATION_TOGGLE: {
        "friendly_name": "Vacation toggle",
        "criticality": "optional",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
    CONF_GUEST_TOGGLE: {
        "friendly_name": "Guest toggle",
        "criticality": "optional",
        "relevant": _always_relevant,
        "checker": _check_scalar_entity,
    },
}


def run_entity_health_sweep(hass: Any, config: dict) -> list[EntityHealthIssue]:
    """Check every configured entity in ENTITY_HEALTH_REGISTRY plus door_window_sensors.

    Cheap, synchronous, pure dict/state lookups only — safe to call every
    coordinator update cycle. Callers are responsible for isolating this from
    the rest of the update cycle (a bug here must not be able to abort a
    cycle whose only job was to detect a *different* problem).
    """
    issues: list[EntityHealthIssue] = []

    for config_key, meta in ENTITY_HEALTH_REGISTRY.items():
        checker: Callable[[Any, dict, str, dict], EntityHealthIssue | None] = meta["checker"]
        issue = checker(hass, config, config_key, meta)
        if issue is not None:
            issues.append(issue)

    for entity_id in config.get(DOOR_WINDOW_SENSORS_CONFIG_KEY) or []:
        state = hass.states.get(entity_id)
        if state is None:
            status = "missing"
        elif state.state in _UNAVAILABLE_STATES:
            status = "unavailable"
        else:
            continue
        issues.append(
            EntityHealthIssue(
                config_key=DOOR_WINDOW_SENSORS_CONFIG_KEY,
                entity_id=entity_id,
                friendly_name=DOOR_WINDOW_SENSOR_FRIENDLY_NAME,
                criticality=DOOR_WINDOW_SENSOR_CRITICALITY,
                status=status,
            )
        )

    return issues
