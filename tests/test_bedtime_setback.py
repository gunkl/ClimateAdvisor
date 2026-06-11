"""Tests for bedtime setback DailyRecord storage + event emission (TDD red phase).

Parts 1 + 2 of Issue #XXX:
  Part 1 — DailyRecord new fields (setback_heat_applied_f, setback_cool_applied_f,
            setback_depth_f, setback_was_adaptive, setback_skipped_reason)
  Part 2 — handle_bedtime() event emission and DailyRecord writes

These tests are written BEFORE the implementation exists and are expected to fail
with AttributeError or AssertionError, NOT ImportError.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
)
from custom_components.climate_advisor.learning import DailyRecord

# ── Shared helpers ────────────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Construct an AutomationEngine wired for bedtime setback tests."""
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
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(
    hvac_mode: str = "heat",
    setback_modifier: float = 0.0,
    day_type: str = "cold",
    **kwargs,
) -> DayClassification:
    """Create a DayClassification bypassing __post_init__ validation."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = kwargs.get("trend_direction", "stable")
    obj.trend_magnitude = kwargs.get("trend_magnitude", 0)
    obj.today_high = kwargs.get("today_high", 55.0)
    obj.today_low = kwargs.get("today_low", 40.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 55.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 40.0)
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


def _make_today_record() -> DailyRecord:
    """Create a minimal DailyRecord for today."""
    return DailyRecord(date="2026-05-17", day_type="cold", trend_direction="stable")


# ── Part 1: DailyRecord new fields ───────────────────────────────────────────


class TestDailyRecordNewFields:
    """DailyRecord must carry five new setback tracking fields."""

    def test_default_values_are_none(self):
        """All five new fields default to None on a freshly created record."""
        record = _make_today_record()
        assert record.setback_heat_applied_f is None
        assert record.setback_cool_applied_f is None
        assert record.setback_depth_f is None
        assert record.setback_was_adaptive is None
        assert record.setback_skipped_reason is None

    def test_fields_are_serializable_via_asdict(self):
        """dataclasses.asdict() must include all five new fields."""
        record = _make_today_record()
        d = dataclasses.asdict(record)
        assert "setback_heat_applied_f" in d
        assert "setback_cool_applied_f" in d
        assert "setback_depth_f" in d
        assert "setback_was_adaptive" in d
        assert "setback_skipped_reason" in d

    def test_field_names_exact(self):
        """Field names must match the spec exactly (no typos, no underscore drift)."""
        field_names = {f.name for f in dataclasses.fields(DailyRecord)}
        for expected in (
            "setback_heat_applied_f",
            "setback_cool_applied_f",
            "setback_depth_f",
            "setback_was_adaptive",
            "setback_skipped_reason",
        ):
            assert expected in field_names, f"DailyRecord missing field: {expected!r}"

    def test_fields_accept_typed_values(self):
        """Fields accept the correct types without raising."""
        record = _make_today_record()
        record.setback_heat_applied_f = 65.0
        record.setback_cool_applied_f = 78.0
        record.setback_depth_f = 5.0
        record.setback_was_adaptive = True
        record.setback_skipped_reason = "occupancy"

        d = dataclasses.asdict(record)
        assert d["setback_heat_applied_f"] == 65.0
        assert d["setback_cool_applied_f"] == 78.0
        assert d["setback_depth_f"] == 5.0
        assert d["setback_was_adaptive"] is True
        assert d["setback_skipped_reason"] == "occupancy"


# ── Part 2: handle_bedtime() event emission + DailyRecord writes ──────────────


class TestHandleBedtimeOccupancySkip:
    """When occupancy is AWAY or VACATION, bedtime must emit skipped event and record reason."""

    def test_away_emits_bedtime_setback_skipped(self):
        """AWAY mode: emit 'bedtime_setback_skipped' with reason='occupancy'."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_AWAY)

        asyncio.run(engine.handle_bedtime())

        assert len(emitted) == 1, f"Expected 1 event, got {len(emitted)}: {emitted}"
        event_type, data = emitted[0]
        assert event_type == "bedtime_setback_skipped"
        assert data["reason"] == "occupancy"
        assert data["occupancy"] == OCCUPANCY_AWAY

    def test_vacation_emits_bedtime_setback_skipped(self):
        """VACATION mode: emit 'bedtime_setback_skipped' with reason='occupancy'."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_VACATION)

        asyncio.run(engine.handle_bedtime())

        assert len(emitted) == 1
        event_type, data = emitted[0]
        assert event_type == "bedtime_setback_skipped"
        assert data["reason"] == "occupancy"
        assert data["occupancy"] == OCCUPANCY_VACATION

    def test_away_writes_skipped_reason_to_today_record(self):
        """AWAY mode: _today_record.setback_skipped_reason must be 'occupancy'."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_AWAY)

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_skipped_reason == "occupancy"

    def test_vacation_writes_skipped_reason_to_today_record(self):
        """VACATION mode: _today_record.setback_skipped_reason must be 'occupancy'."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_VACATION)

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_skipped_reason == "occupancy"


class TestHandleBedtimeHvacOffSkip:
    """When classification.hvac_mode is 'off', bedtime arms the sleep band (no skip)."""

    def test_hvac_off_emits_bedtime_setback_event(self):
        """hvac_mode='off': emit 'bedtime_setback' (band armed, not skipped).

        #249 P3: the old model emitted 'bedtime_setback_skipped' with reason='hvac_off'
        because it dispatched on hvac_mode to pick a single setpoint.  The band model
        always arms the sleep band; off-mode days select active='ceiling' (warm-day path)
        and the band emits 'bedtime_setback' regardless of hvac_mode.
        """
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="off", day_type="warm")

        asyncio.run(engine.handle_bedtime())

        # Band model: bedtime_setback is emitted even for off-mode days.
        setback_events = [(e, d) for e, d in emitted if e == "bedtime_setback"]
        assert len(setback_events) == 1, f"Expected 1 bedtime_setback event, got: {emitted}"
        _, data = setback_events[0]
        assert data["mode"] == "off"

    def test_hvac_off_does_not_write_skipped_reason(self):
        """hvac_mode='off': setback_skipped_reason stays None (band is applied, not skipped).

        #249 P3: the old model wrote setback_skipped_reason='hvac_off'; the band model
        never skips for hvac_mode='off' — the sleep band is armed for all hvac_modes.
        """
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="off", day_type="warm")

        asyncio.run(engine.handle_bedtime())

        # Band model does not write a skipped reason for off-mode days.
        assert engine._today_record.setback_skipped_reason is None


class TestHandleBedtimeNoClassificationSkip:
    """When _current_classification is None, record skipped_reason='no_classification'."""

    def test_no_classification_writes_skipped_reason(self):
        """No classification: _today_record.setback_skipped_reason must be 'no_classification'."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = None

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_skipped_reason == "no_classification"


