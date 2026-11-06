"""Issue #1230 R1c: one INFO line + one Activity entry per real change of the room-temperature sensor.

Occupant view: the hallway thermostat reports 69 F in whole degrees; its finer sensor reads 68.72 F. When Climate
Advisor starts reading the finer sensor (or goes back to the thermostat, or into / out of the bedroom sensor at
bedtime) the owner sees exactly one log line naming the zone, both sensors (id and friendly name), their readings and
the reason, and the dashboard's Activity log shows the same switch. Nothing is written for repeated identical readings
or while a sensor that is not in use flaps, and the startup coalesce stays quiet and ends with a single "using" line.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sys
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.ai_skills_context import _NO_DEDUP, EVENT_RENDERERS  # noqa: E402
from custom_components.climate_advisor.const import INDOOR_SENSOR_STALE_HOURS  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import CompanionSensors  # noqa: E402

NOW = _dt.datetime(2026, 11, 6, 12, 0, tzinfo=_dt.UTC)
CLIMATE = "climate.santa_maria_hallway"
COMP = "sensor.santa_maria_hallway_current_temperature"
COMP2 = "sensor.santa_maria_hallway_temperature_2"
DEDICATED = "sensor.dedicated_indoor"
SLEEP = "sensor.bedroom_temp"
COORD_LOGGER = "custom_components.climate_advisor.coordinator"
RESOLVER_LOGGER = "custom_components.climate_advisor.indoor_temp"


def _state(value, name=None, attributes=None, age_h=0):
    s = MagicMock()
    s.state = value
    s.attributes = dict(attributes or {})
    if name:
        s.attributes["friendly_name"] = name
    s.last_reported = NOW - _dt.timedelta(hours=age_h)
    s.last_updated = s.last_reported
    return s


def _states(climate=69, comp="68.72"):
    out = {
        CLIMATE: _state("heat", "Santa Maria Hallway", {"current_temperature": climate}),
        SLEEP: _state("66.4", "Bedroom"),
        DEDICATED: _state("70.2"),  # no friendly_name: falls back to the id
    }
    if comp is not None:
        out[COMP] = _state(comp, "Santa Maria Hallway Current Temperature")
    return out


class Env:
    """Bare coordinator (object.__new__) driving the real ``_refresh_indoor_sensor_health`` funnel."""

    def __init__(self, states, entities=(COMP,), *, source="climate_fallback", setting="auto", sleep_on=False):
        self.states = states
        self.sleep_on = sleep_on
        coord = object.__new__(ClimateAdvisorCoordinator)
        hass = MagicMock()
        hass.states.get = lambda eid: self.states.get(eid)
        entry = MagicMock()
        entry.title = "Zone 1"
        hass.config_entries.async_get_entry = lambda _eid: entry
        coord.hass = hass
        coord._entry_id = "01ABC"
        coord.config = {
            "climate_entity": CLIMATE,
            "indoor_temp_source": source,
            "indoor_temp_entity": DEDICATED,
            "sleep_indoor_temp_entity": SLEEP,
            "temp_unit": "fahrenheit",
            "thermostat_companion_sensor": setting,
        }
        coord._companion = CompanionSensors(entities)
        coord._startup_coalesce_active = False
        self.coord = coord
        self.emit = MagicMock()

    def tick(self, caplog):
        caplog.clear()
        with (
            patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=self.sleep_on),
            patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", return_value=NOW),
            patch.object(ClimateAdvisorCoordinator, "_emit_event", self.emit),
        ):
            self.coord._refresh_indoor_sensor_health()
        return [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "room temperature" in r.getMessage()
        ]

    def events(self):
        return [c.args[1] for c in self.emit.call_args_list if c.args[0] == "indoor_source_changed"]


@pytest.fixture(autouse=True)
def _debug(caplog):
    caplog.set_level(logging.DEBUG)


def test_companion_enters_and_leaves_one_line_each(caplog):
    env = Env(_states())
    first = env.tick(caplog)
    assert len(first) == 1 and first[0].startswith("Zone Zone 1: room temperature using ")  # startup "using"
    assert env.tick(caplog) == [] and env.tick(caplog) == []  # repeated identical readings: silent
    assert env.events() == []  # "using" is not a switch

    env.states[COMP] = _state("unavailable", "Santa Maria Hallway Current Temperature")
    out = env.tick(caplog)
    assert out == [
        'Zone Zone 1: room temperature now from "Santa Maria Hallway" (climate.santa_maria_hallway, thermostat, 69°F)'
        ' instead of "Santa Maria Hallway Current Temperature" (sensor.santa_maria_hallway_current_temperature,'
        " thermostat's finer sensor, no reading): finer sensor unavailable — using the thermostat"
    ]
    assert env.tick(caplog) == []

    env.states[COMP] = _state("68.72", "Santa Maria Hallway Current Temperature")
    out = env.tick(caplog)  # returning to the same sensor after a switch is a real change
    assert len(out) == 1
    assert (
        'now from "Santa Maria Hallway Current Temperature" (sensor.santa_maria_hallway_current_temperature,'
        ' thermostat\'s finer sensor, 68.72°F) instead of "Santa Maria Hallway" (climate.santa_maria_hallway,'
        " thermostat, 69°F): finer sensor available again" in out[0]
    )
    assert len(env.events()) == 2


def test_first_proof_line_matches_documented_shape(caplog):
    env = Env(_states(comp=None))  # companion not reporting yet
    env.tick(caplog)
    env.states[COMP] = _state("68.72", "Santa Maria Hallway Current Temperature")
    out = env.tick(caplog)
    assert out == [
        'Zone Zone 1: room temperature now from "Santa Maria Hallway Current Temperature"'
        " (sensor.santa_maria_hallway_current_temperature, thermostat's finer sensor, 68.72°F)"
        ' instead of "Santa Maria Hallway" (climate.santa_maria_hallway, thermostat, 69°F): finer sensor proven'
    ]


def test_activity_entry_content(caplog):
    env = Env(_states())
    env.tick(caplog)
    env.states[COMP] = _state("71.9", "Santa Maria Hallway Current Temperature")
    env.tick(caplog)
    (event,) = env.events()
    assert event == {
        "zone": "Zone 1",
        "from_entity": COMP,
        "from_name": "Santa Maria Hallway Current Temperature",
        "from_role": "thermostat's finer sensor",
        "from_value_f": event["from_value_f"],
        "to_entity": CLIMATE,
        "to_name": "Santa Maria Hallway",
        "to_role": "thermostat",
        "to_value_f": 69.0,
        "reason": "more than 1.0 °F apart",
    }
    # The 71.9 reading failed the consistency check, so the snapshot has the raw sensor value
    assert event["from_value_f"] == pytest.approx(71.9)


def test_two_candidates_reason(caplog):
    env = Env(_states(), entities=(COMP, COMP2))
    env.states[COMP2] = _state("68.6")
    env.tick(caplog)  # ambiguous from the start: thermostat in use
    env.states[COMP2] = _state("unavailable")
    env.tick(caplog)  # exactly one proven companion
    env.states[COMP2] = _state("68.6")
    out = env.tick(caplog)
    assert len(out) == 1 and out[0].endswith(": two candidate sensors")


def test_setting_off_reason(caplog):
    env = Env(_states())
    env.tick(caplog)
    env.coord.config["thermostat_companion_sensor"] = "off"
    out = env.tick(caplog)
    assert len(out) == 1 and out[0].endswith(": setting off")


def test_flapping_companion_while_out_of_use_is_silent(caplog):
    env = Env(_states(comp="unavailable"))
    assert len(env.tick(caplog)) == 1  # "using" the thermostat
    for value, age in (("unavailable", 0), ("68.72", INDOOR_SENSOR_STALE_HOURS + 1), ("unavailable", 0)):
        env.states[COMP] = _state(value, age_h=age)
        assert env.tick(caplog) == []
    assert env.events() == []


def test_coalesce_silent_then_one_using_line(caplog):
    env = Env(_states())
    env.coord._startup_coalesce_active = True
    assert env.tick(caplog) == []
    env.states[COMP] = _state("unavailable")  # a change during coalescing stays silent too
    assert env.tick(caplog) == []
    env.coord._startup_coalesce_active = False
    out = env.tick(caplog)
    assert len(out) == 1 and "room temperature using " in out[0] and "changed" not in out[0]
    assert '"Santa Maria Hallway" (climate.santa_maria_hallway, thermostat, 69°F)' in out[0]
    assert env.tick(caplog) == []
    assert env.events() == []


def test_nothing_known_yet_logs_nothing(caplog):
    env = Env({})  # no entity has a state
    assert env.tick(caplog) == []
    assert env.events() == []


def test_outage_and_same_sensor_return_is_not_a_switch(caplog):
    env = Env(_states(comp=None), entities=())
    env.tick(caplog)
    env.states[CLIMATE] = _state("unavailable", "Santa Maria Hallway")
    env.tick(caplog)
    env.states[CLIMATE] = _state("heat", "Santa Maria Hallway", {"current_temperature": 69})
    assert env.tick(caplog) == []
    assert env.events() == []


def test_sleep_sensor_in_and_out(caplog):
    env = Env(_states(comp=None), entities=())
    env.tick(caplog)
    env.sleep_on = True
    out = env.tick(caplog)
    assert out == [
        'Zone Zone 1: room temperature now from "Bedroom" (sensor.bedroom_temp, sleep sensor, 66.4°F)'
        ' instead of "Santa Maria Hallway" (climate.santa_maria_hallway, thermostat, 69°F): sleep window started'
    ]
    assert env.tick(caplog) == []
    env.sleep_on = False
    out = env.tick(caplog)
    assert len(out) == 1 and out[0].endswith(": sleep window ended")
    assert 'instead of "Bedroom" (sensor.bedroom_temp, sleep sensor, 66.4°F)' in out[0]
    assert [e["reason"] for e in env.events()] == ["sleep window started", "sleep window ended"]


def test_sleep_sensor_failing_and_returning(caplog):
    env = Env(_states(comp=None), entities=(), sleep_on=True)
    env.tick(caplog)
    env.states[SLEEP] = _state("unavailable", "Bedroom")
    out = env.tick(caplog)
    assert len(out) == 1 and out[0].endswith(": sleep sensor not usable (unavailable)")
    env.states[SLEEP] = _state("66.4", "Bedroom")
    out = env.tick(caplog)
    assert len(out) == 1 and out[0].endswith(": sleep sensor reporting again")


def test_companion_into_sleep_and_back(caplog):
    env = Env(_states())
    env.tick(caplog)
    env.sleep_on = True
    out = env.tick(caplog)
    assert len(out) == 1 and "(sensor.bedroom_temp, sleep sensor" in out[0] and out[0].endswith("sleep window started")
    env.sleep_on = False
    out = env.tick(caplog)
    assert len(out) == 1 and 'thermostat\'s finer sensor, 68.72°F) instead of "Bedroom"' in out[0]
    assert out[0].endswith(": sleep window ended")


def test_dedicated_sensor_role_and_switch(caplog):
    env = Env(_states(comp=None), entities=(), source="sensor")
    first = env.tick(caplog)
    assert len(first) == 1 and f'"{DEDICATED}" ({DEDICATED}, dedicated indoor sensor, 70.2°F)' in first[0]
    env.coord.config["indoor_temp_source"] = "climate_fallback"
    out = env.tick(caplog)
    assert len(out) == 1
    assert f'instead of "{DEDICATED}" ({DEDICATED}, dedicated indoor sensor, 70.2°F)' in out[0]
    assert out[0].endswith(": indoor sensor setting changed")
    env.coord.config["indoor_temp_source"] = "sensor"
    back = env.tick(caplog)  # and back to the dedicated sensor
    assert len(back) == 1 and "dedicated indoor sensor, 70.2°F) instead of" in back[0]


def test_resolver_logger_is_not_info_for_these_switches(caplog):
    env = Env(_states())
    env.tick(caplog)
    env.sleep_on = True
    env.tick(caplog)
    env.sleep_on = False
    env.states[COMP] = _state("unavailable")
    env.tick(caplog)
    resolver = [r for r in caplog.records if r.name == RESOLVER_LOGGER]
    assert not [r for r in resolver if r.levelno >= logging.INFO]
    assert not [
        r for r in caplog.records if r.name == COORD_LOGGER and "Indoor reading source changed" in r.getMessage()
    ]


def test_note_indoor_companion_writes_no_switch_line(caplog):
    env = Env(_states())
    env.tick(caplog)
    env.states[COMP] = _state("unavailable")
    env.tick(caplog)
    assert not [r for r in caplog.records if "finer sensor entity=" in r.getMessage()]
    assert env.coord._indoor_companion_in_use is None


def test_activity_entry_registered_and_rendered():
    assert "indoor_source_changed" in EVENT_RENDERERS
    assert "indoor_source_changed" in _NO_DEDUP
    label, detail = EVENT_RENDERERS["indoor_source_changed"](
        {
            "to_name": "Santa Maria Hallway Current Temperature",
            "to_role": "thermostat's finer sensor",
            "from_name": "Santa Maria Hallway",
            "reason": "finer sensor proven",
            "to_value_f": 68.72,
            "from_value_f": 69.0,
        },
        "fahrenheit",
    )
    assert label == (
        "Room temperature now from Santa Maria Hallway Current Temperature (thermostat's finer sensor)"
        " instead of Santa Maria Hallway"
    )
    assert detail == "finer sensor proven, now 68.7°F, was 69.0°F"
    assert EVENT_RENDERERS["indoor_source_changed"]({}, "fahrenheit")[0]
    from tools.sim_harness import outcomes

    assert "indoor_source_changed" in outcomes.UNMAPPED_PRODUCTION_EVENTS
