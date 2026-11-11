"""Follow-up regression tests for Design D "nearest-edge arming" (Issue #1066).

Real ``AutomationEngine`` against a fake Home Assistant whose thermostat follows the commands
the engine sends. Covers the independent review's findings:

- F1  TOU banking is not undone by the comfort-edge selector (both directions, Home and
      Away/Vacation, bank ends -> selector resumes).
- F2  A thermostat that ignores commands does not make the reactive check spam service calls
      and "switched" events (rate limit, once-per-wish event, dry-run).
- F3  The ODE ceiling guard does not write cool while the selector preview guards the floor
      (margin zone, hysteresis gap with heat armed); cool zone still writes.
- F4  Blind mode restores no longer write the wrong mode before the selector (single mode
      write, hot-day and cold-day mirror).

Occupant framing: on a hot afternoon with a pre-cool bank the house must not alternate
between furnace and AC; on a thermostat that ignores commands the owner must not get a
service-call storm or a wall of identical "switched" log rows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.comfort_family_decision import (
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    transition,
)
from custom_components.climate_advisor.const import COMFORT_REACTIVE_RETRY_S

_AUTOMATION = "custom_components.climate_advisor.automation"
_BOTH = ["off", "heat", "cool"]
_T0 = datetime(2026, 7, 11, 13, 0, 0, tzinfo=UTC)


def _consume_coroutine(coro):
    coro.close()


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def _make_engine(
    *,
    indoor: float | None,
    mode: str = "cool",
    comfort_heat: float = 68.0,
    comfort_cool: float = 74.0,
    follows_commands: bool = True,
    **cfg,
) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = mode
    state.attributes = {
        "hvac_modes": list(_BOTH),
        "supported_features": 1,
        "current_temperature": indoor,
    }
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate" or not follows_commands:
            return None
        if service == "set_hvac_mode":
            state.state = data["hvac_mode"]
        elif service == "set_temperature":
            if "hvac_mode" in data:
                state.state = data["hvac_mode"]
            state.attributes["temperature"] = data["temperature"]
        return None

    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_call)
    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 62.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        **cfg,
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    engine.inputs.thermal = {}
    engine._test_state = state
    return engine


def _classification(day_type: str, hvac_mode: str) -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 95.0
    obj.today_low = 70.0
    obj.tomorrow_high = 95.0
    obj.tomorrow_low = 70.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_indoor(engine: AutomationEngine, value: float) -> None:
    engine._test_state.attributes["current_temperature"] = value


def _mode(engine: AutomationEngine) -> str:
    return engine._test_state.state


def _service_calls(engine: AutomationEngine) -> list[tuple[str, dict]]:
    return [(c.args[1], c.args[2]) for c in engine.hass.services.async_call.call_args_list if c.args[0] == "climate"]


def _mode_writes(engine: AutomationEngine) -> list[str]:
    """Every mode the engine asked the thermostat to take (set_hvac_mode or set_temperature hvac_mode)."""
    return [
        d["hvac_mode"]
        for s, d in _service_calls(engine)
        if s in ("set_hvac_mode", "set_temperature") and "hvac_mode" in d
    ]


def _set_hvac_mode_calls(engine: AutomationEngine) -> list[str]:
    """Only the explicit climate.set_hvac_mode calls (the 'mode write' the double-write bug duplicated)."""
    return [d["hvac_mode"] for s, d in _service_calls(engine) if s == "set_hvac_mode"]


# ---------------------------------------------------------------------------
# F1 -- TOU banking is not undone by the selector
# ---------------------------------------------------------------------------


class TestLeafDefersToTouPrecondition:
    def _inputs(self, indoor: float, current: str | None, tou: bool) -> ComfortFamilyInputs:
        return ComfortFamilyInputs(
            current_family=current,
            default_family="cooling",
            indoor=indoor,
            floor=68.0,
            ceiling=74.0,
            margin_f=2.0,
            natural_vent_active=False,
            whf_owns_hvac=False,
            now=_T0,
            tou_precondition_active=tou,
        )

    def test_not_applicable_in_every_zone_while_banking(self):
        for indoor in (60.0, 67.5, 69.0, 71.0, 73.5, 80.0):
            for current in ("cooling", "heating", None):
                d = decide_comfort_family(self._inputs(indoor, current, True))
                assert d.outcome is ComfortFamilyOutcome.NOT_APPLICABLE, (indoor, current)
                assert "TOU" in d.reason

    def test_negative_control_same_readings_switch_without_banking(self):
        # Without the flag the cool bank at 69 F WOULD flip to heat -- the flag is what prevents it.
        d = decide_comfort_family(self._inputs(69.0, "cooling", False))
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == "heating"
        d = decide_comfort_family(self._inputs(73.0, "heating", False))
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == "cooling"

    def test_fsm_passes_the_flag_through(self):
        inputs = ComfortFamilyFsmInputs(
            default_family="cooling",
            indoor=66.0,
            floor=68.0,
            ceiling=74.0,
            margin_f=2.0,
            natural_vent_active=False,
            whf_owns_hvac=False,
            now=_T0,
            min_dwell_seconds=600.0,
            tou_precondition_active=True,
        )
        result = transition(None, ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=inputs))
        assert result.decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE
        assert not result.changed


class TestCoordinatorPushesTouWindow:
    def test_precondition_phase_pushes_window_and_mode_and_clears_after(self):
        from tests.test_tou_away_vacation_coordinator import _build_coordinator

        coord = _build_coordinator("home")
        now = datetime(2026, 1, 5, 15, 30)  # 30 min before the 16:00 start -> PRECONDITIONING (45 min default lead)
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
            coord._resolve_tou_schedule_state()
        ae = coord.automation_engine
        until, mode = ae.set_tou_precondition_window.call_args[0]
        assert until == datetime(2026, 1, 5, 16, 0)
        assert mode == "cool"

        later = datetime(2026, 1, 5, 21, 0)
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=later):
            coord._resolve_tou_schedule_state()
        assert ae.set_tou_precondition_window.call_args[0] == (None,)


class TestTouBankNotUndoneBySelector:
    """Hot day: cool/64 bank (floor 68, ceiling 74). Readings 70, 69, 67.5 must not flip it to heat."""

    def _banked(self, *, hvac_mode: str, target: float, indoor: float, mode: str, window: bool = True):
        engine = _make_engine(indoor=indoor, mode=mode)
        clock = _Clock(_T0)
        c = _classification("hot" if hvac_mode == "cool" else "cold", hvac_mode)
        engine._current_classification = c
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            if window:
                engine.set_tou_precondition_window(clock.now + timedelta(hours=1), hvac_mode)
            _run(engine.apply_tou_precondition(c, target, "s1"))
        return engine, clock, c

    def _cycle(self, engine, clock, c, target, reading):
        """One reading: the reactive check, then the coordinator's regular cycle (classification + bank)."""
        clock.advance(minutes=5)
        _set_indoor(engine, reading)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(reading, predicted_indoor=None))
            _run(engine.apply_classification(c))
            _run(engine.apply_tou_precondition(c, target, "s1"))

    def test_precool_bank_survives_readings_70_69_67_5(self):
        engine, clock, c = self._banked(hvac_mode="cool", target=64.0, indoor=71.0, mode="off")
        assert _mode(engine) == "cool" and engine._test_state.attributes["temperature"] == 64.0
        for reading in (70.0, 69.0, 67.5):
            self._cycle(engine, clock, c, 64.0, reading)
            assert _mode(engine) == "cool"
            assert engine._test_state.attributes["temperature"] == 64.0
        assert "heat" not in _mode_writes(engine)

    def test_negative_control_without_the_window_the_selector_flips_to_heat(self):
        engine, clock, c = self._banked(hvac_mode="cool", target=64.0, indoor=71.0, mode="off", window=False)
        # Issue #1416: every bank write is now a "cool ran" record, so the settling guard ALSO holds cool in the heat
        # margin (69) right after the bank; that second defence is correct, but it hides what this control isolates.
        # 62.5 is 5.5 F under the floor, deeper than the hot-day tolerance (5 F): the guard releases (R1), so only
        # the TOU window deferral can stop the flip (with the window, a breach is deferred too, see
        # test_breach_during_banking_is_deliberately_deferred).
        for reading in (70.0, 62.5):
            self._cycle(engine, clock, c, 64.0, reading)
        assert "heat" in _mode_writes(engine), "the deferral is what prevents the heat/cool fight"

    def test_preheat_bank_above_the_ceiling_is_not_flipped_to_cool(self):
        """Mirror: cold day, heat bank to 76 (above the 74 ceiling); readings 72, 73, 74.5, 75."""
        engine, clock, c = self._banked(hvac_mode="heat", target=76.0, indoor=70.0, mode="off")
        assert _mode(engine) == "heat" and engine._test_state.attributes["temperature"] == 76.0
        for reading in (72.0, 73.0, 74.5, 75.0):
            self._cycle(engine, clock, c, 76.0, reading)
            assert _mode(engine) == "heat"
            assert engine._test_state.attributes["temperature"] == 76.0
        assert "cool" not in _mode_writes(engine)

    def test_negative_control_preheat_without_the_window_flips_to_cool(self):
        engine, clock, c = self._banked(hvac_mode="heat", target=76.0, indoor=70.0, mode="off", window=False)
        # Issue #1073: 73.0 twice (cool margin zone, a non-breach switch). The old 75.0 is a 1 F ceiling
        # breach right after the heat bank -- exactly the overshoot the settling guard now HOLDS, so it can
        # no longer serve as the "selector flips without the window" control. The first reading (+5 min) is
        # inside the 600 s lock, the second (+10 min) is past it.
        # Issue #1416: the heat bank write is now a "heat ran" record too, so 73.0 is held by the settling guard; 79.5
        # is 5.5 F over the ceiling, deeper than the cold-day tolerance (5 F), so only the window can stop the flip.
        for reading in (73.0, 79.5):
            self._cycle(engine, clock, c, 76.0, reading)
        assert "cool" in _mode_writes(engine)

    def test_bank_ends_and_the_selector_resumes(self):
        engine, clock, c = self._banked(hvac_mode="cool", target=64.0, indoor=71.0, mode="off")
        self._cycle(engine, clock, c, 64.0, 69.0)
        assert _mode(engine) == "cool"
        clock.advance(hours=2)  # past the window start
        _set_indoor(engine, 69.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(69.0, predicted_indoor=None))
        assert _mode(engine) == "heat"

    def test_mirror_bank_ends_and_the_selector_resumes_to_cool(self):
        engine, clock, c = self._banked(hvac_mode="heat", target=76.0, indoor=70.0, mode="off")
        self._cycle(engine, clock, c, 76.0, 73.0)
        assert _mode(engine) == "heat"
        clock.advance(hours=2)
        _set_indoor(engine, 73.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(73.0, predicted_indoor=None))
        assert _mode(engine) == "cool"

    def test_breach_during_banking_is_deliberately_deferred(self):
        """Decision pin: a floor breach while a cool bank runs does not flip to heat. The bank target IS a
        comfort edge (sleep-aware); the selector resumes when the phase ends."""
        engine, clock, c = self._banked(hvac_mode="cool", target=64.0, indoor=71.0, mode="off")
        self._cycle(engine, clock, c, 64.0, 66.0)  # below the 68 floor
        assert _mode(engine) == "cool"

    def test_plain_band_write_is_pinned_to_the_bank_direction(self):
        """A heat bank while the thermostat still sits in cool must not be flipped to cool by the plain band."""
        engine, clock, c = self._banked(hvac_mode="heat", target=76.0, indoor=70.0, mode="cool")
        for reading in (72.0, 75.0):
            self._cycle(engine, clock, c, 76.0, reading)
        assert _mode(engine) == "heat"


