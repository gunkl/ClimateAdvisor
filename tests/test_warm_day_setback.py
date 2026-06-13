"""Tests for warm-day band arming in apply_classification() — Issue #249 P3.

P3 replaces the off+setback model (Root Cause C from Issue #96) with a comfort band.
On warm/hot days the engine no longer:
  - reads thermostat mode and dispatches to mode-specific setback paths
  - emits warm_day_setback_applied / warm_day_state_confirmed events
  - sets hvac_mode="off" for a warm day

Instead it calls ``_apply_comfort_band`` which arms the band via a single atomic
``set_temperature(hvac_mode="heat_cool", target_temp_low/high)`` (dual — Fix 4, Issue #290) or
``set_hvac_mode("cool") + set_temperature(ceiling)`` (cool-only).  Loop-prevention (Root Cause E) is unchanged.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    if asyncio.iscoroutine(coro):
        coro.close()


def _make_engine(
    comfort_heat: float = 70.0,
    config_overrides: dict | None = None,
    *,
    hvac_modes: list[str] | None = None,
    supported_features: int | None = None,
    current_mode: str = "off",
) -> AutomationEngine:
    """Build an AutomationEngine with a dual-capable thermostat stub by default.

    Under P3 the engine arms a comfort band, which requires the capability attributes to
    be present on the climate state.  Default: full dual-setpoint thermostat (heat_cool mode
    + TARGET_TEMPERATURE_RANGE feature bit).  Pass hvac_modes=[] / supported_features=0 to
    test the no-capability no-op path.
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
        "current_temperature": 72.0,  # default above comfort_heat so comfort-gap guard doesn't fire
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


