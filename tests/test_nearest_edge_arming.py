"""Shell-level tests for Design D "nearest-edge arming" (Issue #1066).

Drives the REAL ``AutomationEngine`` (``apply_classification``, ``_apply_comfort_band``,
``comfort_family_temperature_check``, the ODE ceiling guard, the door/window restore path)
against a fake Home Assistant whose thermostat actually follows the commands the engine
sends -- so "the commanded mode at each step" is read from the thermostat, not mirrored
from the logic under test. The pure decision rule is covered by
``test_comfort_family_decision.py`` / ``test_comfort_family_fsm.py``.

Occupant framing: before #1066 a cold morning (or a restart, or a door close) could leave
the thermostat in COOL below the comfort floor for minutes; these tests pin that the
thermostat guards the nearer comfort edge on the first evaluation and that restarts and
restores keep the correct side.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine, ComfortBand
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.comfort_family_fsm import ComfortFamilyDwellState
from custom_components.climate_advisor.const import COMFORT_MODE_CHANGE_LOG_CAP

_AUTOMATION = "custom_components.climate_advisor.automation"
_BOTH = ["off", "heat", "cool"]


def _consume_coroutine(coro):
    coro.close()


class _Clock:
    """Controllable replacement for ``dt_util.now`` (the engine reads it for dwell/command ages)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


_T0 = datetime(2026, 10, 11, 8, 0, 0, tzinfo=UTC)


def _make_engine(
    *,
    indoor: float | None,
    mode: str = "cool",
    comfort_heat: float = 68.0,
    comfort_cool: float = 74.0,
    hvac_modes: list[str] | None = None,
    sensors: list[str] | None = None,
    **cfg,
) -> AutomationEngine:
    """Engine + a thermostat that follows the engine's climate service calls."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    state = MagicMock()
    state.state = mode
    state.attributes = {
        "hvac_modes": hvac_modes if hvac_modes is not None else list(_BOTH),
        "supported_features": 1,
        "current_temperature": indoor,
    }
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate":
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
        door_window_sensors=sensors or [],
        notify_service=config["notify_service"],
        config=config,
    )
    engine.inputs.thermal = {}
    engine._test_state = state  # handle for tests
    return engine


def _set_indoor(engine: AutomationEngine, value: float | None) -> None:
    engine._test_state.attributes["current_temperature"] = value


def _thermostat_mode(engine: AutomationEngine) -> str:
    return engine._test_state.state


def _classification(day_type: str = "mild", hvac_mode: str = "off") -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 70.0
    obj.today_low = 55.0
    obj.tomorrow_high = 71.0
    obj.tomorrow_low = 56.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _mode_commands(engine: AutomationEngine) -> list[str]:
    return [
        c.args[2]["hvac_mode"]
        for c in engine.hass.services.async_call.call_args_list
        if c.args[1] in ("set_hvac_mode", "set_temperature") and "hvac_mode" in c.args[2]
    ]


def _run(coro):
    return asyncio.run(coro)


class TestFirstEvaluation:
    def test_cold_morning_arms_heat_on_the_first_evaluation(self):
        """Indoor 66 (below the 68 floor), mild day, thermostat in cool: heat at once, no sustain wait."""
        engine = _make_engine(indoor=66.0, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(_classification("mild", "off")))

        assert _thermostat_mode(engine) == "heat"
        assert _mode_commands(engine)[0] == "heat"
        assert engine._test_state.attributes["temperature"] == 68.0
        assert engine._comfort_mode_family == "heating"

    def test_restart_while_heating_keeps_heat_and_never_writes_cool(self):
        """Fresh engine (no history), live thermostat in heat, indoor in the gap: keeps heat."""
        engine = _make_engine(indoor=71.0, mode="heat")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(_classification("mild", "off")))

        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"

    def test_restart_while_heating_below_floor_keeps_heat(self):
        engine = _make_engine(indoor=67.0, mode="heat")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))

        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"

    def test_mirror_restart_while_cooling_on_a_heat_day_keeps_cool(self):
        """Heat-classified day, indoor above the ceiling, thermostat in cool: stays cool (breach)."""
        engine = _make_engine(indoor=75.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("cold", "heat")))

        assert "heat" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "cool"

    def test_mirror_restart_while_cooling_in_the_gap_on_a_heat_day_keeps_cool(self):
        engine = _make_engine(indoor=71.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("cold", "heat")))

        assert "heat" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "cool"


class TestDoorCloseRestore:
    def test_restore_after_door_close_keeps_heat(self):
        """Paused from heat; indoor mid-band after the pause. The restore must not flip heat -> cool
        (the pre-#1066 restore re-armed the day's default cool edge 65 ms after restoring heat)."""
        engine = _make_engine(indoor=71.0, mode="off", sensors=["binary_sensor.front_door"])
        engine._current_classification = _classification("mild", "off")
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"
        engine._paused_since = _T0
        engine._paused_entity = "binary_sensor.front_door"
        engine.hass.states.get.side_effect = lambda eid: (
            engine._test_state if eid == "climate.thermostat" else MagicMock(state="off")
        )

        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.handle_all_doors_windows_closed())
            assert _thermostat_mode(engine) == "heat"
            # The next classification cycle (grace over) re-arms the band: it must see the thermostat
            # in heat and keep it, not re-arm the mild day's default cool edge.
            clock.advance(minutes=30)
            engine._grace_active = False
            _run(engine.apply_classification(_classification("mild", "off")))

        assert _mode_commands(engine), "the restore must command the thermostat"
        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"


