"""Tests for Issue #540 (scoped from #533): nat-vent soft-start at outdoor/indoor parity.

Covers the automation-engine-level wiring around ``decide_nat_vent_soft_start_gate()``
(unit-tested directly in test_nat_vent_gate.py::TestDecideNatVentSoftStartGate):
  - Entry via the paused-by-door reactivation site, on by default (opt-out)
  - Full gate takes precedence over soft-start when both would qualify
  - HVAC-only fan archetype does not qualify
  - Thin sample-count buffer fails safe (restart/timezone edge case)
  - Upgrade from soft-start to full nat-vent as outdoor keeps falling
  - Existing exit hierarchy (outdoor-rise exit) clears the soft-start qualifier too

Mirrors the fixture pattern from test_nat_vent_activation.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    CONF_NAT_VENT_SOFT_START_ENABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 29, 18, 0, 0)

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime


def _make_engine(
    comfort_heat: float = 70.0,
    comfort_cool: float = 74.0,
    nat_vent_delta: float = 3.0,
    indoor_f: float | None = None,
    soft_start_enabled: bool = True,
    fan_mode: str = FAN_MODE_WHOLE_HOUSE,
) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies, soft-start opt-in set."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {}
    if indoor_f is not None:
        climate_state.attributes = {"current_temperature": indoor_f}

    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": nat_vent_delta,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: fan_mode,
        CONF_NAT_VENT_SOFT_START_ENABLED: soft_start_enabled,
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    return engine


def _arm_paused_reactivation(
    engine: AutomationEngine,
    *,
    outdoor: float,
    outdoor_today_peak: float = 90.0,
    outdoor_sample_count: int = 5,
) -> None:
    """Set up engine state so check_natural_vent_conditions() reaches the
    paused-by-door reactivation branch with no lockout in effect."""
    engine._paused_by_door = True
    engine._natural_vent_active = False
    engine._nat_vent_soft_start = False
    engine._last_outdoor_temp = outdoor
    engine._nat_vent_outdoor_exit_time = None
    engine._outdoor_temp_today_peak = outdoor_today_peak
    engine._outdoor_temp_today_sample_count = outdoor_sample_count


class TestSoftStartEntry:
    """Parity-only entry when the full bulk-cooling gate hasn't cleared yet."""

    def test_activates_soft_start_at_parity_past_peak(self):
        # comfort_cool=74, indoor=76 (> comfort_heat and > comfort_cool); outdoor=75.5
        # (<= indoor, so parity holds; full gate needs outdoor < indoor - 1.0 = 75.0, which
        # 75.5 does NOT satisfy — full gate stays False, soft-start should take over).
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0)
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=90.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True

    def test_config_key_absent_falls_back_to_on_by_default(self):
        """DEFAULT_NAT_VENT_SOFT_START_ENABLED is True — an install that predates this
        config key (key absent entirely, not just unset) must still get soft-start."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0)
        del engine.config[CONF_NAT_VENT_SOFT_START_ENABLED]
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=90.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True

    def test_user_disabled_config_stays_paused(self):
        """Opt-out setting (default on, per DEFAULT_NAT_VENT_SOFT_START_ENABLED) — a user who
        explicitly disables it gets the old strict-delta-only behavior."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0, soft_start_enabled=False)
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=90.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False

    def test_full_gate_takes_precedence_over_soft_start(self):
        """When outdoor already clears the full hysteresis gate, the full gate wins —
        the two gates never compete for the same activation."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        # outdoor 74.9 < indoor(76) - hysteresis(1.0) = 75.0 → full gate satisfied
        _arm_paused_reactivation(engine, outdoor=74.9, outdoor_today_peak=90.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is False

    def test_hvac_only_fan_archetype_does_not_qualify(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0, fan_mode=FAN_MODE_HVAC)
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=90.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False

    def test_thin_sample_buffer_fails_safe(self):
        """Issue #540 timezone/restart edge case: a thin post-restart buffer must not
        produce a false 'already past peak' read."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0)
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=90.0, outdoor_sample_count=1)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False

    def test_not_yet_past_peak_stays_paused(self):
        """outdoor within the peak-decline margin of today's peak — not yet 'declining'."""
        engine = _make_engine(comfort_heat=70.0, comfort_cool=74.0, indoor_f=76.0)
        # peak=76.0, margin=1.0 -> needs outdoor < 75.0; 75.5 doesn't qualify as past-peak here
        _arm_paused_reactivation(engine, outdoor=75.5, outdoor_today_peak=76.0)

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False


class TestSoftStartUpgrade:
    """An active soft-start session upgrades to full nat-vent once outdoor falls far
    enough below indoor to independently satisfy the full bulk-cooling gate."""

    def test_upgrades_when_full_gate_clears(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        # outdoor now well below indoor - hysteresis(1.0) -> full gate clears
        engine._last_outdoor_temp = 70.0
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is False

    def test_stays_soft_start_while_full_gate_not_yet_cleared(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        # outdoor still within the hysteresis gap -> full gate not yet satisfied
        engine._last_outdoor_temp = 75.5
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True


class TestSoftStartUpgradeFsmAuthoritative:
    """Issue #594 Phase R, Step 2: with ``_natvent_fsm_authoritative=True``, the
    same two scenarios from ``TestSoftStartUpgrade`` above must produce identical
    outcomes — read authority moved to ``nat_vent_fsm.transition()``, but the
    underlying pure-function inputs and expected result are unchanged."""

    def test_upgrades_when_full_gate_clears(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        engine._natvent_fsm_authoritative = True
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 70.0
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is False

    def test_stays_soft_start_while_full_gate_not_yet_cleared(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        engine._natvent_fsm_authoritative = True
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 75.5
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True


class TestSoftStartExitHierarchy:
    """A soft-start session reuses the existing exit hierarchy unchanged — outdoor-rise
    exit must clear the soft-start qualifier along with _natural_vent_active."""

    def test_outdoor_rise_exit_clears_soft_start_flag(self):
        engine = _make_engine(comfort_heat=70.0, comfort_cool=76.0, indoor_f=76.0)
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        engine._paused_with_hvac_already_off = False
        # outdoor now above indoor -> Priority-3 outdoor-rise exit fires
        engine._last_outdoor_temp = 77.0

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False
