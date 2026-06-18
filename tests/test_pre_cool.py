"""Tests for the overnight pre-cool phase (Issue #258).

Covers:
  - compute_bedtime_setback() cool-mode sign convention (warming trend = lower ceiling)
  - handle_pre_cool() target formula, nat-vent bypass, floor clamp
  - handle_pre_cool() skip conditions (no classification, no warming trend, away, vacation, override)
  - handle_morning_wakeup() indoor_temp parameter passed through
  - No crash when _emit_event_callback is None

Tests are pure unit tests — no HA coordinator or real hass needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stub injection (same pattern as test_bedtime_setback.py)
# ---------------------------------------------------------------------------

_STUBS = Path(__file__).parent / "stubs"
if _STUBS.exists() and str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
    compute_bedtime_setback,
)
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    DEFAULT_SLEEP_COOL,
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
    PRE_COOL_MIN_HEADROOM_F,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Construct an AutomationEngine for pre-cool unit tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70.0,
        "comfort_cool": 74.0,
        "setback_heat": 62.0,
        "setback_cool": 79.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        "wake_time": "06:30",
        "sleep_time": "22:30",
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
    hvac_mode: str = "cool",
    setback_modifier: float = -3.0,
    day_type: str = "warm",
    **kwargs,
) -> DayClassification:
    """Build a DayClassification bypassing __post_init__ validation."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = kwargs.get("trend_direction", "warming")
    obj.trend_magnitude = kwargs.get("trend_magnitude", 3.0)
    obj.today_high = kwargs.get("today_high", 88.0)
    obj.today_low = kwargs.get("today_low", 65.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 96.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 70.0)
    obj.pre_condition = kwargs.get("pre_condition", False)
    obj.pre_condition_target = kwargs.get("pre_condition_target")
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    obj.window_opportunity_morning = kwargs.get("window_opportunity_morning", False)
    obj.window_opportunity_evening = kwargs.get("window_opportunity_evening", False)
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


# ---------------------------------------------------------------------------
# Tests: compute_bedtime_setback() sign convention for cool mode
# ---------------------------------------------------------------------------


class TestComputeBedtimeSetbackCoolSignConvention:
    """setback_modifier must NOT affect compute_bedtime_setback() output (Fix #333).

    Issue #258 introduced modifier application here to bank cold thermal mass before
    hot days. Issue #333 identified that handle_bedtime() uses select_comfort_band()
    (raw sleep temp, no modifier) — so compute_bedtime_setback() was only corrupting
    display/chart, not the thermostat. The modifier is now applied exclusively by
    handle_pre_cool() as a separate mid-night event.
    """

    _BASE_CONFIG = {
        "comfort_heat": 70.0,
        "comfort_cool": 74.0,
        "setback_cool": 79.0,
        "setback_heat": 62.0,
    }

    def test_warming_trend_does_not_change_cool_ceiling(self):
        """setback_modifier=-3 must NOT lower the compute_bedtime_setback() ceiling."""
        config = {**self._BASE_CONFIG, "sleep_cool": 78.0}
        c_baseline = _make_classification(hvac_mode="cool", setback_modifier=0.0)
        c_warming = _make_classification(hvac_mode="cool", setback_modifier=-3.0)

        baseline = compute_bedtime_setback(config, {}, c_baseline)
        warming = compute_bedtime_setback(config, {}, c_warming)

        assert warming == pytest.approx(baseline), (
            f"compute_bedtime_setback() must ignore setback_modifier (pre-cool handles it); "
            f"got baseline={baseline}, warming={warming}"
        )

    def test_warming_trend_cool_ceiling_is_raw_sleep_cool(self):
        """With warming trend and explicit sleep_cool, result must equal sleep_cool (no modifier)."""
        config = {**self._BASE_CONFIG, "sleep_cool": 78.0}
        c = _make_classification(hvac_mode="cool", setback_modifier=-3.0)

        result = compute_bedtime_setback(config, {}, c)

        assert result == pytest.approx(78.0), f"Expected raw sleep_cool=78°F (modifier ignored), got {result}"

    def test_cooling_trend_does_not_change_cool_ceiling(self):
        """setback_modifier=+2 must NOT raise the compute_bedtime_setback() ceiling."""
        config = {**self._BASE_CONFIG, "sleep_cool": 78.0}
        c_baseline = _make_classification(hvac_mode="cool", setback_modifier=0.0)
        c_cooling = _make_classification(hvac_mode="cool", setback_modifier=2.0)

        baseline = compute_bedtime_setback(config, {}, c_baseline)
        cooling = compute_bedtime_setback(config, {}, c_cooling)

        assert cooling == pytest.approx(baseline), (
            f"compute_bedtime_setback() must ignore setback_modifier; got baseline={baseline}, cooling={cooling}"
        )

    def test_heat_mode_warming_trend_does_not_change_heat_floor(self):
        """In heat mode, setback_modifier must not affect compute_bedtime_setback() output."""
        config = {**self._BASE_CONFIG, "sleep_heat": 66.0}
        c_baseline = _make_classification(hvac_mode="heat", setback_modifier=0.0)
        c_warming = _make_classification(hvac_mode="heat", setback_modifier=2.0)

        baseline = compute_bedtime_setback(config, {}, c_baseline)
        warming = compute_bedtime_setback(config, {}, c_warming)

        assert warming == pytest.approx(baseline), (
            f"compute_bedtime_setback() must ignore setback_modifier; got baseline={baseline}, warming={warming}"
        )


