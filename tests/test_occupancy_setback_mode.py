"""Tests for Issue #222 — occupancy setback uses actual thermostat mode.

`handle_occupancy_away()` and `handle_occupancy_vacation()` must read the
thermostat's actual hvac_mode from HA state rather than the classification's
desired hvac_mode.  The two can diverge when a day that started hot (cool
mode) is later reclassified to heat — without this guard the old code would
direct the AC to chill an empty home to the heat setback temperature.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

# ── Helpers ──────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 61,
        "setback_cool": 82,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(hvac_mode: str = "cool", setback_modifier: float = 0.0) -> DayClassification:
    """Create a minimal DayClassification bypassing __post_init__."""
    obj = object.__new__(DayClassification)
    obj.day_type = "warm" if hvac_mode in ("cool", "off") else "cold"
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
    obj.setback_modifier = setback_modifier
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _thermostat_state(mode: str) -> MagicMock:
    """Return a mock HA state object whose .state is ``mode`` with capabilities.

    #249 P3: _apply_comfort_band reads attributes.hvac_modes + attributes.supported_features
    to decide which command shape to use.  Without these the band no-ops silently, so all
    thermostat stubs must carry real capability attrs to exercise the set_temperature path.
    """
    s = MagicMock()
    s.state = mode
    # Expose both heat and cool so the band can arm whichever active edge is needed.
    s.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,  # single-setpoint, no heat_cool
    }
    return s


def _last_set_temperature(engine: AutomationEngine) -> float | None:
    """Extract the temperature from the most recent climate.set_temperature call."""
    calls = engine.hass.services.async_call.call_args_list
    temp_calls = [c for c in calls if len(c[0]) >= 3 and c[0][0] == "climate" and c[0][1] == "set_temperature"]
    if not temp_calls:
        return None
    return temp_calls[-1][0][2]["temperature"]


# ── handle_occupancy_away — 4 cases ─────────────────────────────


class TestHandleOccupancyAwayActualMode:
    """handle_occupancy_away() arms the away comfort band, defending the ceiling edge."""

    def test_thermostat_cool_classification_heat_applies_cool_setback(self):
        """Away band active='ceiling': cool-capable thermostat receives setback_cool (82).

        #249 P3 replaces the old actual-thermostat-mode dispatch — the band model always
        defends the ceiling when away (setback_cool) on a cool-capable device, regardless
        of the day classification's hvac_mode.
        """
        engine = _make_engine()
        # Classification says heat (e.g., rolled over at night) — irrelevant to away band selection.
        engine._current_classification = _make_classification(hvac_mode="heat")
        # Thermostat supports both heat and cool — band picks ceiling (cool) path.
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 82, f"Expected setback_cool=82 (away ceiling), got {temp}"

    def test_thermostat_heat_classification_cool_applies_cool_setback(self):
        """Away band active='ceiling': cool-capable thermostat receives setback_cool (82).

        #249 P3: thermostat state is no longer the dispatch axis — the away band always
        fires the ceiling edge on any cool-capable device.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        # Thermostat in heat mode but still advertises cool capability.
        engine.hass.states.get.return_value = _thermostat_state("heat")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 82, f"Expected setback_cool=82 (away ceiling), got {temp}"

    def test_thermostat_cool_classification_cool_applies_cool_setback(self):
        """Normal path: cool thermostat, cool classification → away ceiling = setback_cool (82)."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 82, f"Expected setback_cool=82, got {temp}"

    def test_thermostat_off_only_logs_and_skips_setpoint(self):
        """Off-only thermostat (no heat/cool capability) → band cannot arm → no setpoint.

        #249 P3: the band checks capabilities, not current state.  A thermostat that only
        advertises 'off' (hvac_modes=['off']) has no capable mode to defend the active edge,
        so _apply_comfort_band logs INFO and returns without calling set_temperature.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        # Only "off" in hvac_modes → no cool/heat capability → band no-ops.
        off_only_state = MagicMock()
        off_only_state.state = "off"
        off_only_state.attributes = {"hvac_modes": ["off"], "supported_features": 0}
        engine.hass.states.get.return_value = off_only_state

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp is None, f"Expected no setpoint when thermostat has no capable mode, got {temp}"


