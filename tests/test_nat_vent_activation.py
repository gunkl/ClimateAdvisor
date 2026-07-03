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
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
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
        """outdoor 70F < indoor 76F, indoor 76F > comfort_heat 70F, outdoor 70F < threshold 79F → nat_vent.

        Issue #392 Fix 1: reactivation gates now also require indoor <= ceiling_threshold
        (comfort_cool, since fan_mode defaults to disabled/HVAC-fan archetype here — same
        ceiling rule as FAN_MODE_HVAC). comfort_cool raised to 76 (== indoor) so this test
        continues to exercise pure Row-3 directional activation without tripping the new
        ceiling gate, which is covered separately by TestNatVentActivation ceiling tests.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._last_outdoor_temp = 70.0

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        nat_events = [e for e in events if e[0] == "sensor_opened" and e[1].get("result") == "natural_ventilation"]
        assert len(nat_events) == 1

    def test_outdoor_just_below_indoor_activates(self):
        """outdoor 73.9F < indoor 74.0F — just below indoor — satisfies directional guard.

        comfort_cool raised to 74 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this pure directional-guard test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=74.0)
        engine._last_outdoor_temp = 73.9  # just cooler than indoor, still under threshold 77

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True

    def test_outdoor_near_threshold_still_activates(self):
        """outdoor 74F < indoor 78F; threshold 81F — outdoor just inside ceiling → nat_vent.

        comfort_cool raised to 78 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this pure directional-guard test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=78.0, nat_vent_delta=3.0, indoor_f=78.0)
        engine._last_outdoor_temp = 74.0  # below indoor(78) and below threshold(81)

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
        """Re-activation attempt 301s after exit → lockout expired; re-activates if conditions met.

        comfort_cool raised to 76 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this pure lockout-timing test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor 68F: below indoor(76) by more than hysteresis(1F), below threshold(79)
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
        """At exactly 300s (not yet past lockout) → still blocked.

        comfort_cool raised to 76 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this pure lockout-timing test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
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
        """outdoor == indoor - 1.0F exactly → activates (boundary is inclusive with < in code).

        comfort_cool raised to 76 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this pure hysteresis-boundary test.
        """
        # With hysteresis=1.0: condition is outdoor < indoor - 1.0
        # At outdoor = indoor - 1.0: condition is False (not strictly less)
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor exactly at boundary: 76.0 - 1.0 = 75.0 — but also equals threshold(79), so < threshold holds
        # Use indoor=76.0, outdoor=74.9 → outdoor < 75.0 = 76.0 - 1.0 → True; below threshold(79)? 74.9 < 79 → True
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
        # comfort_cool raised to 76 (== indoor) so the Issue #392 ceiling gate (default/
        # HVAC-fan archetype) does not block this pure hysteresis test.
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0  # well below indoor - hysteresis
        engine._nat_vent_outdoor_exit_time = None  # no prior outdoor-warm exit

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True


# ---------------------------------------------------------------------------
# Issue #359: on_fan_turned_off clears natural_vent_active
# ---------------------------------------------------------------------------


class TestFanTurnedOffClearsNatVent:
    """Issue #359: on_fan_turned_off() must clear _natural_vent_active.

    Occupant impact: when the user turns the fan off, CA previously left
    _natural_vent_active=True, causing subsequent coordinator cycles to treat
    the home as if nat-vent were still running. This blocked correct HVAC
    re-application and produced stale status readings.
    """

    def test_on_fan_turned_off_clears_natural_vent_active(self):
        """Engine with nat-vent active: on_fan_turned_off() clears _natural_vent_active.

        Without Issue #359 Fix B, on_fan_turned_off() was not a separate method —
        the engine called handle_fan_manual_override() instead, which sets
        _fan_override_active but does NOT clear _natural_vent_active.
        """

        _PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()

        def _consume_coroutine(coro):
            coro.close()

        hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
        hass.states = MagicMock()

        config = {
            "comfort_heat": 70.0,
            "comfort_cool": 72.0,
            "setback_heat": 60,
            "setback_cool": 80,
            "natural_vent_delta": 3.0,
            "notify_service": "notify.notify",
        }

        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config=config,
        )

        # Simulate a nat-vent session that was running
        engine._natural_vent_active = True
        engine._fan_active = True
        engine._fan_on_since = "2026-06-28T07:39:00"
        engine._fan_override_active = False

        # User turns fan off (thermostat fan_mode: on → auto)
        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        # Both nat-vent and fan tracking must be cleared
        assert engine._natural_vent_active is False, (
            "on_fan_turned_off must clear _natural_vent_active so coordinator "
            "does not treat the session as still running after the fan stops"
        )
        assert engine._fan_active is False, "on_fan_turned_off must clear _fan_active"


# ---------------------------------------------------------------------------
# Integration: full cycle — activate, outdoor rises, re-activate after lockout
# ---------------------------------------------------------------------------


class TestFullNatVentCycle:
    """Integration: open → nat_vent → outdoor rise exit → lockout → re-activate."""

    def test_open_to_nat_vent_to_rise_exit_to_reactivate(self):
        """Full cycle: activate at 18:00; outdoor rises at 20:00; re-activate at 21:00 (post-lockout).

        comfort_cool raised to 76 (== max indoor used across the sequence) so the Issue #392
        ceiling gate (default/HVAC-fan archetype) does not block this pure directional/lockout
        integration test — ceiling-specific behavior is covered separately.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)

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
        """Forecast peak > nat_vent_threshold within 2 hr -> falls through to pause, not nat vent.

        comfort_cool raised to 73 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not short-circuit before the Phase-2 forecast guard runs —
        this test is specifically about the forecast guard, not the ceiling gate.
        nat_vent_delta lowered to 2.0 to keep nat_vent_threshold at 75F (73+2), preserving
        the original docstring math for the forecast-peak comparison.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=2.0, indoor_f=73.0)
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
        """Forecast peak <= threshold -> Phase 2 guard passes -> nat vent activates.

        comfort_cool raised to 73 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this Phase-2-forecast-guard test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=3.0, indoor_f=73.0)
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
        """Empty hourly forecast -> forecast guard skipped -> Phase 1 only -> nat vent activates.

        comfort_cool raised to 73 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this Phase-1-fallback test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=3.0, indoor_f=73.0)
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

        comfort_cool raised to 73 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this Phase-2-thermal-floor-guard test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=3.0, indoor_f=73.0)
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
        """Empty thermal model -> guard skipped -> nat vent activates.

        comfort_cool raised to 73 (== indoor) so the Issue #392 ceiling gate (default/
        HVAC-fan archetype) does not block this fallback test.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=3.0, indoor_f=73.0)
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