def _make_classification(
    day_type: str = "warm",
    hvac_mode: str = "off",
) -> DayClassification:
    """Build a DayClassification via object.__new__ (no __init__ required)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
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


def _hvac_calls(engine: AutomationEngine) -> list:
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_hvac_mode"]


def _temp_calls(engine: AutomationEngine) -> list:
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_temperature"]


# ---------------------------------------------------------------------------
# Root Cause E — Classification loop prevention via revisit cancel (unchanged)
# ---------------------------------------------------------------------------


class TestClassificationLoopPrevention:
    """After apply_classification() returns, any pending revisit must be canceled.

    This behavior is unchanged in P3 — revisit-cancel is orthogonal to band arming.
    """

    def test_revisit_canceled_after_classification_applied_heat_mode(self):
        """Heat classification → _revisit_cancel is None after apply_classification."""
        engine = _make_engine(current_mode="heat")
        c = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.apply_classification(c))
        assert engine._revisit_cancel is None

    def test_revisit_canceled_after_classification_applied_off_mode(self):
        """Warm-day classification → _revisit_cancel is None after apply."""
        engine = _make_engine(current_mode="off")
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))
        assert engine._revisit_cancel is None

    def test_revisit_canceled_after_classification_applied_cool_mode(self):
        """Cool classification → _revisit_cancel is None after apply_classification."""
        engine = _make_engine(current_mode="cool")
        c = _make_classification(day_type="hot", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))
        assert engine._revisit_cancel is None

    def test_classification_applied_event_only_emitted_on_first_call(self):
        """Same (day_type, hvac_mode) applied twice → classification_applied emitted once."""
        engine = _make_engine(current_mode="off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))
        asyncio.run(engine.apply_classification(c))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 1

    def test_classification_applied_event_emitted_when_day_type_changes(self):
        """Apply warm then mild (same hvac_mode) → two classification_applied events."""
        engine = _make_engine(current_mode="off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c_warm = _make_classification(day_type="warm", hvac_mode="off")
        c_mild = _make_classification(day_type="mild", hvac_mode="off")
        asyncio.run(engine.apply_classification(c_warm))
        asyncio.run(engine.apply_classification(c_mild))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 2

    def test_classification_applied_event_emitted_when_hvac_mode_changes(self):
        """Same day_type but different hvac_mode → two classification_applied events."""
        engine = _make_engine(current_mode="cool")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c_off = _make_classification(day_type="warm", hvac_mode="off")
        c_cool = _make_classification(day_type="warm", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c_off))
        asyncio.run(engine.apply_classification(c_cool))

        applied_events = [e for e in events if e[0] == "classification_applied"]
        assert len(applied_events) == 2


# ---------------------------------------------------------------------------
# P3 band model — warm-day band arming (replaces off+setback dispatching)
# ---------------------------------------------------------------------------
#
# Old model (Root Cause C fix from Issue #96): read current thermostat mode,
# dispatch to mode-specific setback: heat→setback_heat, cool→setback_cool,
# heat_cool→dual setbacks, unknown→hard-off fallback.  Each path emitted
# warm_day_setback_applied.
#
# P3 model: _apply_comfort_band reads CAPABILITIES (not current mode) and
# emits ONE consistent command shape regardless of what the thermostat currently
# does.  The occupant experiences the same outcome (thermostat holds the ceiling)
# without the Ecobee side-effects of a mode-specific dispatch.


class TestWarmDayBandArming:
    """On warm/hot/mild days, apply_classification arms the comfort band.

    A dual-capable thermostat → heat_cool mode + dual setpoints; no mode-specific dispatch.
    """

    def test_warm_day_dual_thermostat_enters_heat_cool_mode(self):
        """P3+Fix4: warm day + dual-capable → hvac_mode="heat_cool" embedded in set_temperature payload.

        Fix 4 (Issue #290): the dual path emits ONE atomic set_temperature call with hvac_mode
        in the payload; no separate set_hvac_mode call.  Old P3 assertion was: 1 set_hvac_mode
        call to "heat_cool".  Now: 0 set_hvac_mode calls; hvac_mode in set_temperature data.
        """
        engine = _make_engine(comfort_heat=70.0, current_mode="off")
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        # Fix 4: NO separate set_hvac_mode call for dual-setpoint path
        hvac = _hvac_calls(engine)
        assert len(hvac) == 0

        # hvac_mode="heat_cool" must appear inside the set_temperature payload
        temp = _temp_calls(engine)
        assert len(temp) == 1
        assert temp[0].args[2].get("hvac_mode") == "heat_cool"

    def test_warm_day_dual_thermostat_sets_dual_setpoints(self):
        """P3: warm day + dual → target_temp_low=comfort_heat, target_temp_high=comfort_cool."""
        # Old assertion was: temperature=setback_heat (single setpoint for heat mode).
        # P3 new rule: full comfort band [comfort_heat=70/comfort_cool=76] for occupied+awake.
        engine = _make_engine(comfort_heat=70.0, current_mode="off")
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        calls = _temp_calls(engine)
        assert len(calls) == 1
        data = calls[0].args[2]
        assert data["target_temp_low"] == 70.0  # comfort_heat — full occupied+awake floor
        assert data["target_temp_high"] == 76.0  # comfort_cool — defended ceiling

    def test_idempotent_mode_no_redundant_heat_cool_switch(self):
        """Thermostat already in heat_cool → no set_hvac_mode call; setpoints still updated."""
        engine = _make_engine(comfort_heat=70.0, current_mode="heat_cool")
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        hvac = _hvac_calls(engine)
        assert len(hvac) == 0  # already in heat_cool — no redundant switch

        temp = _temp_calls(engine)
        assert len(temp) == 1  # setpoints are still refreshed

    def test_hot_day_same_band_arming_as_warm(self):
        """hot day (hvac_mode=cool from classifier) + dual → same single-call atomic shape as warm.

        Fix 4 (Issue #290): dual path — no separate set_hvac_mode; hvac_mode="heat_cool" in payload.
        Old P3 assertion was: 1 set_hvac_mode call to "heat_cool".  Now: 0 set_hvac_mode calls.
        """
        engine = _make_engine(comfort_heat=70.0, current_mode="off")
        c = _make_classification(day_type="hot", hvac_mode="cool")
        asyncio.run(engine.apply_classification(c))

        # Fix 4: NO separate set_hvac_mode call for dual-setpoint path
        hvac = _hvac_calls(engine)
        assert len(hvac) == 0

        temp = _temp_calls(engine)
        assert len(temp) == 1
        data = temp[0].args[2]
        assert "target_temp_low" in data and "target_temp_high" in data
        assert data.get("hvac_mode") == "heat_cool"

    def test_comfort_band_applied_event_emitted(self):
        """P3 emits comfort_band_applied instead of warm_day_setback_applied."""
        # Old assertion was: warm_day_setback_applied event with thermostat_mode/day_type.
        # P3 replaces that: comfort_band_applied event with floor/ceiling/active.
        engine = _make_engine(comfort_heat=70.0, current_mode="off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        band_events = [e for e in events if e[0] == "comfort_band_applied"]
        assert len(band_events) == 1
        payload = band_events[0][1]
        assert payload["active"] == "ceiling"
        assert payload["floor"] == 70.0  # comfort_heat — full occupied+awake band
        assert payload["ceiling"] == 76.0

    def test_cool_only_thermostat_warm_day_arms_ceiling(self):
        """Cool-only thermostat + warm day → set_hvac_mode(cool) + temperature=ceiling."""
        # Old assertion was: setback_cool via temperature call without mode change.
        # P3 replaces that: _apply_comfort_band enters "cool" mode and sets ceiling setpoint.
        engine = _make_engine(
            comfort_heat=70.0,
            current_mode="off",
            hvac_modes=["off", "cool"],
            supported_features=1,
        )
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        hvac = _hvac_calls(engine)
        assert len(hvac) == 1
        assert hvac[0].args[2]["hvac_mode"] == "cool"

        temp = _temp_calls(engine)
        assert len(temp) == 1
        assert temp[0].args[2]["temperature"] == 76.0  # comfort_cool ceiling

    def test_no_capable_mode_no_service_calls(self):
        """Entity with no capable modes → silent no-op (band not armed, no false promise)."""
        # Old assertion was: unknown mode → hard-off fallback.
        # P3 replaces that: capability-based no-op; band simply not armed this cycle.
        engine = _make_engine(
            comfort_heat=70.0,
            current_mode="off",
            hvac_modes=[],
            supported_features=0,
        )
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        assert len(_hvac_calls(engine)) == 0
        assert len(_temp_calls(engine)) == 0

    def test_indoor_below_comfort_floor_band_arms_normally(self):
        """Indoor (65°F) < comfort_heat (70°F): P3 arms the band unconditionally; no separate guard.

        Old model: comfort-gap guard fired first, setting hvac_mode='heat' to reach comfort floor.
        P3: the comfort-gap guard is gone from apply_classification; the band [setback_heat/comfort_cool]
        arms directly.  The thermostat's floor setpoint (60°F) provides the safety backstop implicitly —
        the heater fires when indoor drops below it; the band ceiling (comfort_cool) prevents overheating.
        The occupant benefits: no awkward "heat then off" oscillation; stable band from the start.
        """
        engine = _make_engine(comfort_heat=70.0, current_mode="heat_cool")

        # Override indoor temp to 65°F (below comfort floor)
        climate_state = MagicMock()
        climate_state.state = "heat_cool"
        climate_state.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
            "current_temperature": 65.0,
        }
        engine.hass.states.get.return_value = climate_state

        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        temp = _temp_calls(engine)
        assert len(temp) == 1  # dual setpoints applied regardless of indoor vs comfort_heat
        data = temp[0].args[2]
        assert "target_temp_low" in data  # band armed: floor setpoint present
        assert "target_temp_high" in data  # band armed: ceiling setpoint present

    def test_celsius_unit_conversion_preserved(self):
        """temp_unit='celsius': setpoints converted to °C before service call."""
        engine = _make_engine(
            config_overrides={
                "temp_unit": "celsius",
                "setback_heat": 60.0,  # 60°F → ~15.6°C
                "comfort_cool": 76.0,  # 76°F → ~24.4°C
                "comfort_heat": 70.0,
            },
            current_mode="off",
        )
        c = _make_classification(day_type="warm", hvac_mode="off")
        asyncio.run(engine.apply_classification(c))

        calls = _temp_calls(engine)
        assert len(calls) == 1
        data = calls[0].args[2]
        # Both low and high must be Celsius values (< 50°F would be wrong)
        assert data["target_temp_low"] < 30.0  # was 60°F → ~15.6°C
        assert data["target_temp_high"] < 30.0  # was 76°F → ~24.4°C