# ── handle_occupancy_vacation — 4 cases ─────────────────────────


class TestHandleOccupancyVacationActualMode:
    """handle_occupancy_vacation() arms the vacation deep-setback band, defending the ceiling edge."""

    def test_thermostat_cool_classification_heat_applies_cool_setback(self):
        """Vacation band active='ceiling': cool-capable thermostat receives setback_cool + EXTRA.

        #249 P3: the vacation band always arms the ceiling on cool-capable devices regardless
        of classification hvac_mode — the old mode-dispatch is replaced by band selection.
        """
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA  # 82 + 3 = 85
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation cool ceiling={expected}, got {temp}"

    def test_thermostat_heat_classification_cool_applies_cool_setback(self):
        """Vacation band active='ceiling': cool-capable thermostat gets ceiling even when in heat state.

        #249 P3: thermostat current state is no longer the dispatch axis — cool capability
        is what matters, and the vacation band always defends the ceiling.
        """
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        # Thermostat in heat mode but advertises cool — band picks ceiling.
        engine.hass.states.get.return_value = _thermostat_state("heat")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA  # 82 + 3 = 85
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation cool ceiling={expected}, got {temp}"

    def test_thermostat_cool_classification_cool_applies_cool_setback(self):
        """Normal path: cool thermostat, cool classification → vacation ceiling = setback_cool + EXTRA."""
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation cool ceiling={expected}, got {temp}"

    def test_thermostat_off_only_logs_and_skips_setpoint(self):
        """Off-only thermostat (no heat/cool capability) → band cannot arm → no setpoint.

        #249 P3: the band checks advertised capabilities, not current thermostat state.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        # Only "off" in hvac_modes → no cool/heat capability → band no-ops.
        off_only_state = MagicMock()
        off_only_state.state = "off"
        off_only_state.attributes = {"hvac_modes": ["off"], "supported_features": 0}
        engine.hass.states.get.return_value = off_only_state

        asyncio.run(engine.handle_occupancy_vacation())

        temp = _last_set_temperature(engine)
        assert temp is None, f"Expected no setpoint when thermostat has no capable mode, got {temp}"


# ── Event emission — Issue #240 ──────────────────────────────────


class TestOccupancyAwayEmitsEvent:
    """handle_occupancy_away() must emit occupancy_setback with the band shape."""

    def test_cool_mode_emits_occupancy_setback_away(self):
        """Away band → occupancy_setback with mode='away', floor/ceiling, occupancy='away'.

        #249 P3: event payload changed from {mode: 'cool', target_f} to {mode: 'away',
        floor, ceiling} — the band covers both edges rather than a single setpoint.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_away())

        # Filter to occupancy_setback events only (comfort_band_applied may also fire).
        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1, f"Expected 1 occupancy_setback event, got {events}"
        evt_name, evt_data = setback_events[0]
        assert evt_name == "occupancy_setback"
        assert evt_data["mode"] == "away"  # band always emits mode='away', not the HVAC mode
        assert evt_data["occupancy"] == "away"
        assert evt_data["floor"] == engine.config["setback_heat"]
        assert evt_data["ceiling"] == engine.config["setback_cool"]

    def test_heat_mode_emits_occupancy_setback_away(self):
        """Heat thermostat → occupancy_setback still reports mode='away' and the full band.

        #249 P3: mode key in the event now indicates occupancy context ('away'), not HVAC mode.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine.hass.states.get.return_value = _thermostat_state("heat")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_away())

        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1
        evt_name, evt_data = setback_events[0]
        assert evt_name == "occupancy_setback"
        assert evt_data["mode"] == "away"
        assert evt_data["occupancy"] == "away"
        assert evt_data["floor"] == engine.config["setback_heat"]
        assert evt_data["ceiling"] == engine.config["setback_cool"]

    def test_off_mode_emits_occupancy_setback_event(self):
        """HVAC off classification → occupancy_setback is still emitted (band is always armed).

        #249 P3: the old model skipped the event when hvac_mode='off'; the band model emits
        the setback event regardless of classification hvac_mode because the band covers both
        edges and the thermostat self-arbitrates.  The band no-ops at the service-call layer
        if the entity has no capable mode, but the event is always emitted.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="off")
        engine.hass.states.get.return_value = _thermostat_state("off")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_away())

        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1, f"Expected 1 occupancy_setback event even for off-mode, got {events}"


