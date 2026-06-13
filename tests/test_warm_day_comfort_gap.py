"""Tests for warm-day P3 band behavior and ODE ceiling guard — Issue #249 P3.

P3 replaces the warm-day comfort-gap guard (indoor < comfort_heat → defer HVAC off, heat first)
with a persistent comfort band.  ``apply_classification`` now unconditionally arms the band via
``_apply_comfort_band``; the thermostat's floor setpoint implicitly provides the cold-floor
backstop without a separate supervisory check.  The ODE ceiling guard (Issue #136) is unchanged:
it still pre-cools when thermal prediction shows an imminent breach.

The old TestWarmDayComfortGap tests asserting ``set_hvac_mode("off")`` or ``set_hvac_mode("heat")``
based on indoor temperature comparisons are replaced by band-arming assertions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

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
    *,
    indoor_temp: float = 72.0,
    current_mode: str = "off",
    hvac_modes: list[str] | None = None,
    supported_features: int | None = None,
) -> AutomationEngine:
    """Build an AutomationEngine with a dual-capable thermostat stub.

    Under P3 the engine arms a comfort band; the climate state must expose capability
    attributes so ``_get_thermostat_capabilities()`` returns a non-empty result.
    Default: full dual-setpoint thermostat (heat_cool + TARGET_TEMPERATURE_RANGE).
    Pass hvac_modes=[] / supported_features=0 for the no-capability path.
    """
    if hvac_modes is None:
        hvac_modes = ["off", "heat", "cool", "heat_cool"]
    if supported_features is None:
        supported_features = CLIMATE_FEATURE_TARGET_TEMP_RANGE

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    # Use a real dict for attributes so both dict indexing and .get() work correctly.
    # _get_thermostat_capabilities reads attrs.get("hvac_modes") / attrs.get("supported_features");
    # _get_indoor_temp_f reads state.attributes.get("current_temperature").
    attrs = {
        "hvac_modes": hvac_modes,
        "supported_features": supported_features,
        "current_temperature": indoor_temp,
    }
    climate_state = MagicMock()
    climate_state.state = current_mode
    climate_state.attributes = attrs
    hass.states.get.return_value = climate_state

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


# ---------------------------------------------------------------------------
# P3 band arming (replaces the old comfort-gap guard)
# ---------------------------------------------------------------------------
#
# Old model: if indoor < comfort_heat → defer off, set hvac_mode="heat" first.
#            if indoor >= comfort_heat or indoor unavailable → set hvac_mode="off".
# These tests asserted set_hvac_mode("heat"/"off") based on temperature comparisons.
#
# P3 model: _apply_comfort_band arms the band unconditionally based on thermostat
# CAPABILITIES, not indoor temperature.  The floor setpoint (setback_heat) provides the
# cold-floor backstop implicitly.  The occupant benefits: no oscillation between "off"
# cycles; the thermostat holds a stable band regardless of current indoor temp.


class TestWarmDayBandArmingReplacesComfortGap:
    """P3: warm day always arms the comfort band; the old off+heat dispatch is gone."""

    def test_indoor_below_comfort_arms_band_not_heat_mode(self):
        """Indoor (68°F) < comfort_heat (70°F): P3+Fix4 arms the band atomically; no separate mode call.

        Old model: set hvac_mode='heat' to reach the comfort floor first.
        P3: arm the dual band [setback_heat/comfort_cool] — the floor setpoint (60°F)
        is a safety backstop; the heater fires naturally if indoor drops below it.
        Fix 4 (Issue #290): dual path emits ONE set_temperature call with hvac_mode="heat_cool"
        embedded; no separate set_hvac_mode call so Ecobee cannot revert between the two calls.
        """
        engine = _make_engine(comfort_heat=70.0, indoor_temp=68.0, current_mode="off")
        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        # Fix 4: NO separate set_hvac_mode for dual path
        assert len(hvac_calls) == 0
        # hvac_mode="heat_cool" must appear inside the set_temperature payload
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        # hvac_mode in pre-write (index 0) only when mode switch needed; target write omits it
        assert temp_calls[0].args[2].get("hvac_mode") == "heat_cool"

    def test_indoor_below_comfort_sets_dual_setpoints(self):
        """Indoor below comfort floor: dual setpoints [60/76] always set, not a single heat target.

        Fix 4 (Issue #290): hvac_mode="heat_cool" is now embedded in this same set_temperature
        payload rather than being a preceding separate service call.
        """
        engine = _make_engine(comfort_heat=70.0, indoor_temp=68.0, current_mode="off")
        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        data = temp_calls[0].args[2]  # pre-write carries hvac_mode + setpoint keys
        # P3: dual setpoints; old assertion was temperature=70.0 (comfort_heat single target)
        assert "target_temp_low" in data
        assert "target_temp_high" in data
        # hvac_mode in pre-write only (Fix P1); target write omits it to prevent comfort-program lookup
        assert data.get("hvac_mode") == "heat_cool"

    def test_indoor_at_comfort_floor_arms_band_not_off(self):
        """Indoor = comfort_heat (70°F): P3+Fix4 arms band atomically; no separate hvac_mode='off' or mode call."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=70.0, current_mode="off")
        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        # Fix 4: NO separate set_hvac_mode for dual path
        assert len(hvac_calls) == 0
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        # hvac_mode in pre-write (index 0) only when mode switch needed; target write omits it
        assert temp_calls[0].args[2].get("hvac_mode") == "heat_cool"

    def test_indoor_above_comfort_floor_arms_band_not_off(self):
        """Indoor (72°F) > comfort_heat (70°F): P3+Fix4 arms band atomically; no separate mode call."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=72.0, current_mode="off")
        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        # Fix 4: NO separate set_hvac_mode for dual path
        assert len(hvac_calls) == 0
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        # hvac_mode in pre-write (index 0) only when mode switch needed; target write omits it
        assert temp_calls[0].args[2].get("hvac_mode") == "heat_cool"

    def test_indoor_unavailable_arms_band(self):
        """Indoor temp unavailable: P3+Fix4 arms band atomically regardless of indoor temp."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=72.0, current_mode="off")
        # Override states.get to return state without indoor temp
        climate_state = MagicMock()
        climate_state.state = "off"
        climate_state.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
            "current_temperature": None,
        }
        engine.hass.states.get.return_value = climate_state

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        # Fix 4: NO separate set_hvac_mode for dual path; band arms regardless of indoor temp
        assert len(hvac_calls) == 0
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        # hvac_mode in pre-write (index 0) only when mode switch needed; target write omits it
        assert temp_calls[0].args[2].get("hvac_mode") == "heat_cool"

    def test_no_warm_day_comfort_gap_event(self):
        """warm_day_comfort_gap event no longer exists in P3; replaced by comfort_band_applied."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=68.0, current_mode="off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        gap_events = [e for e in events if e[0] == "warm_day_comfort_gap"]
        band_events = [e for e in events if e[0] == "comfort_band_applied"]
        assert len(gap_events) == 0  # old event is gone
        assert len(band_events) == 1  # P3 event replaces it

    def test_guard_applies_to_any_off_day_type(self):
        """Any day_type with hvac_mode='off' (mild, warm) arms the ceiling band atomically (Fix 4)."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=65.0, current_mode="off")
        c = _make_warm_off_classification(day_type="mild")
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        # Fix 4: NO separate set_hvac_mode for dual path
        assert len(hvac_calls) == 0
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        # hvac_mode in pre-write (index 0) only when mode switch needed; target write omits it
        assert temp_calls[0].args[2].get("hvac_mode") == "heat_cool"

    def test_no_log_about_deferred_off(self, caplog):
        """'Warm-day off deferred' log no longer exists in P3 — band armed instead."""
        engine = _make_engine(comfort_heat=70.0, indoor_temp=68.0, current_mode="off")
        c = _make_warm_off_classification()

        with caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        deferred_msgs = [r.message for r in caplog.records if "Warm-day off deferred" in r.message]
        assert len(deferred_msgs) == 0  # old log is gone in P3


# ---------------------------------------------------------------------------
# Ceiling guard helpers (unchanged from original)
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


def _make_ceiling_guard_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Engine for ceiling-guard tests: dual-capable + outdoor temp attribute."""
    return _make_engine(
        indoor_temp=73.0,
        current_mode="off",
        config_overrides=config_overrides,
    )


# ---------------------------------------------------------------------------
# Ceiling guard tests (ODE proactive pre-cooling — unchanged behavior)
#
# Under P3 with a dual-capable stub, _apply_comfort_band runs first (sets heat_cool),
# then the ODE ceiling guard may override to cool mode.  The cool-mode assertions
# use hvac_calls[-1] to get the last HVAC command, which remains "cool" when the
# guard fires.
# ---------------------------------------------------------------------------


def _make_cg_engine(comfort_cool: float = 74.0, indoor_temp: float = 73.0) -> AutomationEngine:
    """Ceiling-guard engine with dual-capable thermostat and specified indoor temp."""
    return _make_engine(
        config_overrides={"comfort_cool": comfort_cool},
        indoor_temp=indoor_temp,
        current_mode="off",
    )


class TestCeilingGuardFires:
    """Decision point B: outdoor > indoor, breach within lead_time → set HVAC cool."""

    def test_fires_when_breach_within_120min_and_outdoor_above_indoor(self):
        """k_active_cool=None → 120-min fallback. Breach 1.5h away → guard fires."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
        engine._last_outdoor_temp = 76.0  # outdoor > indoor
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5, 76.0])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [c for c in calls if c.args[1] == "set_hvac_mode"]
        cool_calls = [c for c in hvac_calls if c.args[2].get("hvac_mode") == "cool"]
        # Guard must fire at least one cool call and be the last hvac call (overrides prior band arming)
        assert len(cool_calls) == 1
        assert hvac_calls[-1].args[2]["hvac_mode"] == "cool"

    def test_sets_temperature_to_comfort_cool(self):
        """When guard fires, target temp is set to comfort_cool."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
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
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
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
        """No predicted_indoor passed → guard skips, band still armed."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=75.0)
        engine._last_outdoor_temp = 78.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        asyncio.run(engine.apply_classification(_make_warm_off_classification()))
        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [c for c in calls if c.args[1] == "set_hvac_mode"]
        cool_calls = [c for c in hvac_calls if c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_skips_when_no_model(self):
        """No calibrated model (k_passive=None) → guard skips."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=75.0)
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
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)  # IN band
        engine._last_outdoor_temp = 70.0  # outdoor < indoor
        engine._natural_vent_active = True  # windows actually ventilating
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
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
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=76.0)  # OUT of band
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
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=76.0)
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
        engine = _make_engine(
            config_overrides={"comfort_cool": 74.0, "aggressive_savings": True},
            indoor_temp=75.0,  # within 74 + 2°F savings margin
            current_mode="off",
        )
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
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium")

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 73.5, 73.8])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_skips_when_breach_too_far_away(self):
        """Breach predicted far in future (> lead_time). Guard stands by."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=71.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[71.0, 71.5, 72.0, 72.5, 74.5])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_bridge_tolerance_suppresses_near_miss(self):
        """Bridge home: breach at comfort_cool+0.5 (below bridge threshold of +1.0) → guard skips."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="none", bridge=True)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.4])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))

        calls = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0

    def test_weather_change_guard_dormant_when_no_breach_on_second_call(self):
        """Two consecutive calls: first has breach, second has no breach. Guard dormant on second."""
        engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
        engine._last_outdoor_temp = 76.0
        _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)

        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        curve_breach = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5])
        curve_no_breach = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 73.8])

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve_breach))

        engine.hass.services.async_call.reset_mock()

        now2 = datetime(2026, 5, 11, 10, 30, 0, tzinfo=UTC)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now2
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve_no_breach))

        calls2 = engine.hass.services.async_call.call_args_list
        cool_calls = [c for c in calls2 if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"]
        assert len(cool_calls) == 0