class TestMixedDayWalk:
    def test_commanded_mode_follows_the_nearest_edge_through_a_day(self):
        """floor 68 / ceiling 74 / margin 2: heat to 70, hold through the gap, cool from 72 (and above 74),
        back to heat at 70 and below. Each reading goes through the real reactive check."""
        engine = _make_engine(indoor=66.0, mode="cool")
        clock = _Clock(_T0)
        classification = _classification("mild", "off")
        modes: list[str] = []
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(classification))
            modes.append(_thermostat_mode(engine))  # the 66 reading
            for reading in (68, 70, 71, 72, 73, 75, 73, 71, 70, 67)[:]:
                clock.advance(minutes=15)
                _set_indoor(engine, float(reading))
                _run(engine.comfort_family_temperature_check(float(reading), predicted_indoor=None))
                modes.append(_thermostat_mode(engine))

        #           66      68      70      71      72      73      75      73      71      70      67
        assert modes == ["heat", "heat", "heat", "heat", "cool", "cool", "cool", "cool", "cool", "heat", "heat"]


class TestReactiveGate:
    def _armed_engine(self, *, mode: str, indoor: float) -> tuple[AutomationEngine, _Clock]:
        engine = _make_engine(indoor=indoor, mode=mode)
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_classification(_classification("mild", "off")))
        return engine, clock

    def test_fires_for_an_in_band_reading_that_crosses_a_margin(self):
        engine, clock = self._armed_engine(mode="heat", indoor=69.0)
        assert _thermostat_mode(engine) == "heat"
        clock.advance(minutes=30)
        with (
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock),
            patch.object(engine, "apply_classification", new=AsyncMock()) as apply,
        ):
            _run(engine.comfort_family_temperature_check(72.0, predicted_indoor=None))
        apply.assert_awaited_once()

    def test_does_not_fire_when_the_selector_agrees(self):
        engine, clock = self._armed_engine(mode="heat", indoor=69.0)
        clock.advance(minutes=30)
        with (
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock),
            patch.object(engine, "apply_classification", new=AsyncMock()) as apply,
        ):
            _run(engine.comfort_family_temperature_check(71.0, predicted_indoor=None))
        apply.assert_not_awaited()

    def test_does_not_fire_inside_the_dwell_lock_for_a_non_breach_reading(self):
        engine, clock = self._armed_engine(mode="heat", indoor=69.0)
        engine._comfort_family_dwell_state = ComfortFamilyDwellState(dwell_since=clock.now - timedelta(seconds=100))
        with (
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock),
            patch.object(engine, "apply_classification", new=AsyncMock()) as apply,
        ):
            _run(engine.comfort_family_temperature_check(72.0, predicted_indoor=None))
        apply.assert_not_awaited()

    def test_breach_fires_even_inside_the_dwell_lock(self):
        engine, clock = self._armed_engine(mode="heat", indoor=69.0)
        engine._comfort_family_dwell_state = ComfortFamilyDwellState(dwell_since=clock.now - timedelta(seconds=100))
        with (
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock),
            patch.object(engine, "apply_classification", new=AsyncMock()) as apply,
        ):
            _run(engine.comfort_family_temperature_check(75.0, predicted_indoor=None))
        apply.assert_awaited_once()

    def test_preview_does_not_mutate_engine_state(self):
        engine, clock = self._armed_engine(mode="heat", indoor=69.0)
        dwell_before = engine._comfort_family_dwell_state
        family_before = engine._comfort_mode_family
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            would, result = engine._comfort_family_would_switch(
                "cool", floor=68.0, ceiling=74.0, now=clock.now, day_type="mild", indoor=73.0
            )
        assert would is True
        assert result.to_state.value == "cooling"
        assert engine._comfort_family_dwell_state is dwell_before
        assert engine._comfort_mode_family == family_before

    def test_locked_out_switch_emits_the_lockout_event(self):
        engine = _make_engine(indoor=69.0, mode="cool")
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))
        now = _T0
        engine._comfort_family_dwell_state = ComfortFamilyDwellState(dwell_since=now - timedelta(seconds=100))
        resolved = engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=74.0, now=now, day_type="mild")
        assert resolved == "cool"
        assert [n for n, _ in events] == ["comfort_family_switch_locked_out"]
        assert events[0][1]["candidate_family"] == "heating"