class TestAwayVacationBankNotUndone:
    def _engine(self, *, hvac_mode: str, indoor: float, mode: str, window: bool):
        engine = _make_engine(indoor=indoor, mode=mode)
        c = _classification("hot" if hvac_mode == "cool" else "cold", hvac_mode)
        engine._current_classification = c
        engine._occupancy_mode = "away"
        clock = _Clock(_T0)
        if window:
            engine.set_tou_precondition_window(clock.now + timedelta(hours=1), hvac_mode)
        return engine, clock, c

    def _cycle(self, engine, clock, c, target, reading, bank_mode):
        clock.advance(minutes=5)
        _set_indoor(engine, reading)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(reading, predicted_indoor=None))
            _run(engine.apply_classification(c))
            _run(engine.apply_tou_away_vacation_precondition(c, target, "s1", bank_mode))

    def test_precool_bank_holds_cool_in_away_mode(self):
        engine, clock, c = self._engine(hvac_mode="cool", indoor=78.0, mode="off", window=True)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_away_vacation_precondition(c, 70.0, "s1", "cool"))
        assert _mode(engine) == "cool"
        for reading in (76.0, 72.0, 69.0, 67.5):
            self._cycle(engine, clock, c, 70.0, reading, "cool")
            assert _mode(engine) == "cool"
        assert "heat" not in _mode_writes(engine)

    def test_preheat_bank_holds_heat_in_away_mode(self):
        engine, clock, c = self._engine(hvac_mode="heat", indoor=60.0, mode="off", window=True)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_away_vacation_precondition(c, 66.0, "s1", "heat"))
        assert _mode(engine) == "heat"
        for reading in (62.0, 66.0, 73.0, 75.0):
            self._cycle(engine, clock, c, 66.0, reading, "heat")
            assert _mode(engine) == "heat"
        assert "cool" not in _mode_writes(engine)