class TestOccupancyVacationEmitsEvent:
    """handle_occupancy_vacation() must emit occupancy_setback with the band shape."""

    def test_cool_mode_emits_occupancy_setback_vacation(self):
        """Vacation band → occupancy_setback with mode='vacation', floor/ceiling, occupancy='vacation'.

        #249 P3: event payload changed from {mode: 'cool', target_f} to {mode: 'vacation',
        floor, ceiling} — the band covers both edges using the deeper vacation offsets.
        """
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_vacation())

        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1
        evt_name, evt_data = setback_events[0]
        assert evt_name == "occupancy_setback"
        assert evt_data["mode"] == "vacation"  # band mode key now reports occupancy context
        assert evt_data["occupancy"] == "vacation"
        expected_ceiling = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA
        expected_floor = engine.config["setback_heat"] - VACATION_SETBACK_EXTRA
        assert evt_data["ceiling"] == expected_ceiling
        assert evt_data["floor"] == expected_floor

    def test_heat_mode_emits_occupancy_setback_vacation(self):
        """Heat thermostat → occupancy_setback still reports mode='vacation' with full band.

        #249 P3: mode key in the event indicates occupancy context not HVAC mode.
        """
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine.hass.states.get.return_value = _thermostat_state("heat")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_vacation())

        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1
        evt_name, evt_data = setback_events[0]
        assert evt_name == "occupancy_setback"
        assert evt_data["mode"] == "vacation"
        assert evt_data["occupancy"] == "vacation"
        expected_ceiling = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA
        expected_floor = engine.config["setback_heat"] - VACATION_SETBACK_EXTRA
        assert evt_data["ceiling"] == expected_ceiling
        assert evt_data["floor"] == expected_floor

    def test_off_mode_emits_occupancy_setback_event(self):
        """HVAC off classification → occupancy_setback is still emitted (band always fires).

        #249 P3: the old model skipped the event for hvac_mode='off'; the band always emits
        the setback event regardless of classification mode.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="off")
        engine.hass.states.get.return_value = _thermostat_state("off")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_vacation())

        setback_events = [(n, d) for n, d in events if n == "occupancy_setback"]
        assert len(setback_events) == 1, f"Expected 1 occupancy_setback event even for off-mode, got {events}"


class TestOccupancyHomeEmitsEvent:
    """handle_occupancy_home() must emit occupancy_comfort_restored after restoring comfort."""

    def test_heat_mode_emits_occupancy_comfort_restored(self):
        """Heat classification → occupancy_comfort_restored with mode=heat."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine.hass.states.get.return_value = _thermostat_state("heat")
        engine._natural_vent_active = False
        engine._fan_override_active = False

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_home())

        comfort_events = [(n, d) for n, d in events if n == "occupancy_comfort_restored"]
        assert len(comfort_events) == 1, f"Expected 1 occupancy_comfort_restored event, got {events}"
        evt_data = comfort_events[0][1]
        assert evt_data["mode"] == "heat"
        assert evt_data["target_f"] == engine.config["comfort_heat"]

    def test_cool_mode_emits_occupancy_comfort_restored(self):
        """Cool classification → occupancy_comfort_restored with mode=cool."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")
        engine._natural_vent_active = False
        engine._fan_override_active = False

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_home())

        comfort_events = [(n, d) for n, d in events if n == "occupancy_comfort_restored"]
        assert len(comfort_events) == 1, f"Expected 1 occupancy_comfort_restored event, got {events}"
        evt_data = comfort_events[0][1]
        assert evt_data["mode"] == "cool"
        assert evt_data["target_f"] == engine.config["comfort_cool"]

    def test_hvac_off_emits_no_comfort_event(self):
        """HVAC off classification → no occupancy_comfort_restored event."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="off")
        engine.hass.states.get.return_value = _thermostat_state("off")
        engine._natural_vent_active = False
        engine._fan_override_active = False

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_occupancy_home())

        comfort_events = [(n, d) for n, d in events if n == "occupancy_comfort_restored"]
        assert comfort_events == [], f"Expected no comfort event for off mode, got {events}"