# ---------------------------------------------------------------------------
# Tests: handle_pre_cool() — target formula
# ---------------------------------------------------------------------------


class TestHandlePreCoolTargetFormula:
    """Verify the pre-cool target computation: sleep_cool + setback_modifier, floored at comfort_heat+2."""

    def test_target_uses_sleep_cool_plus_modifier(self):
        """Target = sleep_cool(78) + modifier(-3) = 75 with no clamp needed."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.5, nat_vent_just_closed=False))

        assert result.startswith("applied"), f"Expected applied result, got: {result!r}"
        applied_events = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied_events) == 1
        _, payload = applied_events[0]
        assert payload["target"] == pytest.approx(75.0), f"Expected target 78 + (-3) = 75°F, got {payload['target']}"

    def test_target_uses_default_sleep_cool_when_not_configured(self):
        """Without explicit sleep_cool in config, falls back to DEFAULT_SLEEP_COOL(78)."""
        engine = _make_engine()  # no sleep_cool in config
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        asyncio.run(engine.handle_pre_cool(indoor_temp=76.5, nat_vent_just_closed=False))

        applied_events = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied_events) == 1
        _, payload = applied_events[0]
        # DEFAULT_SLEEP_COOL=78 + (-3) = 75
        assert payload["target"] == pytest.approx(DEFAULT_SLEEP_COOL + (-3.0))

    def test_floor_clamp_prevents_target_below_comfort_heat_plus_headroom(self):
        """When target < comfort_heat + PRE_COOL_MIN_HEADROOM_F, clamp to floor."""
        # sleep_cool=72, modifier=-5 → raw=67, floor=70+2=72, clamped to 72
        engine = _make_engine({"sleep_cool": 72.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-5.0)

        asyncio.run(engine.handle_pre_cool(indoor_temp=70.0, nat_vent_just_closed=False))

        applied_events = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied_events) == 1
        _, payload = applied_events[0]
        floor = 70.0 + PRE_COOL_MIN_HEADROOM_F
        assert payload["target"] == pytest.approx(floor), (
            f"Expected target clamped to comfort_heat(70)+headroom(2)=72°F, got {payload['target']}"
        )

    def test_pre_cool_modifier_in_event_payload(self):
        """Event payload must carry the modifier value for observability."""
        engine = _make_engine({"sleep_cool": 78.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-2.0)

        asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        applied_events = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied_events) == 1
        _, payload = applied_events[0]
        assert "modifier" in payload
        assert payload["modifier"] == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# Tests: handle_pre_cool() — nat-vent bypass
# ---------------------------------------------------------------------------


class TestHandlePreCoolNatVentBypass:
    """When nat-vent just closed and indoor <= target, suppress AC."""

    def test_bypass_when_nat_vent_closed_and_indoor_at_target(self):
        """Exact equality: indoor == target → suppress AC (nat-vent got it to target)."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)
        # target = 75.0; indoor exactly at target

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=75.0, nat_vent_just_closed=True))

        assert result.startswith("suppressed"), f"Expected suppressed result, got: {result!r}"
        suppressed = [(e, d) for e, d in emitted if e == "pre_cool_suppressed_nat_vent"]
        assert len(suppressed) == 1
        applied = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied) == 0, "Should not emit pre_cool_applied when suppressed"

    def test_bypass_when_nat_vent_closed_and_indoor_below_target(self):
        """indoor < target → suppress AC (nat-vent over-delivered)."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)
        # target = 75.0; indoor 74 = below target

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=74.0, nat_vent_just_closed=True))

        assert result.startswith("suppressed"), f"Expected suppressed result, got: {result!r}"

    def test_no_bypass_when_nat_vent_closed_but_indoor_above_target(self):
        """nat_vent_just_closed=True but indoor > target → AC still needed."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)
        # target = 75.0; indoor 76 = above target → AC should run

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=True))

        assert result.startswith("applied"), f"Expected applied result, got: {result!r}"

    def test_no_bypass_when_nat_vent_not_just_closed(self):
        """nat_vent_just_closed=False: bypass check skipped even if indoor <= target."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)
        # indoor at 74 (below target 75) but nat_vent_just_closed=False → fallback path, AC runs

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=74.0, nat_vent_just_closed=False))

        assert result.startswith("applied"), f"Expected applied result, got: {result!r}"

    def test_bypass_suppressed_event_carries_indoor_and_target(self):
        """Suppression event must carry indoor and target for observability."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        asyncio.run(engine.handle_pre_cool(indoor_temp=74.5, nat_vent_just_closed=True))

        suppressed = [(e, d) for e, d in emitted if e == "pre_cool_suppressed_nat_vent"]
        assert len(suppressed) == 1
        _, payload = suppressed[0]
        assert "indoor" in payload
        assert "target" in payload
        assert payload["indoor"] == pytest.approx(74.5)
        assert payload["target"] == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# Tests: handle_pre_cool() — skip conditions
