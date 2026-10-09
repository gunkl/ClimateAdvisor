"""Indoor sensor failure visibility: once-per-episode announcements (Issue #1033).

Exercises the real ``ClimateAdvisorCoordinator._refresh_indoor_sensor_health`` /
``_note_indoor_sensor_state`` funnel through the real listeners registered by the real
``async_setup`` (sim harness dispatch), plus the object.__new__() + types.MethodType() pattern
for the bare-coordinator cases. Never a re-implementation of the logic under test.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re
import types
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.const import (
    INDOOR_SENSOR_STALE_HOURS,
    TEMP_SOURCE_SENSOR,
)

_PRIMARY = "sensor.indoor_temp"
_SLEEP = "sensor.bedroom_temp"
_CLIMATE = "climate.test_thermostat"
STALE_H = INDOOR_SENSOR_STALE_HOURS
_LOGGER_NAME = "custom_components.climate_advisor.coordinator"
# 22:00 -> 09:00 window: the harness clock starts at 08:00 UTC (inside), 10:00 is outside.
_SLEEP_CFG = {"sleep_time": "22:00", "wake_time": "09:00"}


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _build(config=None, *, skip_startup_coalesce=True, climate_attributes=None):
    from tools.sim_harness.build_coordinator import build_headless_coordinator
    from tools.sim_harness.fake_hass import FakeState

    cfg = {"indoor_temp_source": TEMP_SOURCE_SENSOR, "indoor_temp_entity": _PRIMARY}
    cfg.update(config or {})
    coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
        config=cfg,
        skip_startup_coalesce=skip_startup_coalesce,
        climate_attributes=climate_attributes,
    )
    fake_hass.states.set(_PRIMARY, FakeState(state="70.0", attributes={}))
    return coordinator, fake_hass, scheduler, event_log


def _types(event_log, name):
    return [e for e in event_log if e[0] == name]


def _set(fake_hass, scheduler, entity, value):
    with scheduler.installed():
        fake_hass.states.async_set(entity, value)
        scheduler.advance_to(scheduler.now())


class TestPrimarySensorEpisodes:
    def test_one_event_per_episode(self):
        coordinator, hass, sched, log = _build()
        for value in ("unavailable", "unavailable", "unknown"):
            _set(hass, sched, _PRIMARY, value)
        events = _types(log, "indoor_sensor_unavailable")
        assert len(events) == 1
        payload = events[0][1]
        assert payload == {
            "role": "primary",
            "reason": "unavailable",
            "entity": _PRIMARY,
            "using": "none",
            "value_f": None,
        }

    def test_reason_change_reannounces(self):
        _c, hass, sched, log = _build()
        _set(hass, sched, _PRIMARY, "unavailable")
        _set(hass, sched, _PRIMARY, "garbage")
        _set(hass, sched, _PRIMARY, "999")
        reasons = [e[1]["reason"] for e in _types(log, "indoor_sensor_unavailable")]
        assert reasons == ["unavailable", "non_numeric", "implausible"]

    def test_recovery_pairs_with_minutes_and_previous_reason(self):
        _c, hass, sched, log = _build()
        _set(hass, sched, _PRIMARY, "unavailable")
        with sched.installed():
            sched.advance_to(sched.now() + timedelta(minutes=12))
        _set(hass, sched, _PRIMARY, "71.5")
        recovered = _types(log, "indoor_sensor_recovered")
        assert len(recovered) == 1
        payload = recovered[0][1]
        assert payload["role"] == "primary"
        assert payload["entity"] == _PRIMARY
        assert payload["previous_reason"] == "unavailable"
        assert payload["minutes_down"] == 12
        assert payload["value_f"] == pytest.approx(71.5)
        # Healthy again: a second healthy reading announces nothing more.
        _set(hass, sched, _PRIMARY, "72.0")
        assert len(_types(log, "indoor_sensor_recovered")) == 1

    def test_primary_failure_logs_warning_and_recovery_info(self, caplog):
        _c, hass, sched, _log = _build()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _set(hass, sched, _PRIMARY, "unavailable")
            _set(hass, sched, _PRIMARY, "70.0")
        fail = [r for r in caplog.records if r.getMessage().startswith("Indoor sensor unavailable")]
        rec = [r for r in caplog.records if r.getMessage().startswith("Indoor sensor recovered")]
        assert [r.levelno for r in fail] == [logging.WARNING]
        assert "role=primary" in fail[0].getMessage()
        assert "reason=unavailable" in fail[0].getMessage()
        assert [r.levelno for r in rec] == [logging.INFO]

    def test_stale_sensor_announces_and_fresh_does_not(self):
        from tools.sim_harness.fake_hass import FakeState

        coordinator, hass, sched, log = _build()
        now = sched.now()
        with sched.installed():
            hass.states.set(
                _PRIMARY,
                FakeState(state="70.0", attributes={}, last_reported=now - timedelta(hours=STALE_H - 1)),
            )
            coordinator._refresh_indoor_sensor_health()
            assert _types(log, "indoor_sensor_unavailable") == []
            hass.states.set(
                _PRIMARY,
                FakeState(state="70.0", attributes={}, last_reported=now - timedelta(hours=STALE_H + 1)),
            )
            coordinator._refresh_indoor_sensor_health()
        events = _types(log, "indoor_sensor_unavailable")
        assert [e[1]["reason"] for e in events] == ["stale"]

    def test_climate_fallback_zone_announces_via_thermostat_listener(self):
        coordinator, hass, sched, log = _build({"indoor_temp_source": "climate_fallback", "indoor_temp_entity": None})
        # Thermostat reports no current_temperature at all; any thermostat state event runs the funnel.
        with sched.installed():
            hass.states.async_set(_CLIMATE, "heat", {"fan_mode": "auto"})
            sched.advance_to(sched.now())
        events = _types(log, "indoor_sensor_unavailable")
        assert len(events) == 1
        assert events[0][1]["role"] == "primary"
        assert events[0][1]["reason"] == "no_reading"
        assert events[0][1]["entity"] == _CLIMATE
        with sched.installed():
            hass.states.async_set(_CLIMATE, "heat", {"fan_mode": "auto", "current_temperature": 70.0})
            sched.advance_to(sched.now())
        assert len(_types(log, "indoor_sensor_recovered")) == 1


class TestStartupCoalesceGate:
    def test_tracked_but_not_announced_during_coalesce(self):
        coordinator, hass, sched, log = _build(skip_startup_coalesce=False)
        assert coordinator._startup_coalesce_active
        with sched.installed():
            hass.states.async_set(_PRIMARY, "unavailable")
            sched.advance_to(sched.now())
        assert coordinator._indoor_primary_fallback_reason == "unavailable"
        assert _types(log, "indoor_sensor_unavailable") == []

    def test_failure_and_recovery_inside_window_announce_nothing(self):
        coordinator, hass, sched, log = _build(skip_startup_coalesce=False)
        _set(hass, sched, _PRIMARY, "unavailable")
        _set(hass, sched, _PRIMARY, "70.5")
        assert _types(log, "indoor_sensor_unavailable") == []
        assert _types(log, "indoor_sensor_recovered") == []
        assert coordinator._indoor_primary_fallback_reason is None

    def test_still_failing_after_window_is_announced_once(self):
        coordinator, hass, sched, log = _build(skip_startup_coalesce=False)
        _set(hass, sched, _PRIMARY, "unavailable")
        coordinator._startup_coalesce_active = False
        _set(hass, sched, _PRIMARY, "unavailable")
        assert len(_types(log, "indoor_sensor_unavailable")) == 1


class TestSleepRole:
    def _cfg(self):
        return {"sleep_indoor_temp_entity": _SLEEP, **_SLEEP_CFG}

    def _seed_sleep(self, hass, value):
        from tools.sim_harness.fake_hass import FakeState

        hass.states.set(_SLEEP, FakeState(state=value, attributes={}))

    def test_sleep_failure_logs_info_and_announces_role_sleep_then_recovers(self, caplog):
        coordinator, hass, sched, log = _build(self._cfg())
        self._seed_sleep(hass, "68.0")
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _set(hass, sched, _SLEEP, "unavailable")
            _set(hass, sched, _SLEEP, "68.5")
        events = _types(log, "indoor_sensor_unavailable")
        assert len(events) == 1
        assert events[0][1]["role"] == "sleep"
        assert events[0][1]["reason"] == "unavailable"
        assert events[0][1]["using"] == "primary_sensor"
        assert events[0][1]["entity"] == _SLEEP
        fail = [r for r in caplog.records if r.getMessage().startswith("Indoor sensor unavailable")]
        assert [r.levelno for r in fail] == [logging.INFO]
        rec = _types(log, "indoor_sensor_recovered")
        assert len(rec) == 1 and rec[0][1]["role"] == "sleep"
        # The primary role never failed.
        assert all(e[1]["role"] == "sleep" for e in events + rec)

    def test_sleep_window_exit_clears_silently(self):
        coordinator, hass, sched, log = _build(self._cfg())
        self._seed_sleep(hass, "68.0")
        _set(hass, sched, _SLEEP, "unavailable")
        assert coordinator._indoor_sleep_fallback_announced == "unavailable"
        before = len(log)
        with sched.installed():
            sched.advance_to(sched.now() + timedelta(hours=2))  # 10:00, window ended at 09:00
            hass.states.async_set(_PRIMARY, "70.2")  # primary listener runs the funnel
            sched.advance_to(sched.now())
        assert coordinator._indoor_sleep_fallback_reason is None
        assert coordinator._indoor_sleep_fallback_announced is None
        assert coordinator._indoor_sleep_fallback_since is None
        assert [e for e in log[before:] if e[0].startswith("indoor_sensor_")] == []

    def test_sleep_listener_runs_funnel_outside_window(self):
        """The sleep listener must call the funnel BEFORE its window early-return."""
        coordinator, hass, sched, log = _build(self._cfg())
        self._seed_sleep(hass, "68.0")
        _set(hass, sched, _SLEEP, "unavailable")
        with sched.installed():
            sched.advance_to(sched.now() + timedelta(hours=2))
            hass.states.async_set(_SLEEP, "68.4")
            sched.advance_to(sched.now())
        assert coordinator._indoor_sleep_fallback_announced is None
        assert len(_types(log, "indoor_sensor_recovered")) == 0  # cleared silently, not a recovery

    def test_reentering_the_window_with_the_sensor_still_bad_announces_exactly_once_more(self):
        coordinator, hass, sched, log = _build(self._cfg())
        self._seed_sleep(hass, "68.0")
        _set(hass, sched, _SLEEP, "unavailable")  # night 1: announced
        assert len(_types(log, "indoor_sensor_unavailable")) == 1
        with sched.installed():
            sched.advance_to(sched.now() + timedelta(hours=2))  # 10:00, outside: silent clear
            hass.states.async_set(_PRIMARY, "70.2")
            sched.advance_to(sched.now())
        assert coordinator._indoor_sleep_fallback_announced is None
        with sched.installed():
            sched.advance_to(sched.now() + timedelta(hours=12, minutes=30))  # 22:30, inside again
            hass.states.async_set(_PRIMARY, "70.4")  # funnel runs; sleep sensor is STILL unavailable
            sched.advance_to(sched.now())
        events = [e for e in _types(log, "indoor_sensor_unavailable") if e[1]["role"] == "sleep"]
        assert len(events) == 2  # one per night, no spam while the window stays open
        assert _types(log, "indoor_sensor_recovered") == []


class TestIndependentZones:
    def test_two_coordinators_sharing_one_sensor_announce_independently(self):
        cls = _get_coordinator_class()
        shared_state = MagicMock()
        shared_state.state = "unavailable"
        shared_state.last_reported = None
        shared_state.last_updated = None
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=shared_state)

        def make():
            coord = object.__new__(cls)
            coord.config = {
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": _PRIMARY,
                "climate_entity": _CLIMATE,
                "temp_unit": "fahrenheit",
            }
            coord.hass = hass
            coord._emit_event = MagicMock()
            coord._startup_coalesce_active = False
            for name in (
                "_refresh_indoor_sensor_health",
                "_note_indoor_sensor_state",
                "_get_indoor_temp_with_provenance",
            ):
                setattr(coord, name, types.MethodType(getattr(cls, name), coord))
            return coord

        a, b = make(), make()
        a._refresh_indoor_sensor_health()
        a._refresh_indoor_sensor_health()
        assert a._emit_event.call_count == 1
        assert b._emit_event.call_count == 0
        b._refresh_indoor_sensor_health()
        assert b._emit_event.call_count == 1
        assert a._emit_event.call_args.args[0] == "indoor_sensor_unavailable"
        assert b._emit_event.call_args.args[0] == "indoor_sensor_unavailable"


class TestDesignRule:
    def test_event_names_only_in_coordinator_and_never_from_read_paths(self):
        pkg = Path(inspect.getsourcefile(_get_coordinator_class())).parent
        for name in ("indoor_sensor_unavailable", "indoor_sensor_recovered"):
            emitters = []
            for py in sorted(pkg.glob("*.py")):
                text = py.read_text(encoding="utf-8")
                if re.search(rf"""_emit_event\(\s*["']{name}["']""", text):
                    emitters.append(py.name)
            assert emitters == ["coordinator.py"], (name, emitters)

        cls = _get_coordinator_class()
        for fn in ("_get_indoor_temp", "_get_indoor_temp_with_provenance", "get_chart_data", "_emit_event"):
            assert "_refresh_indoor_sensor_health" not in inspect.getsource(getattr(cls, fn)), fn
        # And only the funnel calls the note method.
        mod_src = inspect.getsource(importlib.import_module("custom_components.climate_advisor.coordinator"))
        callers = re.findall(r"self\._note_indoor_sensor_state\(", mod_src)
        assert len(callers) == 2  # primary + sleep, both inside _refresh_indoor_sensor_health
        assert mod_src.count("_note_indoor_sensor_state(") == 3  # 2 calls + the def
        funnel_src = inspect.getsource(cls._refresh_indoor_sensor_health)
        assert funnel_src.count("self._note_indoor_sensor_state(") == 2

    def test_other_modules_do_not_import_the_funnel(self):
        pkg = Path(inspect.getsourcefile(_get_coordinator_class())).parent
        for py in sorted(pkg.glob("*.py")):
            if py.name == "coordinator.py":
                continue
            assert "._refresh_indoor_sensor_health(" not in py.read_text(encoding="utf-8"), py.name