class TestOtherBandsAndCapabilities:
    def test_away_band_uses_the_same_rule(self):
        """Empty house at 63F (setback band 62-80): inside the heat margin, so heat guards 62."""
        engine = _make_engine(indoor=63.0, mode="cool")
        engine._current_classification = _classification("mild", "off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.handle_occupancy_away())

        assert _thermostat_mode(engine) == "heat"
        assert engine._test_state.attributes["temperature"] == 62.0

    def test_sleep_band_uses_the_same_rule(self):
        band = ComfortBand(floor=64.0, ceiling=72.0, active="ceiling", reason="sleep band")
        engine = _make_engine(indoor=65.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._apply_comfort_band(band, reason="bedtime"))

        assert _thermostat_mode(engine) == "heat"
        assert engine._test_state.attributes["temperature"] == 64.0

    def test_heat_only_thermostat_always_gets_heat(self):
        band = ComfortBand(floor=68.0, ceiling=74.0, active="ceiling", reason="test")
        engine = _make_engine(indoor=73.5, mode="heat", hvac_modes=["off", "heat"])
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._apply_comfort_band(band, reason="test"))
        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"

    def test_cool_only_thermostat_always_gets_cool(self):
        band = ComfortBand(floor=68.0, ceiling=74.0, active="floor", reason="test")
        engine = _make_engine(indoor=66.0, mode="cool", hvac_modes=["off", "cool"])
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._apply_comfort_band(band, reason="test"))
        assert "heat" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "cool"

    def test_narrow_band_only_switches_on_a_breach(self):
        """Band 70-72 (width 2): no usable interior margin. 70.5 keeps cool; 69 (a breach) arms heat."""
        engine = _make_engine(indoor=70.5, mode="cool", comfort_heat=70.0, comfort_cool=72.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))
        assert _thermostat_mode(engine) == "cool"

        _set_indoor(engine, 69.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0 + timedelta(hours=1))):
            _run(engine.comfort_family_temperature_check(69.0, predicted_indoor=None))
        assert _thermostat_mode(engine) == "heat"

    def test_unavailable_indoor_does_not_switch(self):
        engine = _make_engine(indoor=None, mode="heat")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))
        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"

    def test_nat_vent_ownership_does_not_switch(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        engine._natural_vent_active = True
        resolved = engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=74.0, now=_T0, day_type="mild")
        assert resolved == "cool"

    def test_nat_vent_ownership_skips_the_reactive_check(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        engine._current_classification = _classification()
        engine._last_comfort_band = ComfortBand(floor=68.0, ceiling=74.0, active="ceiling", reason="x")
        engine._natural_vent_active = True
        with patch.object(engine, "apply_classification", new=AsyncMock()) as apply:
            _run(engine.comfort_family_temperature_check(66.0, predicted_indoor=None))
        apply.assert_not_awaited()

    def test_whf_ownership_does_not_switch(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        with patch.object(engine, "_whf_owns_hvac", return_value=True):
            resolved = engine._resolve_comfort_family_via_fsm(
                "cool", floor=68.0, ceiling=74.0, now=_T0, day_type="mild"
            )
        assert resolved == "cool"


class TestOdeCeilingWriteGate:
    def _engine(self, indoor: float) -> AutomationEngine:
        engine = _make_engine(indoor=indoor, mode="off", comfort_heat=70.0, comfort_cool=74.0)
        engine._last_outdoor_temp = 76.0
        engine.inputs.thermal = {
            "k_passive": -0.05,
            "confidence_k_passive": "medium",
            "k_active_cool": None,
            "k_passive_via_bridge": False,
            "confidence": "medium",
        }
        return engine

    def _curve(self) -> list[dict]:
        base = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate([73.0, 74.5, 76.0])]

    def test_write_skipped_when_the_selector_is_guarding_the_floor(self):
        engine = self._engine(indoor=69.0)
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(now)):
            _run(engine.apply_classification(_classification("warm", "off"), predicted_indoor=self._curve()))

        assert "ceiling_guard_fired" not in events
        assert "cool" not in _mode_commands(engine)
        assert _thermostat_mode(engine) == "heat"

    def test_write_happens_near_the_ceiling(self):
        engine = self._engine(indoor=73.5)
        events: list = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(now)):
            _run(engine.apply_classification(_classification("warm", "off"), predicted_indoor=self._curve()))

        # Issue #1230 R6 (X1): at 73.5 the band arms cool at the ceiling itself; the guard has nothing left to write
        assert "ceiling_guard_fired" not in events
        assert _thermostat_mode(engine) == "cool"