# ---------------------------------------------------------------------------


class TestHandlePreCoolSkipConditions:
    """Pre-cool must be silently skipped in various guard conditions."""

    def test_skip_when_no_classification(self):
        """No classification → return 'skipped: no warming trend'."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = None

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped, got: {result!r}"

    def test_skip_when_modifier_is_zero(self):
        """setback_modifier=0 (stable trend) → no pre-cool."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=0.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped for stable trend, got: {result!r}"

    def test_skip_when_modifier_is_positive(self):
        """setback_modifier=+2 (cooling trend) → no pre-cool (home is already cooling naturally)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=2.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped for cooling trend, got: {result!r}"

    def test_skip_when_away(self):
        """AWAY occupancy → skip (setback already active; pre-cool not needed)."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_AWAY)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped for away, got: {result!r}"
        applied = [e for e, _ in emitted if e == "pre_cool_applied"]
        assert len(applied) == 0

    def test_skip_when_vacation(self):
        """VACATION occupancy → skip."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_VACATION)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped for vacation, got: {result!r}"

    def test_skip_when_manual_override_active(self):
        """Manual override active → skip pre-cool (user is in control)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)
        engine._manual_override_active = True

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))

        assert result.startswith("skipped"), f"Expected skipped for manual override, got: {result!r}"

    def test_no_crash_when_emit_callback_is_none(self):
        """No crash when _emit_event_callback is None (callback is optional)."""
        engine = _make_engine({"sleep_cool": 78.0})
        engine._emit_event_callback = None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        # Should not raise
        result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))
        assert result.startswith("applied")

    def test_no_crash_when_emit_callback_is_none_suppressed(self):
        """No crash when _emit_event_callback is None and nat-vent bypasses AC."""
        engine = _make_engine({"sleep_cool": 78.0})
        engine._emit_event_callback = None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=74.0, nat_vent_just_closed=True))
        assert result.startswith("suppressed")


# ---------------------------------------------------------------------------
# Tests: handle_pre_cool() — indoor_temp is None
# ---------------------------------------------------------------------------


class TestHandlePreCoolIndoorTempNone:
    """When indoor_temp is None, pre-cool must still apply setpoint (no bypass possible)."""

    def test_applies_setpoint_when_indoor_unknown(self):
        """No indoor temp → cannot evaluate nat-vent bypass → AC runs as safety measure."""
        engine = _make_engine({"sleep_cool": 78.0, "comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        result = asyncio.run(engine.handle_pre_cool(indoor_temp=None, nat_vent_just_closed=True))

        # Even with nat_vent_just_closed=True, no bypass without indoor temp
        assert result.startswith("applied"), f"Expected applied when indoor=None, got: {result!r}"
        applied = [(e, d) for e, d in emitted if e == "pre_cool_applied"]
        assert len(applied) == 1


# ---------------------------------------------------------------------------
# Tests: handle_morning_wakeup() — indoor_temp parameter
# ---------------------------------------------------------------------------


class TestHandleMorningWakeupIndoorTemp:
    """handle_morning_wakeup() now accepts indoor_temp for pre-cool overshoot guard."""

    def test_accepts_indoor_temp_parameter(self):
        """handle_morning_wakeup(indoor_temp=72.0) must not raise."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        # Should not raise
        asyncio.run(engine.handle_morning_wakeup(indoor_temp=72.0))

    def test_accepts_indoor_temp_none(self):
        """handle_morning_wakeup(indoor_temp=None) must not raise."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        asyncio.run(engine.handle_morning_wakeup(indoor_temp=None))

    def test_overshoot_emits_event_when_indoor_below_comfort_heat(self):
        """If indoor < comfort_heat at wake-up, emit pre_cool_overshoot event."""
        engine = _make_engine({"comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        asyncio.run(engine.handle_morning_wakeup(indoor_temp=68.0))  # below comfort_heat=70

        overshoot = [e for e, _ in emitted if e == "pre_cool_overshoot"]
        assert len(overshoot) == 1, "Expected pre_cool_overshoot event when indoor(68) < comfort_heat(70)"

    def test_no_overshoot_when_indoor_at_or_above_comfort_heat(self):
        """If indoor >= comfort_heat at wake-up, no pre_cool_overshoot event."""
        engine = _make_engine({"comfort_heat": 70.0})
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda e, d: emitted.append((e, d))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(setback_modifier=-3.0)

        asyncio.run(engine.handle_morning_wakeup(indoor_temp=70.0))  # exactly at comfort_heat

        overshoot = [e for e, _ in emitted if e == "pre_cool_overshoot"]
        assert len(overshoot) == 0, "No pre_cool_overshoot event when indoor(70) >= comfort_heat(70)"
