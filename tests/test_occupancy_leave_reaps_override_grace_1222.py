"""Issue #1222: leaving home must end a cleared override's grace, not leave it for the stuck-grace watchdog.

Occupant story: you nudged the thermostat (or pressed the fan) and then everyone left. Climate Advisor drops your
override for the away/vacation setback. The grace period that protected that override has nothing left to protect,
so it must end with it. Before the fix it kept running until the coordinator's orphaned-grace watchdog reaped it
with an ERROR ("Stuck grace detected") up to a cycle later, and the dashboard said "Grace period active" for a
home that was already in setback (live: 2026-11-01 13:07:41 to 13:12:41).

Both away and vacation, both fan modes (``whole_house_fan``, ``hvac_fan``; ``both`` is unsupported), three override
shapes: the confirmed thermostat change, the same change with the occupant's fan override beside it (RF-timer hours
must reach the ``grace_cancelled`` row), and a change still waiting for confirmation. Controls: a fan-off hold, a
window-close resume and a nat-vent-exit grace are never override-protecting and must survive; so must a grace when
nothing was cleared.

These call the real ``handle_occupancy_away()`` / ``handle_occupancy_vacation()``; the real-HA twin is
``tests_ha/test_occupancy_leave_reaps_override_grace_1222.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.climate_advisor.automation as _auto_mod
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE, OCCUPANCY_HOME
from custom_components.climate_advisor.override_orphaned_grace import decide_orphaned_grace
from tests.test_grace_cancel_observability import _make_classification

_NOW = datetime(2026, 11, 1, 13, 7, 41)
_FAN_MODES = [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC]
_LEAVE = [("handle_occupancy_away", "occupancy_away"), ("handle_occupancy_vacation", "occupancy_vacation")]


def _consume_coroutine(coro):
    coro.close()


def _parse(s):
    return datetime.fromisoformat(s) if s else None


def _make_engine(fan_mode: str) -> AutomationEngine:
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
        CONF_FAN_MODE: fan_mode,
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    engine._current_classification = _make_classification()
    engine._occupancy_mode = OCCUPANCY_HOME
    return engine


def _arm_grace(engine: AutomationEngine, trigger: str, *, protects: bool, source: str = "manual") -> None:
    engine._grace_active = True
    engine._grace_protects_override = protects
    engine._last_resume_source = source
    engine._last_grace_trigger = trigger
    engine._grace_end_time = (_NOW + timedelta(minutes=5)).isoformat()


def _arm_thermostat_override(engine: AutomationEngine) -> None:
    engine._manual_override_active = True
    engine._manual_override_mode = "heat"
    engine._manual_override_time = _NOW.isoformat()
    _arm_grace(engine, "override_confirmed", protects=True)


def _arm_fan_beside_override(engine: AutomationEngine, fan_mode: str) -> None:
    """The occupant's fan override beside the thermostat change; the fan override's grace is the live one."""
    _arm_thermostat_override(engine)
    engine._fan_override_active = True
    engine._fan_override_time = _NOW.isoformat()
    engine._fan_override_is_hvac_fan = fan_mode == FAN_MODE_HVAC
    engine._fan_remote_timer_hours = 12.0
    _arm_grace(engine, "fan_manual_override", protects=True)


def _wire(engine: AutomationEngine) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    return events


def _of(events, name):
    return [p for n, p in events if n == name]


def _leave(engine: AutomationEngine, handler: str) -> None:
    async def go():
        with patch.object(engine, "_apply_comfort_band", new_callable=AsyncMock):
            await getattr(engine, handler)()

    with (
        patch.object(_auto_mod.dt_util, "now", lambda: _NOW),
        patch.object(_auto_mod.dt_util, "parse_datetime", _parse),
        patch.object(_auto_mod, "async_call_later", MagicMock(return_value=MagicMock())),
    ):
        asyncio.run(go())


def _watchdog_would_reap(engine: AutomationEngine) -> bool:
    """The coordinator's own orphaned-grace predicate, read off the live engine."""
    return decide_orphaned_grace(
        grace_active=bool(engine._grace_active),
        grace_protects_override=bool(engine._grace_protects_override),
        manual_override_active=bool(engine._manual_override_active),
        fan_override_active=bool(engine._fan_override_active),
        override_confirm_pending=bool(engine._override_confirm_pending),
    )


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(("handler", "reason"), _LEAVE)
class TestLeavingHomeEndsTheClearedOverridesGrace:
    def test_thermostat_override_grace_ends_with_the_override(self, fan_mode, handler, reason):
        engine = _make_engine(fan_mode)
        _arm_thermostat_override(engine)
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._manual_override_active is False
        assert engine._grace_active is False, "the grace protected only the cleared override"
        assert not _watchdog_would_reap(engine), "nothing left for the stuck-grace watchdog to reap"
        cancelled = _of(events, "grace_cancelled")
        assert [c["reason"] for c in cancelled] == [reason], events
        assert cancelled[0]["trigger"] == "override_confirmed"
        assert cancelled[0]["source"] == "automation"
        assert not _of(events, "stuck_grace_recovered")

    def test_fan_override_beside_it_ends_too_and_keeps_the_rf_timer_hours(self, fan_mode, handler, reason):
        engine = _make_engine(fan_mode)
        _arm_fan_beside_override(engine, fan_mode)
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._fan_override_active is False
        assert engine._grace_active is False
        cancelled = _of(events, "grace_cancelled")
        assert [c["reason"] for c in cancelled] == [reason], events
        assert cancelled[0]["trigger"] == "fan_manual_override"
        assert cancelled[0]["remote_timer_hours"] == 12.0, "the pre-clear snapshot, not the wiped live value"
        assert not _of(events, "stuck_grace_recovered")

    def test_change_still_awaiting_confirmation_ends_its_grace(self, fan_mode, handler, reason):
        engine = _make_engine(fan_mode)
        engine._override_confirm_pending = True
        engine._override_confirm_mode = "heat"
        engine._override_confirm_time = _NOW.isoformat()
        _arm_grace(engine, "override_confirmed", protects=True)
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._override_confirm_pending is False
        assert engine._grace_active is False
        assert [c["reason"] for c in _of(events, "grace_cancelled")] == [reason], events

    @pytest.mark.parametrize("trigger", ["fan_off", "window_close", "nat_vent_exit"])
    def test_non_override_graces_survive_when_nothing_was_cleared(self, fan_mode, handler, reason, trigger):
        engine = _make_engine(fan_mode)
        _arm_grace(engine, trigger, protects=False, source="automation")
        if trigger == "fan_off":
            engine._fan_off_hold_until = _NOW + timedelta(minutes=5)
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._grace_active is True, f"a {trigger} grace is not an override's grace"
        assert not _of(events, "grace_cancelled")
        if trigger == "fan_off":
            assert engine._fan_off_hold_until is not None, "a fan-off hold survives away/vacation"

    @pytest.mark.parametrize("trigger", ["fan_off", "window_close", "nat_vent_exit"])
    def test_non_override_graces_survive_even_when_an_override_is_cleared(self, fan_mode, handler, reason, trigger):
        engine = _make_engine(fan_mode)
        engine._manual_override_active = True  # an override with no grace of its own
        engine._manual_override_mode = "heat"
        _arm_grace(engine, trigger, protects=False, source="automation")
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._manual_override_active is False
        assert engine._grace_active is True
        assert not _of(events, "grace_cancelled")


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(
    ("handler", "reason"), [("handle_bedtime", "bedtime"), ("handle_morning_wakeup", "morning_wakeup")]
)
class TestBedtimeAndWakeupShareTheHelper:
    """#874's two callers now go through the same helper as leaving home; their behaviour is unchanged."""

    def test_thermostat_override_grace_ends_and_the_rf_hours_ride_along(self, fan_mode, handler, reason):
        engine = _make_engine(fan_mode)
        # bedtime/wake-up defer to a live thermostat override, so what they clear is the fan override (RF timer)
        _arm_fan_beside_override(engine, fan_mode)
        engine._manual_override_active = False
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._grace_active is False and engine._manual_override_active is False
        cancelled = _of(events, "grace_cancelled")
        assert [c["reason"] for c in cancelled] == [reason], events
        assert cancelled[0]["remote_timer_hours"] == 12.0
        assert not _of(events, "stuck_grace_recovered")

    def test_fan_off_grace_survives(self, fan_mode, handler, reason):
        engine = _make_engine(fan_mode)
        engine._fan_override_active = True
        _arm_grace(engine, "fan_off", protects=False, source="automation")
        engine._fan_off_hold_until = _NOW + timedelta(minutes=5)
        events = _wire(engine)

        _leave(engine, handler)

        assert engine._grace_active is True and engine._fan_off_hold_until is not None
        assert not _of(events, "grace_cancelled")
