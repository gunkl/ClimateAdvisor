"""Tests for warm-day comfort gap guard in apply_classification().

When the day is classified as "warm" (or any day with hvac_mode="off") and the
indoor temperature is below the configured comfort floor, the automation should
defer the HVAC-off command and heat to the comfort floor instead.

GitHub Issue: warm-day comfort gap guard
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(
    comfort_heat: float = 70.0,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Build an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": 76.0,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
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


def _make_warm_off_classification(day_type: str = "warm") -> DayClassification:
    """Build a DayClassification with hvac_mode='off' (warm day scenario)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = "off"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 78.0
    obj.today_low = 58.0
    obj.tomorrow_high = 79.0
    obj.tomorrow_low = 59.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_indoor_temp_via_climate(engine: AutomationEngine, temp_f: float | None) -> None:
    """Configure hass.states.get so _get_indoor_temp_f() returns temp_f.

    Uses the climate-fallback path (no indoor_temp_source in config).
    When temp_f is None, states.get returns None → _get_indoor_temp_f returns None.
    """
    if temp_f is None:
        engine.hass.states.get.return_value = None
        return
    climate_state = MagicMock()
    climate_state.attributes.get.return_value = temp_f  # current_temperature attribute
    engine.hass.states.get.return_value = climate_state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWarmDayComfortGap:
    """Guard: warm day + indoor < comfort floor → heat to comfort first."""

    def test_indoor_below_comfort_sets_heat_mode(self):
        """When indoor (68°F) < comfort floor (70°F), HVAC is set to heat."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"

    def test_indoor_below_comfort_sets_temperature_to_comfort_floor(self):
        """When guard fires, target temperature is set to comfort_heat."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        # comfort_heat=70 in °F → service receives 70 (fahrenheit unit, no conversion)
        assert temp_calls[0].args[2]["temperature"] == 70.0

    def test_indoor_below_comfort_emits_event(self):
        """When guard fires, warm_day_comfort_gap event is emitted."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        gap_events = [e for e in events if e[0] == "warm_day_comfort_gap"]
        assert len(gap_events) == 1
        payload = gap_events[0][1]
        assert payload["day_type"] == "warm"
        assert payload["indoor_temp"] == 68.0
        assert payload["comfort_heat"] == 70.0

    def test_indoor_below_comfort_logs_warning(self, caplog):
        """Guard fires a WARNING log mentioning the temperature gap."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        with caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        deferred_msgs = [m for m in warn_msgs if "Warm-day off deferred" in m]
        assert len(deferred_msgs) == 1
        assert "68.0" in deferred_msgs[0]
        assert "70.0" in deferred_msgs[0]

    def test_indoor_at_comfort_floor_sets_off(self):
        """When indoor equals comfort floor, guard does NOT fire — HVAC set to off."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 70.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_above_comfort_floor_sets_off(self):
        """When indoor (72°F) > comfort floor (70°F), HVAC is set to off."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 72.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_above_comfort_no_temp_call(self):
        """When guard does not fire, no set_temperature service call is made."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 74.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_indoor_temp_unavailable_falls_back_to_off(self):
        """Safe fallback: when indoor temp is unavailable, HVAC goes off normally."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, None)  # states.get returns None

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_temp_unavailable_no_temp_call(self):
        """Safe fallback: when indoor temp is unavailable, no set_temperature call."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, None)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_comfort_heat_not_configured_falls_back_to_off(self):
        """Safe fallback: when comfort_heat is missing from config, HVAC goes off."""
        engine = _make_engine()
        del engine.config["comfort_heat"]
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_guard_does_not_fire_without_event_callback(self):
        """Guard works correctly when no event callback is registered."""
        engine = _make_engine(comfort_heat=70.0)
        engine._emit_event_callback = None
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification()
        # Should not raise — event callback being None is handled gracefully
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"

    def test_guard_applies_to_any_off_day_type(self):
        """Guard fires for any day_type with hvac_mode='off', not just 'warm'."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification(day_type="mild")
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"


# ---------------------------------------------------------------------------
# Ceiling guard helpers
# ---------------------------------------------------------------------------


def _make_predicted_indoor(
    start_hour_utc: int,
    temps: list[float],
    date: str = "2026-05-11",
) -> list[dict]:
    """Build a predicted_indoor curve list."""
    base = datetime(2026, 5, 11, start_hour_utc, 0, 0, tzinfo=UTC)
    return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]


def _set_thermal_model(
    engine,
    k_passive: float = -0.05,
    conf: str = "medium",
    k_active_cool: float | None = None,
    bridge: bool = False,
) -> None:
    engine._thermal_model = {
        "k_passive": k_passive,
        "confidence_k_passive": conf,
        "k_active_cool": k_active_cool,
        "k_passive_via_bridge": bridge,
        "confidence": conf,
    }


# ---------------------------------------------------------------------------
# Ceiling guard tests
# ---------------------------------------------------------------------------


