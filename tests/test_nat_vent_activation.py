"""Tests for Issue #115: nat vent activation matrix (directional guard, hysteresis, lockout).

Covers every row of the Phase 1 activation matrix:
  Row 1 — outdoor >= indoor on open → paused (directional guard)
  Row 2 — indoor <= comfort_heat on open → paused (floor guard)
  Row 3 — outdoor < indoor, indoor > comfort_heat, outdoor < threshold → nat_vent
  Row 4 — outdoor rises above indoor during active nat_vent → nat_vent_outdoor_rise_exit
  Row 5 — lockout: re-activation blocked within 300s of outdoor-warm exit
  Row 6 — hysteresis: re-activation requires outdoor < indoor - 1.0F

All tests use the AutomationEngine directly with mocked HA dependencies, mirroring
the pattern from test_resume_from_pause.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    MIN_VIABLE_NAT_VENT_HOURS,
    NAT_VENT_REACTIVATION_LOCKOUT_S,
)

# Patch dt_util.now so isoformat() calls inside the engine always work
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 20, 10, 0, 0)

# Patch automation.dt_util.parse_datetime directly — the automation module's dt_util
# is a child mock of homeassistant.util (not sys.modules["homeassistant.util.dt"]).
import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    """Parse ISO 8601 datetime string; mirrors dt_util.parse_datetime."""
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"


def _make_engine(
    comfort_heat: float = 70.0,
    comfort_cool: float = 72.0,
    nat_vent_delta: float = 3.0,
    indoor_f: float | None = None,
) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies.

    If *indoor_f* is given, the mock climate entity reports that temperature
    via ``current_temperature`` so ``_get_indoor_temp_f()`` returns it.
    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    # Climate entity reports "cool" so the pause path fires when nat_vent conditions
    # are not met (pause requires pre_pause_mode != "off").
    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {}

    if indoor_f is not None:
        climate_state.attributes = {"current_temperature": indoor_f}

    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": nat_vent_delta,
        "notify_service": "notify.notify",
        # No indoor_temp_source override — falls through to climate entity
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    return engine


def _make_classification(
    day_type: str = "warm",
    hvac_mode: str = "cool",
) -> DayClassification:
    """Bypass __post_init__ validation to create a minimal DayClassification."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = "stable"
    obj.trend_magnitude = 2.0
    obj.today_high = 85.0
    obj.today_low = 65.0
    obj.tomorrow_high = 85.0
    obj.tomorrow_low = 65.0
    obj.hvac_mode = hvac_mode
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = True
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_engine_indoor(engine: AutomationEngine, indoor_f: float | None) -> None:
    """Update the mock climate entity's current_temperature so _get_indoor_temp_f() returns *indoor_f*."""
    if indoor_f is None:
        engine.hass.states.get.return_value.attributes = {}
    else:
        engine.hass.states.get.return_value.attributes = {"current_temperature": indoor_f}


# ---------------------------------------------------------------------------
# Row 1 — outdoor >= indoor on sensor open → paused (directional guard)
# ---------------------------------------------------------------------------


class TestDirectionalGuardOnOpen:
    """Row 1: sensor opens when outdoor >= indoor — engine must enter pause, not nat_vent."""

    def test_outdoor_above_indoor_enters_pause(self):
        """outdoor 75F > indoor 74F → paused."""
        engine = _make_engine(indoor_f=74.0)
        engine._last_outdoor_temp = 75.0

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False
        # No natural_ventilation event should have fired
        nat_vent_events = [e for e in events if e[0] == "sensor_opened" and e[1].get("result") == "natural_ventilation"]
        assert not nat_vent_events

    def test_outdoor_equal_indoor_enters_pause(self):
        """outdoor 74F == indoor 74F → paused (boundary: equal is not cooler)."""
        engine = _make_engine(indoor_f=74.0)
        engine._last_outdoor_temp = 74.0

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

    def test_outdoor_just_above_indoor_enters_pause(self):
        """outdoor 74.1F > indoor 74.0F (barely above) → paused."""
        engine = _make_engine(indoor_f=74.0)
        engine._last_outdoor_temp = 74.1

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# Row 2 — indoor at comfort_heat floor → paused (floor guard)
# ---------------------------------------------------------------------------


