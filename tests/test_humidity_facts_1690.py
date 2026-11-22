"""Issue #1690 (plan #1507 H0) and Issue #1689: the decision dew point and the reconnect-glitch rule.

Occupant view: before humidity may change what the house does (later #1507 waves), Climate Advisor must know the
house's moisture reliably: a dew point from the same device's humidity and temperature, never a reconnect glitch, a
stale reading or a non-% sensor, the bedroom in the sleep window with the day room as fallback, and nothing paired
with the thermostat while it reads falsely high after a heating shutdown. Unknown always means today's behaviour.
#1689: after an outage the outdoor sensor can report 0 % / 32.0 F for a few seconds; that must never become the
outside temperature a decision reads.

Drives the real coordinator methods (``_read_dew_point``, ``_dew_point_fact``, ``_sample_humidity_facts``,
``engine_facts``, ``_read_outdoor_sensor``, ``_get_outdoor_temp``), the real resolver over the faked registry of
``tests/test_chart_humidity_1414.py`` (its house and helpers are reused), the real ``StaticEngineInputs`` and the
FakeHass harness. Every test pairs a rejected case with an accepted one, so deleting the feature fails it.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.engine_inputs import (  # noqa: E402
    EngineFacts,
    StaticEngineInputs,
    build_engine_facts,
)
from custom_components.climate_advisor.feels_like import dew_point_f  # noqa: E402
from custom_components.climate_advisor.humidity_facts import (  # noqa: E402
    RESERVED_EVENT_TYPES,
    SIGNAL_INDOOR,
    SIGNAL_OUTDOOR,
    DewPoint,
    LatchSpec,
    ReconnectWatch,
    latch_above,
    paired_glitch,
    smoothed_dew_point,
    update_latches,
)
from custom_components.climate_advisor.thermostat_recal import ThermostatRecal  # noqa: E402
from tests.test_chart_humidity_1414 import (  # noqa: E402
    BED_RH,
    BED_TEMP,
    CLIMATE,
    HALL_RH,
    NOW,
    OUT_RH,
    OUT_TEMP,
    REGISTRY,
    _coord,
    _coord_mod,
    _fake_er,
    _house,
    _state,
)

PCT = {"unit_of_measurement": "%"}


@pytest.fixture
def registry():
    """The 1414 house's devices (thermostat, bedroom, patio), as the entity registry reports them."""
    with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er(REGISTRY)):
        yield


DEG_F = {"unit_of_measurement": "°F"}
DEG_C = {"unit_of_measurement": "°C"}
CHOSEN_RH = "sensor.chosen_humidity"


def _house_pct(**over):
    """The 1414 house with real units on its humidity and temperature sensors."""
    base = {
        HALL_RH: _state("52.4", PCT),
        BED_RH: _state("61.7", PCT),
        OUT_RH: _state("58.25", PCT),
        BED_TEMP: _state("71.2", DEG_F),
        OUT_TEMP: _state("92.0", DEG_F),
    }
    base.update(over)
    return _house(**base)


def _warm(coord, *, sleep=False):
    """One 5-minute sample: warms the device lookups the pure readers use (the event loop's job)."""
    with patch.object(_coord_mod(), "_in_sleep_window", return_value=sleep):
        coord._sample_humidity_facts(NOW)


def _read(coord, signal, *, sleep=False, now=NOW):
    with patch.object(_coord_mod(), "_in_sleep_window", return_value=sleep):
        return coord._read_dew_point(signal, now)


# ── the one dew-point function, the glitch rule, the latch ──────────────────────────────────────────────────────────
class TestDewPointFunction:
    def test_magnus_values(self):
        assert dew_point_f(76.0, 60.0) == pytest.approx(61.1, abs=0.1)  # muggy
        assert dew_point_f(76.0, 35.0) == pytest.approx(46.4, abs=0.15)  # dry
        assert dew_point_f(70.0, 100.0) == pytest.approx(70.0, abs=1e-9)  # saturated air: dew point = temperature

    def test_no_dew_point_without_moisture_or_input(self):
        assert dew_point_f(76.0, 0.0) is None  # 0 % has no dew point (and is the reconnect glitch)
        assert dew_point_f(76.0, 100.5) is None
        assert dew_point_f(None, 50.0) is None and dew_point_f(76.0, None) is None
        assert dew_point_f(float("nan"), 50.0) is None


