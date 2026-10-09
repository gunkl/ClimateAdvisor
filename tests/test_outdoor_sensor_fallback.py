"""Dedicated outdoor sensor: failure handling, fallback events, and live refresh (Issue #1032).

Exercises the real ``ClimateAdvisorCoordinator._get_outdoor_temp`` / ``_read_outdoor_sensor`` /
``_note_outdoor_sensor_state`` via the object.__new__() + types.MethodType() pattern, and the real
outdoor-sensor state listener (registered by the real ``async_setup``) through the sim harness —
never a re-implementation of the logic under test.
"""

from __future__ import annotations

import importlib
import math
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (
    MAX_PLAUSIBLE_OUTDOOR_F,
    MIN_PLAUSIBLE_OUTDOOR_F,
    OUTDOOR_SENSOR_STALE_HOURS,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
)
from custom_components.climate_advisor.temperature import read_sensor_state_f

_SENSOR = "sensor.outdoor_temp"
_NOW = datetime(2026, 10, 9, 18, 0, 0, tzinfo=UTC)


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_coordinator(source: str = TEMP_SOURCE_SENSOR, **config_overrides):
    cls = _get_coordinator_class()
    coord = object.__new__(cls)
    coord.config = {
        "outdoor_temp_source": source,
        "outdoor_temp_entity": _SENSOR,
        "weather_entity": "weather.forecast_home",
        "temp_unit": "fahrenheit",
        **config_overrides,
    }
    coord.hass = MagicMock()
    coord._hourly_forecast_temps = []  # force the weather nowcast attribute path
    coord._hourly_interp_unavailable_streak = 0
    coord._emit_event = MagicMock()
    coord._startup_coalesce_active = False
    coord._outdoor_sensor_fallback_reason = None
    coord._outdoor_sensor_fallback_announced = None
    coord._outdoor_sensor_fallback_since = None
    for name in (
        "_get_outdoor_temp",
        "_get_weather_outdoor_temp",
        "_read_outdoor_sensor",
        "_note_outdoor_sensor_state",
    ):
        setattr(coord, name, types.MethodType(getattr(cls, name), coord))
    return coord


def _state(value: str | None, *, last_reported=None, last_updated=None):
    st = MagicMock()
    st.state = value
    st.last_reported = last_reported
    st.last_updated = last_updated
    return st


def _wire(coord, sensor_state, weather_temp: float = 60.0):
    weather = MagicMock()
    weather.attributes = {"temperature": weather_temp}

    def _get(entity_id):
        if entity_id == coord.config.get("outdoor_temp_entity"):
            return sensor_state
        return weather

    coord.hass.states.get = MagicMock(side_effect=_get)


def _events(coord, event_type: str) -> list[dict]:
    return [c.args[1] for c in coord._emit_event.call_args_list if c.args[0] == event_type]


class TestReadSensorStateF:
    @pytest.mark.parametrize(
        ("raw", "unit", "expected"),
        [("72.5", "fahrenheit", 72.5), (" 72.5 ", "fahrenheit", 72.5), ("20", "celsius", 68.0)],
    )
    def test_good_readings(self, raw, unit, expected):
        value, reason = read_sensor_state_f(_state(raw), unit)
        assert reason is None
        assert value == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("unavailable", "unavailable"),
            ("unknown", "unavailable"),
            ("", "non_numeric"),
            ("None", "non_numeric"),
            ("72,3", "non_numeric"),
            ("nan", "non_finite"),
            ("NaN", "non_finite"),
            ("inf", "non_finite"),
            ("-inf", "non_finite"),
            ("1e309", "non_finite"),
        ],
    )
    def test_bad_readings_are_rejected_with_a_reason(self, raw, reason):
        value, got = read_sensor_state_f(_state(raw), "fahrenheit")
        assert value is None
        assert got == reason

    def test_missing_state(self):
        assert read_sensor_state_f(None, "fahrenheit") == (None, "entity_missing")


