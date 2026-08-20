"""Tests for Issue #698 (Epic #594 Phase R, Phase 2d): wiring
``nat_vent_temperature_check()``'s hard-exit check and mid-session cycling
behind ``_natvent_fsm_authoritative``.

Invokes the REAL ``AutomationEngine.nat_vent_temperature_check()`` (no mirror
logic) with the flag set both True and False, proving:
  - Decision 1: FSM-authoritative reacts to all 5 exit reasons (not just
    comfort-floor), and this is a genuinely NEW behavior vs. the legacy
    (flag=False) branch, which only ever reacts to comfort-floor at this
    call site.
  - Decision 2 is pre-existing infrastructure (confirmed via code archaeology,
    not re-implemented here) -- no test needed in this file; see
    tests/test_nat_vent_thermostat.py's TestNatVentFanStatusNewValue and
    tests/test_fan_control.py's WHF/HVAC-fan idle-session status tests.
  - Decision 3: the outdoor-warm reactivation guard (previously a
    hand-duplicated `outdoor >= current_temp` check) now delegates to
    `is_outdoor_rise_exit()`, applied identically in BOTH the
    FSM-authoritative and legacy branches (a pure de-duplication, not new
    behavior) -- proven by a revert-test-style pair showing both branches
    agree at the exact boundary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine

_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"
_AWAKE_NOW = datetime(2026, 4, 20, 14, 0, 0)  # 14:00 -- outside sleep window


def _make_engine(
    *,
    comfort_heat: float = 70.0,
    comfort_cool: float = 74.0,
    nat_vent_delta: float = 3.0,
    fsm_authoritative: bool,
    occupancy_mode: str = "home",
    thermal_model: dict | None = None,
) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "off"  # nat-vent has HVAC suppressed to off, matching real sessions
    climate_state.attributes = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": nat_vent_delta,
        "notify_service": "notify.notify",
        "fan_mode": "whole_house_fan",
        "fan_entity": "fan.whole_house",
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    engine._natvent_fsm_authoritative = fsm_authoritative
    engine._natural_vent_active = True
    engine._occupancy_mode = occupancy_mode
    engine._thermal_model = thermal_model
    engine._sensor_check_callback = MagicMock(return_value=True)  # a window is genuinely open
    engine._async_save_state = AsyncMock()
    return engine


class TestDecision1AwayCeilingExitOnlyReachableWhenAuthoritative:
    """AWAY_CEILING is one of the 5 exit reasons decide_nat_vent_exit() checks,
    but legacy's comfort-floor-only fast check never reaches it -- proving the
    new fast-loop widening is a genuine, gated behavior change."""

    def _run(self, *, fsm_authoritative: bool) -> AutomationEngine:
        engine = _make_engine(
            comfort_heat=60.0,
            comfort_cool=74.0,
            fsm_authoritative=fsm_authoritative,
            occupancy_mode="away",
        )
        engine._fan_active = True
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            # indoor >= comfort_cool while away -- AWAY_CEILING fires, but indoor
            # (75) is well above the comfort-floor-only legacy check (60), so
            # legacy must NOT exit here.
            asyncio.run(engine.nat_vent_temperature_check(75.0, outdoor=65.0))
        return engine

    def test_legacy_branch_does_not_exit_on_away_ceiling(self) -> None:
        engine = self._run(fsm_authoritative=False)
        assert engine._natural_vent_active is True, "legacy fast check only reacts to comfort-floor"

    def test_fsm_authoritative_branch_exits_on_away_ceiling(self) -> None:
        engine = self._run(fsm_authoritative=True)
        assert engine._natural_vent_active is False, "FSM-authoritative reacts to all 5 exit reasons"
        assert engine._fan_active is False


class TestDecision1OutdoorRiseExitOnlyReachableWhenAuthoritative:
    def _run(self, *, fsm_authoritative: bool) -> AutomationEngine:
        engine = _make_engine(comfort_heat=60.0, comfort_cool=74.0, fsm_authoritative=fsm_authoritative)
        engine._fan_active = True
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            # outdoor >= indoor -- OUTDOOR_RISE fires, but indoor (70) is well
            # above the comfort-floor-only legacy check (60).
            asyncio.run(engine.nat_vent_temperature_check(70.0, outdoor=71.0))
        return engine

    def test_legacy_branch_does_not_exit_on_outdoor_rise(self) -> None:
        engine = self._run(fsm_authoritative=False)
        assert engine._natural_vent_active is True

    def test_fsm_authoritative_branch_exits_on_outdoor_rise(self) -> None:
        engine = self._run(fsm_authoritative=True)
        assert engine._natural_vent_active is False
        assert engine._fan_active is False


class TestDecision1ComfortFloorExitIdenticalInBothBranches:
    """The comfort-floor exit reason is unchanged between branches -- both
    legacy and FSM-authoritative must still exit at the same boundary."""

    def _run(self, *, fsm_authoritative: bool) -> AutomationEngine:
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, fsm_authoritative=fsm_authoritative)
        engine._fan_active = True
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            asyncio.run(engine.nat_vent_temperature_check(70.0, outdoor=60.0))  # indoor == comfort_heat floor
        return engine

    def test_legacy_exits_at_comfort_floor(self) -> None:
        engine = self._run(fsm_authoritative=False)
        assert engine._natural_vent_active is False

    def test_fsm_authoritative_exits_at_comfort_floor(self) -> None:
        engine = self._run(fsm_authoritative=True)
        assert engine._natural_vent_active is False


class TestDecision3OutdoorWarmGuardDeduplication:
    """Decision 3: the on-threshold outdoor-warm reactivation guard now
    delegates to is_outdoor_rise_exit() in BOTH branches -- pure
    de-duplication, boundary semantics (non-strict >=) unchanged. Revert-test
    style: both branches must agree at the exact equality boundary."""

    def _run(self, *, fsm_authoritative: bool) -> AutomationEngine:
        # comfort_heat=70, comfort_cool=74 -> midpoint 72, hysteresis 1.0 -> on_threshold=73.
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, fsm_authoritative=fsm_authoritative)
        engine._fan_active = False
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            # indoor (73) >= on_threshold (73), outdoor (73) == indoor -- exact
            # equality boundary for the outdoor-warm guard.
            asyncio.run(engine.nat_vent_temperature_check(73.0, outdoor=73.0))
        return engine

    def test_legacy_blocks_reactivation_at_exact_equality(self) -> None:
        engine = self._run(fsm_authoritative=False)
        assert engine._fan_active is False, "legacy's outdoor>=current_temp check must block at equality"
        assert engine._natural_vent_active is True, "session stays alive, just fan withheld"

    def test_fsm_authoritative_blocks_reactivation_at_exact_equality(self) -> None:
        # Under FSM-authoritative, outdoor==indoor also satisfies decide_nat_vent_exit()'s
        # own OUTDOOR_RISE check (check 4), so the session exits outright here instead of
        # merely withholding the fan -- see test_nat_vent_fsm.py's
        # TestCyclingWiring.test_outdoor_at_or_above_indoor_exits_via_chain_before_cycling_runs
        # for the root cause (the exit chain runs before cycling and already excludes this
        # case). Both branches agree that the fan must NOT turn on here -- they differ only
        # in whether the session itself also ends, which is Decision 1's documented, approved
        # widening, not a Decision 3 regression.
        engine = self._run(fsm_authoritative=True)
        assert engine._fan_active is False


class TestDecision1CyclingContinuesWhenNoExitFires:
    """When none of the 5 exit reasons fire, FSM-authoritative must still cycle
    the fan hardware on/off exactly like legacy -- Decision 1 only widens the
    EXIT check, it doesn't change cycling itself."""

    def test_cycles_off_when_indoor_drops_to_off_threshold(self) -> None:
        # comfort_heat=70, comfort_cool=76 -> midpoint 73, hysteresis 1.0 -> off=72.
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, fsm_authoritative=True)
        engine.config["nat_vent_hysteresis_f"] = 1.0
        engine._fan_active = True
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            asyncio.run(engine.nat_vent_temperature_check(72.0, outdoor=60.0))
        assert engine._fan_active is False
        assert engine._natural_vent_active is True, "cycling off must not end the session"

    def test_cycles_on_when_indoor_rises_to_on_threshold(self) -> None:
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, fsm_authoritative=True)
        engine.config["nat_vent_hysteresis_f"] = 1.0
        engine._fan_active = False
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            asyncio.run(engine.nat_vent_temperature_check(74.0, outdoor=60.0))
        assert engine._fan_active is True
        assert engine._natural_vent_active is True

    def test_holds_state_between_thresholds(self) -> None:
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, fsm_authoritative=True)
        engine.config["nat_vent_hysteresis_f"] = 1.0
        engine._fan_active = True
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            asyncio.run(engine.nat_vent_temperature_check(73.0, outdoor=60.0))
        assert engine._fan_active is True
        assert engine._natural_vent_active is True


class TestDecision1ProactiveFloorExitEventPayload:
    """Confirms the fast-loop PROACTIVE_FLOOR exit emits the SAME event_type
    (nat_vent_predicted_floor_exit) its slow-loop sibling uses, with the same
    key fields, rather than mislabeling it as a comfort-floor exit."""

    def test_proactive_floor_exit_emits_correct_event_type(self) -> None:
        engine = _make_engine(
            comfort_heat=65.0,
            comfort_cool=78.0,
            fsm_authoritative=True,
            thermal_model={"confidence": "high", "k_passive": -0.5},
        )
        engine._fan_active = True
        emitted: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))
        with patch(_DT_NOW_PATH, return_value=_AWAKE_NOW):
            # passive_rate = -0.5*(67-60) = -3.5F/hr; time_to_floor=(67-65)/3.5=0.57h < 1.0h
            asyncio.run(engine.nat_vent_temperature_check(67.0, outdoor=60.0))
        assert engine._natural_vent_active is False
        event_names = [e[0] for e in emitted]
        assert "nat_vent_predicted_floor_exit" in event_names
        payload = next(p for n, p in emitted if n == "nat_vent_predicted_floor_exit")
        assert payload["source"] == "temp_check"
        assert payload["indoor_temp"] == 67.0