# ---------------------------------------------------------------------------
# F2 -- a thermostat that ignores commands
# ---------------------------------------------------------------------------


class TestIgnoringThermostat:
    def _run_readings(self, engine, clock, readings, *, step_s=60):
        c = _classification("mild", "off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            for reading in readings:
                clock.advance(seconds=step_s)
                _set_indoor(engine, reading)
                _run(engine.comfort_family_temperature_check(reading, predicted_indoor=None))
        return events

    def test_floor_breach_on_an_ignoring_thermostat_is_bounded(self):
        """Indoor 66 (below the 68 floor), thermostat stuck in cool and ignoring commands, one reading/min
        for 10 minutes: bounded service calls and ONE switch event (was 17 calls / 11 events)."""
        engine = _make_engine(indoor=66.0, mode="cool", follows_commands=False)
        events = self._run_readings(engine, _Clock(_T0), [66.0] * 10)
        switches = [e for e in events if e[0] == "comfort_family_switch"]
        assert len(switches) == 1
        # one evaluation per 120 s retry window (initial + 5) x (mode switch + setpoint) = 12; was 22+ for 11 readings
        assert len(_service_calls(engine)) <= 12, _service_calls(engine)

    def test_mirror_ceiling_breach_on_an_ignoring_thermostat_is_bounded(self):
        engine = _make_engine(indoor=76.0, mode="heat", follows_commands=False)
        events = self._run_readings(engine, _Clock(_T0), [76.0] * 10)
        assert len([e for e in events if e[0] == "comfort_family_switch"]) == 1
        # one evaluation per 120 s retry window (initial + 5) x (mode switch + setpoint) = 12; was 22+ for 11 readings
        assert len(_service_calls(engine)) <= 12, _service_calls(engine)

    def test_retry_resumes_after_the_window(self):
        engine = _make_engine(indoor=66.0, mode="cool", follows_commands=False)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            attempted = len(_service_calls(engine))
            clock.advance(seconds=COMFORT_REACTIVE_RETRY_S - 5)
            _run(engine.comfort_family_temperature_check(66.0, predicted_indoor=None))
            assert len(_service_calls(engine)) == attempted, "inside the window: no retry"
            clock.advance(seconds=10)
            _run(engine.comfort_family_temperature_check(66.0, predicted_indoor=None))
            assert len(_service_calls(engine)) > attempted, "after the window: retried"

    def test_mirror_retry_resumes_after_the_window(self):
        engine = _make_engine(indoor=76.0, mode="heat", follows_commands=False)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            attempted = len(_service_calls(engine))
            clock.advance(seconds=COMFORT_REACTIVE_RETRY_S - 5)
            _run(engine.comfort_family_temperature_check(76.0, predicted_indoor=None))
            assert len(_service_calls(engine)) == attempted
            clock.advance(seconds=10)
            _run(engine.comfort_family_temperature_check(76.0, predicted_indoor=None))
            assert len(_service_calls(engine)) > attempted

    def test_opposite_side_breach_is_not_rate_limited(self):
        engine = _make_engine(indoor=66.0, mode="cool", follows_commands=False)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))  # wants heating (attempt recorded)
            before = len(_service_calls(engine))
            clock.advance(seconds=10)
            engine._test_state.state = "heat"  # the thermostat finally follows...
            _set_indoor(engine, 76.0)  # ...and now the OTHER edge is breached
            _run(engine.comfort_family_temperature_check(76.0, predicted_indoor=None))
        assert len(_service_calls(engine)) > before, "a wish for the other family must go through at once"

    def test_mirror_opposite_side_breach_cool_then_heat(self):
        engine = _make_engine(indoor=76.0, mode="heat", follows_commands=False)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))  # wants cooling
            before = len(_service_calls(engine))
            clock.advance(seconds=10)
            engine._test_state.state = "cool"
            _set_indoor(engine, 66.0)
            _run(engine.comfort_family_temperature_check(66.0, predicted_indoor=None))
        assert len(_service_calls(engine)) > before

    def test_switch_event_repeats_only_when_the_resolved_family_changes(self):
        engine = _make_engine(indoor=66.0, mode="cool", follows_commands=True)
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))  # cool -> heat
            clock.advance(minutes=30)
            _set_indoor(engine, 76.0)
            _run(engine.apply_classification(c))  # heat -> cool (genuinely new switch)
        assert events.count("comfort_family_switch") == 2


