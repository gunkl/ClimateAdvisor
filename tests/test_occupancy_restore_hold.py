"""Issue #1050 (#1048 change 18): arriving home holds the comfort restore while a reason is active.

Occupant: coming home during a manual thermostat change (even an unconfirmed one), a door/window
pause or an open window no longer forces comfort anyway (and no longer gets flipped back off at the
next cycle); the push says comfort resumes once the reason clears. Leaving home while a manual change
is unconfirmed cancels it and applies the setback. Nat-vent behaviour on arrival is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.occupancy_fsm import (
    AwayVacationInputs,
    AwayVacationOutcome,
    HomeInputs,
    HomeNotifyOutcome,
    decide_away_vacation_dispatch,
    decide_home_dispatch,
)

_MOD = "custom_components.climate_advisor.automation"


def _consume(coro):
    coro.close()


def _state(mode: str = "cool") -> MagicMock:
    s = MagicMock()
    s.state = mode
    s.attributes = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1}
    return s


def _classification(hvac_mode: str = "cool") -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = "warm"
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0
    obj.today_high = 80.0
    obj.today_low = 60.0
    obj.tomorrow_high = 80.0
    obj.tomorrow_low = 60.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _engine(sensor_open: bool = False) -> tuple[AutomationEngine, list, AsyncMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume)
    hass.states.get.return_value = _state("cool")
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    eng = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    eng._current_classification = _classification("cool")
    eng._get_indoor_temp_f = lambda: 90.0  # far from comfort -> welcome-home push goes out
    eng._sensor_check_callback = lambda: sensor_open
    events: list[tuple[str, dict]] = []
    eng._emit_event_callback = lambda e, d: events.append((e, d))
    eng._notify = AsyncMock()
    eng.set_occupancy_mode("away")
    # The #1050 premise: the away setback reached the thermostat while the house was empty (Issue #1425: a deferred
    # restore is a "still on the setback" hold only then; a skipped setback is tests/test_away_band_chart_1425.py).
    eng._away_setback_sent = True
    hass.services.async_call.reset_mock()
    return eng, events, hass.services.async_call


def _home(eng: AutomationEngine) -> None:
    asyncio.run(eng.handle_occupancy_home())


def _names(events):
    return [e for e, _ in events]


class TestArrivingHomeHold:
    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            ("_manual_override_active", "manual_override"),
            ("_override_confirm_pending", "override_confirm_pending"),
            ("_paused_by_door", "paused_by_door"),
        ],
    )
    def test_flag_holds_restore(self, setup, reason, caplog):
        eng, events, call = _engine()
        setattr(eng, setup, True)
        with caplog.at_level("INFO"):
            _home(eng)
        assert call.await_count == 0, "no thermostat write while the restore is held"
        assert "occupancy_comfort_restored" not in _names(events)
        deferred = [d for e, d in events if e == "occupancy_restore_deferred"]
        assert len(deferred) == 1 and deferred[0]["reason"] == reason
        assert f"restore deferred: reason={reason}" in caplog.text
        msg = eng._notify.await_args.args[0]
        assert "Restoring comfort" not in msg and "resume once" in msg
        assert eng.occupancy_restore_hold_reason() == reason

    def test_open_sensor_holds_restore(self):
        eng, events, call = _engine(sensor_open=True)
        _home(eng)
        assert call.await_count == 0
        assert [d["reason"] for e, d in events if e == "occupancy_restore_deferred"] == ["open_sensor"]
        assert "occupancy_comfort_restored" not in _names(events)

    def test_nat_vent_with_open_sensor_is_unchanged(self):
        """Nat-vent owns the open window: arrival behaves exactly as before (restore proceeds)."""
        eng, events, _call = _engine(sensor_open=True)
        eng._natural_vent_active = True
        _home(eng)
        assert "occupancy_restore_deferred" not in _names(events)
        assert "occupancy_comfort_restored" in _names(events)
        assert eng.occupancy_restore_hold_reason() is None

    def test_no_hold_restores_as_before(self):
        eng, events, _call = _engine()
        _home(eng)
        assert "occupancy_comfort_restored" in _names(events)
        assert "occupancy_restore_deferred" not in _names(events)
        assert "Restoring comfort" in eng._notify.await_args.args[0]

    def test_deferred_event_is_windowed_deduped(self):
        eng, events, _call = _engine()
        eng._manual_override_active = True
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now", return_value=datetime.now().astimezone()
        ):
            _home(eng)
            _home(eng)
        assert _names(events).count("occupancy_restore_deferred") == 1

    def test_hold_clears_when_reason_clears_or_occupant_leaves(self):
        eng, _events, _call = _engine()
        eng._manual_override_active = True
        _home(eng)
        assert eng.occupancy_restore_hold_reason() == "manual_override"
        eng._manual_override_active = False
        assert eng.occupancy_restore_hold_reason() is None
        eng._manual_override_active = True  # a new unrelated override must not look like a stale hold
        assert eng.occupancy_restore_hold_reason() is None

    def test_off_day_defers_like_any_other_day(self):
        """Issue #1208: warm/mild days (label "off") arm the comfort band, so the arrival restore exists and the
        #1050 hold applies to it. Rewritten from ``test_off_day_has_nothing_to_defer`` (its premise was the bug)."""
        eng, events, call = _engine()
        eng._current_classification = _classification("off")
        eng._manual_override_active = True
        _home(eng)
        assert "occupancy_restore_deferred" in _names(events)
        assert "occupancy_comfort_restored" not in _names(events)


class TestLeavingWithPendingChange:
    def test_away_cancels_unconfirmed_change_and_applies_setback(self):
        eng, events, call = _engine()
        eng.set_occupancy_mode("home")
        eng._override_confirm_pending = True
        asyncio.run(eng.handle_occupancy_away())
        assert eng._override_confirm_pending is False
        assert "occupancy_setback" in _names(events)
        assert call.await_count >= 1

    def test_vacation_cancels_unconfirmed_change(self):
        eng, _events, _call = _engine()
        eng._override_confirm_pending = True
        asyncio.run(eng.handle_occupancy_vacation())
        assert eng._override_confirm_pending is False


class TestPureDecisions:
    def _home_inputs(self, **kw):
        base = {
            "has_classification": True,
            "hvac_mode": "cool",
            "indoor_temp_f": 90.0,
            "comfort_f": 75.0,
            "setback_f": 80.0,
            "debounce_seconds": 0.0,
            "seconds_since_last_notified": None,
        }
        base.update(kw)
        return HomeInputs(**base)

    def test_hold_defers_restore_but_keeps_notify(self):
        d = decide_home_dispatch(self._home_inputs(restore_hold="paused_by_door"))
        assert d.restore is False and d.restore_deferred_reason == "paused_by_door"
        assert d.notify is HomeNotifyOutcome.SEND

    def test_default_inputs_unchanged(self):
        d = decide_home_dispatch(self._home_inputs())
        assert d.restore is True and d.restore_deferred_reason is None

    def test_away_clear_override_includes_pending(self):
        d = decide_away_vacation_dispatch(
            AwayVacationInputs(
                paused_by_door=False,
                manual_override_active=False,
                has_classification=True,
                override_confirm_pending=True,
            )
        )
        assert d.outcome is AwayVacationOutcome.APPLY and d.clear_override is True
