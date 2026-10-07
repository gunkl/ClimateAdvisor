"""Issue #1029: every intentional early grace/fan-override ending is visible in the Activity Log.

Covers the two new events ``fan_override_cleared`` and ``grace_cancelled``:
- bedtime / morning wake-up with an RF-timer fan override + protecting manual grace
- cancel_override() fan-only (exactly one ``override_cleared``, no ``fan_override_cleared``)
- natural grace expiry (no ``fan_override_cleared``; ``grace_expired`` already covers it)
- superseded filter (manual/protecting grace replaced -> emit; automation grace -> quiet)
- nothing active -> neither event
- source attribution via ``_event_source_label``
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.climate_advisor.automation as _auto_mod
from custom_components.climate_advisor.ai_skills_context import _event_source_label
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import OCCUPANCY_HOME

_NOW = datetime(2026, 3, 20, 22, 0, 0)


def _consume_coroutine(coro):
    coro.close()


def _parse(s):
    return datetime.fromisoformat(s) if s else None


def _make_engine() -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    engine._occupancy_mode = OCCUPANCY_HOME
    return engine


def _make_classification() -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = "cold"
    obj.hvac_mode = "heat"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0.0
    obj.today_high = 55.0
    obj.today_low = 40.0
    obj.tomorrow_high = 56.0
    obj.tomorrow_low = 41.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _arm_rf_timer_override(engine: AutomationEngine, hours_left: float = 11.0) -> None:
    """Fan override from a 12 h RF timer plus its protecting manual grace."""
    engine._fan_override_active = True
    engine._fan_override_time = "2026-03-20T10:00:00"
    engine._fan_remote_timer_hours = 12.0
    engine._grace_active = True
    engine._grace_protects_override = True
    engine._last_resume_source = "manual"
    engine._last_grace_trigger = "fan_manual_override"
    engine._grace_end_time = (_NOW + timedelta(hours=hours_left)).isoformat()


def _wire(engine: AutomationEngine) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    return events


def _of(events, name):
    return [p for n, p in events if n == name]


def _run(coro_fn):
    with (
        patch.object(_auto_mod.dt_util, "now", lambda: _NOW),
        patch.object(_auto_mod.dt_util, "parse_datetime", _parse),
        patch.object(_auto_mod, "async_call_later", MagicMock(return_value=MagicMock())),
    ):
        return asyncio.run(coro_fn())


class TestBedtimeWakeupVisibility:
    def _check(self, handler_name: str, reason: str) -> None:
        engine = _make_engine()
        engine._current_classification = _make_classification()
        _arm_rf_timer_override(engine)
        events = _wire(engine)

        async def go():
            with patch.object(engine, "_set_temperature", new_callable=AsyncMock):
                await getattr(engine, handler_name)()

        _run(go)

        fan_cleared = _of(events, "fan_override_cleared")
        assert len(fan_cleared) == 1, events
        assert fan_cleared[0]["reason"] == reason
        assert fan_cleared[0]["source"] == "automation"
        assert fan_cleared[0]["remote_timer_hours"] == 12.0

        cancelled = _of(events, "grace_cancelled")
        assert len(cancelled) == 1, events
        assert cancelled[0]["reason"] == reason
        assert cancelled[0]["source"] == "automation"
        assert cancelled[0]["remote_timer_hours"] == 12.0
        assert cancelled[0]["remaining_minutes"] > 0
        assert cancelled[0]["trigger"] == "fan_manual_override"

    def test_bedtime_emits_both_events_once(self):
        self._check("handle_bedtime", "bedtime")

    def test_morning_wakeup_emits_both_events_once(self):
        self._check("handle_morning_wakeup", "morning_wakeup")

    def test_nothing_active_emits_neither(self):
        for handler in ("handle_bedtime", "handle_morning_wakeup"):
            engine = _make_engine()
            engine._current_classification = _make_classification()
            events = _wire(engine)

            async def go(engine=engine, handler=handler):
                with patch.object(engine, "_set_temperature", new_callable=AsyncMock):
                    await getattr(engine, handler)()

            _run(go)
            assert _of(events, "fan_override_cleared") == [], (handler, events)
            assert _of(events, "grace_cancelled") == [], (handler, events)


class TestCancelOverrideFanOnly:
    def test_fan_only_cancel_one_override_cleared_no_fan_override_cleared(self):
        engine = _make_engine()
        _arm_rf_timer_override(engine)
        events = _wire(engine)

        with (
            patch.object(_auto_mod.dt_util, "now", lambda: _NOW),
            patch.object(_auto_mod.dt_util, "parse_datetime", _parse),
        ):
            assert engine.cancel_override(reason="user_cancel_fan_override") is True

        assert len(_of(events, "override_cleared")) == 1, events
        assert _of(events, "fan_override_cleared") == [], events
        cancelled = _of(events, "grace_cancelled")
        assert len(cancelled) == 1, events
        assert cancelled[0]["reason"] == "user_cancel_fan_override"
        assert cancelled[0]["source"] == "manual"


class TestNaturalExpiry:
    def test_grace_expired_clear_emits_no_fan_override_cleared(self):
        engine = _make_engine()
        engine._fan_override_active = True
        engine._fan_override_time = "2026-03-20T10:00:00"
        events = _wire(engine)

        engine._clear_manual_override_active("grace_expired")

        assert engine._fan_override_active is False
        assert _of(events, "fan_override_cleared") == []

    def test_clear_fan_override_other_reason_emits_once_and_is_idempotent(self):
        engine = _make_engine()
        engine._fan_override_active = True
        engine._fan_remote_timer_hours = 3.0
        events = _wire(engine)

        engine.clear_fan_override(reason="something_else")
        engine.clear_fan_override(reason="something_else")

        cleared = _of(events, "fan_override_cleared")
        assert len(cleared) == 1
        assert cleared[0]["remote_timer_hours"] == 3.0


class TestSupersededFilter:
    def _replace(self, engine: AutomationEngine, source: str = "manual") -> None:
        with (
            patch.object(_auto_mod.dt_util, "now", lambda: _NOW),
            patch.object(_auto_mod.dt_util, "parse_datetime", _parse),
            patch.object(_auto_mod, "async_call_later", MagicMock(return_value=MagicMock())),
        ):
            engine._start_grace_period_action(source, "dashboard_resume")

    def test_manual_grace_replaced_emits_superseded(self):
        engine = _make_engine()
        _arm_rf_timer_override(engine, hours_left=2.0)
        events = _wire(engine)

        self._replace(engine, "manual")

        cancelled = _of(events, "grace_cancelled")
        assert len(cancelled) == 1, events
        assert cancelled[0]["reason"] == "superseded"
        assert cancelled[0]["source"] == "automation"
        assert cancelled[0]["remaining_minutes"] > 0

    def test_automation_grace_replaced_is_quiet(self):
        engine = _make_engine()
        engine._grace_active = True
        engine._grace_protects_override = False
        engine._last_resume_source = "automation"
        engine._last_grace_trigger = "sensor_closed_resume"
        engine._grace_end_time = (_NOW + timedelta(minutes=10)).isoformat()
        events = _wire(engine)

        self._replace(engine, "automation")

        assert _of(events, "grace_cancelled") == [], events

    def test_manual_grace_nearly_expired_is_quiet(self):
        engine = _make_engine()
        _arm_rf_timer_override(engine)
        engine._grace_end_time = (_NOW + timedelta(seconds=30)).isoformat()
        events = _wire(engine)

        self._replace(engine, "manual")

        assert _of(events, "grace_cancelled") == [], events


class TestEventSourceAttribution:
    def test_bedtime_payload_is_automation(self):
        assert _event_source_label("grace_cancelled", {"reason": "bedtime", "source": "automation"}) == "automation"
        assert (
            _event_source_label("fan_override_cleared", {"reason": "bedtime", "source": "automation"}) == "automation"
        )

    def test_user_cancel_payload_is_manual(self):
        assert (
            _event_source_label("grace_cancelled", {"reason": "user_cancel_override", "source": "manual"}) == "manual"
        )
        assert (
            _event_source_label("fan_override_cleared", {"reason": "user_cancel_fan_override", "source": "manual"})
            == "manual"
        )

    def test_override_event_source_helper(self):
        from custom_components.climate_advisor.automation import _override_event_source

        assert _override_event_source("user_cancel_override") == "manual"
        assert _override_event_source("user_cancel_fan_override") == "manual"
        assert _override_event_source("bedtime") == "automation"
        assert _override_event_source("superseded") == "automation"