class TestDryRunSelector:
    def test_dry_run_does_not_reswitch_every_reading(self):
        """Observe-only: nothing is written, so the live mode never moves. The last resolved family stands
        in for it -- one switch event, not one per reading."""
        engine = _make_engine(indoor=66.0, mode="cool")
        engine.dry_run = True
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            for _ in range(6):
                clock.advance(minutes=3)
                _run(engine.comfort_family_temperature_check(66.0, predicted_indoor=None))
        assert events.count("comfort_family_switch") == 1
        assert _service_calls(engine) == []

    def test_dry_run_mirror_ceiling(self):
        engine = _make_engine(indoor=76.0, mode="heat")
        engine.dry_run = True
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            for _ in range(6):
                clock.advance(minutes=3)
                _run(engine.comfort_family_temperature_check(76.0, predicted_indoor=None))
        assert events.count("comfort_family_switch") == 1


# ---------------------------------------------------------------------------
# F3 -- ODE ceiling guard vs the selector preview
# ---------------------------------------------------------------------------


class TestOdeGuardSkipsWhileSelectorGuardsFloor:
    def _engine(self, indoor: float, mode: str = "off") -> AutomationEngine:
        engine = _make_engine(indoor=indoor, mode=mode, comfort_heat=68.0, comfort_cool=74.0)
        engine._last_outdoor_temp = 76.0
        engine.inputs.thermal = {
            "k_passive": -0.05,
            "confidence_k_passive": "medium",
            "k_active_cool": None,
            "confidence": "medium",
        }
        return engine

    def _curve(self) -> list[dict]:
        base = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate([73.0, 74.5, 76.0])]

    def _fire(self, engine) -> tuple[list, str]:
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(now)):
            _run(engine.apply_classification(_classification("warm", "off"), predicted_indoor=self._curve()))
        return events, _mode(engine)

    def test_margin_zone_70_skips_the_cool_write(self):
        events, mode = self._fire(self._engine(70.0))
        assert "ceiling_guard_fired" not in events and mode == "heat"

    def test_hysteresis_gap_with_heat_armed_skips_the_cool_write(self):
        """71 F on 68/74 (m=2): the gap. Thermostat already in heat -> keep heat, no cool write."""
        events, mode = self._fire(self._engine(71.0, mode="heat"))
        assert "ceiling_guard_fired" not in events and mode == "heat"

    def test_hysteresis_gap_with_cool_armed_still_writes(self):
        events, mode = self._fire(self._engine(71.0, mode="cool"))
        # Issue #1230 R6 (X1): the band arms cool at the ceiling itself, so the guard has nothing left to write
        assert "ceiling_guard_fired" not in events and mode == "cool"

    def test_cool_zone_writes(self):
        for indoor in (72.0, 73.5):
            events, mode = self._fire(self._engine(indoor, mode="heat"))
            # Issue #1230 R6 (X1): the band switches heat -> cool at the ceiling itself; the guard adds nothing
            assert "ceiling_guard_fired" not in events and mode == "cool", indoor


