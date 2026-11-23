"""Issue #1709 (part of #1507): the outdoor dew point falls back to the weather service when the outdoor humidity
sensor has no usable temperature on its own device.

Occupant view: with a humidity-only outdoor sensor (no temperature on the same device) the outdoor dew point used to
read as unknown, so the humid-night guard (#1691 fan starts, #1692 window advice) never acted: on a muggy night the
whole-house fan could still start and the windows could still be suggested. Now the weather entity's own dew point
(else its own temperature + humidity) answers, as it already did when no outdoor humidity sensor is configured.
Indoor stays strict: a humidity-only indoor sensor still gives no dew point (no weather in a room).

Drives the real pure reader (``humidity_facts.read_dew_point``) and the real headless coordinator + engine (FakeHass
harness: ``_sample_humidity_facts``, ``engine_facts``, the engine's humid-air verdict and fan-start guard). Each test
pairs the fallback with a control, so deleting the fallback fails it.
"""

from __future__ import annotations

import logging
import sys

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.feels_like import dew_point_f  # noqa: E402
from custom_components.climate_advisor.humidity_facts import (  # noqa: E402
    HUMID_AIR_REASON_CEILING,
    humid_air_verdict,
)
from tests.test_humidity_facts_1690 import CLIMATE, PCT, _pure, _st  # noqa: E402

WEATHER = "weather.home"
RH_ONLY = "sensor.patio_humidity_only"
OUT_T = "sensor.patio_temperature"
DEG_F = {"unit_of_measurement": "°F"}


def _weather(**attrs):
    base = {"humidity": 90.0, "dew_point": 20.0, "temperature": 22.0, "temperature_unit": "°C"}
    base.update(attrs)
    return _st("cloudy", {k: v for k, v in base.items() if v is not None})


def _out(states, **kw):
    kw.setdefault("stale_hours", 6.0)
    kw.setdefault("fallback", WEATHER)
    return _pure(states, "outdoor", **kw)


