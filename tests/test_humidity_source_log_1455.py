"""Issue #1455: one INFO line + one Activity entry per real change of a chart humidity line's source.

Occupant view: the Status chart's outdoor humidity line comes from the patio sensor. When that sensor goes unavailable
the line switches to the weather service's humidity, which is for the whole area, not the yard; the bedroom line goes
blank when the bedroom sensor dies; the room line leaves the sensor chosen in Settings for the thermostat's device
sensor. Each switch is now written once to the log and once to the Activity log, naming the old and new source and why,
so a continuous-looking line never silently changes meaning. Nothing is written for repeated identical reads, nothing
during the startup coalesce, and the first source after it is a single "using" line. Chart only: no Repairs card.

Drives the real coordinator methods (``_read_chart_humidity`` -> ``_note_humidity_source`` -> the shared
``_source_changed`` / ``_announce_source`` that the room temperature uses) over the real resolver and the real
companion discovery (registry faked).
"""

from __future__ import annotations

import ast
import datetime as _dt
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.ai_skills_context import _NO_DEDUP, EVENT_RENDERERS  # noqa: E402

NOW = _dt.datetime(2026, 7, 14, 15, 0, tzinfo=_dt.UTC)
CLIMATE = "climate.hallway"
WEATHER = "weather.home"
HALL_RH = "sensor.hallway_humidity"
BED_TEMP = "sensor.bedroom_temp"
BED_RH = "sensor.bedroom_humidity"
OUT_TEMP = "sensor.patio_temp"
OUT_RH = "sensor.patio_humidity"
CHOSEN = "sensor.chosen_humidity"
COORD_LOGGER = "custom_components.climate_advisor.coordinator"
PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"


def _get_coordinator_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


def _coord_mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _state(value, name=None, attributes=None):
    s = MagicMock()
    s.state = value
    s.attributes = dict(attributes or {})
    if name:
        s.attributes["friendly_name"] = name
    s.last_reported = NOW - _dt.timedelta(minutes=5)
    s.last_updated = s.last_reported
    return s


def _reg(entity_id, device_id, device_class=None, original_name=None):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".")[0],
        device_id=device_id,
        device_class=None,
        original_device_class=device_class,
        original_name=original_name,
        translation_key=None,
    )


REGISTRY = {
    CLIMATE: _reg(CLIMATE, "thermostat"),
    HALL_RH: _reg(HALL_RH, "thermostat", "humidity", "Humidity"),
    BED_TEMP: _reg(BED_TEMP, "bedroom", "temperature"),
    BED_RH: _reg(BED_RH, "bedroom", "humidity"),
    OUT_TEMP: _reg(OUT_TEMP, "patio", "temperature"),
    OUT_RH: _reg(OUT_RH, "patio", "humidity"),
}


def _fake_er(entries: dict):
    reg = SimpleNamespace(async_get=lambda eid: entries.get(eid))

    def siblings(_reg, device_id, include_disabled_entities=True):
        return [e for e in entries.values() if e.device_id == device_id]

    return SimpleNamespace(async_get=lambda _hass: reg, async_entries_for_device=siblings)


@pytest.fixture(autouse=True)
def _env(frozen_now, caplog):
    frozen_now(NOW)
    caplog.set_level(logging.DEBUG)
    with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er(REGISTRY)):
        yield


def _house() -> dict:
    return {
        CLIMATE: _state("cool", "Hallway", {"current_temperature": 76.0, "current_humidity": 41.0, "humidity": 99.0}),
        WEATHER: _state("sunny", "Home", {"humidity": 33.0}),
        HALL_RH: _state("52.4", "Hallway Humidity"),
        BED_TEMP: _state("71.2", "Bedroom"),
        BED_RH: _state("61.7", "Bedroom Humidity"),
        OUT_TEMP: _state("92.0", "Patio"),
        OUT_RH: _state("58.25", "Patio Humidity"),
        CHOSEN: _state("47.5", "Chosen Humidity"),
    }