class TestPairedGlitch:
    def test_reconnect_pair_rejected_real_pair_kept(self):
        assert paired_glitch(32.0, 0.0) is True
        assert paired_glitch(52.9, 96.7) is False
        assert paired_glitch(32.0, 41.0) is False  # a real freezing reading with real humidity
        assert paired_glitch(32.0, None) is False  # no same-device humidity: only the second rule can fire
        # the first reading after unavailable is a glitch only when it carries a reconnect default
        assert paired_glitch(32.0, None, first_after_unavailable=True) is True
        # a real reconnect temperature is used at once, even while its humidity still reads the 0 % default
        assert paired_glitch(52.9, 0.0, first_after_unavailable=True) is False
        assert paired_glitch(52.9, 96.7, first_after_unavailable=True) is False

    def test_reconnect_watch_holds_only_the_first_state_after_unavailable(self):
        watch = ReconnectWatch()
        back = SimpleNamespace(state="32.0", last_changed=NOW - _dt.timedelta(seconds=3))
        watch.note(OUT_TEMP, SimpleNamespace(state="unavailable"), back)
        assert watch.first_after_unavailable(OUT_TEMP, back, NOW) is True
        assert watch.first_after_unavailable(OUT_TEMP, back, NOW + _dt.timedelta(seconds=60)) is False  # settled
        real = SimpleNamespace(state="52.9", last_changed=NOW)
        watch.note(OUT_TEMP, back, real)  # the real value 4 s later
        assert watch.first_after_unavailable(OUT_TEMP, real, NOW) is False
        # a scenario's first write (no previous state) is not an outage
        watch.note(OUT_TEMP, None, back)
        assert watch.first_after_unavailable(OUT_TEMP, back, NOW) is False

    def test_reconnect_through_unknown_is_still_a_reconnect(self):
        """unavailable -> unknown -> 32.0: the first real-looking reading after the outage is still held back."""
        watch = ReconnectWatch()
        unknown = SimpleNamespace(state="unknown", last_changed=NOW - _dt.timedelta(seconds=4))
        back = SimpleNamespace(state="32.0", last_changed=NOW - _dt.timedelta(seconds=3))
        watch.note(OUT_TEMP, SimpleNamespace(state="unavailable"), unknown)
        watch.note(OUT_TEMP, unknown, back)
        assert watch.first_after_unavailable(OUT_TEMP, back, NOW) is True
        assert paired_glitch(32.0, None, first_after_unavailable=watch.first_after_unavailable(OUT_TEMP, back, NOW))

    def test_reconnect_watch_polling_path(self):
        watch = ReconnectWatch()
        watch.observe(HALL_RH, SimpleNamespace(state="unavailable"))
        back = SimpleNamespace(state="0.0", last_changed=NOW - _dt.timedelta(seconds=2))
        watch.observe(HALL_RH, back)
        assert watch.first_after_unavailable(HALL_RH, back, NOW) is True
        fresh = ReconnectWatch()
        fresh.observe(HALL_RH, back)  # never seen unavailable
        assert fresh.first_after_unavailable(HALL_RH, back, NOW) is False


class TestLatch:
    def test_on_at_on_off_below_off(self):
        assert latch_above(64.9, 65.0, 64.0, False) is False
        assert latch_above(65.0, 65.0, 64.0, False) is True
        assert latch_above(64.5, 65.0, 64.0, True) is True  # inside the hysteresis band: stays on
        assert latch_above(64.0, 65.0, 64.0, True) is False
        assert latch_above(None, 65.0, 64.0, True) is False  # unknown releases: today's behaviour
        with pytest.raises(ValueError):
            latch_above(60.0, 60.0, 61.0, False)

    def test_update_latches_steps_every_spec(self):
        specs = (LatchSpec("humid_air", SIGNAL_OUTDOOR, 65.0, 64.0), LatchSpec("overcool", SIGNAL_INDOOR, 60.0, 58.0))
        state = update_latches(specs, {SIGNAL_OUTDOOR: 66.0, SIGNAL_INDOOR: 59.0}, {})
        assert state == {"humid_air": True, "overcool": False}
        state = update_latches(specs, {SIGNAL_OUTDOOR: 64.5, SIGNAL_INDOOR: None}, state)
        assert state == {"humid_air": True, "overcool": False}