class TestPureReader:
    def test_humidity_only_sensor_reads_the_weather_dew_point(self):
        states = {RH_ONLY: _st("88.0", PCT), WEATHER: _weather()}
        dp = _out(states, chosen=RH_ONLY)
        assert dp.known and dp.dp_f == pytest.approx(68.0)  # 20 C
        assert dp.humidity_source == f"{WEATHER}#humidity" and dp.temp_source == f"{WEATHER}#dew_point"
        assert dp.fallback_from == RH_ONLY and dp.fallback_reason == "no_temperature"
        # the humid-night guard now acts on it (65 F ceiling), where before the dew point was unknown
        verdict = humid_air_verdict(None, dp.dp_f, ceiling_f=65.0)
        assert verdict.blocks and verdict.reason == HUMID_AIR_REASON_CEILING

    def test_same_device_temperature_keeps_the_sensor_pair(self):
        states = {RH_ONLY: _st("60.0", PCT), OUT_T: _st("70.0", DEG_F), WEATHER: _weather()}
        dp = _out(states, chosen=RH_ONLY, temps={RH_ONLY: (OUT_T,)})
        assert dp.known and dp.humidity_source == RH_ONLY and dp.temp_source == OUT_T
        assert dp.fallback_from is None and dp.dp_f == pytest.approx(dew_point_f(70.0, 60.0))

    def test_device_sensor_with_unusable_temperature_falls_back(self):
        """The humidity found on the outdoor sensor's device, the outdoor temperature unavailable or stale."""
        comp = {OUT_T: (RH_ONLY,)}
        for temp_state, reason in ((_st("unavailable"), "unavailable"), (_st("70.0", DEG_F, age_h=7.0), "stale")):
            states = {RH_ONLY: _st("60.0", PCT), OUT_T: temp_state, WEATHER: _weather()}
            dp = _out(states, sources=(OUT_T,), companions=comp)
            assert dp.known and dp.temp_source == f"{WEATHER}#dew_point" and dp.fallback_reason == reason
        # control: a fresh temperature is paired as before
        states = {RH_ONLY: _st("60.0", PCT), OUT_T: _st("70.0", DEG_F), WEATHER: _weather()}
        assert _out(states, sources=(OUT_T,), companions=comp).temp_source == OUT_T

    def test_stale_humidity_is_not_a_missing_temperature(self):
        """Only the temperature side falls back: a stale humidity reading stays unknown (today's behaviour)."""
        states = {RH_ONLY: _st("60.0", PCT, age_h=7.0), OUT_T: _st("70.0", DEG_F), WEATHER: _weather()}
        assert _out(states, chosen=RH_ONLY, temps={RH_ONLY: (OUT_T,)}).reason == "stale"

    def test_stale_humidity_on_a_dead_device_falls_back(self):
        """Review FINAL-A A-6 (kept, better for the occupant): the temperature is checked first, so a sensor whose
        humidity is stale AND whose temperature is unavailable (the device is dead) reads the weather."""
        states = {RH_ONLY: _st("60.0", PCT, age_h=7.0), OUT_T: _st("unavailable"), WEATHER: _weather()}
        dp = _out(states, chosen=RH_ONLY, temps={RH_ONLY: (OUT_T,)})
        assert dp.known and dp.fallback_from == RH_ONLY and dp.fallback_reason == "unavailable"

    def test_wrong_unit_does_not_fall_back(self):
        """A humidity entity not in % is not a humidity reading: unknown (as before), never the weather."""
        states = {RH_ONLY: _st("60.0", {"unit_of_measurement": "g/m³"}), WEATHER: _weather()}
        dp = _out(states, chosen=RH_ONLY)
        assert not dp.known and dp.reason == "wrong_unit" and dp.fallback_from is None
        # control: the same humidity-only sensor in % falls back
        states[RH_ONLY] = _st("60.0", PCT)
        assert _out(states, chosen=RH_ONLY).fallback_from == RH_ONLY

    def test_implausible_sensor_temperature_does_not_fall_back(self):
        """A same-device temperature that reads but is out of range is a broken pair: unknown, never the weather."""
        states = {RH_ONLY: _st("60.0", PCT), OUT_T: _st("180.0", DEG_F), WEATHER: _weather()}
        dp = _out(states, chosen=RH_ONLY, temps={RH_ONLY: (OUT_T,)})
        assert not dp.known and dp.reason == "implausible" and dp.fallback_from is None

    def test_weather_without_dew_point_uses_its_own_temperature_and_humidity(self):
        states = {RH_ONLY: _st("88.0", PCT), WEATHER: _weather(dew_point=None)}
        dp = _out(states, chosen=RH_ONLY)
        assert dp.known and dp.temp_source == f"{WEATHER}#temperature" and dp.rh == 90.0
        assert dp.dp_f == pytest.approx(dew_point_f(22.0 * 9 / 5 + 32, 90.0))

    def test_celsius_weather_and_celsius_install(self):
        # a °C weather entity on a Fahrenheit install: read in its own temperature_unit
        states = {RH_ONLY: _st("88.0", PCT), WEATHER: _weather(dew_point=18.0)}
        assert _out(states, chosen=RH_ONLY).dp_f == pytest.approx(64.4)
        # a Celsius install whose weather entity names no unit: the configured unit
        states[WEATHER] = _weather(dew_point=18.0, temperature_unit=None)
        assert _out(states, chosen=RH_ONLY, unit="celsius").dp_f == pytest.approx(64.4)

    def test_weather_checks_still_apply(self):
        states = {RH_ONLY: _st("88.0", PCT), WEATHER: _st("cloudy", {"humidity": 90.0, "dew_point": 68.0}, age_h=7.0)}
        dp = _out(states, chosen=RH_ONLY)
        assert not dp.known and dp.reason == "no_temperature"  # stale weather: unknown, the sensor's own reason
        states[WEATHER] = _st("cloudy", {"humidity": 90.0, "dew_point": 400.0})
        assert not _out(states, chosen=RH_ONLY).known  # implausible weather dew point is not used
        assert not _out(states, chosen=RH_ONLY, fallback=None).known  # no weather entity: unknown as before
        # a weather glitch (a lone 0 % RH) stays a glitch, so it drops only that reading (the median window answers)
        states[WEATHER] = _st("cloudy", {"humidity": 0.0, "temperature": 70.0, "temperature_unit": "°F"})
        glitch = _out(states, chosen=RH_ONLY)
        assert glitch.reason == "glitch" and glitch.fallback_from == RH_ONLY

    def test_indoor_humidity_only_sensor_stays_unknown(self):
        """Indoor is same-device only: never the weather, never the thermostat's own pair."""
        states = {
            RH_ONLY: _st("55.0", PCT),
            CLIMATE: _st("cool", {"current_temperature": 76.0, "current_humidity": 60.0}),
            WEATHER: _weather(),
        }
        assert _pure(states, chosen=RH_ONLY, fallback=CLIMATE).reason == "no_temperature"
        assert _pure(states, chosen=RH_ONLY, fallback=WEATHER).reason == "no_temperature"