# ---------------------------------------------------------------------------
# F4 -- blind restores no longer write the wrong mode first
# ---------------------------------------------------------------------------


class TestRestoreWritesModeOnce:
    def test_resume_from_pause_hot_day_floor_breach_writes_heat_once(self):
        """Hot day (cool classification) but indoor is below the floor when the user resumes: one mode
        write, to heat -- never cool then heat (65 ms double write)."""
        engine = _make_engine(indoor=66.0, mode="off")
        c = _classification("hot", "cool")
        engine._current_classification = c
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            restored = _run(engine.resume_from_pause())
        assert restored == "cool"
        assert _set_hvac_mode_calls(engine) == ["heat"]
        assert set(_mode_writes(engine)) == {"heat"}
        assert _mode(engine) == "heat"

    def test_resume_from_pause_cold_day_ceiling_breach_writes_cool_once(self):
        engine = _make_engine(indoor=76.0, mode="off")
        c = _classification("cold", "heat")
        engine._current_classification = c
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.resume_from_pause())
        assert _set_hvac_mode_calls(engine) == ["cool"]
        assert set(_mode_writes(engine)) == {"cool"}

    def test_economizer_off_with_floor_breach_writes_heat_once(self):
        engine = _make_engine(indoor=66.0, mode="off")
        engine._current_classification = _classification("hot", "cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._deactivate_economizer(70.0))
        assert _set_hvac_mode_calls(engine) == ["heat"]
        assert set(_mode_writes(engine)) == {"heat"}

    def test_economizer_off_normal_hot_day_still_restores_cool(self):
        engine = _make_engine(indoor=75.0, mode="off")
        engine._current_classification = _classification("hot", "cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._deactivate_economizer(70.0))
        assert _set_hvac_mode_calls(engine) == ["cool"]
        assert set(_mode_writes(engine)) == {"cool"}
        assert _mode(engine) == "cool"


# ---------------------------------------------------------------------------
# Second review (Opus) -- fixes 1-4
# ---------------------------------------------------------------------------


def _resolution(phase, *, mode="cool", schedule_id="s1", start=None, target=64.0):
    from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

    start = start if start is not None else _T0 + timedelta(hours=1)
    return TOUPhaseResolution(
        TOUPhase.PRECONDITIONING if phase == "P" else TOUPhase.NONE,
        target,
        mode,
        schedule_id,
        start,
        start - timedelta(minutes=30),
    )


class TestStickyTouPreconditionWindow:
    """Fix 1: the scheduler's per-cycle lead recompute can drop PRECONDITIONING to NONE mid-bank. The
    coordinator keeps pushing the same window until it lapses at schedule_start, so the selector does not
    resume, flip a banked house to heat (or cool) and alternate furnace / AC."""

    _COORD = "custom_components.climate_advisor.coordinator"

    def _setup(self, *, bank_mode: str, indoor: float, away: bool = False):
        from tests.test_tou_away_vacation_coordinator import _build_coordinator

        coord = _build_coordinator("away" if away else "home")
        engine = _make_engine(indoor=indoor, mode="off")
        engine._occupancy_mode = "away" if away else "home"
        c = _classification("hot" if bank_mode == "cool" else "cold", bank_mode)
        engine._current_classification = c
        coord.automation_engine = engine
        coord._current_classification = c
        coord._get_indoor_temp = MagicMock(return_value=indoor)
        return coord, engine, c, _Clock(_T0)

    def _sync(self, coord, clock, res):
        """One coordinator cycle's TOU resolution with the scheduler's answer pinned to ``res``."""
        name = "resolve_tou_away_vacation_phase" if coord._occupancy_mode == "away" else "resolve_tou_phase"
        with (
            patch(f"{self._COORD}.dt_util.now", side_effect=clock),
            patch(f"{self._COORD}.{name}", return_value=res),
        ):
            coord._resolve_tou_schedule_state()

    def _engine_cycle(self, engine, clock, c, res, reading, away):
        """The engine half of one cycle: reactive check, plain classification, then the bank when PRECONDITIONING."""
        _set_indoor(engine, reading)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(reading, predicted_indoor=None))
            _run(engine.apply_classification(c))
            if res.phase.name == "PRECONDITIONING":
                if away:
                    _run(engine.apply_tou_away_vacation_precondition(c, res.target, "s1", res.mode))
                else:
                    _run(engine.apply_tou_precondition(c, res.target, "s1"))

    def _drive(self, coord, engine, clock, c, steps, *, away=False, **res_kw):
        """steps: list of (phase 'P'|'N', indoor)."""
        for phase, reading in steps:
            clock.advance(minutes=5)
            res = _resolution(phase, **res_kw)
            coord._get_indoor_temp = MagicMock(return_value=reading)
            self._sync(coord, clock, res)
            self._engine_cycle(engine, clock, c, res, reading, away)

    def test_cool_bank_survives_a_none_gap_with_indoor_in_the_heat_margin(self):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=71.0)
        self._drive(
            coord, engine, clock, c, [("P", 71.0), ("P", 70.5), ("N", 69.6), ("N", 69.6), ("P", 69.6), ("N", 69.6)]
        )
        assert _mode(engine) == "cool"
        assert "heat" not in _mode_writes(engine)
        assert engine._tou_precondition_until == _T0 + timedelta(hours=1)

    def test_negative_control_dropping_the_window_in_the_gap_flips_to_heat(self):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=71.0)
        self._drive(coord, engine, clock, c, [("P", 70.5)])
        engine.set_tou_precondition_window(None)  # what the pre-fix coordinator did on a NONE cycle
        _set_indoor(engine, 69.6)
        # Issue #1073 (S3): the TOU bank write now arms the 600 s mode-change lock, which would
        # also stop this non-breach flip; advance past it so the control still isolates the window.
        # Issue #1416: the bank write is also a "cool ran" record for the settling guard, which would hold cool in
        # the heat margin; advance past comfort_family_recency_window_min (120) as well, for the same reason.
        clock.advance(minutes=121)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.comfort_family_temperature_check(69.6, predicted_indoor=None))
        assert "heat" in _mode_writes(engine)

    def test_heat_bank_survives_a_none_gap_with_indoor_near_the_ceiling(self):
        coord, engine, c, clock = self._setup(bank_mode="heat", indoor=70.0)
        self._drive(
            coord,
            engine,
            clock,
            c,
            [("P", 70.0), ("P", 72.0), ("N", 72.6), ("N", 72.6), ("P", 72.6), ("N", 72.6)],
            mode="heat",
            target=76.0,
        )
        assert _mode(engine) == "heat"
        assert "cool" not in _mode_writes(engine)

    def test_away_cool_bank_survives_a_none_gap(self):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=78.0, away=True)
        self._drive(
            coord, engine, clock, c, [("P", 78.0), ("P", 69.6), ("N", 69.6), ("N", 69.6), ("P", 69.6)], away=True
        )
        assert _mode(engine) == "cool"
        assert "heat" not in _mode_writes(engine)

    def test_away_heat_bank_survives_a_none_gap(self):
        coord, engine, c, clock = self._setup(bank_mode="heat", indoor=60.0, away=True)
        self._drive(
            coord,
            engine,
            clock,
            c,
            [("P", 60.0), ("P", 72.6), ("N", 72.6), ("N", 72.6), ("P", 72.6)],
            away=True,
            mode="heat",
            target=66.0,
        )
        assert _mode(engine) == "heat"
        assert "cool" not in _mode_writes(engine)

    def test_window_lapses_at_schedule_start(self):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=71.0)
        self._drive(coord, engine, clock, c, [("P", 70.0), ("N", 69.6)])
        assert engine._tou_precondition_until is not None
        clock.now = _T0 + timedelta(hours=1)  # exactly schedule_start
        self._sync(coord, clock, _resolution("N"))
        assert engine._tou_precondition_until is None
        assert coord._tou_precondition_sticky is None

    def _banked_then(self, res_after, *, away_after: bool = False):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=71.0)
        self._drive(coord, engine, clock, c, [("P", 70.0)])
        assert engine._tou_precondition_until is not None
        clock.advance(minutes=5)
        if away_after:
            coord._occupancy_mode = "away"
        if res_after is None:
            coord.config = {**coord.config, "schedules": []}
            with patch(f"{self._COORD}.dt_util.now", side_effect=clock):
                coord._resolve_tou_schedule_state()
        else:
            self._sync(coord, clock, res_after)
        return coord, engine, clock

    def test_cleared_when_the_schedule_changes(self):
        coord, engine, clock = self._banked_then(_resolution("N", schedule_id="s2"))
        assert engine._tou_precondition_until is None

    def test_cleared_when_the_schedule_start_changes(self):
        coord, engine, clock = self._banked_then(_resolution("N", start=_T0 + timedelta(days=1)))
        assert engine._tou_precondition_until is None

    def test_cleared_when_the_mode_changes(self):
        coord, engine, clock = self._banked_then(_resolution("N", mode="heat"))
        assert engine._tou_precondition_until is None

    def test_cleared_when_the_occupancy_class_changes(self):
        coord, engine, clock = self._banked_then(_resolution("N"), away_after=True)
        assert engine._tou_precondition_until is None

    def test_cleared_when_the_resolution_is_none(self):
        coord, engine, clock = self._banked_then(None)
        assert engine._tou_precondition_until is None
        assert coord._tou_precondition_sticky is None

    def test_a_cleared_window_is_not_resurrected_by_a_later_matching_none(self):
        coord, engine, clock = self._banked_then(_resolution("N", schedule_id="s2"))
        clock.advance(minutes=5)
        self._sync(coord, clock, _resolution("N"))  # the original key again, but never seen PRECONDITIONING since
        assert engine._tou_precondition_until is None

    def test_none_without_a_prior_preconditioning_never_holds_a_window(self):
        coord, engine, c, clock = self._setup(bank_mode="cool", indoor=71.0)
        self._sync(coord, clock, _resolution("N"))
        assert engine._tou_precondition_until is None