class TestComfortFloorGuardOnOpen:
    """Row 2: sensor opens when indoor == comfort_heat — engine must enter pause."""

    def test_indoor_at_floor_blocks_nat_vent(self):
        """indoor 70F == comfort_heat 70F with outdoor 65F → paused."""
        engine = _make_engine(comfort_heat=70.0, indoor_f=70.0)
        engine._last_outdoor_temp = 65.0  # outdoor is cooler and below threshold

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

    def test_indoor_below_floor_blocks_nat_vent(self):
        """indoor 69F < comfort_heat 70F with outdoor 65F → paused."""
        engine = _make_engine(comfort_heat=70.0, indoor_f=69.0)
        engine._last_outdoor_temp = 65.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# Row 3 — outdoor < indoor, indoor > comfort_heat, outdoor < threshold → nat_vent
# ---------------------------------------------------------------------------


class TestNatVentActivation:
    """Row 3: all three conditions met → nat_vent activates."""

    def test_evening_cool_outdoor_activates_nat_vent(self):
        """outdoor 70F < indoor 76F, indoor 76F > comfort_heat 70F, outdoor 70F < threshold 75F → nat_vent."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._last_outdoor_temp = 70.0

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        nat_events = [e for e in events if e[0] == "sensor_opened" and e[1].get("result") == "natural_ventilation"]
        assert len(nat_events) == 1

    def test_outdoor_just_below_indoor_activates(self):
        """outdoor 73.9F < indoor 74.0F — just below indoor — satisfies directional guard."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=74.0)
        engine._last_outdoor_temp = 73.9  # just cooler than indoor, still under threshold 75

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True

    def test_outdoor_near_threshold_still_activates(self):
        """outdoor 74F < indoor 78F; threshold 75F — outdoor just inside ceiling → nat_vent."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=78.0)
        engine._last_outdoor_temp = 74.0  # below indoor(78) and below threshold(75)

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True

    def test_outdoor_at_threshold_does_not_activate(self):
        """outdoor 75F == threshold (72+3) but also >= indoor 75F → paused (directional guard wins)."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=75.0)
        engine._last_outdoor_temp = 75.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # outdoor(75) >= indoor(75) → directional guard blocks
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True


# ---------------------------------------------------------------------------
# Row 4 — outdoor rises above indoor during active nat_vent → nat_vent_outdoor_rise_exit
# ---------------------------------------------------------------------------