# ── the coordinator's decision reader ───────────────────────────────────────────────────────────────────────────────
class TestIndoorReader:
    def test_thermostat_humidity_pairs_with_the_thermostat_temperature(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord)
        dp = _read(coord, SIGNAL_INDOOR)
        assert dp.known and dp.humidity_source == HALL_RH and dp.temp_source == CLIMATE
        assert dp.dp_f == pytest.approx(dew_point_f(76.0, 52.4))

    def test_glitch_rejected_real_reading_accepted(self, registry, frozen_now):
        frozen_now(NOW)
        glitch = _house_pct(**{HALL_RH: _state("0.0", PCT)})
        glitch[CLIMATE] = _state("cool", {"current_temperature": 32.0, "current_humidity": 0.0})
        coord = _coord(glitch)
        _warm(coord)
        assert _read(coord, SIGNAL_INDOOR).reason == "glitch"
        coord.hass.states.get = _house_pct().get  # the real values arrive
        assert _read(coord, SIGNAL_INDOOR).known

    def test_stale_ignored_fresh_used(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct(**{HALL_RH: _state("52.4", PCT, age_h=2.5)}))  # > 120 min
        _warm(coord)
        assert _read(coord, SIGNAL_INDOOR).reason == "stale"
        coord.hass.states.get = _house_pct(**{HALL_RH: _state("52.4", PCT, age_h=1.5)}).get
        assert _read(coord, SIGNAL_INDOOR).known

    def test_recalibration_makes_the_thermostat_dew_point_unknown(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct())
        coord.config.pop("sleep_indoor_temp_entity")  # no other room sensor to take over the reading
        coord._recal = ThermostatRecal()
        _warm(coord)
        assert _read(coord, SIGNAL_INDOOR).known
        coord._recal.active = True  # the thermostat reads 6-7 F high after a heating shutdown (#1676)
        dp = _read(coord, SIGNAL_INDOOR)
        assert dp.reason == "recalibrating" and dp.temp_source == CLIMATE

    def test_recalibration_reads_the_room_sensor_that_took_over(self, registry, frozen_now):
        """With a bedroom sensor configured, #1676 moves the room reading there, and the dew point follows it."""
        frozen_now(NOW)
        coord = _coord(_house_pct())
        coord._recal = ThermostatRecal()
        coord._recal.active = True
        _warm(coord)
        dp = _read(coord, SIGNAL_INDOOR)
        assert dp.known and dp.temp_source == BED_TEMP

    def test_non_percent_humidity_rejected(self, registry, frozen_now):
        frozen_now(NOW)
        for attrs in ({"unit_of_measurement": "g/m³"}, {}):
            coord = _coord(_house_pct(**{HALL_RH: _state("52.4", attrs)}))
            _warm(coord)
            assert _read(coord, SIGNAL_INDOOR).reason == "wrong_unit"
        coord = _coord(_house_pct())
        _warm(coord)
        assert _read(coord, SIGNAL_INDOOR).known

    def test_sleep_window_reads_the_bedroom_then_falls_back_to_the_room(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord, sleep=True)
        bed = _read(coord, SIGNAL_INDOOR, sleep=True)
        assert bed.humidity_source == BED_RH and bed.temp_source == BED_TEMP
        assert bed.dp_f == pytest.approx(dew_point_f(71.2, 61.7))
        coord.hass.states.get = _house_pct(**{BED_RH: None}).get  # the bedroom has no humidity now
        room = _read(coord, SIGNAL_INDOOR, sleep=True)
        assert room.known and room.humidity_source == HALL_RH  # dew point is house-wide (#1507 F9)

    def test_celsius_install_matches_its_fahrenheit_twin(self, registry, frozen_now):
        frozen_now(NOW)
        f_coord = _coord(_house_pct())
        _warm(f_coord)
        c_states = _house_pct(
            **{
                OUT_TEMP: _state(str((92.0 - 32) * 5 / 9), DEG_C),
                BED_TEMP: _state(str((71.2 - 32) * 5 / 9), DEG_C),
            }
        )
        c_states[CLIMATE] = _state("cool", {"current_temperature": (76.0 - 32) * 5 / 9, "current_humidity": 41.0})
        c_coord = _coord(c_states, temp_unit="celsius")
        _warm(c_coord)
        for signal in (SIGNAL_INDOOR, SIGNAL_OUTDOOR):
            assert _read(c_coord, signal).dp_f == pytest.approx(_read(f_coord, signal).dp_f, abs=1e-6)

    def test_one_sensor_gives_one_temperature(self, registry, frozen_now):
        """The dew point pairs the humidity with the SAME temperature the outdoor reader gives the decisions, read the
        same way (configured unit), on a Fahrenheit and on a Celsius install alike."""
        frozen_now(NOW)
        for unit, raw in (("fahrenheit", "92.0"), ("celsius", "33.0")):
            coord = _coord(_house_pct(**{OUT_TEMP: _state(raw, DEG_F)}), temp_unit=unit)
            _warm(coord)
            read = _read(coord, SIGNAL_OUTDOOR)
            assert read.temp_f == coord._read_outdoor_sensor()[0]
            assert read.dp_f == pytest.approx(dew_point_f(read.temp_f, 58.25))