class Env:
    """Bare coordinator (``object.__new__``) driving the real chart humidity read, one call per chart row."""

    def __init__(self, states: dict, **cfg):
        self.states = states
        cls = _get_coordinator_class()
        coord = object.__new__(cls)
        hass = MagicMock()
        hass.states.get = lambda eid: self.states.get(eid)
        hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
        hass.config_entries.async_get_entry = lambda _eid: SimpleNamespace(title="Zone 1")
        coord.hass = hass
        coord._entry_id = "01ABC"
        coord.config = {
            "climate_entity": CLIMATE,
            "weather_entity": WEATHER,
            "indoor_temp_source": "climate_fallback",
            "sleep_indoor_temp_entity": BED_TEMP,
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": OUT_TEMP,
            "temp_unit": "fahrenheit",
            **cfg,
        }
        coord._async_save_state = AsyncMock()
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_override_active = False
        ae._manual_override_active = False
        ae._grace_active = False
        coord.automation_engine = ae
        coord._companion_sensors_cache = {}
        coord._startup_coalesce_active = False
        self.coord = coord
        self.emit = MagicMock()

    def row(self, caplog, *, sleep=False) -> list[str]:
        caplog.clear()
        cls = _get_coordinator_class()
        with (
            patch.object(_coord_mod(), "_in_sleep_window", return_value=sleep),
            patch.object(cls, "_emit_event", self.emit),
        ):
            out = self.coord._read_chart_humidity(indoor_f=76.0, outdoor_f=92.0)
        self.last = out
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == COORD_LOGGER and r.levelno == logging.INFO and "humidity" in r.getMessage()
        ]

    def events(self) -> list[dict]:
        return [c.args[1] for c in self.emit.call_args_list if c.args[0] == "humidity_source_changed"]


def test_startup_using_lines_then_silent_on_repeats(caplog):
    env = Env(_house())
    env.coord._startup_coalesce_active = True
    assert env.row(caplog) == []  # startup coalesce: nothing announced, nothing recorded
    env.coord._startup_coalesce_active = False
    first = env.row(caplog)
    assert first == [
        'Zone "Zone 1": room humidity using "Hallway Humidity" (sensor.hallway_humidity, sensor on the device, 52.4%):'
        " humidity sensor found on the device",
        'Zone "Zone 1": outdoor humidity using "Patio Humidity" (sensor.patio_humidity, sensor on the device, 58.25%):'
        " humidity sensor found on the device",
    ]
    assert env.events() == []  # "using" is not a switch
    for _ in range(3):
        assert env.row(caplog) == []  # repeated identical reads
    assert env.events() == []
    assert env.last["outdoor_humidity"] == 58.25  # the chart row is unchanged by the announcement


def test_outdoor_weather_fallback_and_return(caplog):
    env = Env(_house())
    env.row(caplog)
    env.states[OUT_RH] = _state("unavailable", "Patio Humidity")
    out = env.row(caplog)
    assert out == [
        'Zone "Zone 1": outdoor humidity now from "Home" (weather.home#humidity, weather service, 33%) instead of'
        ' "Patio Humidity" (sensor.patio_humidity, sensor on the device, no reading):'
        " weather service (no outdoor humidity sensor)"
    ]
    (ev,) = env.events()
    assert ev == {
        "zone": "Zone 1",
        "role": "outdoor",
        "subject": "outdoor humidity",
        "from_entity": OUT_RH,
        "from_name": "Patio Humidity",
        "from_kind": "sensor on the device",
        "from_rh": None,
        "to_entity": "weather.home#humidity",
        "to_name": "Home",
        "to_kind": "weather service",
        "to_rh": 33.0,
        "reason": "weather service (no outdoor humidity sensor)",
    }
    assert env.row(caplog) == [] and len(env.events()) == 1  # still on the weather service: silent

    env.states[OUT_RH] = _state("58.25", "Patio Humidity")
    out = env.row(caplog)
    assert out == [
        'Zone "Zone 1": outdoor humidity now from "Patio Humidity" (sensor.patio_humidity, sensor on the device,'
        ' 58.25%) instead of "Home" (weather.home#humidity, weather service, 33%): humidity sensor found on the device'
    ]
    assert len(env.events()) == 2