class TestCeilingGuardFires:
    """Decision point B: outdoor > indoor, breach within lead_time → set HVAC cool."""

    def test_fires_when_breach_within_120min_and_outdoor_above_indoor(self):
        """k_active_cool=None → 120-min fallback. Breach 1.5h away → guard fires."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0  # outdoor > indoor
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        # Breach in 1.5 hours (breach at 11:30, now=10:00)
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5, 76.0])  # hour 11 crosses 74.0

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [c for c in calls if c.args[1] == "set_hvac_mode"]
        cool_calls = [c for c in hvac_calls if c.args[2].get("hvac_mode") == "cool"]
        # Guard must fire at least one cool call and be the last hvac call (overrides prior setback)
        assert len(cool_calls) == 1
        assert hvac_calls[-1].args[2]["hvac_mode"] == "cool"

    def test_sets_temperature_to_comfort_cool(self):
        """When guard fires, target temp is set to comfort_cool."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert any(c.args[2].get("temperature") == 74.0 for c in temp_calls)

    def test_emits_ceiling_guard_fired_event(self):
        """Guard fires ceiling_guard_fired event with breach_time and hours_to_breach."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        events: list[tuple] = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        fired = [e for e in events if e[0] == "ceiling_guard_fired"]
        assert len(fired) == 1


class TestCeilingGuardSkips:
    """Guard should skip when guard conditions are not met."""

    def test_skips_when_no_predicted_indoor(self):
        """No predicted_indoor passed → guard skips, setback still applied."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 75.0)  # above comfort_heat, comfort_gap guard doesn't fire
        engine._last_outdoor_temp = 78.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        asyncio.run(engine.apply_classification(_make_warm_off_classification()))
        # Should not set hvac_mode=cool
        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [c for c in calls if c.args[1] == "set_hvac_mode"]
        cool_calls = [c for c in hvac_calls if c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_skips_when_no_model(self):
        """No calibrated model (k_passive=None) → guard skips."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 75.0)
        engine._last_outdoor_temp = 78.0
        engine._thermal_model = {}  # no model

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5, 76.0])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [c for c in calls if c.args[1] == "set_hvac_mode"]
        assert not any(c.args[2].get("hvac_mode") == "cool" for c in hvac_calls)

    def test_dormant_when_outdoor_below_natvent_running_in_band(self):
        """Issue #247: dormant ONLY when outdoor<=indoor AND nat-vent running AND indoor in band.

        This replaces the old `test_skips_when_outdoor_below_indoor`, which asserted "no AC whenever
        outdoor <= indoor" UNCONDITIONALLY — even with indoor 76°F above comfort_cool 74°F. That
        encoded the bug: it validated the one-condition dormancy that #218 was supposed to replace
        (its 3-condition fix was specified but never committed). The correct behavior is: defer to
        free cooling only while it is actually viable — outdoor cool, windows actually ventilating,
        and indoor still within band.
        """
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)  # IN band
        engine._last_outdoor_temp = 70.0  # outdoor < indoor
        engine._natural_vent_active = True  # windows actually ventilating
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        # Curve DOES breach later — proves dormancy holds on the indoor-in-band + nat-vent condition,
        # not merely because nothing breaches (the breach scan is never reached while dormant).
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5, 75.0])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0  # free cooling still viable → dormant

    def test_fires_when_indoor_breaches_ceiling_despite_outdoor_below(self):
        """Issue #247: nat-vent active, outdoor < indoor, indoor ABOVE comfort_cool → escalate to AC.

        Solar/internal gains exceed ventilated cooling; free cooling is demonstrably losing. The
        guard must fire even though outdoor < indoor (this is the exact #247 case the old test
        wrongly asserted should be dormant).
        """
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 76.0)  # OUT of band
        engine._last_outdoor_temp = 70.0  # outdoor < indoor
        engine._natural_vent_active = True
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) >= 1  # escalated to AC

    def test_fires_when_natvent_not_running_and_indoor_above_ceiling(self):
        """Issue #215/#247: nat-vent NOT running (sensors closed / fan override), indoor above ceiling,
        outdoor below indoor → guard must fire (do not defer to a ventilation that is not happening)."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 76.0)
        engine._last_outdoor_temp = 70.0  # outdoor < indoor
        engine._natural_vent_active = False  # windows closed / fan off
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) >= 1

    def test_aggressive_savings_widens_escalation_threshold(self):
        """Issue #247: in aggressive_savings, tolerate a small overshoot (comfort_cool + margin) before
        escalating — so a 75°F reading (within comfort_cool 74 + 2°F margin) stays dormant under
        active nat-vent."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0, "aggressive_savings": True})
        _set_indoor_temp_via_climate(engine, 75.0)  # within 74 + 2°F savings margin
        engine._last_outdoor_temp = 70.0
        engine._natural_vent_active = True
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[75.0, 75.5, 75.8])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0  # savings margin tolerates the small overshoot

    def test_skips_when_no_breach_in_curve(self):
        """All predicted temps below comfort_cool → guard dormant."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 73.5, 73.8])  # never exceeds 74.0

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_skips_when_breach_too_far_away(self):
        """Breach predicted far in future (> lead_time). Guard stands by."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 71.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        # now=10:00, breach at 14:00 (4h away). Fallback lead_time=120min=2h. 4h > 2h → stand by.
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        # breach at index 4 = 14:00 (4h from now)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[71.0, 71.5, 72.0, 72.5, 74.5])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_bridge_tolerance_suppresses_near_miss(self):
        """Bridge home: breach at comfort_cool+0.5 (below bridge threshold of +1.0) → guard skips."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="none", bridge=True)  # bridge: conf="none" but bridge=True

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        # breach at 74.4°F = comfort_cool(74) + 0.4; bridge threshold = 74 + 1.0 = 75.0 → no breach
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.4])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_weather_change_guard_dormant_when_no_breach_on_second_call(self):
        """Two consecutive calls: first has breach, second has no breach. Guard dormant on second."""
        engine = _make_engine(config_overrides={"comfort_cool": 74.0})
        _set_indoor_temp_via_climate(engine, 73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve_breach = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5])  # breach in 1h
        curve_no_breach = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 73.8])  # no breach

        # First call: breach → HVAC cool
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve_breach))

        engine.hass.services.async_call.reset_mock()

        # Second call (30 min later, new forecast shows no breach): guard dormant
        now2 = datetime(2026, 5, 11, 10, 30, 0, tzinfo=UTC)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now2
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve_no_breach))

        calls2 = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls2 if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0