class TestOutdoorReader:
    def test_outdoor_sensor_then_weather_dew_point(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord)
        dp = _read(coord, SIGNAL_OUTDOOR)
        assert dp.humidity_source == OUT_RH and dp.temp_source == OUT_TEMP
        states = _house_pct(**{OUT_RH: None})
        states["weather.home"] = _state("sunny", {"humidity": 80.0, "dew_point": 18.0, "temperature_unit": "°C"})
        coord.hass.states.get = states.get
        weather = _read(coord, SIGNAL_OUTDOOR)
        assert weather.temp_source == "weather.home#dew_point" and weather.dp_f == pytest.approx(64.4)

    def test_outdoor_goes_stale_with_the_outdoor_temperature_rule(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct(**{OUT_RH: _state("58.25", PCT, age_h=3.0)}))  # fine outdoors (6 h)
        _warm(coord)
        assert _read(coord, SIGNAL_OUTDOOR).known
        coord.hass.states.get = _house_pct(**{OUT_RH: _state("58.25", PCT, age_h=7.0)}).get
        assert _read(coord, SIGNAL_OUTDOOR).reason == "stale"


class TestFactsAndMedian:
    def test_engine_facts_carry_the_15_minute_median(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord)
        history = coord._dew_point_history
        history.clear(SIGNAL_INDOOR)
        history.add(SIGNAL_INDOOR, NOW - _dt.timedelta(minutes=30), 99.0, HALL_RH)  # outside the window
        history.add(SIGNAL_INDOOR, NOW - _dt.timedelta(minutes=10), 50.0, HALL_RH)
        history.add(SIGNAL_INDOOR, NOW - _dt.timedelta(minutes=5), 51.0, HALL_RH)
        current = dew_point_f(76.0, 52.4)
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            facts = coord.engine_facts(NOW)
        assert facts.indoor_dew_point_f == pytest.approx(51.0)  # median of 50, 51, current (~57)
        assert current > 51.0
        assert facts.outdoor_dew_point_f is not None

    def test_a_glitch_keeps_the_window_a_lasting_unknown_does_not(self):
        glitch = DewPoint(None, reason="glitch")
        assert smoothed_dew_point(glitch, [55.0, 56.0]) == pytest.approx(55.5)
        assert smoothed_dew_point(glitch, []) is None
        assert smoothed_dew_point(DewPoint(None, reason="stale"), [55.0, 56.0]) is None
        assert smoothed_dew_point(DewPoint(57.0), [55.0, 56.0]) == pytest.approx(56.0)

    def test_missing_inputs_leave_each_fact_unknown_on_its_own(self, registry, frozen_now):
        """The plan's missing-input table: indoor and outdoor are independent (one unknown never hides the other)."""
        frozen_now(NOW)
        no_indoor = _house_pct(**{HALL_RH: None})
        no_indoor[CLIMATE] = _state("cool", {"current_temperature": 76.0})
        coord = _coord(no_indoor, indoor_temp_source="climate_fallback")
        coord.config.pop("sleep_indoor_temp_entity")
        _warm(coord)
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            facts = coord.engine_facts(NOW)
        assert facts.indoor_dew_point_f is None and facts.outdoor_dew_point_f is not None
        coord2 = _coord(_house_pct(**{OUT_RH: None}))
        _warm(coord2)
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            facts2 = coord2.engine_facts(NOW)
        assert facts2.indoor_dew_point_f is not None and facts2.outdoor_dew_point_f is None

    def test_static_inputs_build_through_the_one_builder(self):
        now = NOW
        assert StaticEngineInputs().build_facts(now) == EngineFacts(as_of=now)
        facts = StaticEngineInputs(
            today_peak=84.0, today_sample_count=3, indoor_f=76.0, humidity=60.0, outdoor_f=70.0, outdoor_humidity=90.0
        ).build_facts(now)
        assert (facts.outdoor_today_peak, facts.outdoor_today_sample_count) == (84.0, 3)
        assert facts.indoor_dew_point_f == pytest.approx(dew_point_f(76.0, 60.0))
        assert facts.outdoor_dew_point_f == pytest.approx(dew_point_f(70.0, 90.0))
        built = build_engine_facts(
            now, hourly_forecast=None, outdoor_history=[("a", 70.0)], tou_av=None, humid_latches=frozenset({"x"})
        )
        assert built.humid_latches == frozenset({"x"}) and built.outdoor_today_peak == 70.0