def test_chosen_sensor_unavailable_and_back(caplog):
    env = Env(_house(), indoor_humidity_entity=CHOSEN)
    first = env.row(caplog)
    assert (
        first[0]
        == 'Zone "Zone 1": room humidity using "Chosen Humidity" (sensor.chosen_humidity, your chosen sensor, 47.5%)'
    )
    env.states[CHOSEN] = _state("unavailable", "Chosen Humidity")
    out = env.row(caplog)
    assert out == [
        'Zone "Zone 1": room humidity now from "Hallway Humidity" (sensor.hallway_humidity, sensor on the device,'
        ' 52.4%) instead of "Chosen Humidity" (sensor.chosen_humidity, your chosen sensor, no reading):'
        " the chosen sensor is unavailable"
    ]
    env.states[HALL_RH] = _state("unavailable", "Hallway Humidity")
    out = env.row(caplog)  # still because the chosen one is down: the reason says so
    assert out[0].endswith(
        'now from "Hallway" (climate.hallway#current_humidity, thermostat, 41%) instead of "Hallway Humidity"'
        " (sensor.hallway_humidity, sensor on the device, no reading): the chosen sensor is unavailable"
    )
    env.states[CHOSEN] = _state("47.5", "Chosen Humidity")
    out = env.row(caplog)
    assert out == [
        'Zone "Zone 1": room humidity now from "Chosen Humidity" (sensor.chosen_humidity, your chosen sensor, 47.5%)'
        ' instead of "Hallway" (climate.hallway#current_humidity, thermostat, 41%): back to your chosen sensor'
    ]
    reasons = [e["reason"] for e in env.events()]
    assert reasons == [
        "the chosen sensor is unavailable",
        "the chosen sensor is unavailable",
        "back to your chosen sensor",
    ]
    assert all(e["role"] == "indoor_day" for e in env.events())


def test_chosen_unavailable_at_startup_is_named_on_the_using_line(caplog):
    states = _house()
    states[CHOSEN] = _state("unavailable", "Chosen Humidity")
    env = Env(states, outdoor_humidity_entity=CHOSEN)
    out = env.row(caplog)
    assert out[1] == (
        'Zone "Zone 1": outdoor humidity using "Patio Humidity" (sensor.patio_humidity, sensor on the device, 58.25%):'
        " the chosen sensor is unavailable"
    )


def test_room_thermostat_fallback_reason(caplog):
    env = Env(_house())
    env.row(caplog)
    env.states[HALL_RH] = _state("unknown", "Hallway Humidity")
    out = env.row(caplog)
    assert out == [
        'Zone "Zone 1": room humidity now from "Hallway" (climate.hallway#current_humidity, thermostat, 41%) instead'
        ' of "Hallway Humidity" (sensor.hallway_humidity, sensor on the device, no reading):'
        " no humidity sensor found on the device"
    ]
    assert env.events()[0]["to_kind"] == "thermostat"


def test_bedroom_role_no_source_and_back_tracked_apart_from_the_day_room(caplog):
    env = Env(_house())
    env.row(caplog)  # day: room + outdoor "using"
    night = env.row(caplog, sleep=True)
    assert night == [
        'Zone "Zone 1": bedroom humidity using "Bedroom Humidity" (sensor.bedroom_humidity, sensor on the device,'
        " 61.7%): humidity sensor found on the device"
    ]
    env.states[BED_RH] = _state("unavailable", "Bedroom Humidity")
    out = env.row(caplog, sleep=True)
    assert out == [
        'Zone "Zone 1": bedroom humidity now from no humidity source instead of "Bedroom Humidity"'
        " (sensor.bedroom_humidity, sensor on the device, no reading): no humidity sensor found on the device"
    ]
    assert "humidity" not in env.last  # the bedroom row carries none, never the hallway's
    assert env.row(caplog, sleep=True) == []  # still none: silent
    assert env.row(caplog) == []  # morning: the day room's source did not change, nothing to say
    env.states[BED_RH] = _state("61.7", "Bedroom Humidity")
    out = env.row(caplog, sleep=True)
    assert out == [
        'Zone "Zone 1": bedroom humidity now from "Bedroom Humidity" (sensor.bedroom_humidity, sensor on the device,'
        " 61.7%) instead of no humidity source: humidity sensor found on the device"
    ]
    evs = env.events()
    assert [e["role"] for e in evs] == ["indoor_sleep", "indoor_sleep"]
    assert evs[0]["to_entity"] is None and evs[0]["to_kind"] is None and evs[1]["from_entity"] is None


