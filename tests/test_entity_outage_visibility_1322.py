"""Entity outages show in the Activity log; the notification title says what happened (Issue #1322).

Occupant story (2026-11-03/04): the weather source (Met.no) failed three times overnight. Climate Advisor retried and
recovered on its own, but the Activity log showed nothing, the notification said "entity not found" for an entity
that only reported unavailable, and the log claimed a session-long cosine fallback that did not happen.
"""

from __future__ import annotations

import asyncio
import datetime as _datetime
import logging
from unittest.mock import AsyncMock, MagicMock

from tests.test_entity_health_coordinator import BASE_CONFIG, _make_coordinator, _patch_now

T0 = _datetime.datetime(2026, 11, 4, 4, 32, 8, tzinfo=_datetime.UTC)


def _kinds(coord) -> list[str]:
    return [k for k, _ in coord.emitted if k.startswith("entity_")]


def _states(coord, mapping: dict[str, str | None]) -> None:
    def get(eid):
        if mapping.get(eid) is None:
            return None
        s = MagicMock()
        s.state = mapping[eid]
        s.last_changed = T0 - _datetime.timedelta(minutes=1)
        return s

    coord.hass.states.get.side_effect = get


class TestActivityRows:
    def test_an_outage_and_its_recovery_each_leave_one_row(self, monkeypatch):
        _patch_now(monkeypatch, [T0, T0 + _datetime.timedelta(minutes=30), T0 + _datetime.timedelta(minutes=113)])
        coord = _make_coordinator(BASE_CONFIG)
        _states(coord, {"climate.thermostat": "heat", "weather.home": "unavailable"})

        coord._run_entity_health_check()  # outage starts
        coord._run_entity_health_check()  # still out: no second row
        _states(coord, {"climate.thermostat": "heat", "weather.home": "sunny"})
        coord._run_entity_health_check()  # recovered

        assert _kinds(coord) == ["entity_unavailable", "entity_recovered"]
        down, up = coord.emitted[0][1], coord.emitted[-1][1]
        assert down["entity_id"] == "weather.home" and down["status"] == "unavailable"
        assert down["down_since"] == (T0 - _datetime.timedelta(minutes=1)).isoformat(), "when it really went down"
        assert up["entity_id"] == "weather.home" and up["outage_minutes"] == 114

    def test_a_daily_reminder_does_not_add_a_second_outage_row(self, monkeypatch):
        _patch_now(monkeypatch, [T0, T0 + _datetime.timedelta(hours=25)])
        coord = _make_coordinator(BASE_CONFIG)
        _states(coord, {"climate.thermostat": "heat", "weather.home": "unavailable"})
        coord._run_entity_health_check()
        coord._run_entity_health_check()  # reminder notification is due here
        assert _kinds(coord) == ["entity_unavailable"]

    def test_unavailable_then_missing_is_the_same_outage(self, monkeypatch):
        _patch_now(monkeypatch, [T0, T0 + _datetime.timedelta(minutes=30)])
        coord = _make_coordinator(BASE_CONFIG)
        _states(coord, {"climate.thermostat": "heat", "weather.home": "unavailable"})
        coord._run_entity_health_check()
        _states(coord, {"climate.thermostat": "heat", "weather.home": None})
        coord._run_entity_health_check()
        assert _kinds(coord) == ["entity_unavailable"]

    def test_the_thermostat_and_a_door_sensor_get_rows_too(self, monkeypatch):
        """Mirror: every monitored entity shares the tracker, not only the weather source."""
        config = {**BASE_CONFIG, "door_window_sensors": ["binary_sensor.front_door"]}
        _patch_now(monkeypatch, [T0, T0 + _datetime.timedelta(minutes=30)])
        coord = _make_coordinator(config)
        _states(coord, {"weather.home": "sunny", "climate.thermostat": "unavailable"})
        coord._sensor_last_readings = lambda: {}
        coord._get_fan_physical_state = lambda: None
        coord._run_entity_health_check()
        _states(coord, {"weather.home": "sunny", "climate.thermostat": "heat", "binary_sensor.front_door": "off"})
        coord._run_entity_health_check()
        ids = [(k, d["entity_id"]) for k, d in coord.emitted]
        assert ("entity_unavailable", "climate.thermostat") in ids and ("entity_recovered", "climate.thermostat") in ids
        assert ("entity_unavailable", "binary_sensor.front_door") in ids
        assert ("entity_recovered", "binary_sensor.front_door") in ids

    def test_nothing_is_recorded_during_startup_coalescing(self):
        coord = _make_coordinator(BASE_CONFIG)
        coord._startup_coalesce_active = True
        _states(coord, {"climate.thermostat": "heat", "weather.home": "unavailable"})
        coord._run_entity_health_check()
        assert coord.emitted == []