# ── #1689: the outdoor temperature path ─────────────────────────────────────────────────────────────────────────────
class TestOutdoorReconnectGlitch1689:
    def test_paired_0_percent_32_f_is_not_the_outside_temperature(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house_pct(**{OUT_TEMP: _state("32.0", DEG_F), OUT_RH: _state("0.0", PCT)}))
        _warm(coord)
        assert coord._read_outdoor_sensor() == (None, "implausible")
        # the weather service answers instead (its live attribute: no hourly forecast in this house)
        assert coord._get_outdoor_temp({"temperature": 50.0}, weather_down=False) == pytest.approx(50.0)
        coord.hass.states.get = _house_pct(**{OUT_TEMP: _state("52.9", DEG_F), OUT_RH: _state("96.7", PCT)}).get
        assert coord._read_outdoor_sensor() == (52.9, None)

    def test_first_reading_after_unavailable_waits_for_the_next(self, registry, frozen_now):
        frozen_now(NOW)
        back = _state("32.0", DEG_F)
        back.last_changed = NOW - _dt.timedelta(seconds=3)
        coord = _coord(_house_pct(**{OUT_TEMP: back, OUT_RH: None}))  # humidity not back yet
        coord._reconnect_watch_for().note(OUT_TEMP, SimpleNamespace(state="unavailable"), back)
        assert coord._read_outdoor_sensor() == (None, "implausible")
        frozen_now(NOW + _dt.timedelta(seconds=40))  # a real 32 F that stays is used once it has settled
        assert coord._read_outdoor_sensor() == (32.0, None)

    def test_a_real_first_reading_with_humidity_still_at_0_percent_is_used(self, registry, frozen_now):
        """Review A2: a real temperature is not rejected because its humidity still reads the 0 % default."""
        frozen_now(NOW)
        back = _state("52.9", DEG_F)
        back.last_changed = NOW - _dt.timedelta(seconds=3)
        coord = _coord(_house_pct(**{OUT_TEMP: back, OUT_RH: _state("0.0", PCT)}))
        _warm(coord)
        coord._reconnect_watch_for().note(OUT_TEMP, SimpleNamespace(state="unavailable"), back)
        assert coord._read_outdoor_sensor() == (52.9, None)
        assert _read(coord, SIGNAL_OUTDOOR).reason == "glitch"  # the humidity side drops only that reading

    def test_a_real_first_reading_after_unavailable_is_used_at_once(self, registry, frozen_now):
        frozen_now(NOW)
        back = _state("52.9", DEG_F)
        back.last_changed = NOW - _dt.timedelta(seconds=3)
        coord = _coord(_house_pct(**{OUT_TEMP: back, OUT_RH: None}))
        coord._reconnect_watch_for().note(OUT_TEMP, SimpleNamespace(state="unavailable"), back)
        assert coord._read_outdoor_sensor() == (52.9, None)


# ── the harness: humidity on temp_update ────────────────────────────────────────────────────────────────────────────
def _scenario(**config) -> dict:
    return {
        "name": "humidity_facts_1690",
        "config": {
            "comfort_heat": 68,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.outdoor_temp",
            **config,
        },
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "events": [],
        "assertions": [],
    }


class TestHarnessHumidity:
    def test_temp_update_humidity_reaches_the_facts(self):
        from tools.sim_harness import humidity_devices
        from tools.sim_harness.run_production import FakeHassSession

        with FakeHassSession(_scenario(), use_coordinator=True) as session:
            start = session.now()
            session.feed(
                {"time": start.isoformat(), "type": "temp_update", "indoor_f": 76.0, "outdoor_f": 70.0}
            )  # no humidity: nothing known
            assert session.coordinator.engine_facts(session.now()).indoor_dew_point_f is None
            session.feed(
                {
                    "time": start.isoformat(),
                    "type": "temp_update",
                    "indoor_f": 76.0,
                    "outdoor_f": 70.0,
                    "humidity": 60.0,
                    "outdoor_humidity": 90.0,
                }
            )
            facts = session.coordinator.engine_facts(session.now())
            assert facts.indoor_dew_point_f == pytest.approx(dew_point_f(76.0, 60.0))
            assert facts.outdoor_dew_point_f == pytest.approx(dew_point_f(70.0, 90.0))
            stamped = session.fake_hass.states.get(humidity_devices.INDOOR_HUMIDITY_ENTITY)
            assert stamped.last_reported == session.now()
            # the thermostat itself is never stamped (the goldens' staleness checks stay off)
            assert session.fake_hass.states.get(session.coordinator.config["climate_entity"]).last_reported is None
            session.advance_to(start + _dt.timedelta(hours=3))  # no new humidity report: indoor goes stale
            assert session.coordinator.engine_facts(session.now()).indoor_dew_point_f is None

    def test_outdoor_humidity_needs_an_outdoor_sensor(self):
        from tools.sim_harness.run_production import FakeHassSession

        session = FakeHassSession(_scenario(outdoor_temp_source="weather_service"), use_coordinator=True)
        with session, pytest.raises(ValueError, match="outdoor_humidity"):
            session.feed(
                {
                    "time": session.now().isoformat(),
                    "type": "temp_update",
                    "outdoor_f": 70.0,
                    "outdoor_humidity": 90,
                }
            )


def test_reserved_event_types_are_registered_unmapped():
    """The later #1507 lanes' event names are taken now and can never shadow a real outcome."""
    from tools.sim_harness import outcomes

    assert set(RESERVED_EVENT_TYPES) <= outcomes.UNMAPPED_PRODUCTION_EVENTS
    doc = (Path(__file__).resolve().parent.parent / "docs" / "activity-report-table.md").read_text(encoding="utf-8")
    assert all(f"`{name}`" in doc for name in RESERVED_EVENT_TYPES)
    json.dumps(RESERVED_EVENT_TYPES)  # plain names