def test_outdoor_no_source_reason(caplog):
    env = Env(_house())
    env.row(caplog)
    env.states[OUT_RH] = _state("unavailable", "Patio Humidity")
    env.states[WEATHER] = _state("sunny", "Home", {})
    out = env.row(caplog)
    assert out[0].endswith(": no outdoor humidity sensor and no weather service humidity")


def test_an_announcement_failure_never_costs_the_chart_row(caplog):
    env = Env(_house())
    cls = _get_coordinator_class()
    with patch.object(cls, "_announce_source", side_effect=RuntimeError("boom")):
        env.row(caplog)
    assert env.last["humidity"] == 52.4 and env.last["outdoor_humidity"] == 58.25


def test_activity_row_text_and_registration():
    import tools.sim_harness.outcomes as outcomes

    assert "humidity_source_changed" in EVENT_RENDERERS
    assert "humidity_source_changed" in _NO_DEDUP
    assert "humidity_source_changed" in outcomes.UNMAPPED_PRODUCTION_EVENTS
    render = EVENT_RENDERERS["humidity_source_changed"]
    label, detail = render(
        {
            "subject": "outdoor humidity",
            "from_entity": OUT_RH,
            "from_name": "Patio Humidity",
            "to_entity": "weather.home#humidity",
            "to_name": "Home",
            "to_kind": "weather service",
            "to_rh": 33.0,
            "from_rh": None,
            "reason": "weather service (no outdoor humidity sensor)",
        },
        "celsius",
    )
    assert label == "Outdoor humidity now from Home (weather service) instead of Patio Humidity"
    assert detail == "weather service (no outdoor humidity sensor), now 33%"
    label, detail = render(
        {"subject": "bedroom humidity", "from_name": "Bedroom Humidity", "to_entity": None, "reason": "x"},
        "fahrenheit",
    )
    assert label == "Bedroom humidity: no humidity source (was Bedroom Humidity)" and detail == "x"
    assert render({}, "fahrenheit")[0]


def _class_functions():
    tree = ast.parse((PKG / "coordinator.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _callers(fns: dict, attr: str) -> set[str]:
    return {
        name
        for name, fn in fns.items()
        if any(isinstance(n, ast.Attribute) and n.attr == attr for n in ast.walk(fn)) and name != attr
    }


def test_change_tracking_is_only_reached_from_the_event_loop():
    """The announced-source map has no lock: every path into it must run on the event loop, never an executor."""
    fns = _class_functions()
    assert _callers(fns, "_note_humidity_source") == {"_read_chart_humidity"}
    assert _callers(fns, "_read_chart_humidity") == {"_read_chart_context"}
    ctx_callers = _callers(fns, "_read_chart_context")
    occ_callers = _callers(fns, "_append_occupancy_chart_row")
    assert len(ctx_callers) >= 3 and occ_callers
    for name in ctx_callers - {"_append_occupancy_chart_row"} | occ_callers:
        assert isinstance(fns[name], ast.AsyncFunctionDef), f"{name} must be a coroutine on the event loop"
    src = (PKG / "coordinator.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if "executor_job(" in line:
            for name in ("_read_chart_context", "_read_chart_humidity", "_append_occupancy_chart_row"):
                assert name not in line


def test_temperature_and_humidity_share_one_announcement_helper():
    fns = _class_functions()
    assert _callers(fns, "_announce_source") == {"_note_indoor_source", "_note_humidity_source"}
    assert _callers(fns, "_source_changed") == {"_note_indoor_source", "_note_humidity_source"}
    src = (PKG / "coordinator.py").read_text(encoding="utf-8")
    assert src.count('_LOGGER.info(\'Zone "%s": %s now from') == 1  # one writer of the switch line