class TestRetryLimitCoversAnUnknownModeThermostat:
    """Fix 2: a thermostat that is Off (family unknown) and ignores commands is rate-limited too."""

    def _readings(self, engine, readings):
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))
            for reading in readings:
                clock.advance(seconds=60)
                _set_indoor(engine, reading)
                _run(engine.comfort_family_temperature_check(reading, predicted_indoor=None))
        return events

    def test_off_thermostat_ignoring_commands_floor_breach_is_bounded(self):
        engine = _make_engine(indoor=66.0, mode="off", follows_commands=False)
        self._readings(engine, [66.0] * 10)
        assert len(_service_calls(engine)) <= 12, _service_calls(engine)

    def test_off_thermostat_ignoring_commands_ceiling_breach_is_bounded(self):
        engine = _make_engine(indoor=78.0, mode="off", follows_commands=False)
        self._readings(engine, [78.0] * 10)
        assert len(_service_calls(engine)) <= 12, _service_calls(engine)

    def test_off_thermostat_opposite_side_breach_is_not_limited(self):
        engine = _make_engine(indoor=66.0, mode="off", follows_commands=False)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))  # wants heating from Off
            before = len(_service_calls(engine))
            clock.advance(seconds=10)
            _set_indoor(engine, 78.0)
            _run(engine.comfort_family_temperature_check(78.0, predicted_indoor=None))
        assert len(_service_calls(engine)) > before

    def test_off_thermostat_that_follows_still_switches_immediately(self):
        engine = _make_engine(indoor=66.0, mode="off", follows_commands=True)
        self._readings(engine, [66.0])
        assert _mode(engine) == "heat"