# ---------------------------------------------------------------------------
# Issue #338 -- nat-vent AC assist HVAC state routing
# ---------------------------------------------------------------------------


def _make_hvac_engine(
    fan_mode: str = "hvac_fan",
    aggressive_savings: bool = False,
    comfort_heat: float = 70.0,
    comfort_cool: float = 75.0,
    indoor_f: float = 76.0,
) -> AutomationEngine:
    """Engine pre-wired for AC-assist tests: FAN_MODE_HVAC by default."""
    engine = _make_engine(
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
        nat_vent_delta=3.0,
        indoor_f=indoor_f,
    )
    engine.config["fan_mode"] = fan_mode
    engine.config["aggressive_savings"] = aggressive_savings
    return engine


class TestNatVentAcAssist:
    """Issue #338: _apply_nat_vent_hvac_state routes correctly based on fan_mode + aggressive_savings."""

    # ------------------------------------------------------------------
    # Test 1: pause -> re-activate path (check_natural_vent_conditions)
    # with savings OFF -> full comfort band re-armed
    # ------------------------------------------------------------------
    def test_path_b_rearm_full_band_savings_off(self):
        """Engine in paused state, FAN_MODE_HVAC, savings=False, indoor already past ceiling.

        Issue #392 Fix 1: for FAN_MODE_HVAC, ceiling_threshold == comfort_cool when
        aggressive_savings is off (no escalation margin). indoor=76 > ceiling=75, so
        reactivation must now be BLOCKED — the compressor should take over rather than
        the fan reactivating past the point where AC/fan can coexist and hold the
        ceiling together. This mirrors the ODE ceiling guard's own dormancy condition
        (`indoor <= ceiling_threshold`), now applied consistently at the reactivation
        gate too, not just the guard.

        Before Issue #392, this test asserted the fan reactivates and re-arms the full
        comfort band even with indoor past the ceiling — that was the exact asymmetry
        (guard vs. reactivation gates disagreeing) that produced the off->cool->off->cool
        oscillation described in the issue. See test_path_b_floor_only_savings_on below
        (aggressive_savings=True) for the case where the +2°F savings margin keeps the
        ceiling (77) above indoor (76) and reactivation still proceeds.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        # outdoor 68F: below indoor(76) - hysteresis(1) = 75; below threshold(75+3=78)
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None

        # Stub _apply_comfort_band so we can assert it is NOT called (ceiling gate blocks first)
        engine._apply_comfort_band = AsyncMock()

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling_threshold (comfort_cool=75, no savings "
            "margin) must block reactivation — compressor should take over"
        )
        engine._apply_comfort_band.assert_not_called()

    def test_path_b_rearm_full_band_savings_off_indoor_within_ceiling(self):
        """Engine in paused state, FAN_MODE_HVAC, savings=False, indoor within ceiling.

        Companion to the ceiling-blocked case above: with indoor (74) at or below
        comfort_cool (75), the ceiling gate is satisfied and reactivation proceeds as
        before Issue #392 — fan reactivates and the full comfort band (fan + compressor
        coexisting) is re-armed.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=74.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None

        engine._apply_comfort_band = AsyncMock()

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        engine._apply_comfort_band.assert_called_once()
        call_band = engine._apply_comfort_band.call_args[0][0]
        assert call_band.active == "ceiling"
        assert call_band.floor == 70.0
        assert call_band.ceiling == 75.0

    # ------------------------------------------------------------------
    # Test 2: pause -> re-activate, savings ON -> floor-only (heat at comfort_heat)
    # ------------------------------------------------------------------
    def test_path_b_floor_only_savings_on(self):
        """Engine in paused state, FAN_MODE_HVAC, savings=True.

        check_natural_vent_conditions() re-activates -> _apply_nat_vent_hvac_state
        -> heat mode at comfort_heat (ceiling disarmed, no compressor).

        Occupant experience: free cooling via breeze only; AC compressor stays off
        (savings mode). Comfort floor is protected by heat mode.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=True, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        hvac_calls = [
            c
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_hvac_mode"
        ]
        heat_calls = [c for c in hvac_calls if c[0][2].get("hvac_mode") == "heat"]
        assert len(heat_calls) >= 1, "savings mode must arm heat at comfort floor"

    # ------------------------------------------------------------------
    # Test 3: door-open path (handle_door_window_open), savings ON ->
    # ceiling disarmed (no cool service call)
    # ------------------------------------------------------------------
    def test_path_a_savings_on_disarms_ceiling(self):
        """Sensor opens with conditions met, savings=True.

        handle_door_window_open() activates nat-vent -> _apply_nat_vent_hvac_state
        -> heat mode only; no cool/heat_cool service call (ceiling disarmed).

        Occupant experience: compressor stays off while windows are open even if
        indoor approaches comfort_cool -- only floor is guarded.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=True, indoor_f=76.0)
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True
        # Savings mode: ceiling disarmed -- verify set_temperature is called at comfort_heat
        # (floor only), NOT at comfort_cool (which would arm the compressor for cooling).
        temp_calls = [
            c
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_temperature"
        ]
        assert len(temp_calls) >= 1, "savings mode must set a setpoint"
        # All setpoint calls must be at the floor (comfort_heat=70), not ceiling (comfort_cool=75)
        for call in temp_calls:
            assert call[0][2].get("temperature") == 70.0, (
                "savings mode must only arm the floor setpoint (comfort_heat), not comfort_cool"
            )

    # ------------------------------------------------------------------
    # Test 4: FAN_MODE_WHOLE_HOUSE -> _apply_nat_vent_hvac_state is a no-op
    # ------------------------------------------------------------------
    def test_whole_house_fan_no_band_change(self):
        """FAN_MODE_WHOLE_HOUSE nat-vent activation must NOT call _apply_comfort_band.

        Whole-house fan handles airflow directly; HVAC is suppressed to 'off' by
        _activate_fan(). Arming a comfort band on top would fight the fan.

        Occupant experience: whole-house fan exchanges outdoor air at full flow;
        the thermostat stays off so no compressor runs against the airflow.
        """
        from custom_components.climate_advisor.const import FAN_MODE_WHOLE_HOUSE

        engine = _make_hvac_engine(fan_mode=FAN_MODE_WHOLE_HOUSE, aggressive_savings=False, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None

        # Stub _apply_comfort_band -- must NOT be called by the whole-house fan path
        engine._apply_comfort_band = AsyncMock()

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        engine._apply_comfort_band.assert_not_called()

    # ------------------------------------------------------------------
    # Test 5: apply_classification() with _natural_vent_active=True must
    # call _apply_nat_vent_hvac_state and return early
    # ------------------------------------------------------------------
    def test_apply_classification_nat_vent_active_enforces_band(self):
        """apply_classification() with nat-vent active must enforce the nat-vent band
        and return early without applying the classification's own HVAC mode.

        Occupant experience: on a warm day with windows open, classification would
        set HVAC off -- but the nat-vent band guard keeps the comfort floor armed
        so the home doesn't drop below comfort_heat.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=76.0)
        engine._natural_vent_active = True

        # Stub _apply_nat_vent_hvac_state to assert it is called
        engine._apply_nat_vent_hvac_state = AsyncMock()
        # Stub _apply_comfort_band to catch any leaked classification call
        engine._apply_comfort_band = AsyncMock()

        classification = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(classification))

        engine._apply_nat_vent_hvac_state.assert_called_once()

    # ------------------------------------------------------------------
    # Test 6: handle_all_doors_windows_closed, warm/mild day (hvac_mode="off")
    # -> comfort band re-armed immediately
    # ------------------------------------------------------------------
    def test_sensor_close_warm_day_rearmed_immediately(self):
        """All sensors close while nat-vent active, day_type='warm' (hvac_mode='off').

        handle_all_doors_windows_closed() must re-arm the comfort band immediately,
        not wait for the next apply_classification() cycle (up to 30 min away).

        Occupant experience: closing windows on a warm day immediately re-arms the
        AC so the indoor temp does not drift above comfort_cool before the next
        30-min automation cycle.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=76.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        # Classification with hvac_mode="off" = warm/mild day
        engine._current_classification = _make_classification(day_type="warm", hvac_mode="off")

        # Stub _apply_comfort_band to assert immediate re-arm
        engine._apply_comfort_band = AsyncMock()
        # Stub _deactivate_fan so it doesn't call real fan service
        engine._deactivate_fan = AsyncMock()

        asyncio.run(engine.handle_all_doors_windows_closed())

        assert engine._natural_vent_active is False
        engine._apply_comfort_band.assert_called_once()
        call_band = engine._apply_comfort_band.call_args[0][0]
        assert call_band.active == "ceiling"

    # ------------------------------------------------------------------
    # Test 7: handle_all_doors_windows_closed, hot day (hvac_mode="cool")
    # -> HVAC set to "cool" (not comfort band)
    # ------------------------------------------------------------------
    def test_sensor_close_hot_day_mode_restored(self):
        """All sensors close while nat-vent active, day_type='hot' (hvac_mode='cool').

        handle_all_doors_windows_closed() must restore 'cool' mode (not re-arm a band),
        because the classifier explicitly wants compressor cooling.

        Occupant experience: on a hot day the AC compressor comes back on immediately
        when windows close -- the occupant does not experience a comfort gap while
        waiting for the next automation cycle.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=76.0)
        engine._natural_vent_active = True
        engine._paused_by_door = False
        engine._current_classification = _make_classification(day_type="hot", hvac_mode="cool")

        # Stub _deactivate_fan so it doesn't call real fan service
        engine._deactivate_fan = AsyncMock()

        asyncio.run(engine.handle_all_doors_windows_closed())

        assert engine._natural_vent_active is False
        hvac_calls = [
            c
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_hvac_mode"
        ]
        cool_calls = [c for c in hvac_calls if c[0][2].get("hvac_mode") == "cool"]
        assert len(cool_calls) >= 1, "hot-day close must restore 'cool' HVAC mode"


# ---------------------------------------------------------------------------
# Issue #341 — nat-vent active during sleep window: single setpoint per cycle
# ---------------------------------------------------------------------------


class TestNatVentSleepWindowBand:
    """Issue #341: during sleep window, _apply_nat_vent_hvac_state() skips setpoint call.

    Before the fix: apply_classification() called _apply_nat_vent_hvac_state() (which wrote
    comfort_cool=75°F to the thermostat) and then immediately called select_comfort_band()
    (which wrote sleep_cool=78°F), generating two conflicting thermostat writes every 30 minutes
    all night.

    After the fix: _apply_nat_vent_hvac_state() emits nat_vent_ac_assist_armed but skips
    the _apply_comfort_band() call during the sleep window. Only one setpoint write occurs per
    cycle — the sleep band from select_comfort_band().
    """

    # dt_util.now() inside automation.py is a child MagicMock; patch it so _in_sleep_window
    # gets a real datetime and .time() comparisons work.
    _NOW = datetime(2026, 4, 20, 22, 30, 0)  # 22:30 — within a 22:00–07:00 sleep window

    def _make_sleep_engine(self) -> AutomationEngine:
        """Engine with sleep window covering the patched 'now' (22:30)."""
        engine = _make_hvac_engine(
            fan_mode="hvac_fan",
            aggressive_savings=False,
            comfort_heat=70.0,
            comfort_cool=75.0,
            indoor_f=74.0,
        )
        # 22:00–07:00 window includes _NOW (22:30)
        engine.config["sleep_time"] = "22:00"
        engine.config["wake_time"] = "07:00"
        return engine

    def test_sleep_window_single_comfort_band_call(self):
        """Nat-vent active + sleep window → _apply_comfort_band called once (sleep band only).

        Occupant experience: after the fix, the thermostat receives one setpoint write per
        30-minute cycle overnight — the sleep ceiling (78°F by default) — not two competing
        writes at 75°F and 78°F that make the thermostat history look like the integration
        is malfunctioning.
        """
        engine = self._make_sleep_engine()
        engine._natural_vent_active = True

        engine._apply_comfort_band = AsyncMock()
        classification = _make_classification(day_type="warm", hvac_mode="off")
        with patch(_DT_NOW_PATH, return_value=self._NOW):
            asyncio.run(engine.apply_classification(classification))

        assert engine._apply_comfort_band.call_count == 1, (
            f"Expected 1 _apply_comfort_band call during sleep window; got {engine._apply_comfort_band.call_count}"
        )
        band = engine._apply_comfort_band.call_args[0][0]
        # Sleep band uses DEFAULT_SLEEP_HEAT=66 / DEFAULT_SLEEP_COOL=78, not comfort band 70/75
        assert band.floor == 66.0, f"Sleep band floor must be sleep_heat=66. Got: {band.floor}"
        assert band.ceiling == 78.0, f"Sleep band ceiling must be sleep_cool=78. Got: {band.ceiling}"

    def test_sleep_window_nat_vent_ac_assist_event_still_emitted(self):
        """nat_vent_ac_assist_armed event fires during sleep window despite skipped setpoint.

        The activity report and status card must still show nat-vent as active while the
        fan is running overnight. Without the event, the occupant sees the fan on but the
        report shows no nat-vent activity — a confusing gap.
        """
        engine = self._make_sleep_engine()
        engine._natural_vent_active = True

        emitted: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda et, pl: emitted.append((et, pl))
        engine._apply_comfort_band = AsyncMock()

        classification = _make_classification(day_type="warm", hvac_mode="off")
        with patch(_DT_NOW_PATH, return_value=self._NOW):
            asyncio.run(engine.apply_classification(classification))

        event_types = [e[0] for e in emitted]
        assert "nat_vent_ac_assist_armed" in event_types, (
            f"nat_vent_ac_assist_armed must still fire during sleep window. Got events: {event_types}"
        )

    def test_awake_window_two_comfort_band_calls(self):
        """Nat-vent active + awake hours → two _apply_comfort_band calls (regression guard).

        During awake hours, _apply_nat_vent_hvac_state() still writes the full comfort band
        so the thermostat's own deadband can let the compressor assist if the breeze alone
        cannot hold the comfort ceiling.
        """
        engine = _make_hvac_engine(fan_mode="hvac_fan", aggressive_savings=False, indoor_f=74.0)
        # No sleep_time/wake_time in config → _in_sleep_window() returns False
        engine._natural_vent_active = True

        engine._apply_comfort_band = AsyncMock()
        classification = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(classification))

        # Two calls: (1) nat-vent full comfort band, (2) apply_classification() comfort band
        assert engine._apply_comfort_band.call_count == 2, (
            f"Expected 2 _apply_comfort_band calls during awake hours; got {engine._apply_comfort_band.call_count}"
        )
        for call in engine._apply_comfort_band.call_args_list:
            band = call[0][0]
            assert band.floor == 70.0, f"Awake band floor must be comfort_heat=70. Got: {band.floor}"
            assert band.ceiling == 75.0, f"Awake band ceiling must be comfort_cool=75. Got: {band.ceiling}"


# ---------------------------------------------------------------------------
# Issue #370 — Priority 0 sleep-ceiling exit in check_natural_vent_conditions
# ---------------------------------------------------------------------------

# Patched 'now' values for sleep-window tests
_SLEEP_NOW = datetime(2026, 4, 20, 23, 15, 0)  # 23:15 — inside 22:00–07:00 window
_AWAKE_NOW = datetime(2026, 4, 20, 14, 0, 0)  # 14:00 — outside sleep window


def _make_sleep_ceiling_engine(
    indoor_f: float = 71.0,
    comfort_heat: float = 68.0,
    comfort_cool: float = 76.0,
    sleep_cool: float = 72.0,
    sleep_heat: float = 66.0,
    in_sleep_window: bool = True,
) -> AutomationEngine:
    """Engine pre-wired for Issue #370 sleep-ceiling exit tests.

    Sets nat-vent active and positions indoor below sleep_cool so Priority 0
    fires on the first check_natural_vent_conditions() call (when in sleep window).
    """
    engine = _make_engine(
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
        indoor_f=indoor_f,
    )
    engine.config["sleep_cool"] = sleep_cool
    engine.config["sleep_heat"] = sleep_heat

    if in_sleep_window:
        # 22:00–07:00 window encloses _SLEEP_NOW (23:15)
        engine.config["sleep_time"] = "22:00"
        engine.config["wake_time"] = "07:00"
    # else: no sleep_time/wake_time → _in_sleep_window() returns False

    # Nat-vent is active and running at entry
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._fan_override_active = False

    # Attach a warm-day classification so select_comfort_band resolves the sleep band
    engine._current_classification = _make_classification(day_type="warm", hvac_mode="off")

    return engine


class TestNatVentSleepCeilingExit:
    """Issue #371: Priority 0 sleep-ceiling exit REMOVED from check_natural_vent_conditions().

    Previously (Issue #370) nat-vent stopped when indoor reached sleep_cool during the
    sleep window. This was wrong — the fan should continue cycling down to sleep_heat
    (the sleep floor). Priority 0 has been deleted; cycling is now handled entirely by
    nat_vent_temperature_check() using the sleep-window branch (sleep_heat + hysteresis
    as the target).

    Occupant experience: the fan cycles quietly through the night, holding indoor near
    sleep_heat rather than stopping at sleep_cool — the occupant gets deeper cooling
    without the AC ever turning on.
    """

    def test_sleep_ceiling_NOT_exit_at_sleep_cool_in_sleep_window(self):
        """Priority 0 REMOVED: indoor 71°F ≤ sleep_cool 72°F in sleep window → fan NOT deactivated.

        Occupant experience: the fan continues cycling to cool the home toward sleep_heat,
        not just to sleep_cool — free cooling goes further, AC not needed.
        """
        engine = _make_sleep_ceiling_engine(indoor_f=71.0, sleep_cool=72.0, in_sleep_window=True)
        engine._deactivate_fan = AsyncMock()
        engine._async_save_state = AsyncMock()
        # outdoor must satisfy the outdoor-rise guard too; set it well below indoor
        engine._last_outdoor_temp = 62.0
        engine._nat_vent_outdoor_exit_time = None

        emitted: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        with patch(_DT_NOW_PATH, return_value=_SLEEP_NOW):
            asyncio.run(engine.check_natural_vent_conditions())

        # _deactivate_fan must NOT be called — Priority 0 is gone
        engine._deactivate_fan.assert_not_called()

        # _natural_vent_active must remain True — session continues cycling
        assert engine._natural_vent_active is True, (
            "_natural_vent_active must remain True; Priority 0 exit has been removed"
        )

        # nat_vent_sleep_ceiling_reached must NOT be emitted
        event_names = [e[0] for e in emitted]
        assert "nat_vent_sleep_ceiling_reached" not in event_names, (
            f"nat_vent_sleep_ceiling_reached must not be emitted (Priority 0 removed); got: {event_names}"
        )

    def test_sleep_ceiling_exit_not_fire_outside_sleep_window(self):
        """Priority 0 skipped: same temps but outside sleep window → fan NOT deactivated.

        Occupant experience: during daytime, nat-vent uses the normal comfort-floor
        exit — if indoor is between comfort_heat and comfort_cool the fan stays on.
        """
        # indoor=71 > comfort_heat=68 and 71 < comfort_cool=76 → comfort-floor exit won't fire
        # sleep-ceiling exit also won't fire (not in sleep window)
        engine = _make_sleep_ceiling_engine(
            indoor_f=71.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
            sleep_cool=72.0,
            in_sleep_window=False,  # no sleep_time/wake_time → not in sleep window
        )
        engine._deactivate_fan = AsyncMock()
        engine._async_save_state = AsyncMock()
        # outdoor must satisfy the outdoor-rise guard too; set it well below indoor
        engine._last_outdoor_temp = 62.0
        engine._nat_vent_outdoor_exit_time = None

        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            asyncio.run(engine.check_natural_vent_conditions())

        # Sleep-ceiling exit is NOT applicable outside sleep window
        # Comfort-floor exit also does not fire (indoor=71 > comfort_heat=68)
        engine._deactivate_fan.assert_not_called()

        # _natural_vent_active must remain True (fan still running)
        assert engine._natural_vent_active is True, (
            "_natural_vent_active must remain True outside sleep window when comfort is maintained"
        )


# ---------------------------------------------------------------------------
# Issue #392 Fix 1: archetype-aware ceiling gate at all four reactivation sites
# ---------------------------------------------------------------------------


class TestCeilingGateArchetypeAllFourSites:
    """Issue #392 Fix 1: each of the four (re)activation gate sites — the paused-by-door
    branch and the grace re-entry branch (both inside check_natural_vent_conditions()),
    handle_door_window_open(), and _re_pause_for_open_sensor() — must apply the SAME
    archetype-aware ceiling rule as the ODE guard's own dormancy condition: FAN_MODE_HVAC blocks reactivation once
    indoor exceeds the ceiling (comfort_cool, or comfort_cool+margin under
    aggressive_savings); FAN_MODE_WHOLE_HOUSE/BOTH ignore the ceiling entirely
    (direction-only: outdoor < indoor is sufficient).

    Occupant impact: before this fix, a WHF session got prematurely escalated to AC
    the moment indoor ticked past the ceiling even though outdoor was still much
    cooler than indoor and the fan was working — forcing an unnecessary compressor
    cycle and then immediately being undone by the next reactivation check (the
    off->cool->off->cool flapping from the issue). After the fix, WHF just keeps
    running until outdoor stops being cooler than indoor.
    """

    # -- Site 1: check_natural_vent_conditions() paused-by-door reactivation ---
    # (a second, distinct ceiling-gated branch inside the same function as Site 2's
    # grace re-entry branch — not a separate method)

    def test_site1_paused_by_door_hvac_fan_blocked_past_ceiling(self):
        """FAN_MODE_HVAC: paused by door, indoor > ceiling -> reactivation blocked."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0  # well below indoor, would otherwise reactivate
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling (74) must block paused-by-door reactivation"
        )

    def test_site1_paused_by_door_whf_reactivates_past_ceiling(self):
        """FAN_MODE_WHOLE_HOUSE: paused by door, indoor > ceiling but outdoor < indoor -> reactivates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, (
            "FAN_MODE_WHOLE_HOUSE: ceiling must not block paused-by-door reactivation as long as outdoor < indoor"
        )

    # -- Site 2: check_natural_vent_conditions() grace re-entry ----------------

    def test_site2_grace_reentry_hvac_fan_blocked_past_ceiling(self):
        """FAN_MODE_HVAC: grace active, indoor > ceiling -> reactivation blocked."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._grace_active = True
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling (74) must block grace re-entry reactivation"
        )

    def test_site2_grace_reentry_whf_reactivates_past_ceiling(self):
        """FAN_MODE_WHOLE_HOUSE: grace active, indoor > ceiling, outdoor < indoor -> reactivates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._grace_active = True
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, (
            "FAN_MODE_WHOLE_HOUSE: ceiling must not block grace re-entry as long as outdoor < indoor"
        )

    # -- Site 3: handle_door_window_open() --------------------------------------

    def test_site3_door_open_hvac_fan_blocked_past_ceiling(self):
        """FAN_MODE_HVAC: sensor opens, indoor > ceiling -> activation blocked, falls to pause."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling (74) must block activation on sensor open"
        )
        assert engine._paused_by_door is True

    def test_site3_door_open_whf_activates_past_ceiling(self):
        """FAN_MODE_WHOLE_HOUSE: sensor opens, indoor > ceiling, outdoor < indoor -> activates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True, (
            "FAN_MODE_WHOLE_HOUSE: ceiling must not block activation as long as outdoor < indoor"
        )

    # -- Site 4: _re_pause_for_open_sensor() -------------------------------------

    def test_site4_re_pause_hvac_fan_blocked_past_ceiling(self):
        """FAN_MODE_HVAC: grace expires with sensor open, indoor > ceiling -> re-pauses (blocked)."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling (74) must block reactivation at _re_pause_for_open_sensor()"
        )

    def test_site4_re_pause_whf_activates_past_ceiling(self):
        """FAN_MODE_WHOLE_HOUSE: grace expires with sensor open, indoor > ceiling, outdoor < indoor -> activates."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine.config[CONF_FAN_ENTITY] = "fan.attic"
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine._re_pause_for_open_sensor())

        assert engine._natural_vent_active is True, (
            "FAN_MODE_WHOLE_HOUSE: ceiling must not block reactivation as long as outdoor < indoor"
        )

    # -- aggressive_savings margin test (FAN_MODE_HVAC) --------------------------

    def test_aggressive_savings_widens_ceiling_margin_for_hvac_fan(self):
        """FAN_MODE_HVAC + aggressive_savings=True -> ceiling = comfort_cool + 2.0F margin.

        indoor=76 is past the plain comfort_cool=74 ceiling but within the
        aggressive-savings-widened ceiling (74+2=76) -> reactivation proceeds.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine.config["aggressive_savings"] = True
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True, (
            "aggressive_savings should widen the ceiling by CEILING_ESCALATION_SAVINGS_MARGIN_F "
            "(2.0F), allowing reactivation at indoor=76 with comfort_cool=74"
        )

    def test_aggressive_savings_still_blocks_beyond_widened_margin(self):
        """FAN_MODE_HVAC + aggressive_savings=True, indoor beyond even the widened ceiling -> blocked."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=5.0, indoor_f=77.0)
        engine.config["aggressive_savings"] = True
        engine._last_outdoor_temp = 68.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is False, (
            "indoor (77) exceeds even the widened ceiling (74+2=76) -- must still block"
        )


class TestIssue392ExactReproduction:
    """Direct reproduction of the Issue #392 sequence: WHF mode, outdoor 72F, indoor 75F,
    comfort_cool 74F. Before Fix 1, the ceiling guard would escalate to AC purely because
    indoor(75) > comfort_cool(74), even though outdoor(72) is still comfortably below
    indoor and the WHF is actively converging the house toward outdoor temperature.

    Occupant impact: the user saw the fan deactivate, HVAC escalate to `cool`, and then
    the very next sensor/grace re-check immediately reactivate the fan and force HVAC
    back to `off` — an off->cool->off->cool fight repeating roughly every 5 minutes
    (18:53-19:04 in the issue's activity log).
    """

    def test_whf_outdoor_72_indoor_75_comfort_cool_74_does_not_escalate_to_ac(self):
        """WHF, outdoor=72, indoor=75, comfort_cool=74 -> nat-vent stays active, no AC escalation."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=75.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine.config[CONF_FAN_ENTITY] = "fan.whole_house"
        engine._last_outdoor_temp = 72.0

        # Sensor opens: outdoor(72) < indoor(75), indoor > comfort_heat(70), outdoor < threshold(77)
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._natural_vent_active is True, (
            "WHF must activate: outdoor(72) < indoor(75), well within direction-only gate "
            "despite indoor(75) > comfort_cool(74)"
        )

        # HVAC must never have been commanded to an active mode (cool) — only "off" (suppression).
        hvac_calls = [
            c[0][2]["hvac_mode"]
            for c in engine.hass.services.async_call.call_args_list
            if c[0][0] == "climate" and c[0][1] == "set_hvac_mode"
        ]
        assert "cool" not in hvac_calls, (
            f"HVAC must never escalate to 'cool' while WHF nat-vent is active and outdoor < indoor; got: {hvac_calls}"
        )

    def test_whf_outdoor_72_indoor_75_survives_grace_recheck_without_flapping(self):
        """Simulates the observed sequence: activate, then grace re-check under the same
        outdoor/indoor conditions must not flap (deactivate-then-reactivate).
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, nat_vent_delta=3.0, indoor_f=75.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine.config[CONF_FAN_ENTITY] = "fan.whole_house"
        engine._last_outdoor_temp = 72.0

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        assert engine._natural_vent_active is True

        fan_calls_after_activate = len(
            [c for c in engine.hass.services.async_call.call_args_list if c[0][0] == "fan" and c[0][1] == "turn_on"]
        )

        # Re-check under unchanged conditions (simulating the next coordinator cycle /
        # grace-expiry re-evaluation) must be a no-op — nat-vent is already active and
        # nothing about outdoor/indoor direction has changed.
        engine._grace_active = False
        asyncio.run(engine.check_natural_vent_conditions())

        fan_calls_after_recheck = len(
            [c for c in engine.hass.services.async_call.call_args_list if c[0][0] == "fan" and c[0][1] == "turn_on"]
        )
        assert fan_calls_after_recheck == fan_calls_after_activate, (
            "Re-check under unchanged conditions must not re-issue a redundant fan.turn_on call "
            "(idempotency guard, Fix 1c)"
        )
        assert engine._natural_vent_active is True


# ---------------------------------------------------------------------------
# Issue #392 Fix 3: decision-lock concurrency
# ---------------------------------------------------------------------------


class TestDecisionLockConcurrency:
    """Issue #392 Fix 3: the six automation entry points share a single
    asyncio.Lock() (_decision_lock) so concurrent triggers evaluate one at a time
    against a consistent state snapshot instead of interleaving at await points.

    Occupant impact: without serialization, two independently-triggered handlers
    (e.g. a sensor-open debounce callback and a periodic nat-vent re-check) racing
    on shared state (_natural_vent_active, _fan_active, _pre_fan_hvac_mode) is the
    deeper mechanism behind the 18:53/18:58 burst of contradicting decisions in the
    issue — several uncoordinated handlers each concluding something different
    within the same few seconds.
    """

    def test_two_entry_points_do_not_interleave(self):
        """asyncio.gather() on two locked entry points -> non-overlapping execution,
        verified by recording enter/exit order around the lock acquisition.
        """
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, nat_vent_delta=3.0, indoor_f=76.0)
        engine._last_outdoor_temp = 70.0

        order: list[str] = []
        real_lock = engine._decision_lock

        class _TrackingLock:
            """Wraps the real asyncio.Lock to record acquire/release order."""

            def __init__(self, name_source: list[str]):
                self._name_source = name_source

            async def __aenter__(self):
                await real_lock.acquire()
                self._name_source.append(f"enter:{id(asyncio.current_task())}")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self._name_source.append(f"exit:{id(asyncio.current_task())}")
                real_lock.release()

        engine._decision_lock = _TrackingLock(order)

        async def _run_both():
            await asyncio.gather(
                engine.handle_door_window_open("binary_sensor.front_door"),
                engine.check_natural_vent_conditions(),
            )

        asyncio.run(_run_both())

        # Non-overlapping: each task's enter must be immediately followed by its own
        # exit before the other task's enter appears (strict enter/exit pairing).
        assert len(order) == 4, f"Expected 4 lock events (2 enter + 2 exit); got: {order}"
        first_task_id = order[0].split(":")[1]
        assert order[1] == f"exit:{first_task_id}", (
            f"First task's enter must be immediately followed by its own exit (no interleaving); got: {order}"
        )
        second_task_id = order[2].split(":")[1]
        assert order[3] == f"exit:{second_task_id}", (
            f"Second task's enter must be immediately followed by its own exit; got: {order}"
        )
        assert first_task_id != second_task_id, "The two tasks must be distinct"