class TestHandleBedtimeHeatApplied:
    """When hvac_mode='heat', handle_bedtime must emit event and write DailyRecord fields."""

    def test_heat_emits_bedtime_setback_event(self):
        """Emit 'bedtime_setback' event when heat setback is applied."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        asyncio.run(engine.handle_bedtime())

        setback_events = [(e, d) for e, d in emitted if e == "bedtime_setback"]
        assert len(setback_events) == 1, f"Expected 'bedtime_setback' event, got {emitted}"

    def test_heat_event_contains_required_keys(self):
        """'bedtime_setback' event data must have mode, floor, ceiling, active, modifier.

        #249 P3: event payload changed from {target_f, depth_f, adaptive} to {floor, ceiling,
        active} — the band carries both edges and the active flag instead of a single setpoint.
        """
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat", setback_modifier=0.0)

        asyncio.run(engine.handle_bedtime())

        setback_events = [(e, d) for e, d in emitted if e == "bedtime_setback"]
        assert len(setback_events) == 1
        _, data = setback_events[0]
        assert "mode" in data, f"Missing 'mode' in event data: {data}"
        assert "floor" in data, f"Missing 'floor' in event data: {data}"
        assert "ceiling" in data, f"Missing 'ceiling' in event data: {data}"
        assert "active" in data, f"Missing 'active' in event data: {data}"
        assert "modifier" in data, f"Missing 'modifier' in event data: {data}"
        assert data["mode"] == "heat"

    def test_heat_writes_setback_heat_applied_f(self):
        """heat mode: _today_record.setback_heat_applied_f must be set to the target temp."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_heat_applied_f is not None
        # Target must be below comfort_heat (70) — it's a setback
        assert engine._today_record.setback_heat_applied_f < 70

    def test_heat_writes_setback_depth_f(self):
        """heat mode: _today_record.setback_depth_f must be positive (comfort minus target)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_depth_f is not None
        assert engine._today_record.setback_depth_f > 0

    def test_heat_writes_setback_was_adaptive(self):
        """heat mode: _today_record.setback_was_adaptive must be True or False (not None)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_was_adaptive is not None
        assert isinstance(engine._today_record.setback_was_adaptive, bool)

    def test_heat_does_not_write_cool_field(self):
        """heat mode: setback_cool_applied_f must remain None."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_cool_applied_f is None


class TestHandleBedtimeCoolApplied:
    """When hvac_mode='cool', handle_bedtime must emit event and write DailyRecord fields."""

    def test_cool_emits_bedtime_setback_event(self):
        """Emit 'bedtime_setback' event when cool setback is applied."""
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        setback_events = [(e, d) for e, d in emitted if e == "bedtime_setback"]
        assert len(setback_events) == 1, f"Expected 'bedtime_setback' event, got {emitted}"

    def test_cool_event_data_has_mode_cool(self):
        """'bedtime_setback' event data must have mode='cool', floor, ceiling, active, modifier.

        #249 P3: event payload changed from {target_f, depth_f, adaptive} to {floor, ceiling,
        active} — the band carries both edges and the active flag instead of a single setpoint.
        """
        engine = _make_engine()
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda event, data: emitted.append((event, data))
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        setback_events = [(e, d) for e, d in emitted if e == "bedtime_setback"]
        assert len(setback_events) == 1
        _, data = setback_events[0]
        assert data["mode"] == "cool"
        assert "floor" in data
        assert "ceiling" in data
        assert "active" in data
        assert "modifier" in data

    def test_cool_writes_setback_cool_applied_f(self):
        """cool mode: _today_record.setback_cool_applied_f must be set to the target temp."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_cool_applied_f is not None
        # Target must be above comfort_cool (75) — it's a setback
        assert engine._today_record.setback_cool_applied_f > 75

    def test_cool_writes_setback_depth_f(self):
        """cool mode: _today_record.setback_depth_f must be positive (target minus comfort)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_depth_f is not None
        assert engine._today_record.setback_depth_f > 0

    def test_cool_writes_setback_was_adaptive(self):
        """cool mode: _today_record.setback_was_adaptive must be True or False (not None)."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_was_adaptive is not None
        assert isinstance(engine._today_record.setback_was_adaptive, bool)

    def test_cool_does_not_write_heat_field(self):
        """cool mode: setback_heat_applied_f must remain None."""
        engine = _make_engine()
        engine._emit_event_callback = lambda *_: None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(
            hvac_mode="cool", day_type="warm", today_high=88.0, today_low=65.0
        )

        asyncio.run(engine.handle_bedtime())

        assert engine._today_record.setback_heat_applied_f is None


class TestHandleBedtimeEventCallbackGuard:
    """_emit_event_callback must be optional — no crash when it is None."""

    def test_no_callback_heat_does_not_raise(self):
        """handle_bedtime with heat classification and no callback must not raise."""
        engine = _make_engine()
        engine._emit_event_callback = None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._current_classification = _make_classification(hvac_mode="heat")

        # Should not raise
        asyncio.run(engine.handle_bedtime())

    def test_no_callback_skip_does_not_raise(self):
        """handle_bedtime with AWAY occupancy and no callback must not raise."""
        engine = _make_engine()
        engine._emit_event_callback = None
        engine._today_record = _make_today_record()
        engine.set_occupancy_mode(OCCUPANCY_AWAY)

        asyncio.run(engine.handle_bedtime())