class TestBypassWriteDoesNotSuppressARealSwitch:
    """Fix 3: after a mode change made WITHOUT the selector (TOU bank here), a genuine later switch back
    to the previously resolved family must emit its INFO / event / _last_comfort_switch."""

    def _scenario(self, *, indoor_first: float, indoor_second: float, mode: str, bank_mode: str, target: float):
        engine = _make_engine(indoor=indoor_first, mode=mode)
        events: list[str] = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        clock = _Clock(_T0)
        c = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(c))  # selector switch #1
            clock.advance(minutes=30)
            bank_c = _classification("hot" if bank_mode == "cool" else "cold", bank_mode)
            engine._current_classification = bank_c
            _run(engine.apply_tou_precondition(bank_c, target, "s1"))  # bypasses the selector
            assert _mode(engine) == bank_mode
            clock.advance(minutes=30)
            engine._current_classification = c
            # Issue #1073: the second breach is deeper than the mild 2 F tolerance, otherwise the settling
            # guard HOLDS it (the bank just ran the opposite side) and there is no second switch to emit.
            _set_indoor(engine, indoor_second)
            _run(engine.apply_classification(c))  # selector wants the SAME family as switch #1 again
        return engine, events

    def test_heat_then_bank_cool_then_heat_again_emits_second_event(self):
        engine, events = self._scenario(
            indoor_first=66.0, indoor_second=62.0, mode="cool", bank_mode="cool", target=64.0
        )
        assert _mode(engine) == "heat"
        assert events.count("comfort_family_switch") == 2

    def test_cool_then_bank_heat_then_cool_again_emits_second_event(self):
        engine, events = self._scenario(
            indoor_first=76.0, indoor_second=80.0, mode="heat", bank_mode="heat", target=76.0
        )
        assert _mode(engine) == "cool"
        assert events.count("comfort_family_switch") == 2

    def test_selector_writes_do_not_clear_the_remembered_wish(self):
        engine = _make_engine(indoor=66.0, mode="cool", follows_commands=False)
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(_classification("mild", "off")))
        assert engine._last_resolved_family == "heating"