class TestHeadlessCoordinator:
    """The real coordinator and engine on FakeHass: the humid-night guard blocks a fan start on a muggy night."""

    def _zone(self, *, weather_dp_f):
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState

        zones, fake_hass, scheduler = build_headless_multi_zone(
            configs=[
                {
                    "climate_entity": "climate.zone_a",
                    "weather_entity": WEATHER,
                    "outdoor_humidity_entity": RH_ONLY,
                }
            ]
        )
        coord = zones["zone_0"]["coordinator"]
        attrs = {"humidity": 92.0, "temperature": 70.0, "temperature_unit": "°F"}
        if weather_dp_f is not None:
            attrs["dew_point"] = weather_dp_f
        fake_hass.states.set(WEATHER, FakeState(state="cloudy", attributes=attrs))
        return coord, fake_hass, scheduler

    def test_humid_night_blocks_the_fan_with_a_humidity_only_outdoor_sensor(self, caplog):
        coord, fake_hass, scheduler = self._zone(weather_dp_f=68.0)
        engine = coord.automation_engine
        coord._startup_coalesce_active = False
        with scheduler.installed(), caplog.at_level(logging.INFO):
            fake_hass.states.async_report_humidity(RH_ONLY, 93.0)
            now = scheduler.now()
            coord._sample_humidity_facts(now)
            facts = coord.engine_facts(now)
            assert facts.outdoor_dew_point_f == pytest.approx(68.0)
            assert facts.outdoor_dew_point_source == f"{WEATHER}#humidity"
            verdict = engine.humid_air_verdict(now)
            assert verdict.blocks and verdict.reason == HUMID_AIR_REASON_CEILING
            assert engine._humid_air_blocks_fan_start(starter="economizer") is True
            # the chart keeps the sensor's own raw humidity
            assert coord._read_chart_humidity(indoor_f=76.0, outdoor_f=70.0)["outdoor_humidity"] == 93.0
            coord._sample_humidity_facts(now)  # a repeat is not announced again
        lines = [r.getMessage() for r in caplog.records if "outdoor dew point" in r.getMessage()]
        assert len(lines) == 1, lines
        assert f"weather service {WEATHER}#dew_point (dew_point_f=68.00)" in lines[0]
        assert RH_ONLY in lines[0] and "no_temperature" in lines[0]

    def test_a_dry_night_still_lets_the_fan_start(self):
        coord, fake_hass, scheduler = self._zone(weather_dp_f=50.0)
        with scheduler.installed():
            fake_hass.states.async_report_humidity(RH_ONLY, 93.0)
            coord._sample_humidity_facts(scheduler.now())
            assert coord.engine_facts(scheduler.now()).outdoor_dew_point_f == pytest.approx(50.0)
            assert coord.automation_engine._humid_air_blocks_fan_start(starter="economizer") is False

    def test_back_on_the_sensor_is_announced_once(self, caplog):
        from tools.sim_harness.fake_hass import FakeState

        coord, fake_hass, scheduler = self._zone(weather_dp_f=68.0)
        coord._startup_coalesce_active = False
        cache = coord.__dict__.setdefault("_companion_sensors_cache", {})
        with scheduler.installed(), caplog.at_level(logging.INFO):
            fake_hass.states.async_report_humidity(RH_ONLY, 60.0)
            coord._sample_humidity_facts(scheduler.now())
            # the sensor's device now reports a temperature: the sensor pair answers again
            fake_hass.states.set(OUT_T, FakeState(state="70.0", attributes=DEG_F))
            cache[(RH_ONLY, "temperature", False)] = ((OUT_T,), float("inf"))
            coord._sample_humidity_facts(scheduler.now())
            assert coord.engine_facts(scheduler.now()).outdoor_dew_point_source == RH_ONLY
        lines = [r.getMessage() for r in caplog.records if "outdoor dew point" in r.getMessage()]
        assert len(lines) == 2, lines
        assert "now from" in lines[1] and OUT_T in lines[1] and "usable again" in lines[1]
