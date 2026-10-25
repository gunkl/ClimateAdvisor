"""A disabled fan mode must never leave a stale _fan_active=True behind (Issue #1104)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_DISABLED, FAN_MODE_HVAC
from tests.test_fan_control import _make_automation_engine


class TestFanDisabledStaleFlag:
    def test_deactivate_fan_clears_stale_flags_when_disabled(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})
        engine._fan_active = True
        engine._fan_on_since = None

        result = asyncio.run(engine._deactivate_fan(reason="test"))

        assert result is FanCommandResult.DISABLED
        assert engine._fan_active is False
        assert engine._fan_on_since is None
        assert engine._fan_running is False

    def test_reconcile_turn_off_with_open_sensor_does_not_strand_flag(self):
        """Blower running + window open + fan disabled takes the reconcile turn-off branch."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})
        engine._sensor_check_callback = lambda: True

        async def _exit_nat_vent(**kw):
            return await engine._deactivate_fan(reason=kw["reason"], emit_event=False)

        engine._exit_nat_vent = AsyncMock(side_effect=_exit_nat_vent)

        asyncio.run(
            engine._reconcile_fan_on_startup_locked(
                indoor=70.0,
                outdoor=60.0,
                thermostat_fan_running=True,
                any_sensor_open=True,
                trigger="thermostat_state_change",
            )
        )

        assert engine._fan_active is False
        assert engine._fan_running is False

    def test_reconcile_does_not_adopt_thermostat_blower_when_disabled(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        asyncio.run(
            engine._reconcile_fan_on_startup_locked(
                indoor=70.0,
                outdoor=60.0,
                thermostat_fan_running=True,
                any_sensor_open=False,
                trigger="thermostat_state_change",
            )
        )

        assert engine._fan_active is False
        assert engine._fan_on_since is None

    def test_reconcile_still_adopts_hvac_circulation(self):
        """FAN_MODE_HVAC still adopts circulation."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        asyncio.run(
            engine._reconcile_fan_on_startup_locked(
                indoor=70.0,
                outdoor=60.0,
                thermostat_fan_running=True,
                any_sensor_open=False,
                trigger="thermostat_state_change",
            )
        )

        assert engine._fan_active is True

    def test_restore_state_ignores_persisted_fan_active_when_disabled(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        engine.restore_state({"fan_active": True, "fan_on_since": None})

        assert engine._fan_active is False
        assert engine._fan_on_since is None

    def test_restore_state_keeps_fan_active_when_fan_configured(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        engine.restore_state({"fan_active": True, "fan_on_since": "2026-03-20T10:00:00"})

        assert engine._fan_active is True
        assert engine._fan_on_since == "2026-03-20T10:00:00"