class TestOutdoorSensorFallbackReasons:
    def test_good_sensor_value_is_used_and_not_in_fallback(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3"))
        assert coord._get_outdoor_temp({}) == pytest.approx(72.3)
        assert coord._outdoor_sensor_fallback_reason is None
        coord._emit_event.assert_not_called()

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("unavailable", "unavailable"),
            ("unknown", "unavailable"),
            ("garbage", "non_numeric"),
            ("nan", "non_finite"),
            ("inf", "non_finite"),
            (str(MAX_PLAUSIBLE_OUTDOOR_F + 1), "implausible"),
            (str(MIN_PLAUSIBLE_OUTDOOR_F - 1), "implausible"),
        ],
    )
    def test_bad_sensor_falls_back_to_weather_and_says_why(self, raw, reason):
        coord = _make_coordinator()
        _wire(coord, _state(raw), weather_temp=61.0)
        value = coord._get_outdoor_temp({"temperature": 61.0})
        assert value == pytest.approx(61.0)
        assert math.isfinite(value)
        assert coord._outdoor_sensor_fallback_reason == reason
        (event,) = _events(coord, "outdoor_sensor_fallback")
        assert event["reason"] == reason
        assert event["entity"] == _SENSOR
        assert event["source_in_use"] == "weather_attribute"
        assert event["value_f"] == pytest.approx(61.0)

    def test_missing_entity(self):
        coord = _make_coordinator()
        _wire(coord, None)
        coord._get_outdoor_temp({"temperature": 58.0})
        assert coord._outdoor_sensor_fallback_reason == "entity_missing"

    def test_source_needs_entity_but_none_configured(self):
        coord = _make_coordinator(outdoor_temp_entity=None)
        _wire(coord, _state("72.3"))
        coord._get_outdoor_temp({"temperature": 58.0})
        assert coord._outdoor_sensor_fallback_reason == "not_configured"

    def test_celsius_sensor_is_converted_before_the_plausibility_check(self):
        coord = _make_coordinator(temp_unit="celsius")
        _wire(coord, _state("20"))  # 20 °C = 68 °F, plausible
        assert coord._get_outdoor_temp({}) == pytest.approx(68.0)

    def test_weather_service_source_never_touches_the_state_machine(self):
        coord = _make_coordinator(source=TEMP_SOURCE_WEATHER_SERVICE)
        _wire(coord, _state("nan"))
        assert coord._get_outdoor_temp({"temperature": 63.0}) == pytest.approx(63.0)
        assert coord._outdoor_sensor_fallback_reason is None
        coord._emit_event.assert_not_called()