# ── the pure reader's remaining paths (no coordinator) ──────────────────────────────────────────────────────────────
def _st(state, attrs=None, *, age_h=0.0, changed=None):
    stamp = NOW - _dt.timedelta(hours=age_h)
    return SimpleNamespace(
        state=state, attributes=attrs or {}, last_updated=stamp, last_reported=stamp, last_changed=changed or stamp
    )


def _pure(states, role="indoor_day", *, chosen=None, sources=(), fallback=None, companions=None, temps=None, **kw):
    from custom_components.climate_advisor.humidity_facts import read_dew_point

    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return read_dew_point(
        hass,
        role,
        chosen=chosen,
        source_entities=sources,
        fallback_entity=fallback,
        discover=lambda eid, excl: (companions or {}).get(eid, ()),
        discover_temp=lambda eid: (temps or {}).get(eid, ()),
        unit=kw.pop("unit", "fahrenheit"),
        stale_hours=kw.pop("stale_hours", 2.0),
        now=kw.pop("now", NOW),
        **kw,
    )


class TestPureReaderPaths:
    def test_climate_attribute_pairs_with_its_own_temperature(self):
        states = {CLIMATE: _st("cool", {"current_temperature": 76.0, "current_humidity": 60.0})}
        dp = _pure(states, sources=(CLIMATE,), fallback=CLIMATE)
        assert dp.known and dp.temp_source == CLIMATE and dp.dp_f == pytest.approx(dew_point_f(76.0, 60.0))
        states[CLIMATE] = _st("cool", {"current_temperature": 76.0, "current_humidity": 60.0}, age_h=3.0)
        assert _pure(states, sources=(CLIMATE,), fallback=CLIMATE).reason == "stale"
        states[CLIMATE] = _st("cool", {"current_temperature": 120.0, "current_humidity": 60.0})
        assert _pure(states, sources=(CLIMATE,), fallback=CLIMATE).reason == "implausible"
        states[CLIMATE] = _st("cool", {"current_humidity": 60.0})
        assert _pure(states, sources=(CLIMATE,), fallback=CLIMATE).reason == "no_reading"

    def test_weather_temperature_when_it_has_no_dew_point(self):
        weather = "weather.home"
        states = {weather: _st("sunny", {"humidity": 90.0, "temperature": 21.0, "temperature_unit": "°C"})}
        dp = _pure(states, "outdoor", fallback=weather, stale_hours=6.0)
        assert dp.temp_source == f"{weather}#temperature"
        assert dp.dp_f == pytest.approx(dew_point_f(21.0 * 9 / 5 + 32, 90.0))
        states[weather] = _st("sunny", {"humidity": 90.0, "dew_point": 400.0})
        assert _pure(states, "outdoor", fallback=weather, stale_hours=6.0).reason == "implausible"

    def test_chosen_entity_needs_a_temperature_on_its_device(self):
        states = {CHOSEN_RH: _st("50.0", PCT), "sensor.chosen_temp": _st("75.0", DEG_F)}
        assert _pure(states, chosen=CHOSEN_RH).reason == "no_temperature"
        dp = _pure(states, chosen=CHOSEN_RH, temps={CHOSEN_RH: ("sensor.chosen_temp",)})
        assert dp.known and dp.temp_source == "sensor.chosen_temp"
        states["sensor.chosen_temp"] = _st("unavailable")
        assert _pure(states, chosen=CHOSEN_RH, temps={CHOSEN_RH: ("sensor.chosen_temp",)}).reason == "unavailable"

    def test_freshness_applies_to_the_humidity_and_its_temperature_but_not_a_helper(self):
        comp = {BED_TEMP: (BED_RH,)}
        states = {BED_RH: _st("60.0", PCT, age_h=3.0), BED_TEMP: _st("71.0", DEG_F)}
        assert _pure(states, sources=(BED_TEMP,), companions=comp).reason == "stale"
        states = {BED_RH: _st("60.0", PCT), BED_TEMP: _st("71.0", DEG_F, age_h=3.0)}
        assert _pure(states, sources=(BED_TEMP,), companions=comp).reason == "stale"
        helper = "input_number.bedroom_temp"
        states = {BED_RH: _st("60.0", PCT), helper: _st("71.0", DEG_F, age_h=30.0)}
        assert _pure(states, sources=(helper,), companions={helper: (BED_RH,)}).known

    def test_climate_unavailable_and_reconnect_on_the_temperature_side(self):
        comp = {CLIMATE: (HALL_RH,)}
        states = {HALL_RH: _st("50.0", PCT), CLIMATE: _st("unavailable")}
        assert _pure(states, sources=(CLIMATE,), companions=comp).reason == "unavailable"
        back = _st("cool", {"current_temperature": 32.0}, changed=NOW - _dt.timedelta(seconds=2))
        states[CLIMATE] = back
        watch = ReconnectWatch()
        watch.note(CLIMATE, SimpleNamespace(state="unavailable"), back)
        assert _pure(states, sources=(CLIMATE,), companions=comp, reconnect=watch).reason == "glitch"
        real = _st("cool", {"current_temperature": 75.0}, changed=NOW - _dt.timedelta(seconds=2))
        states[CLIMATE] = real
        watch.note(CLIMATE, SimpleNamespace(state="unavailable"), real)
        assert _pure(states, sources=(CLIMATE,), companions=comp, reconnect=watch).known

    def test_never_raises(self):
        broken = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: 1 / 0))
        from custom_components.climate_advisor.humidity_facts import read_dew_point

        dp = read_dew_point(
            broken,
            "indoor_day",
            chosen=None,
            source_entities=(CLIMATE,),
            fallback_entity=CLIMATE,
            discover=lambda e, x: (),
            discover_temp=lambda e: (),
            unit="fahrenheit",
            stale_hours=2.0,
            now=NOW,
        )
        assert dp.known is False


