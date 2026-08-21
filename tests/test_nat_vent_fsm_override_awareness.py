"""Tests for Issue #706 (Bug D, Bug F, closes #688): nat-vent FSM override/grace
awareness in real production wiring.

Occupant-first framing: without these fixes, a manual fan override (e.g. an
RF remote or a physical switch) could be silently overridden by Climate
Advisor's own decision machinery — the dashboard/automation state would claim
free cooling is running while the actual fan command was rejected, or a
just-started manual override could be clobbered by a stale decision computed
a moment earlier. These tests confirm the real ``AutomationEngine`` methods
(not a re-implementation) now keep decided state and actual hardware/override
reality in agreement.

- Bug D: ``_build_nat_vent_fsm_inputs()`` (the real production builder used by
  all 5 real call sites) previously left ``override_active``/``grace_active``
  at their dataclass default (``False``) always — only
  ``coordinator._evaluate_nat_vent_fsm()``'s shadow/diagnostic construction
  passed real values. Fixed by reading ``self._fan_override_active or
  self._manual_override_active`` and ``self._grace_active`` live.
- Bug F: 5 production call sites compute an FSM decision, then
  ``await self._activate_fan(...)`` (a real yield point) under
  ``_decision_lock``, then apply the pre-await decision. A manual override
  arriving during that await (via the NOT lock-protected
  ``handle_fan_manual_override()``/``_async_fan_entity_changed()``) is
  recorded correctly by override/grace, but the resumed decision then
  silently overwrote it. Fixed via
  ``_apply_nat_vent_fsm_state_after_activation()``, which checks
  ``_activate_fan()``'s return value and applies ``INACTIVE`` instead of the
  stale decision when it returns ``FanCommandResult.OVERRIDDEN``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE

_NOW = datetime(2026, 7, 15, 14, 0, 0)
sys.modules["homeassistant.util.dt"].now = lambda: _NOW

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime


def _make_engine(
    *,
    comfort_heat: float = 68.0,
    comfort_cool: float = 74.0,
    nat_vent_delta: float = 3.0,
    hysteresis: float = 1.0,
    indoor_f: float | None = None,
    fan_mode: str = FAN_MODE_WHOLE_HOUSE,
    authoritative: bool = True,
) -> AutomationEngine:
    """Create a real AutomationEngine with mocked HA dependencies. Same pattern
    as tests/test_nat_vent_fsm_phase2b_wiring.py's _make_engine()."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "off"
    climate_state.attributes = {} if indoor_f is None else {"current_temperature": indoor_f}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": nat_vent_delta,
        "nat_vent_hysteresis_f": hysteresis,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: fan_mode,
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    engine._natvent_fsm_authoritative = authoritative
    return engine


# ---------------------------------------------------------------------------
# (a) Production NatVentFsmInputs build reflects real override/grace state.
# ---------------------------------------------------------------------------


class TestBugDInputsReflectRealState:
    """_build_nat_vent_fsm_inputs() is the real production builder (used by all
    5 real call sites, distinct from coordinator.py's shadow-diagnostic
    construction) -- it must reflect live override/grace flags, not the
    dataclass default of False."""

    def test_override_active_true_when_fan_override_active(self) -> None:
        engine = _make_engine()
        engine._fan_override_active = True
        engine._manual_override_active = False
        engine._grace_active = False
        inputs = engine._build_nat_vent_fsm_inputs(now=_NOW, indoor=72.0, outdoor=65.0)
        assert inputs.override_active is True

    def test_override_active_true_when_manual_override_active(self) -> None:
        engine = _make_engine()
        engine._fan_override_active = False
        engine._manual_override_active = True
        engine._grace_active = False
        inputs = engine._build_nat_vent_fsm_inputs(now=_NOW, indoor=72.0, outdoor=65.0)
        assert inputs.override_active is True

    def test_override_active_false_when_no_override(self) -> None:
        engine = _make_engine()
        engine._fan_override_active = False
        engine._manual_override_active = False
        engine._grace_active = False
        inputs = engine._build_nat_vent_fsm_inputs(now=_NOW, indoor=72.0, outdoor=65.0)
        assert inputs.override_active is False

    def test_grace_active_reflects_live_flag(self) -> None:
        engine = _make_engine()
        engine._fan_override_active = False
        engine._manual_override_active = False
        engine._grace_active = True
        inputs = engine._build_nat_vent_fsm_inputs(now=_NOW, indoor=72.0, outdoor=65.0)
        assert inputs.grace_active is True

        engine._grace_active = False
        inputs = engine._build_nat_vent_fsm_inputs(now=_NOW, indoor=72.0, outdoor=65.0)
        assert inputs.grace_active is False

    def test_all_5_real_call_sites_use_this_shared_builder(self) -> None:
        """Guards against a future call site bypassing _build_nat_vent_fsm_inputs()
        and reconstructing NatVentFsmInputs by hand (which would silently
        reintroduce Bug D at that one site)."""
        import inspect

        src = inspect.getsource(_automation_mod)
        assert src.count("self._build_nat_vent_fsm_inputs(") == 5


