"""Coordinator-level wiring tests for the entity-health sweep (Issue #805).

Covers what test_entity_health.py's pure-function tests can't: the
transition/debounce state machine, notification batching, startup-coalesce
suppression, and failure isolation — all owned by
ClimateAdvisorCoordinator._run_entity_health_check() and its two helpers.

Uses the established object.__new__() + types.MethodType() partial-
instantiation pattern (see test_contact_status.py, test_daily_record_accuracy.py)
so these tests exercise the real coordinator methods, not a re-implementation
of their logic (no-mirror-tests doctrine).
"""

from __future__ import annotations

import datetime as _datetime
import importlib
import types
from unittest.mock import MagicMock

import custom_components.climate_advisor.coordinator as coordinator_module


def _patch_now(monkeypatch, moments: list[_datetime.datetime]):
    """Patch coordinator.dt_util.now() to return successive real datetimes.

    homeassistant.util.dt is a bare MagicMock module in this test harness
    (tools/sim_harness/ha_stubs.py) — dt_util.now() returns a MagicMock, not a
    real datetime, by default. Any test exercising the transition tracker's
    time-elapsed arithmetic must patch it, per the project's documented
    dt_util testing gotcha (CLAUDE.md).
    """
    calls = iter(moments)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: next(calls))


def _get_coordinator_class():
    """Import fresh via importlib — see test_daily_record_accuracy.py's rationale:
    test_occupancy.py deletes the coordinator module from sys.modules and re-imports
    it, which would leave a module-level reference's bound methods with stale
    __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_coordinator(config: dict, states: dict[str, str | None] | None = None, services: set | None = None):
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = config
    coord._entity_health_state = {}
    coord._startup_coalesce_active = False

    states = states or {}
    services = services or {("notify", "mobile_app")}

    def _states_get(entity_id):
        if entity_id not in states or states[entity_id] is None:
            return None
        s = MagicMock()
        s.state = states[entity_id]
        return s

    hass = MagicMock()
    hass.states.get.side_effect = _states_get
    hass.services.has_service.side_effect = lambda domain, service: (domain, service) in services
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    coord.hass = hass

    coord.automation_engine = MagicMock()

    coord._run_entity_health_check = types.MethodType(ClimateAdvisorCoordinator._run_entity_health_check, coord)
    coord._process_entity_health_transitions = types.MethodType(
        ClimateAdvisorCoordinator._process_entity_health_transitions, coord
    )
    coord._notify_entity_health_issues = types.MethodType(ClimateAdvisorCoordinator._notify_entity_health_issues, coord)
    return coord


BASE_CONFIG = {
    "climate_entity": "climate.thermostat",
    "weather_entity": "weather.home",
    "notify_service": "notify.mobile_app",
}

ALL_OK_STATES = {"climate.thermostat": "cool", "weather.home": "sunny"}


class TestTransitionDebounce:
    def test_ok_to_missing_fires_exactly_one_notification(self):
        coord = _make_coordinator(BASE_CONFIG, states={"weather.home": "sunny"})  # climate.thermostat missing

        issues = coord._run_entity_health_check()

        assert len(issues) == 1
        assert issues[0].config_key == "climate_entity"
        coord.hass.async_create_task.assert_called_once()

    def test_missing_missing_across_cycles_does_not_renotify(self, monkeypatch):
        t0 = _datetime.datetime(2026, 1, 1, 12, 0, 0)
        _patch_now(monkeypatch, [t0, t0 + _datetime.timedelta(minutes=30), t0 + _datetime.timedelta(minutes=60)])
        coord = _make_coordinator(BASE_CONFIG, states={"weather.home": "sunny"})

        coord._run_entity_health_check()  # cycle 1 @ t0: new outage, notifies
        coord.hass.async_create_task.reset_mock()
        coord._run_entity_health_check()  # cycle 2 @ t0+30m: still missing
        coord._run_entity_health_check()  # cycle 3 @ t0+60m: still missing

        coord.hass.async_create_task.assert_not_called()

    def test_daily_reminder_fires_after_24h(self, monkeypatch):
        t0 = _datetime.datetime(2026, 1, 1, 12, 0, 0)
        t1 = t0 + _datetime.timedelta(hours=25)
        _patch_now(monkeypatch, [t0, t1])
        coord = _make_coordinator(BASE_CONFIG, states={"weather.home": "sunny"})

        coord._run_entity_health_check()  # @ t0: new outage, notifies
        coord.hass.async_create_task.reset_mock()

        coord._run_entity_health_check()  # @ t1 (25h later): still missing, past the reminder window

        coord.hass.async_create_task.assert_called_once()

    def test_missing_to_ok_clears_state_with_no_notification(self):
        coord = _make_coordinator(BASE_CONFIG, states={"weather.home": "sunny"})
        coord._run_entity_health_check()  # goes missing
        assert "climate_entity" in coord._entity_health_state
        coord.hass.async_create_task.reset_mock()

        # Now climate.thermostat is back.
        coord.hass.states.get.side_effect = lambda eid: (
            MagicMock(state="cool") if eid == "climate.thermostat" else MagicMock(state="sunny")
        )

        issues = coord._run_entity_health_check()

        assert issues == []
        assert "climate_entity" not in coord._entity_health_state
        coord.hass.async_create_task.assert_not_called()


class TestBatching:
    def test_multiple_simultaneous_losses_batch_into_one_notify_call(self):
        config = {**BASE_CONFIG, "home_toggle_entity": "input_boolean.home"}
        coord = _make_coordinator(config, states={})  # everything missing at once

        issues = coord._run_entity_health_check()

        assert len(issues) == 3  # climate, weather, home_toggle
        coord.hass.async_create_task.assert_called_once()


class TestStartupCoalesceSuppression:
    def test_suppressed_entirely_during_startup_coalesce(self):
        coord = _make_coordinator(BASE_CONFIG, states={})
        coord._startup_coalesce_active = True

        issues = coord._run_entity_health_check()

        assert issues == []
        coord.hass.async_create_task.assert_not_called()
        assert coord._entity_health_state == {}

    def test_fresh_notification_after_coalesce_clears(self):
        """An entity still missing when coalescing ends must notify fresh, not wait
        out some stale window — since nothing was tracked during coalesce, this is
        naturally a new ok->missing transition once coalescing ends."""
        coord = _make_coordinator(BASE_CONFIG, states={})
        coord._startup_coalesce_active = True
        coord._run_entity_health_check()
        coord._run_entity_health_check()
        assert coord._entity_health_state == {}

        coord._startup_coalesce_active = False
        issues = coord._run_entity_health_check()

        assert len(issues) == 2
        coord.hass.async_create_task.assert_called_once()


class TestFailureIsolation:
    def test_sweep_exception_does_not_propagate(self, monkeypatch):
        coord = _make_coordinator(BASE_CONFIG, states=ALL_OK_STATES)
        import custom_components.climate_advisor.coordinator as coordinator_module

        monkeypatch.setattr(
            coordinator_module,
            "run_entity_health_sweep",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        issues = coord._run_entity_health_check()  # must not raise

        assert issues == []


class TestCriticalityLogLevel:
    def test_critical_issue_logs_at_error(self, caplog):
        import logging

        coord = _make_coordinator(BASE_CONFIG, states={"weather.home": "sunny"})
        with caplog.at_level(logging.ERROR, logger="custom_components.climate_advisor.coordinator"):
            coord._run_entity_health_check()
        assert any("climate.thermostat" in r.message and r.levelno == logging.ERROR for r in caplog.records)