class TestMorningWakeupEmitsEvent:
    """handle_morning_wakeup() must emit morning_wakeup with the band shape on the success path."""

    def test_heat_mode_emits_morning_wakeup(self):
        """Heat classification → morning_wakeup with mode='heat', floor, ceiling, active.

        #249 P3: event payload changed from {mode, target_f} to {mode, floor, ceiling, active}
        — the band arms both edges; mode still reflects the classification's HVAC intent.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._fan_active = False
        engine._fan_override_active = False
        # Provide capabilities so the band can arm on the floor (heat) edge.
        engine.hass.states.get.return_value = _thermostat_state("heat")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_morning_wakeup())

        wakeup_events = [(n, d) for n, d in events if n == "morning_wakeup"]
        assert len(wakeup_events) == 1, f"Expected 1 morning_wakeup event, got {events}"
        evt_data = wakeup_events[0][1]
        assert evt_data["mode"] == "heat"
        assert "floor" in evt_data  # band carries floor (comfort_heat for heat day)
        assert "ceiling" in evt_data
        assert "active" in evt_data

    def test_cool_mode_emits_morning_wakeup(self):
        """Cool classification → morning_wakeup with mode='cool', floor, ceiling, active.

        #249 P3: event payload no longer has target_f; it carries floor/ceiling/active.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine._fan_active = False
        engine._fan_override_active = False
        engine.hass.states.get.return_value = _thermostat_state("cool")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_morning_wakeup())

        wakeup_events = [(n, d) for n, d in events if n == "morning_wakeup"]
        assert len(wakeup_events) == 1, f"Expected 1 morning_wakeup event, got {events}"
        evt_data = wakeup_events[0][1]
        assert evt_data["mode"] == "cool"
        assert "floor" in evt_data
        assert "ceiling" in evt_data
        assert "active" in evt_data

    def test_skipped_when_occupancy_away_emits_no_event(self):
        """Wakeup skipped (away) → no morning_wakeup event."""
        from custom_components.climate_advisor.const import OCCUPANCY_AWAY

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._occupancy_mode = OCCUPANCY_AWAY
        engine._fan_active = False

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_morning_wakeup())

        wakeup_events = [(n, d) for n, d in events if n == "morning_wakeup"]
        assert wakeup_events == [], f"Expected no morning_wakeup when away, got {events}"

    def test_hvac_off_classification_emits_morning_wakeup_event(self):
        """HVAC off (mild/warm day) → morning_wakeup event is still emitted with the band.

        #249 P3: the old model skipped the event for hvac_mode='off'; the band model emits
        the event regardless because select_comfort_band handles off-mode days (active='ceiling',
        warm-day ceiling path).  The service call may no-op if the thermostat has no cool mode,
        but the event fires.
        """
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="off")
        engine._fan_active = False
        engine._fan_override_active = False
        engine.hass.states.get.return_value = _thermostat_state("off")

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        asyncio.run(engine.handle_morning_wakeup())

        wakeup_events = [(n, d) for n, d in events if n == "morning_wakeup"]
        assert len(wakeup_events) == 1, f"Expected 1 morning_wakeup event for off-mode day, got {events}"