class TestModeChangeRingBuffer:
    def test_records_each_commanded_mode_change_with_context(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        engine._last_selector_margin = None
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))

        changes = engine.comfort_mode_changes
        assert len(changes) == 1
        entry = changes[0]
        assert entry["from_mode"] is None
        assert entry["to_mode"] == "heat"
        assert entry["indoor_f"] == 66.0
        assert entry["floor_f"] == 68.0
        assert entry["ceiling_f"] == 74.0
        assert entry["reason"] == "selector"
        assert entry["ts"] == _T0.isoformat()

    def test_unchanged_mode_is_not_recorded_again(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))
            _run(engine.apply_classification(_classification("mild", "off")))
        assert len(engine.comfort_mode_changes) == 1

    def test_pause_and_restore_reasons(self):
        engine = _make_engine(indoor=71.0, mode="heat", sensors=["binary_sensor.front_door"])
        engine._current_classification = _classification("mild", "off")
        engine.hass.states.get.side_effect = lambda eid: (
            engine._test_state if eid == "climate.thermostat" else MagicMock(state="off")
        )
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine._set_hvac_mode("heat", reason="seed"))
            _run(
                engine._pause_for_door_window_action(
                    entity_label="door", reason="open", notify_message="m", notify_type="t"
                )
            )
            engine._paused_by_door = True
            _run(engine.handle_all_doors_windows_closed())

        reasons = [(c["to_mode"], c["reason"]) for c in engine.comfort_mode_changes]
        assert ("off", "pause") in reasons
        assert ("heat", "restore") in reasons

    def test_buffer_is_capped(self):
        engine = _make_engine(indoor=70.0, mode="off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            for i in range(COMFORT_MODE_CHANGE_LOG_CAP + 50):
                engine._record_commanded_mode("heat" if i % 2 == 0 else "cool")
        changes = engine.comfort_mode_changes
        assert len(changes) == COMFORT_MODE_CHANGE_LOG_CAP == 200
        # newest entries are the ones kept
        assert changes[-1]["to_mode"] == "cool"

    def test_property_returns_a_copy(self):
        engine = _make_engine(indoor=70.0, mode="off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            engine._record_commanded_mode("heat")
        engine.comfort_mode_changes[0]["to_mode"] = "mutated"
        assert engine.comfort_mode_changes[0]["to_mode"] == "heat"

    def test_round_trip_through_serializable_state(self):
        engine = _make_engine(indoor=66.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock(_T0)):
            _run(engine.apply_classification(_classification("mild", "off")))
        state = json.loads(json.dumps(engine.get_serializable_state()))

        fresh = _make_engine(indoor=66.0, mode="heat")
        fresh.restore_state(state)
        assert fresh.comfort_mode_changes == engine.comfort_mode_changes

    def test_restore_rejects_garbage_and_tolerates_absence(self):
        good = {
            "ts": _T0.isoformat(),
            "from_mode": "cool",
            "to_mode": "heat",
            "indoor_f": 66.0,
            "floor_f": 68.0,
            "ceiling_f": 74.0,
            "margin_f": 2.0,
            "reason": "selector",
        }
        garbage = [
            "not a dict",
            42,
            None,
            {"ts": "not-a-date", "to_mode": "heat"},
            {"ts": _T0.isoformat(), "to_mode": 7},
            {"ts": _T0.isoformat(), "to_mode": "heat", "from_mode": 3},
            {**good, "indoor_f": "warm", "reason": "<script>", "extra": "ignored"},
        ]
        engine = _make_engine(indoor=70.0, mode="off")
        engine.restore_state({"comfort_mode_changes": [good, *garbage]})
        changes = engine.comfort_mode_changes
        assert changes[0] == good
        assert len(changes) == 2  # the good one + the sanitizable one
        sanitized = changes[1]
        assert sanitized["indoor_f"] is None
        assert sanitized["reason"] == "other"
        assert "extra" not in sanitized

        for absent in ({}, {"comfort_mode_changes": None}, {"comfort_mode_changes": "x"}, {"comfort_mode_changes": {}}):
            fresh = _make_engine(indoor=70.0, mode="off")
            fresh.restore_state(absent)
            assert fresh.comfort_mode_changes == []

    def test_restore_caps_oversized_buffers(self):
        entry = {"ts": _T0.isoformat(), "from_mode": None, "to_mode": "heat", "reason": "other"}
        engine = _make_engine(indoor=70.0, mode="off")
        engine.restore_state({"comfort_mode_changes": [dict(entry) for _ in range(1000)]})
        assert len(engine.comfort_mode_changes) == COMFORT_MODE_CHANGE_LOG_CAP