# ---------------------------------------------------------------------------
# (b) State-desync scenario: override already active before the decision.
# ---------------------------------------------------------------------------


class TestStateDesyncWithPreExistingOverride:
    """When a manual override is ALREADY active before a nat-vent decision is
    made, the FSM must never mark _natural_vent_active True -- both because
    Bug D now feeds override_active=True into the decision itself (so the
    entry gate is never entered, and _activate_fan() is never even called),
    and because Bug F's post-activation guard is a backstop if it were."""

    def test_handle_door_window_open_skips_activation_when_override_active(self) -> None:
        engine = _make_engine(indoor_f=72.0)
        engine._last_outdoor_temp = 60.0  # would activate easily if override were ignored
        engine._fan_override_active = True
        # If Bug D were unfixed, the entry gate would clear (override_active
        # defaults to False in the FSM inputs) and _activate_fan() WOULD be
        # called. Track calls to prove it never fires.
        engine._activate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        engine._activate_fan.assert_not_awaited()
        assert engine._natural_vent_active is False

    def test_handle_door_window_open_skips_activation_when_grace_active(self) -> None:
        engine = _make_engine(indoor_f=72.0)  # indoor < comfort_cool -> no #134 exception
        engine._last_outdoor_temp = 60.0
        engine._grace_active = True
        engine._activate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        engine._activate_fan.assert_not_awaited()
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# (c) Bug F race: override arrives DURING the await window.
# ---------------------------------------------------------------------------


class TestBugFRaceDuringActivation:
    """The FSM decision is computed BEFORE `await self._activate_fan(...)`.
    _activate_fan() returning FanCommandResult.OVERRIDDEN is the definitive
    signal a real override intervened during that await -- the pre-await
    decision must not be applied in that case."""

    def _arm_idle_open(self, engine: AutomationEngine, *, outdoor: float) -> None:
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._grace_active = False
        engine._fan_override_active = False
        engine._sensor_check_callback = lambda: True
        engine._last_outdoor_temp = outdoor
        engine.hass.states.get.return_value.state = "off"

    def test_idle_open_reentry_does_not_clobber_override_that_arrives_mid_activation(self) -> None:
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0)
        self._arm_idle_open(engine, outdoor=68.0)  # favorable -> FSM decides ACTIVE_FULL_GATE

        async def _activate_fan_races_override(*, reason: str, emit_event: bool = True) -> FanCommandResult:
            # Simulates handle_fan_manual_override() winning the race during this
            # await window: a real override starts and the fan command it
            # protects is rejected.
            engine._fan_override_active = True
            engine._grace_active = True
            return FanCommandResult.OVERRIDDEN

        engine._activate_fan = AsyncMock(side_effect=_activate_fan_races_override)
        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "the pre-await ACTIVE_FULL_GATE decision must not be applied once "
            "_activate_fan() reports the command was overridden mid-activation"
        )

    def test_paused_reactivation_does_not_clobber_override_that_arrives_mid_activation(self) -> None:
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0)
        engine._paused_by_door = True
        engine._paused_with_hvac_already_off = False
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._last_outdoor_temp = 68.0
        engine._nat_vent_outdoor_exit_time = None
        engine._fan_override_active = False
        engine._grace_active = False

        async def _activate_fan_races_override(*, reason: str, emit_event: bool = True) -> FanCommandResult:
            engine._fan_override_active = True
            return FanCommandResult.OVERRIDDEN

        engine._activate_fan = AsyncMock(side_effect=_activate_fan_races_override)
        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "reactivation-while-paused must not apply its pre-await decision once "
            "the activation was actually overridden"
        )

    def test_idle_open_reentry_applies_decision_normally_when_no_race(self) -> None:
        """Control: without any override intervening, the pre-await decision is
        still applied exactly as before -- proves the guard doesn't suppress the
        normal case."""
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0)
        self._arm_idle_open(engine, outdoor=68.0)
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is True


# ---------------------------------------------------------------------------
# (d) Issue #134 overheat-during-grace exception at the engine level.
# ---------------------------------------------------------------------------


class TestOverheatDuringGraceIntegration:
    """Complements the pure-function coverage in test_nat_vent_fsm.py's
    TestGraceOverheatException with an engine-level check: with Bug D now
    wiring grace_active=True into the real decision, nat-vent must still
    engage during grace when indoor genuinely exceeds comfort_cool, and must
    stay inactive during grace otherwise."""

    def test_grace_active_and_overheating_still_engages(self) -> None:
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=78.0)
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._grace_active = True
        engine._fan_override_active = False
        engine._last_outdoor_temp = 65.0
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is True

    def test_grace_active_and_not_overheating_stays_inactive(self) -> None:
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=70.0)
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._grace_active = True
        engine._fan_override_active = False
        engine._last_outdoor_temp = 65.0
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is False