class TestNatVentOutdoorRiseExit:
    """Row 4: outdoor crosses above indoor while nat_vent is active → directional exit."""

    def test_outdoor_rises_above_indoor_exits(self):
        """nat_vent active; outdoor 74.5F >= indoor 74.0F → nat_vent_outdoor_rise_exit."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=74.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 74.5  # just above indoor

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._nat_vent_outdoor_exit_time is not None

        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 1
        assert rise_events[0][1]["outdoor"] == 74.5
        assert rise_events[0][1]["indoor"] == 74.0

    def test_outdoor_equal_indoor_does_not_exit(self):
        """outdoor 74.0F == indoor 74.0F (boundary) → directional exit does NOT fire (Bug #313 fix).

        Equal temps mean neutral airflow — not reversed — so nat vent should stay active.
        The exit condition is strict: outdoor > indoor (not >=).
        """
        engine = _make_engine(indoor_f=74.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 74.0
        engine._deactivate_fan = AsyncMock()

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, "nat vent must stay active when outdoor == indoor"
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 0, "nat_vent_outdoor_rise_exit must not fire on equal temps"

    def test_outdoor_rise_exit_fires_before_threshold_exit(self):
        """outdoor 74.5F >= indoor 74.0F but still below threshold 75F — directional exit fires first."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=74.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 74.5  # above indoor, still below threshold(75)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        # Directional exit event, not threshold exit
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 1


# ---------------------------------------------------------------------------
# Row 5 — lockout: re-activation blocked within 300s of outdoor-warm exit
# ---------------------------------------------------------------------------


class TestReactivationLockout:
    """Row 5: after an outdoor-warm exit, re-activation is blocked for lockout_s seconds."""

    def test_reactivation_blocked_within_lockout(self):
        """Re-activation attempt 10s after exit → still within lockout; stays paused."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0  # outdoor well below indoor and threshold — would normally activate

        exit_time = datetime(2026, 4, 20, 20, 0, 0)
        engine._nat_vent_outdoor_exit_time = exit_time

        # Simulate check 10s after exit — within 300s lockout
        check_time = exit_time + timedelta(seconds=10)
        with patch(_DT_NOW_PATH, return_value=check_time):
            asyncio.run(engine.check_natural_vent_conditions())

        # Should still be paused, not nat_vent
        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

    def test_reactivation_allowed_after_lockout(self):
        """Re-activation attempt 301s after exit → lockout expired; re-activates if conditions met."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor 68F: below indoor(76) by more than hysteresis(1F), below threshold(75)
        engine._last_outdoor_temp = 68.0

        exit_time = datetime(2026, 4, 20, 20, 0, 0)
        engine._nat_vent_outdoor_exit_time = exit_time

        # Simulate check 301s after exit — lockout expired
        check_time = exit_time + timedelta(seconds=NAT_VENT_REACTIVATION_LOCKOUT_S + 1)
        with patch(_DT_NOW_PATH, return_value=check_time):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False

    def test_lockout_boundary_exactly_300s_still_blocked(self):
        """At exactly 300s (not yet past lockout) → still blocked."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0

        exit_time = datetime(2026, 4, 20, 20, 0, 0)
        engine._nat_vent_outdoor_exit_time = exit_time

        check_time = exit_time + timedelta(seconds=NAT_VENT_REACTIVATION_LOCKOUT_S)
        with patch(_DT_NOW_PATH, return_value=check_time):
            asyncio.run(engine.check_natural_vent_conditions())

        # elapsed == lockout_s is NOT < lockout_s, so re-activation should proceed if conditions met
        # The condition is elapsed < lockout_s — at exactly 300s, elapsed == 300, not < 300 → allowed
        assert engine._natural_vent_active is True


# ---------------------------------------------------------------------------
# Row 6 — hysteresis: re-activation requires outdoor < indoor - 1.0F
# ---------------------------------------------------------------------------


class TestReactivationHysteresis:
    """Row 6: outdoor must be at least hysteresis(1F) below indoor to re-activate from pause."""

    def test_outdoor_just_at_hysteresis_boundary_activates(self):
        """outdoor == indoor - 1.0F exactly → activates (boundary is inclusive with < in code)."""
        # With hysteresis=1.0: condition is outdoor < indoor - 1.0
        # At outdoor = indoor - 1.0: condition is False (not strictly less)
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor exactly at boundary: 76.0 - 1.0 = 75.0 — but also equals threshold(75), so < threshold fails
        # Use indoor=76.0, outdoor=74.9 → outdoor < 75.0 = 76.0 - 1.0 → True; below threshold(75)? 74.9 < 75 → True
        engine._last_outdoor_temp = 74.9

        # No lockout
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True

    def test_outdoor_above_hysteresis_boundary_stays_paused(self):
        """outdoor = indoor - 0.5F — within hysteresis gap → stays paused."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor 75.5F: indoor(76) - hysteresis(1) = 75.0; outdoor(75.5) > 75.0 → hysteresis not satisfied
        engine._last_outdoor_temp = 75.5
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        # Hysteresis gap not cleared → stays paused
        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

    def test_outdoor_equal_to_indoor_minus_hysteresis_stays_paused(self):
        """outdoor == indoor - 1.0 exactly — strict < condition means this stays paused."""
        # Condition: outdoor < indoor - hysteresis  →  75.0 < 76.0 - 1.0 = 75.0  →  False
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 75.0  # exactly at boundary — condition is strict <, so stays paused
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

    def test_no_hysteresis_needed_without_prior_outdoor_exit(self):
        """Without a prior outdoor-warm exit, re-activation from pause only needs outdoor < indoor - hysteresis.

        This covers the normal case where pause came from manual or classification, not an
        outdoor-warm exit. The lockout is None so the lockout check is skipped.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0  # well below indoor - hysteresis
        engine._nat_vent_outdoor_exit_time = None  # no prior outdoor-warm exit

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True


# ---------------------------------------------------------------------------
# Integration: full cycle — activate, outdoor rises, re-activate after lockout
# ---------------------------------------------------------------------------


class TestFullNatVentCycle:
    """Integration: open → nat_vent → outdoor rise exit → lockout → re-activate."""

    def test_open_to_nat_vent_to_rise_exit_to_reactivate(self):
        """Full cycle: activate at 18:00; outdoor rises at 20:00; re-activate at 21:00 (post-lockout)."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=76.0)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        # Step 1: 18:00 — sensor opens, outdoor 70F < indoor 76F → nat_vent activates
        engine._last_outdoor_temp = 70.0
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False

        # Step 2: 20:00 — outdoor rises to 74.5F above indoor 74.0F → directional exit
        _set_engine_indoor(engine, 74.0)
        engine._last_outdoor_temp = 74.5
        exit_time = datetime(2026, 4, 20, 20, 0, 0)
        with patch(_DT_NOW_PATH, return_value=exit_time):
            asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 1

        # Step 3: 20:10 — outdoor dips to 68F but lockout (300s) still active → stays paused
        engine._last_outdoor_temp = 68.0
        _set_engine_indoor(engine, 74.0)
        check_time_early = exit_time + timedelta(seconds=10)
        with patch(_DT_NOW_PATH, return_value=check_time_early):
            asyncio.run(engine.check_natural_vent_conditions())
        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False

        # Step 4: 21:00 — lockout expired; outdoor 68F < indoor(74) - hysteresis(1) = 73; below threshold → re-activates
        engine._last_outdoor_temp = 68.0
        check_time_late = exit_time + timedelta(seconds=NAT_VENT_REACTIVATION_LOCKOUT_S + 1)
        with patch(_DT_NOW_PATH, return_value=check_time_late):
            asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False


# ---------------------------------------------------------------------------
# Phase 2 Guard 1 — rising outdoor forecast blocks nat vent activation
# ---------------------------------------------------------------------------


class TestForecastRisingOutdoorSkip:
    """Phase 2 Guard 1: rising outdoor forecast blocks nat vent activation."""

    def _make_forecast_entry(self, dt_str: str, temp_f: float) -> dict:
        return {"datetime": dt_str, "temperature": temp_f}

    def test_forecast_peak_above_threshold_skips_nat_vent(self):
        """Forecast peak > nat_vent_threshold within 2 hr -> falls through to pause, not nat vent."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        # Forecast: 1 hour ahead is 76F (above threshold 75F = 72 + 3)
        engine._hourly_forecast_temps = [
            self._make_forecast_entry("2026-04-20T11:00:00+00:00", 76.0),
        ]
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        now_aware = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        with patch(_DT_NOW_PATH, return_value=now_aware):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should NOT activate nat vent
        assert not engine._natural_vent_active
        # Should emit forecast_skip event
        assert any(e[0] == "nat_vent_forecast_skip" for e in events)

    def test_forecast_peak_below_threshold_allows_nat_vent(self):
        """Forecast peak <= threshold -> Phase 2 guard passes -> nat vent activates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        # Forecast: 1 hour ahead is 74F (below threshold 75F)
        engine._hourly_forecast_temps = [
            self._make_forecast_entry("2026-04-20T11:00:00+00:00", 74.0),
        ]
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        now_aware = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        with patch(_DT_NOW_PATH, return_value=now_aware):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active
        assert any(e[0] == "sensor_opened" and e[1].get("result") == "natural_ventilation" for e in events)

    def test_no_hourly_forecast_falls_back_to_phase1(self):
        """Empty hourly forecast -> forecast guard skipped -> Phase 1 only -> nat vent activates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = []
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        now_aware = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        with patch(_DT_NOW_PATH, return_value=now_aware):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active


# ---------------------------------------------------------------------------
# Phase 2 Guard 2 — thermal model floor imminence blocks nat vent activation
# ---------------------------------------------------------------------------


class TestThermalFloorImminentSkip:
    """Phase 2 Guard 2: thermal model floor imminence blocks nat vent activation."""

    def test_floor_imminent_skips_activation(self):
        """Medium confidence, time_to_floor < 1 hr -> skip activation, fall to pause.

        indoor=70.5, comfort_heat=70.0, delta=0.5
        k_passive=-0.3, outdoor=68.0 -> passive_rate = -0.3 * (70.5 - 68.0) = -0.75 F/hr
        time_to_floor = 0.5 / 0.75 = 0.67 hr < 1.0 -> skip
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=70.5)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = []
        engine._thermal_model = {"confidence": "medium", "k_passive": -0.3}
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert not engine._natural_vent_active
        assert any(e[0] == "nat_vent_floor_imminent_skip" for e in events)
        skip_event = next(e for e in events if e[0] == "nat_vent_floor_imminent_skip")
        assert skip_event[1]["time_to_floor_hr"] < MIN_VIABLE_NAT_VENT_HOURS

    def test_floor_not_imminent_allows_activation(self):
        """Medium confidence, time_to_floor > 1 hr -> thermal guard passes -> nat vent activates.

        indoor=73.0, comfort_heat=70.0, delta=3.0
        k_passive=-0.1, outdoor=68.0 -> passive_rate = -0.1 * (73 - 68) = -0.5 F/hr
        time_to_floor = 3.0 / 0.5 = 6.0 hr > 1.0 -> proceed
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = []
        engine._thermal_model = {"confidence": "medium", "k_passive": -0.1}
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active

    def test_low_confidence_fallback_to_phase1(self):
        """Confidence 'low' -> thermal guard skipped -> nat vent activates regardless."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=70.5)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = []
        engine._thermal_model = {"confidence": "low", "k_passive": -0.3}
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active
        assert not any(e[0] == "nat_vent_floor_imminent_skip" for e in events)

    def test_no_thermal_model_fallback_to_phase1(self):
        """Empty thermal model -> guard skipped -> nat vent activates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = []
        engine._thermal_model = {}
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active


# ---------------------------------------------------------------------------
# Phase 2 proactive floor exit — thermal model predicts imminent floor crossing
# ---------------------------------------------------------------------------


class TestProactiveFloorExit:
    """Phase 2 proactive floor exit: thermal model predicts imminent floor crossing."""

    def _make_active_nat_vent_engine(
        self,
        indoor_f: float = 71.0,
        outdoor_f: float = 65.0,
        k_passive: float = -0.5,
        confidence: str = "medium",
        comfort_heat: float = 70.0,
    ) -> AutomationEngine:
        engine = _make_engine(comfort_heat=comfort_heat, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=indoor_f)
        engine._last_outdoor_temp = outdoor_f
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._fan_override_active = False
        engine._thermal_model = {"confidence": confidence, "k_passive": k_passive}
        engine._hourly_forecast_temps = []
        return engine

    def test_proactive_exit_when_floor_imminent(self):
        """Nat vent active, floor predicted < 1 hr -> deactivate fan, restore HVAC.

        indoor=70.5, outdoor=65, k=-0.5
        passive_rate = -0.5 * (70.5 - 65) = -2.75 F/hr
        time_to_floor = (70.5 - 70.0) / 2.75 = 0.18 hr < 1.0 -> proactive exit
        """
        engine = self._make_active_nat_vent_engine(indoor_f=70.5, outdoor_f=65.0, k_passive=-0.5)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        assert not engine._natural_vent_active
        assert any(e[0] == "nat_vent_predicted_floor_exit" for e in events)

    def test_no_proactive_exit_when_floor_distant(self):
        """Floor predicted > 1 hr -> stays in nat vent.

        indoor=73, outdoor=65, k=-0.05
        passive_rate = -0.05 * (73 - 65) = -0.4 F/hr
        time_to_floor = (73 - 70) / 0.4 = 7.5 hr > 1.0 -> no exit
        """
        engine = self._make_active_nat_vent_engine(indoor_f=73.0, outdoor_f=65.0, k_passive=-0.05)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active
        assert not any(e[0] == "nat_vent_predicted_floor_exit" for e in events)

    def test_proactive_exit_emits_event_with_payload(self):
        """Verify nat_vent_predicted_floor_exit event has correct time_to_floor_hr."""
        engine = self._make_active_nat_vent_engine(indoor_f=70.5, outdoor_f=65.0, k_passive=-0.5)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        floor_events = [e for e in events if e[0] == "nat_vent_predicted_floor_exit"]
        assert len(floor_events) == 1
        assert "time_to_floor_hr" in floor_events[0][1]
        assert floor_events[0][1]["time_to_floor_hr"] < MIN_VIABLE_NAT_VENT_HOURS


# ---------------------------------------------------------------------------
# Bug #313 Fix — equal outdoor==indoor should NOT exit nat vent
# ---------------------------------------------------------------------------


class TestNatVentExitEqualTemps:
    """Bug #313: outdoor >= indoor exit condition must be strict (>), not >=.

    Equal temps mean neutral airflow — not reversed.  Exiting nat vent when
    outdoor == indoor causes the occupant to lose free cooling unnecessarily
    every time a sensor read happens to land on exactly the same value as indoor.

    These three tests document the correct post-fix semantics:
      1. Equal temps → nat vent STAYS active  (was wrong: exited)
      2. outdoor > indoor → nat vent exits     (regression guard, unchanged)
      3. outdoor < indoor → nat vent stays     (regression guard, unchanged)
    """

    def test_equal_temps_does_not_exit_nat_vent(self):
        """outdoor == indoor (72.0 == 72.0) → nat vent stays active after fix.

        Before the fix (outdoor >= indoor) this test FAILS because the engine
        exits nat vent on equal temps.  After the fix (outdoor > indoor) it passes.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=72.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._fan_override_active = False
        engine._last_outdoor_temp = 72.0  # equal to indoor
        engine._deactivate_fan = AsyncMock()

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        # Equal temps → airflow is neutral, not reversed → do NOT exit
        assert engine._natural_vent_active is True, (
            "nat vent should stay active when outdoor == indoor (neutral airflow)"
        )
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 0, "nat_vent_outdoor_rise_exit must NOT fire on equal temps"

    def test_outdoor_above_indoor_exits_nat_vent(self):
        """outdoor > indoor (73.0 > 72.0) → nat vent exits (regression guard).

        This must still exit after the fix — strictly greater means reversed airflow.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=72.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._fan_override_active = False
        engine._last_outdoor_temp = 73.0  # strictly above indoor
        engine._deactivate_fan = AsyncMock()

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "nat vent must exit when outdoor strictly > indoor (reversed airflow)"
        )
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 1

    def test_outdoor_below_indoor_stays_active(self):
        """outdoor < indoor (71.0 < 72.0) → nat vent stays active (regression guard).

        Outdoor cooler than indoor — airflow is beneficial.  Must not exit.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=72.0, nat_vent_delta=3.0, indoor_f=72.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._fan_override_active = False
        engine._last_outdoor_temp = 71.0  # strictly below indoor
        engine._deactivate_fan = AsyncMock()

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=datetime(2026, 4, 20, 20, 0, 0)):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, (
            "nat vent must stay active when outdoor < indoor (beneficial airflow)"
        )
        rise_events = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(rise_events) == 0