class TestOutdoorSensorStale:
    def _stale_age(self):
        return timedelta(hours=OUTDOOR_SENSOR_STALE_HOURS + 1)

    def test_sensor_not_reported_for_too_long_falls_back(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3", last_reported=_NOW - self._stale_age()))
        with patch("custom_components.climate_advisor.coordinator.dt_util.utcnow", return_value=_NOW):
            value = coord._get_outdoor_temp({"temperature": 60.0})
        assert value == pytest.approx(60.0)
        assert coord._outdoor_sensor_fallback_reason == "stale"

    def test_recent_report_is_fresh_even_if_value_unchanged(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3", last_reported=_NOW - timedelta(minutes=10)))
        with patch("custom_components.climate_advisor.coordinator.dt_util.utcnow", return_value=_NOW):
            assert coord._get_outdoor_temp({}) == pytest.approx(72.3)

    def test_last_updated_is_used_when_last_reported_absent(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3", last_reported=None, last_updated=_NOW - self._stale_age()))
        with patch("custom_components.climate_advisor.coordinator.dt_util.utcnow", return_value=_NOW):
            coord._get_outdoor_temp({"temperature": 60.0})
        assert coord._outdoor_sensor_fallback_reason == "stale"

    def test_input_number_helper_is_never_stale(self):
        coord = _make_coordinator(source=TEMP_SOURCE_INPUT_NUMBER)
        _wire(coord, _state("72.3", last_reported=_NOW - timedelta(days=30)))
        with patch("custom_components.climate_advisor.coordinator.dt_util.utcnow", return_value=_NOW):
            assert coord._get_outdoor_temp({}) == pytest.approx(72.3)
        assert coord._outdoor_sensor_fallback_reason is None

    def test_fails_open_when_no_timestamp_is_available(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3"))  # last_reported / last_updated are None (stubs, old HA)
        assert coord._get_outdoor_temp({}) == pytest.approx(72.3)

    def test_fails_open_when_the_clock_is_not_a_datetime(self):
        coord = _make_coordinator()
        _wire(coord, _state("72.3", last_reported=_NOW - self._stale_age()))
        with patch("custom_components.climate_advisor.coordinator.dt_util.utcnow", return_value=MagicMock()):
            assert coord._get_outdoor_temp({}) == pytest.approx(72.3)


class TestFallbackTransitions:
    def test_repeated_reads_in_one_episode_announce_once(self):
        coord = _make_coordinator()
        _wire(coord, _state("unavailable"))
        for _ in range(5):
            coord._get_outdoor_temp({"temperature": 60.0})
        assert len(_events(coord, "outdoor_sensor_fallback")) == 1
        assert _events(coord, "outdoor_sensor_recovered") == []

    def test_reason_change_is_announced_again(self):
        coord = _make_coordinator()
        _wire(coord, _state("unavailable"))
        coord._get_outdoor_temp({"temperature": 60.0})
        _wire(coord, _state("nan"))
        coord._get_outdoor_temp({"temperature": 60.0})
        assert [e["reason"] for e in _events(coord, "outdoor_sensor_fallback")] == ["unavailable", "non_finite"]

    def test_recovery_is_announced_once_with_context(self):
        coord = _make_coordinator()
        _wire(coord, _state("unavailable"))
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW):
            coord._get_outdoor_temp({"temperature": 60.0})
        _wire(coord, _state("72.3"))
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW + timedelta(minutes=42)
        ):
            coord._get_outdoor_temp({})
            coord._get_outdoor_temp({})
        (recovered,) = _events(coord, "outdoor_sensor_recovered")
        assert recovered["previous_reason"] == "unavailable"
        assert recovered["minutes_in_fallback"] == 42
        assert recovered["value_f"] == pytest.approx(72.3)
        assert coord._outdoor_sensor_fallback_reason is None

    def test_a_new_episode_after_recovery_announces_again(self):
        coord = _make_coordinator()
        for raw in ("unavailable", "72.3", "unavailable"):
            _wire(coord, _state(raw))
            coord._get_outdoor_temp({"temperature": 60.0})
        assert len(_events(coord, "outdoor_sensor_fallback")) == 2
        assert len(_events(coord, "outdoor_sensor_recovered")) == 1

    def test_nothing_is_announced_during_startup_coalesce(self):
        """At HA boot the sensor entity may simply not be loaded yet — track, don't announce."""
        coord = _make_coordinator()
        coord._startup_coalesce_active = True
        _wire(coord, None)
        coord._get_outdoor_temp({"temperature": 60.0})
        assert coord._outdoor_sensor_fallback_reason == "entity_missing"  # still tracked for the Settings text
        coord._emit_event.assert_not_called()

    def test_boot_blip_that_recovers_before_coalesce_ends_is_never_announced(self):
        coord = _make_coordinator()
        coord._startup_coalesce_active = True
        _wire(coord, None)
        coord._get_outdoor_temp({"temperature": 60.0})
        _wire(coord, _state("72.3"))
        coord._get_outdoor_temp({})
        coord._startup_coalesce_active = False
        coord._get_outdoor_temp({})
        coord._emit_event.assert_not_called()

    def test_failure_that_outlasts_startup_coalesce_is_announced_once_afterwards(self):
        coord = _make_coordinator()
        coord._startup_coalesce_active = True
        _wire(coord, _state("unavailable"))
        coord._get_outdoor_temp({"temperature": 60.0})
        coord._startup_coalesce_active = False
        coord._get_outdoor_temp({"temperature": 60.0})
        coord._get_outdoor_temp({"temperature": 60.0})
        assert len(_events(coord, "outdoor_sensor_fallback")) == 1


class TestLiveListenerThroughRealSetup:
    """The real async_setup() registers the real outdoor-sensor listener (sim harness dispatch)."""

    def _build(self):
        from tools.sim_harness.build_coordinator import build_headless_coordinator
        from tools.sim_harness.fake_hass import FakeState

        coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
            config={"outdoor_temp_source": TEMP_SOURCE_SENSOR, "outdoor_temp_entity": _SENSOR},
            skip_startup_coalesce=True,
        )
        fake_hass.states.set(_SENSOR, FakeState(state="70.0", attributes={}))
        return coordinator, fake_hass, scheduler, event_log

    def test_sensor_change_refreshes_status_and_engine_without_fan_or_natvent(self):
        coordinator, fake_hass, scheduler, _log = self._build()
        ae = coordinator.automation_engine
        assert not ae._fan_active
        assert not ae._natural_vent_active
        with scheduler.installed():
            fake_hass.states.async_set(_SENSOR, "75.5")
            scheduler.advance_to(scheduler.now())
        assert coordinator._last_outdoor_temp == pytest.approx(75.5)
        assert ae._last_outdoor_temp == pytest.approx(75.5)

    def test_sub_threshold_change_does_not_refresh(self):
        coordinator, fake_hass, scheduler, _log = self._build()
        with scheduler.installed():
            fake_hass.states.async_set(_SENSOR, "75.5")
            scheduler.advance_to(scheduler.now())
            coordinator.automation_engine.update_outdoor_temp = MagicMock()
            fake_hass.states.async_set(_SENSOR, "75.52")
            scheduler.advance_to(scheduler.now())
        coordinator.automation_engine.update_outdoor_temp.assert_not_called()

    def test_unavailable_sensor_falls_back_and_logs_an_activity_event(self):
        coordinator, fake_hass, scheduler, event_log = self._build()
        with scheduler.installed():
            fake_hass.states.async_set(_SENSOR, "75.5")
            fake_hass.states.async_set(_SENSOR, "unavailable")
            scheduler.advance_to(scheduler.now())
            fake_hass.states.async_set(_SENSOR, "76.0")
            scheduler.advance_to(scheduler.now())
        types_seen = [e[0] for e in event_log]
        assert types_seen.count("outdoor_sensor_fallback") == 1
        assert types_seen.count("outdoor_sensor_recovered") == 1
        assert coordinator._last_outdoor_temp == pytest.approx(76.0)

    def test_nan_state_is_never_applied(self):
        coordinator, fake_hass, scheduler, _log = self._build()
        with scheduler.installed():
            fake_hass.states.async_set(_SENSOR, "75.5")
            fake_hass.states.async_set(_SENSOR, "nan")
            scheduler.advance_to(scheduler.now())
        assert math.isfinite(coordinator._last_outdoor_temp)
        assert math.isfinite(coordinator.automation_engine._last_outdoor_temp)