class TestStateHelpersEdges:
    def test_reconnect_watch_odd_clocks_and_repeats(self):
        watch = ReconnectWatch()
        naive = SimpleNamespace(state="1.0", last_changed=_dt.datetime(2026, 7, 14, 15, 0))
        watch.note(OUT_TEMP, SimpleNamespace(state="unavailable"), naive)
        assert watch.first_after_unavailable(OUT_TEMP, naive, NOW) is False  # naive vs aware: not the same clock
        assert watch.first_after_unavailable(None, naive, NOW) is False
        watch.observe(HALL_RH, SimpleNamespace(state="unavailable"))
        first = SimpleNamespace(state="5.0", last_changed=NOW)
        watch.observe(HALL_RH, first)
        watch.observe(HALL_RH, SimpleNamespace(state="unavailable"))
        watch.observe(HALL_RH, SimpleNamespace(state="unknown"))  # still not back
        assert watch.first_after_unavailable(HALL_RH, first, NOW) is False

    def test_history_window_ignores_odd_clocks(self):
        from custom_components.climate_advisor.humidity_facts import DewPointHistory

        history = DewPointHistory(_dt.timedelta(minutes=15))
        history.add(SIGNAL_INDOOR, _dt.datetime(2026, 7, 14, 14, 55), 50.0)  # naive: dropped from the window
        history.add(SIGNAL_INDOOR, NOW, 51.0)
        assert history.recent(SIGNAL_INDOOR, NOW) == [51.0]
        assert history.recent(SIGNAL_OUTDOOR, NOW) == []
        history.add(SIGNAL_OUTDOOR, NOW - _dt.timedelta(minutes=40), 60.0)
        history.add(SIGNAL_OUTDOOR, NOW, 61.0)  # the 40-minute-old sample is pruned
        assert history.recent(SIGNAL_OUTDOOR, NOW + _dt.timedelta(minutes=30)) == []
        assert len(history._samples[SIGNAL_OUTDOOR]) == 1

    def test_dew_point_input_edges(self):
        assert dew_point_f(True, 50.0) is None  # type: ignore[arg-type]
        assert dew_point_f("x", 50.0) is None  # type: ignore[arg-type]
        assert dew_point_f(-500.0, 50.0) is None  # below the formula's pole


