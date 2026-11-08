"""Indoor and outdoor relative humidity for the Status charts (Issue #1414): one resolver, three roles.

Occupant view: the chart shows how humid the room CA is reading feels next to its temperature (the day room by day,
the bedroom in the sleep window), and how humid it is outside. CHART DISPLAY ONLY: no decision, setpoint or control
path reads anything here (pinned by ``tests/test_chart_humidity_1414.py``).

Source order per role (first usable value wins):

- ``indoor_day``: (1) the user's chosen entity; (2) a ``device_class: humidity`` sensor on the same device as the
  climate entity, then as the comfort temperature source entity; (3) the climate entity's ``current_humidity``.
- ``indoor_sleep``: (1) the user's chosen entity; (2) a humidity sensor on the sleep temperature sensor's device;
  (3) none (never the day room's humidity).
- ``outdoor``: (1) the user's chosen entity; (2) a humidity sensor on the outdoor temperature sensor's device;
  (3) the weather entity's ``humidity`` attribute.

Never used: the climate entity's ``humidity`` attribute (Home Assistant's TARGET humidity,
``ClimateEntityStateAttribute.TARGET_HUMIDITY = "humidity"``; the measured value is ``current_humidity``), the weather
entity's humidity for an indoor role, any ``number.*`` entity (a humidifier's target), and, for the indoor roles'
auto-detection, a sensor whose own name says outdoor / outside / exterior / floor (``_names_non_room_sensor``).
The user's choice is trusted, but its value must be finite and within 0-100 %. Values are never rounded.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import Any

from .indoor_temp import discover_companion_sensors

ROLE_INDOOR_DAY = "indoor_day"
ROLE_INDOOR_SLEEP = "indoor_sleep"
ROLE_OUTDOOR = "outdoor"
ROLES = (ROLE_INDOOR_DAY, ROLE_INDOOR_SLEEP, ROLE_OUTDOOR)

# The 3rd source per role: (entity domain it must belong to, attribute). Fixed by role so an indoor role can never
# read a weather entity and no role can ever read the climate TARGET humidity attribute ("humidity").
_FALLBACK_ATTR: dict[str, tuple[str, str] | None] = {
    ROLE_INDOOR_DAY: ("climate", "current_humidity"),
    ROLE_INDOOR_SLEEP: None,
    ROLE_OUTDOOR: ("weather", "humidity"),
}

Discover = Callable[[str, bool], tuple[str, ...]]


def _valid_rh(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except ValueError, TypeError:
        return None
    if not math.isfinite(v) or not 0.0 <= v <= 100.0:
        return None
    return v


def read_humidity_state(hass: Any, entity_id: str | None) -> float | None:
    """A humidity entity's state as % (finite, 0-100), else None. ``number.*`` entities are never read."""
    if not isinstance(entity_id, str) or not entity_id or entity_id.startswith("number."):
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    return _valid_rh(st.state)


def _read_fallback(hass: Any, role: str, entity_id: str | None) -> float | None:
    spec = _FALLBACK_ATTR.get(role)
    if spec is None or not isinstance(entity_id, str) or not entity_id.startswith(spec[0] + "."):
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    return _valid_rh(st.attributes.get(spec[1]))


def resolve_humidity(
    hass: Any,
    role: str,
    *,
    chosen: str | None = None,
    source_entities: Iterable[str | None] = (),
    fallback_entity: str | None = None,
    discover: Discover | None = None,
) -> tuple[float | None, str | None]:
    """``(rh_percent, source)`` for ``role`` by the module's source order; ``(None, None)`` when nothing is usable.

    ``source_entities``: the entities whose devices are searched for a humidity sensor, in order (indoor_day: the
    climate entity then the comfort temperature source; indoor_sleep: the sleep sensor; outdoor: the outdoor
    temperature sensor). ``fallback_entity``: the climate entity (indoor_day) or the weather entity (outdoor).
    ``discover(entity_id, exclude_non_room)``: companion lookup (the coordinator passes its cached one).
    ``source`` is an entity id, or ``"<entity>#<attribute>"`` for the attribute fallback. Never raises.
    """
    if role not in ROLES:
        return None, None
    try:
        v = read_humidity_state(hass, chosen)
        if v is not None:
            return v, chosen
        exclude_non_room = role != ROLE_OUTDOOR
        lookup = discover or (
            lambda eid, excl: discover_companion_sensors(hass, eid, "humidity", exclude_non_room=excl)
        )
        seen: set[str] = set()
        for src in source_entities:
            if not isinstance(src, str) or not src or src in seen:
                continue
            seen.add(src)
            for cand in lookup(src, exclude_non_room):
                v = read_humidity_state(hass, cand)
                if v is not None:
                    return v, cand
        v = _read_fallback(hass, role, fallback_entity)
        if v is not None:
            return v, f"{fallback_entity}#{_FALLBACK_ATTR[role][1]}"  # type: ignore[index]
    except Exception:  # noqa: BLE001 - a chart row must never fail on humidity
        return None, None
    return None, None


def read_humidity_source(hass: Any, source: str | None) -> float | None:
    """The current % of a ``resolve_humidity`` source (an entity id or ``"<entity>#<attribute>"``), else None.

    Used for the "instead of" value when the source in use changes (Issue #1455). Only the two fallback attributes
    ``resolve_humidity`` can return are read (never the climate TARGET humidity). Pure read; never raises.
    """
    if not isinstance(source, str) or not source:
        return None
    if "#" not in source:
        return read_humidity_state(hass, source)
    entity_id, attr = source.split("#", 1)
    for role, spec in _FALLBACK_ATTR.items():
        if spec is not None and spec[1] == attr:
            return _read_fallback(hass, role, entity_id)
    return None


def indoor_role_for(source_entity: str | None, sleep_entity: str | None) -> str:
    """The indoor role a row uses: ``indoor_sleep`` when the comfort reading came from the sleep sensor, else day.

    ``source_entity`` is ``IndoorTempReading.source_entity`` (already sleep-window swapped), so the humidity always
    belongs to the room whose temperature the row records.
    """
    if sleep_entity and source_entity == sleep_entity:
        return ROLE_INDOOR_SLEEP
    return ROLE_INDOOR_DAY