class TestOdeGuardUsesTheGuardsIndoor:
    """Fix 4: the selector preview inside the ODE ceiling write gate reads the SAME indoor the guard decided on."""

    def test_live_indoor_none_but_guard_indoor_in_the_heat_margin_skips_the_cool_write(self):
        engine = _make_engine(indoor=None, mode="off", comfort_heat=68.0, comfort_cool=74.0)
        engine._last_outdoor_temp = 76.0
        engine.inputs.thermal = {
            "k_passive": -0.05,
            "confidence_k_passive": "medium",
            "k_active_cool": None,
            "confidence": "medium",
        }
        curve = TestOdeGuardSkipsWhileSelectorGuardsFloor()._curve()
        events: list[str] = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        real_inputs = engine._comfort_family_inputs
        seen_indoor: list[float | None] = []

        def _spy(*args, **kwargs):
            seen_indoor.append(kwargs.get("indoor"))
            return real_inputs(*args, **kwargs)

        engine._comfort_family_inputs = _spy
        # The guard decides on the value the ODE decision carries; the write gate must preview with that
        # same value, not re-read the live sensor (which may have gone None in between).
        guard_indoor = 70.0
        with (
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(now)),
            patch.object(AutomationEngine, "_get_indoor_temp_f", side_effect=[guard_indoor] * 40 + [None] * 40),
        ):
            _run(engine.apply_classification(_classification("warm", "off"), predicted_indoor=curve))
        assert "ceiling_guard_fired" not in events
        assert guard_indoor in seen_indoor, seen_indoor