class TestReviewFollowUps:
    def test_a_lone_0_percent_reading_keeps_the_window(self, registry, frozen_now):
        """Review A5: a thermostat reconnect's 0 % drops only that reading; the 15-minute median still answers."""
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord)  # one real sample
        real = dew_point_f(76.0, 52.4)
        coord.hass.states.get = _house_pct(**{HALL_RH: _state("0.0", PCT)}).get
        assert _read(coord, SIGNAL_INDOOR).reason == "glitch"
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            coord._sample_humidity_facts(NOW)  # the sample tick keeps the window on a glitch
            fact = coord._dew_point_fact(SIGNAL_INDOOR, NOW)
        assert fact.dp_f == pytest.approx(real) and fact.humidity_source == HALL_RH

    def test_a_new_source_restarts_the_window_and_the_fact_names_it(self, registry, frozen_now):
        """Review B7: the median never mixes two rooms; the fact says which sensor it came from."""
        from custom_components.climate_advisor.humidity_facts import DewPointHistory

        history = DewPointHistory(_dt.timedelta(minutes=15))
        history.add(SIGNAL_INDOOR, NOW - _dt.timedelta(minutes=5), 50.0, BED_RH)
        history.add(SIGNAL_INDOOR, NOW, 60.0, HALL_RH)  # the sleep window ended: another sensor
        assert history.recent(SIGNAL_INDOOR, NOW) == [60.0]
        assert history.recent(SIGNAL_INDOOR, NOW, BED_RH) == []
        frozen_now(NOW)
        coord = _coord(_house_pct())
        _warm(coord)
        coord._dew_point_history.add(SIGNAL_INDOOR, NOW - _dt.timedelta(minutes=5), 10.0, BED_RH)  # another room
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            facts = coord.engine_facts(NOW)
        assert facts.indoor_dew_point_f == pytest.approx(dew_point_f(76.0, 52.4))  # the bedroom sample is not used
        assert facts.indoor_dew_point_source == HALL_RH and facts.outdoor_dew_point_source == OUT_RH

    def test_outdoor_sensor_follows_every_outdoor_f_once_humidity_was_seen(self):
        """Review B2: after one outdoor_humidity event, a later outdoor_f is not reverted by the 5-minute refresh."""
        from tools.sim_harness.run_production import FakeHassSession

        with FakeHassSession(_scenario(), use_coordinator=True) as session:
            start = session.now()
            session.feed(
                {"time": start.isoformat(), "type": "temp_update", "outdoor_f": 70.0, "outdoor_humidity": 90.0}
            )
            session.advance_to(start + _dt.timedelta(minutes=1))
            session.feed({"time": session.now().isoformat(), "type": "temp_update", "outdoor_f": 60.0})
            session.advance_to(start + _dt.timedelta(minutes=7))  # past a 5-minute outdoor refresh
            assert session.coordinator._last_outdoor_temp == pytest.approx(60.0)
            facts = session.coordinator.engine_facts(session.now())
            assert facts.outdoor_dew_point_f == pytest.approx(dew_point_f(60.0, 90.0))

    def test_engine_only_tracks_temperatures_on_every_event(self):
        """Review B3: engine-only facts pair the last humidity with the CURRENT temperature."""
        from tools.sim_harness.run_production import FakeHassSession

        scenario = _scenario()
        scenario["use_coordinator"] = False
        with FakeHassSession(scenario, use_coordinator=False) as session:
            now = session.now()
            session.feed({"time": now.isoformat(), "type": "temp_update", "indoor_f": 76.0, "humidity": 60.0})
            session.feed({"time": now.isoformat(), "type": "temp_update", "indoor_f": 70.0})
            facts = session.engine._inputs.build_facts(now)
            assert facts.indoor_dew_point_f == pytest.approx(dew_point_f(70.0, 60.0))


_EMIT_PATTERNS = (
    r'_emit_event(?:_callback)?\s*\(\s*["\']({name})["\']',
    r'event_type\s*=\s*["\']({name})["\']',
    r'["\']type["\']\s*:\s*["\']({name})["\']',
)


def _emitted_reserved(sources: dict[str, str]) -> set[str]:
    import re

    found: set[str] = set()
    for name in RESERVED_EVENT_TYPES:
        for pattern in _EMIT_PATTERNS:
            regex = re.compile(pattern.format(name=re.escape(name)))
            if any(regex.search(text) for text in sources.values()):
                found.add(name)
    return found


def test_a_reserved_event_must_be_mapped_when_production_first_emits_it():
    """Review A1 (rule 8): the reserved names sit in UNMAPPED_PRODUCTION_EVENTS only while nothing emits them.

    The lane that first emits one must map it in tools/sim_harness/outcomes.py, or move it to a conscious unmapped
    registration with its own rule-8 note, and take it off humidity_facts.RESERVED_EVENT_TYPES: an overcool that
    commands a setpoint must not be skipped silently by the outcome reader.
    """
    from tools.sim_harness import outcomes

    pkg = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
    sources = {p.name: p.read_text(encoding="utf-8") for p in pkg.glob("*.py") if p.name != "humidity_facts.py"}
    stale = sorted(_emitted_reserved(sources) & outcomes.UNMAPPED_PRODUCTION_EVENTS)
    assert not stale, (
        f"{stale} are emitted by production but still registered as reserved/unmapped: move each to a mapping in "
        "tools/sim_harness/outcomes.py or re-justify it under rule 8, and drop it from RESERVED_EVENT_TYPES"
    )
    # the scanner itself sees every emit shape (so the guard cannot pass vacuously)
    # #1691 / #1692 took nat_vent_humid_skip / humid_windows_close_notice off the list: each shape is probed on its own
    # with a name still reserved, so a shape the scanner misses fails here.
    assert _emitted_reserved({"a.py": 'self._emit_event("overcool_started", {})'}) == {"overcool_started"}
    assert _emitted_reserved({"b.py": 'self._exit_nat_vent(event_type="overcool_ended")'}) == {"overcool_ended"}
    assert _emitted_reserved({"c.py": 'events.append({"type": "overcool_ended"})'}) == {"overcool_ended"}