class TestRenderers:
    def test_rows_read_in_plain_english(self):
        from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS

        down = {
            "entity_id": "weather.forecast_home",
            "friendly_name": "Forecast Home",
            "status": "unavailable",
            "detail": "",
            "down_since": T0.isoformat(),
        }
        label, settings = EVENT_RENDERERS["entity_unavailable"](down, "fahrenheit")
        assert label == "Forecast Home unavailable" and settings.startswith("since ")
        label, _ = EVENT_RENDERERS["entity_unavailable"]({**down, "status": "missing"}, "fahrenheit")
        assert label == "Forecast Home not found"
        label, settings = EVENT_RENDERERS["entity_recovered"](
            {"entity_id": "weather.forecast_home", "friendly_name": "Forecast Home", "outage_minutes": 113},
            "fahrenheit",
        )
        assert label == "Forecast Home available again" and "out 113 min" in settings

    def test_rows_are_never_deduplicated_away(self):
        from custom_components.climate_advisor.ai_skills_context import _NO_DEDUP

        assert {"entity_unavailable", "entity_recovered"} <= _NO_DEDUP


class TestNotificationTitle:
    def _title(self, mapping: dict[str, str | None]) -> str:
        coord = _make_coordinator(BASE_CONFIG)
        coord._notify_in_background = MagicMock()
        _states(coord, mapping)
        coord._run_entity_health_check()
        return coord._notify_in_background.call_args.args[0]

    def test_an_unavailable_entity_is_not_called_not_found(self):
        assert self._title({"climate.thermostat": "heat", "weather.home": "unavailable"}) == (
            "Climate Advisor — entity unavailable"
        )

    def test_a_missing_entity_is_still_not_found(self):
        assert self._title({"climate.thermostat": "heat", "weather.home": None}) == "Climate Advisor — entity not found"

    def test_a_mix_says_both(self):
        assert self._title({"climate.thermostat": "unavailable", "weather.home": None}) == (
            "Climate Advisor — entities unavailable or not found"
        )


class TestHourlyForecastLog:
    def test_each_outage_is_announced_and_its_return_logged(self, caplog):
        from tests.test_issue_874_log_levels import _make_coordinator_for_outdoor_temp

        coord = _make_coordinator_for_outdoor_temp()
        coord.config["weather_entity"] = "weather.forecast_home"
        coord.hass.states.get.return_value = MagicMock()
        failing = AsyncMock(side_effect=RuntimeError("weather unavailable"))
        working = AsyncMock(return_value={"weather.forecast_home": {"forecast": [{"temperature": 60}]}})
        logger = "custom_components.climate_advisor.coordinator"

        with caplog.at_level(logging.DEBUG, logger=logger):
            for call in (failing, failing, working, failing):  # outage, still out, back, a second outage
                coord.hass.services.async_call = call
                asyncio.run(coord._get_hourly_forecast_data())
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(errors) == 2, "each outage is announced once, the second too"
        assert all("until it returns" in m and "rest of this session" not in m for m in errors)
        assert sum("available again" in m for m in infos) == 1


def test_a_failure_recording_the_row_never_costs_the_notification():
    """The detector exists to catch silent failures: the Activity row is recorded after, and apart from, the push."""
    coord = _make_coordinator(BASE_CONFIG)
    coord._notify_in_background = MagicMock()

    def broken(event_type, data):
        raise RuntimeError("event log unavailable")

    coord._emit_event = broken
    _states(coord, {"climate.thermostat": "heat", "weather.home": "unavailable"})
    issues = coord._run_entity_health_check()
    assert [i.entity_id for i in issues] == ["weather.home"], "the sweep result was lost"
    coord._notify_in_background.assert_called_once()