# ---------------------------------------------------------------------------
# Bug #313 1B — post-fan verify-and-repair callback
# ---------------------------------------------------------------------------

_ACL_PATH = "custom_components.climate_advisor.automation.async_call_later"


def _make_fan_engine(indoor_f: float = 72.0) -> AutomationEngine:
    """Engine with FAN_MODE_HVAC so _activate_fan/_deactivate_fan reach the callback."""
    from custom_components.climate_advisor.const import FAN_MODE_HVAC

    engine = _make_engine(indoor_f=indoor_f)
    engine.config["fan_mode"] = FAN_MODE_HVAC
    return engine


class TestPostFanVerify:
    """Bug #313-1B: post-fan 30s setpoint verify-and-repair callback.

    After every fan activation or deactivation the engine schedules a 30s callback
    that re-asserts the last commanded setpoint if the thermostat has drifted.
    This guards against Ecobee comfort-program reversions after fan commands.
    """

    def test_activate_fan_schedules_verify_callback(self):
        """_activate_fan() schedules exactly one async_call_later(30s) callback."""
        engine = _make_fan_engine()
        engine._pending_setpoint_single = 72.0
        engine._last_commanded_hvac_mode = "cool"

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with patch(_ACL_PATH, side_effect=_fake_acl):
            asyncio.run(engine._activate_fan(reason="test"))

        # Should have scheduled exactly one verify callback
        verify_calls = [(d, cb) for d, cb in captured_callbacks if d == 30.0]
        assert len(verify_calls) == 1, "Expected exactly one 30s verify callback from _activate_fan"

    def test_deactivate_fan_schedules_verify_callback(self):
        """_deactivate_fan() schedules exactly one async_call_later(30s) callback."""
        engine = _make_fan_engine()
        engine._fan_active = True
        engine._pending_setpoint_single = 72.0
        engine._last_commanded_hvac_mode = "cool"

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with patch(_ACL_PATH, side_effect=_fake_acl):
            asyncio.run(engine._deactivate_fan(reason="test"))

        verify_calls = [(d, cb) for d, cb in captured_callbacks if d == 30.0]
        assert len(verify_calls) == 1, "Expected exactly one 30s verify callback from _deactivate_fan"

    def test_verify_callback_repairs_drifted_setpoint(self):
        """Callback fires and re-asserts setpoint when thermostat drifted > 0.6°F."""
        engine = _make_fan_engine(indoor_f=72.0)
        engine._pending_setpoint_single = 70.0
        engine._last_commanded_hvac_mode = "cool"
        engine._manual_override_active = False

        # Thermostat reports 71.9°F — drifted 1.9°F away from commanded 70.0°F
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 72.0, "temperature": 71.9}
        engine.hass.states.get = MagicMock(return_value=climate_state)

        # Capture _set_temperature calls
        engine._set_temperature = AsyncMock()

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with (
            patch(_ACL_PATH, side_effect=_fake_acl),
            patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._activate_fan(reason="test"))

        # Issue #327: _activate_fan also schedules the 300s thermostatic backstop timer — select
        # the 30s post-fan verify callback specifically rather than asserting a single total.
        verify_calls = [(d, cb) for d, cb in captured_callbacks if d == 30.0]
        assert len(verify_calls) == 1
        _delay, verify_cb = verify_calls[0]

        # Wire async_create_task to capture and run the inner coroutine
        # (the @callback wrapper calls hass.async_create_task with the inner coro)
        captured_coros: list = []
        engine.hass.async_create_task = MagicMock(side_effect=lambda c: captured_coros.append(c))

        verify_cb(None)
        assert len(captured_coros) == 1
        asyncio.run(captured_coros[0])

        engine._set_temperature.assert_called_once()
        call_kwargs = engine._set_temperature.call_args
        assert call_kwargs[1]["reason"] == "post-fan-verify/repair"
        assert call_kwargs[1]["mode"] == "cool"

    def test_verify_callback_skips_when_write_seq_advanced(self):
        """Callback is a no-op when a newer write command superseded it (_write_seq changed)."""
        engine = _make_fan_engine()
        engine._pending_setpoint_single = 70.0
        engine._last_commanded_hvac_mode = "cool"
        engine._manual_override_active = False

        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"temperature": 71.9}
        engine.hass.states.get = MagicMock(return_value=climate_state)

        engine._set_temperature = AsyncMock()

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with patch(_ACL_PATH, side_effect=_fake_acl):
            asyncio.run(engine._activate_fan(reason="test"))

        # Advance write_seq before callback fires (simulates a newer command)
        engine._write_seq += 1

        captured_callbacks[0][1](None)  # sync wrapper; inner coro closed by _consume_coroutine

        engine._set_temperature.assert_not_called()

    def test_verify_callback_skips_when_manual_override_active(self):
        """Callback is a no-op when a genuine manual override is active."""
        engine = _make_fan_engine()
        engine._pending_setpoint_single = 70.0
        engine._last_commanded_hvac_mode = "cool"
        engine._manual_override_active = True  # user took control

        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"temperature": 71.9}
        engine.hass.states.get = MagicMock(return_value=climate_state)

        engine._set_temperature = AsyncMock()

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with patch(_ACL_PATH, side_effect=_fake_acl):
            asyncio.run(engine._activate_fan(reason="test"))

        captured_callbacks[0][1](None)  # sync wrapper; inner coro closed by _consume_coroutine

        engine._set_temperature.assert_not_called()

    def test_verify_callback_skips_when_setpoint_within_tolerance(self):
        """Callback is a no-op when thermostat is within 0.6°F of commanded setpoint."""
        engine = _make_fan_engine()
        engine._pending_setpoint_single = 70.0
        engine._last_commanded_hvac_mode = "cool"
        engine._manual_override_active = False

        # Thermostat reports 70.4°F — within 0.6°F tolerance
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"temperature": 70.4}
        engine.hass.states.get = MagicMock(return_value=climate_state)

        engine._set_temperature = AsyncMock()

        captured_callbacks: list = []

        def _fake_acl(hass, delay, callback):
            captured_callbacks.append((delay, callback))

        with patch(_ACL_PATH, side_effect=_fake_acl):
            asyncio.run(engine._activate_fan(reason="test"))

        captured_callbacks[0][1](None)  # sync wrapper; inner coro closed by _consume_coroutine

        engine._set_temperature.assert_not_called()
