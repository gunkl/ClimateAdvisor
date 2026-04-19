"""Tests for whole-house fan control (Issue #18 Phase 4, Issue #37, Issue #55).

Tests cover:
- _activate_fan: whole_house_fan, hvac_fan, both, disabled
- _deactivate_fan: whole_house_fan, hvac_fan, both, disabled
- switch domain detection (switch.attic_fan)
- dry_run mode skips all service calls
- fan activation integrates with economizer maintain phase
- fan deactivation integrates with economizer off
- Fan state tracking (_fan_active, _fan_on_since, runtime) (Issue #37)
- Fan override detection and handling (Issue #37)
- Fan behavior at transitions (bedtime, wakeup) (Issue #37)
- Fan state serialization (save/restore) (Issue #37)
- _compute_fan_status sub-states (Issue #55)
- ClimateAdvisorFanStatusSensor attributes fan_override_since + fan_running (Issue #55)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Patch dt_util.now to return a real datetime (needed for isoformat() calls)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 19, 14, 30, 0)

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    ATTR_FAN_OVERRIDE_SINCE,
    ATTR_FAN_RUNNING,
    ATTR_FAN_RUNTIME,
    CONF_FAN_ENTITY,
    CONF_FAN_MIN_RUNTIME_PER_HOUR,
    CONF_FAN_MODE,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
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
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


def _make_hot_classification() -> DayClassification:
    """Build a HOT DayClassification (bypasses __post_init__)."""
    c = object.__new__(DayClassification)
    c.day_type = DAY_TYPE_HOT
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 90.0
    c.today_low = 70.0
    c.tomorrow_high = 90.0
    c.tomorrow_low = 70.0
    c.hvac_mode = "cool"
    c.pre_condition = True
    c.pre_condition_target = -2.0
    c.windows_recommended = False
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


def _make_heat_classification() -> DayClassification:
    """Build a MILD/heat DayClassification (bypasses __post_init__)."""
    c = object.__new__(DayClassification)
    c.day_type = DAY_TYPE_MILD
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 65.0
    c.today_low = 50.0
    c.tomorrow_high = 65.0
    c.tomorrow_low = 50.0
    c.hvac_mode = "heat"
    c.pre_condition = False
    c.pre_condition_target = 0.0
    c.windows_recommended = False
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


def _get_service_calls(engine, domain: str, service: str) -> list:
    """Extract calls matching a specific domain and service."""
    return [c for c in engine.hass.services.async_call.call_args_list if c[0][0] == domain and c[0][1] == service]


# ---------------------------------------------------------------------------
# _activate_fan tests
# ---------------------------------------------------------------------------


class TestActivateFan:
    """Tests for _activate_fan."""

    def test_activate_whole_house_fan(self):
        """fan_mode=whole_house_fan, fan_entity=fan.attic → calls fan.turn_on."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "fan.attic"
        # Should NOT call HVAC fan mode
        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 0

    def test_activate_hvac_fan(self):
        """fan_mode=hvac_fan → calls climate.set_fan_mode with 'on'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "on"
        assert calls[0][0][2]["entity_id"] == "climate.thermostat"

    def test_activate_both_fans(self):
        """fan_mode=both → calls both fan.turn_on and climate.set_fan_mode 'on'."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_BOTH,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        asyncio.run(engine._activate_fan(reason="test"))

        fan_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["entity_id"] == "fan.attic"

        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 1
        assert hvac_fan_calls[0][0][2]["fan_mode"] == "on"

    def test_fan_disabled_skips_all_activate(self):
        """fan_mode=disabled → no service calls on activate."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        asyncio.run(engine._activate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()

    def test_fan_disabled_by_default_skips_all(self):
        """No fan_mode in config → defaults to disabled, no service calls."""
        engine = _make_automation_engine()  # no fan config at all

        asyncio.run(engine._activate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# _deactivate_fan tests
# ---------------------------------------------------------------------------


class TestDeactivateFan:
    """Tests for _deactivate_fan."""

    def test_deactivate_whole_house_fan(self):
        """fan_mode=whole_house_fan, fan_entity=fan.attic → calls fan.turn_off."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "fan.attic"
        # Should NOT call HVAC fan mode
        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 0

    def test_deactivate_hvac_fan(self):
        """fan_mode=hvac_fan → calls climate.set_fan_mode with 'auto'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "auto"
        assert calls[0][0][2]["entity_id"] == "climate.thermostat"

    def test_deactivate_both_fans(self):
        """fan_mode=both → calls both fan.turn_off and climate.set_fan_mode 'auto'."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_BOTH,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        asyncio.run(engine._deactivate_fan(reason="test"))

        fan_calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["entity_id"] == "fan.attic"

        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 1
        assert hvac_fan_calls[0][0][2]["fan_mode"] == "auto"

    def test_fan_disabled_skips_all_deactivate(self):
        """fan_mode=disabled → no service calls on deactivate."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        asyncio.run(engine._deactivate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Switch domain detection
# ---------------------------------------------------------------------------


class TestSwitchDomainFan:
    """Fan entity in switch domain uses switch.turn_on / switch.turn_off."""

    def test_switch_domain_activate(self):
        """fan_entity=switch.attic_fan → calls switch.turn_on."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "switch.attic_fan",
            }
        )

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "switch", "turn_on")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "switch.attic_fan"

    def test_switch_domain_deactivate(self):
        """fan_entity=switch.attic_fan → calls switch.turn_off."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "switch.attic_fan",
            }
        )

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "switch", "turn_off")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "switch.attic_fan"


# ---------------------------------------------------------------------------
# Dry run mode
# ---------------------------------------------------------------------------


class TestDryRunFan:
    """When dry_run=True, fan methods log but do not call any services."""

    def test_dry_run_skips_activate(self):
        """dry_run=True → _activate_fan logs but makes no service calls."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine.dry_run = True

        asyncio.run(engine._activate_fan(reason="dry run test"))

        engine.hass.services.async_call.assert_not_called()

    def test_dry_run_skips_deactivate(self):
        """dry_run=True → _deactivate_fan logs but makes no service calls."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_BOTH,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine.dry_run = True

        asyncio.run(engine._deactivate_fan(reason="dry run test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Economizer integration
# ---------------------------------------------------------------------------


class TestFanEconomizerIntegration:
    """Fan activates/deactivates together with the economizer."""

    def test_fan_activates_with_economizer_maintain(self):
        """When economizer enters maintain phase, fan activates."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._current_classification = _make_hot_classification()

        # indoor at/below comfort → maintain phase
        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=75.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert engine._economizer_phase == "maintain"
        fan_on_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_on_calls) == 1
        assert fan_on_calls[0][0][2]["entity_id"] == "fan.attic"

    def test_fan_activates_with_economizer_maintain_savings_mode(self):
        """Savings mode also activates fan when entering maintain phase."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
                "aggressive_savings": True,
            }
        )
        engine._current_classification = _make_hot_classification()

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert engine._economizer_phase == "maintain"
        fan_on_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_on_calls) == 1

    def test_fan_deactivates_with_economizer_off(self):
        """When economizer deactivates, fan deactivates too."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        # Trigger deactivation: outdoor too warm
        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=80.0,
                indoor_temp=76.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert engine._economizer_active is False
        fan_off_calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(fan_off_calls) == 1
        assert fan_off_calls[0][0][2]["entity_id"] == "fan.attic"

    def test_no_fan_calls_when_fan_disabled_in_economizer(self):
        """When fan_mode=disabled, economizer transitions make no fan service calls."""
        engine = _make_automation_engine()  # no fan config
        engine._current_classification = _make_hot_classification()

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=75.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert engine._economizer_phase == "maintain"
        # Only HVAC calls (set_hvac_mode) — no fan calls
        fan_calls = [c for c in engine.hass.services.async_call.call_args_list if c[0][0] in ("fan", "switch")]
        assert len(fan_calls) == 0


# ---------------------------------------------------------------------------
# Fan state tracking (Issue #37)
# ---------------------------------------------------------------------------


class TestFanStateTracking:
    """Tests for fan state tracking fields (_fan_active, _fan_on_since)."""

    def test_activate_fan_sets_fan_active(self):
        """_activate_fan sets _fan_active=True and _fan_on_since."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        assert engine._fan_active is False
        assert engine._fan_on_since is None

        asyncio.run(engine._activate_fan(reason="test"))

        assert engine._fan_active is True
        assert engine._fan_on_since is not None

    def test_deactivate_fan_clears_fan_active(self):
        """_deactivate_fan clears _fan_active and _fan_on_since."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._fan_active = True
        engine._fan_on_since = "2026-03-20T10:00:00"

        asyncio.run(engine._deactivate_fan(reason="test"))

        assert engine._fan_active is False
        assert engine._fan_on_since is None

    def test_activate_fan_records_action(self):
        """_activate_fan calls _record_action with fan-specific reason."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
            }
        )

        asyncio.run(engine._activate_fan(reason="economizer maintain"))

        assert engine._last_action_reason is not None
        assert "Fan activated" in engine._last_action_reason

    def test_deactivate_fan_records_action(self):
        """_deactivate_fan calls _record_action."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
            }
        )
        engine._fan_active = True

        asyncio.run(engine._deactivate_fan(reason="economizer off"))

        assert engine._last_action_reason is not None
        assert "Fan deactivated" in engine._last_action_reason

    def test_get_fan_runtime_minutes_when_inactive(self):
        """_get_fan_runtime_minutes returns 0.0 when fan is inactive."""
        engine = _make_automation_engine()

        assert engine._get_fan_runtime_minutes() == 0.0

    def test_get_fan_runtime_minutes_when_active(self):
        """_get_fan_runtime_minutes returns positive value when fan is on."""
        from datetime import timedelta
        from unittest.mock import patch

        import custom_components.climate_advisor.automation as auto_mod

        engine = _make_automation_engine()
        engine._fan_active = True

        mock_now = datetime(2026, 3, 19, 14, 30, 0)
        ten_min_before = mock_now - timedelta(minutes=10)
        engine._fan_on_since = ten_min_before.isoformat()

        # Patch dt_util directly on the automation module
        mock_dt = MagicMock()
        mock_dt.now = MagicMock(return_value=mock_now)
        with patch.object(auto_mod, "dt_util", mock_dt):
            runtime = engine._get_fan_runtime_minutes()
        assert 9.0 <= runtime <= 11.0

    def test_fan_command_pending_set_during_activate(self):
        """_fan_command_pending is False after _activate_fan completes."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )

        asyncio.run(engine._activate_fan(reason="test"))

        # After completion, pending should be cleared
        assert engine._fan_command_pending is False


# ---------------------------------------------------------------------------
# Fan override (Issue #37)
# ---------------------------------------------------------------------------


class TestFanOverride:
    """Tests for fan manual override detection and handling."""

    def test_handle_fan_manual_override_sets_flags(self):
        """handle_fan_manual_override sets _fan_override_active and time."""
        engine = _make_automation_engine()

        engine.handle_fan_manual_override()

        assert engine._fan_override_active is True
        assert engine._fan_override_time is not None

    def test_clear_fan_override_resets_flags(self):
        """clear_fan_override resets _fan_override_active and time."""
        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_override_time = "2026-03-20T10:00:00"

        engine.clear_fan_override()

        assert engine._fan_override_active is False
        assert engine._fan_override_time is None

    def test_activate_fan_skips_when_override_active(self):
        """_activate_fan does nothing when _fan_override_active is True."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._fan_override_active = True

        asyncio.run(engine._activate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_active is False

    def test_deactivate_fan_skips_when_override_active(self):
        """_deactivate_fan does nothing when _fan_override_active is True."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._fan_override_active = True
        engine._fan_active = True

        asyncio.run(engine._deactivate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_active is True  # unchanged

    def test_clear_manual_override_also_clears_fan_override(self):
        """clear_manual_override clears both HVAC and fan overrides."""
        engine = _make_automation_engine()
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        engine._manual_override_time = "2026-03-20T10:00:00"
        engine._fan_override_active = True
        engine._fan_override_time = "2026-03-20T10:00:00"

        engine.clear_manual_override()

        assert engine._manual_override_active is False
        assert engine._fan_override_active is False
        assert engine._fan_override_time is None


# ---------------------------------------------------------------------------
# Fan behavior at transitions (Issue #37)
# ---------------------------------------------------------------------------


class TestFanTransitions:
    """Tests for fan deactivation at bedtime and morning wakeup."""

    def test_bedtime_deactivates_fan(self):
        """handle_bedtime deactivates fan if active."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
                "comfort_cool": 75,
            }
        )
        engine._current_classification = _make_hot_classification()
        engine._fan_active = True
        engine._fan_on_since = "2026-03-20T18:00:00"

        asyncio.run(engine.handle_bedtime())

        assert engine._fan_active is False
        fan_off_calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(fan_off_calls) == 1

    def test_bedtime_deactivates_economizer(self):
        """handle_bedtime deactivates economizer if active."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
                "comfort_cool": 75,
            }
        )
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        asyncio.run(engine.handle_bedtime())

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_morning_wakeup_deactivates_fan(self):
        """handle_morning_wakeup deactivates fan if still running."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
                "comfort_cool": 75,
            }
        )
        engine._current_classification = _make_hot_classification()
        engine._fan_active = True
        engine._fan_on_since = "2026-03-20T06:00:00"

        asyncio.run(engine.handle_morning_wakeup())

        assert engine._fan_active is False

    def test_morning_wakeup_clears_fan_override(self):
        """handle_morning_wakeup clears fan override."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._fan_override_active = True
        engine._fan_override_time = "2026-03-20T22:00:00"

        asyncio.run(engine.handle_morning_wakeup())

        assert engine._fan_override_active is False

    def test_bedtime_clears_fan_override_then_deactivates(self):
        """handle_bedtime clears fan override (transition point) and deactivates fan."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
                CONF_FAN_ENTITY: "fan.attic",
            }
        )
        engine._current_classification = _make_hot_classification()
        engine._fan_active = True
        engine._fan_override_active = True

        asyncio.run(engine.handle_bedtime())

        # Bedtime is a transition point — overrides are cleared, then fan deactivated
        assert engine._fan_override_active is False
        assert engine._fan_active is False


# ---------------------------------------------------------------------------
# Fan state serialization (Issue #37)
# ---------------------------------------------------------------------------


class TestFanSerialization:
    """Tests for fan state persistence via get_serializable_state / restore_state."""

    def test_serializable_state_includes_fan_fields(self):
        """get_serializable_state includes all fan tracking fields."""
        engine = _make_automation_engine()
        engine._fan_active = True
        engine._fan_on_since = "2026-03-20T10:00:00"
        engine._fan_override_active = True
        engine._fan_override_time = "2026-03-20T10:05:00"

        state = engine.get_serializable_state()

        assert state["fan_active"] is True
        assert state["fan_on_since"] == "2026-03-20T10:00:00"
        assert state["fan_override_active"] is True
        assert state["fan_override_time"] == "2026-03-20T10:05:00"

    def test_restore_state_loads_fan_fields(self):
        """restore_state populates fan tracking fields from saved data."""
        engine = _make_automation_engine()

        engine.restore_state(
            {
                "fan_active": True,
                "fan_on_since": "2026-03-20T10:00:00",
                "fan_override_active": True,
                "fan_override_time": "2026-03-20T10:05:00",
            }
        )

        assert engine._fan_active is True
        assert engine._fan_on_since == "2026-03-20T10:00:00"
        assert engine._fan_override_active is True
        assert engine._fan_override_time == "2026-03-20T10:05:00"

    def test_restore_state_defaults_fan_fields(self):
        """restore_state defaults fan fields to inactive when not present."""
        engine = _make_automation_engine()

        engine.restore_state({})

        assert engine._fan_active is False
        assert engine._fan_on_since is None
        assert engine._fan_override_active is False
        assert engine._fan_override_time is None


# ---------------------------------------------------------------------------
# _compute_fan_status tests (Issue #55)
# ---------------------------------------------------------------------------


def _compute_fan_status(fan_override_active: bool, fan_active: bool, fan_mode: str) -> str:
    """Mirror of ClimateAdvisorCoordinator._compute_fan_status for unit testing."""
    if fan_mode == FAN_MODE_DISABLED:
        return "disabled"
    if fan_override_active:
        return "running (manual override)" if fan_active else "off (manual override)"
    if fan_active:
        return "active"
    return "inactive"


class TestFanStatusComputation:
    """Unit tests for _compute_fan_status() logic (Issue #55).

    Tests the five distinct status strings returned based on
    fan_mode config, override flag, and fan active state.

    Note (Issue #91): _compute_fan_status intentionally does NOT guard against
    hvac_mode=off because the fan can legitimately run when HVAC is off
    (natural ventilation mode sets hvac_mode=off then activates the fan).
    The fix for stale hvac_action display is in _async_climate_entity_changed
    (clearing _fan_active when thermostat goes to off externally).
    """

    def test_status_disabled(self):
        """fan_mode=disabled always returns 'disabled' regardless of other state."""
        result = _compute_fan_status(False, False, FAN_MODE_DISABLED)
        assert result == "disabled"

    def test_status_disabled_even_if_override(self):
        """fan_mode=disabled returns 'disabled' even when override flag is set."""
        result = _compute_fan_status(True, True, FAN_MODE_DISABLED)
        assert result == "disabled"

    def test_status_inactive(self):
        """No override, fan not running -> 'inactive'."""
        result = _compute_fan_status(False, False, FAN_MODE_HVAC)
        assert result == "inactive"

    def test_status_active(self):
        """No override, fan running -> 'active'."""
        result = _compute_fan_status(False, True, FAN_MODE_WHOLE_HOUSE)
        assert result == "active"

    def test_status_active_hvac_fan_while_hvac_off(self):
        """_fan_active=True with hvac_mode=off is valid during natural ventilation."""
        # FAN_MODE_HVAC fan can run while thermostat is off (nat vent sets hvac_mode=off
        # then activates fan). _compute_fan_status must return "active" in this case.
        result = _compute_fan_status(False, True, FAN_MODE_HVAC)
        assert result == "active"

    def test_status_override_on(self):
        """Override active and fan is running -> 'running (manual override)'."""
        result = _compute_fan_status(True, True, FAN_MODE_HVAC)
        assert result == "running (manual override)"

    def test_status_override_off(self):
        """Override active but fan is NOT running -> 'off (manual override)'."""
        result = _compute_fan_status(True, False, FAN_MODE_WHOLE_HOUSE)
        assert result == "off (manual override)"


# ---------------------------------------------------------------------------
# ClimateAdvisorFanStatusSensor attribute tests (Issue #55)
# ---------------------------------------------------------------------------


def _fan_sensor_extra_state_attributes(data: dict) -> dict:
    """Mirror of ClimateAdvisorFanStatusSensor.extra_state_attributes for unit testing.

    Replicates the attribute computation without importing sensor.py
    (which triggers a metaclass conflict in the HA stub environment).
    """
    if not data:
        return {}
    return {
        "fan_runtime_minutes": round(data.get(ATTR_FAN_RUNTIME, 0.0), 1),
        "fan_override_since": data.get(ATTR_FAN_OVERRIDE_SINCE),
        "fan_running": data.get(ATTR_FAN_RUNNING, False),
    }


class TestFanSensorAttributes:
    """Unit tests for ClimateAdvisorFanStatusSensor.extra_state_attributes (Issue #55).

    Verifies fan_override_since and fan_running are exposed correctly.
    Uses a replicated helper instead of importing sensor.py directly
    (HA entity metaclass conflicts in test stubs prevent direct instantiation).
    """

    def test_attributes_include_runtime(self):
        """fan_runtime_minutes is always present and rounded to 1 decimal."""
        attrs = _fan_sensor_extra_state_attributes({ATTR_FAN_RUNTIME: 12.456})
        assert attrs["fan_runtime_minutes"] == 12.5

    def test_attributes_fan_override_since_when_active(self):
        """fan_override_since returns the ISO timestamp when override is active."""
        ts = "2026-03-27T10:05:00"
        attrs = _fan_sensor_extra_state_attributes(
            {ATTR_FAN_OVERRIDE_SINCE: ts, ATTR_FAN_RUNNING: False, ATTR_FAN_RUNTIME: 0.0}
        )
        assert attrs["fan_override_since"] == ts

    def test_attributes_fan_override_since_none_when_no_override(self):
        """fan_override_since is None when no override is active."""
        attrs = _fan_sensor_extra_state_attributes(
            {ATTR_FAN_OVERRIDE_SINCE: None, ATTR_FAN_RUNNING: False, ATTR_FAN_RUNTIME: 0.0}
        )
        assert attrs["fan_override_since"] is None

    def test_attributes_fan_running_true_when_active(self):
        """fan_running is True when the fan is on."""
        attrs = _fan_sensor_extra_state_attributes(
            {ATTR_FAN_RUNNING: True, ATTR_FAN_OVERRIDE_SINCE: None, ATTR_FAN_RUNTIME: 5.0}
        )
        assert attrs["fan_running"] is True

    def test_attributes_fan_running_false_when_inactive(self):
        """fan_running is False when the fan is off."""
        attrs = _fan_sensor_extra_state_attributes(
            {ATTR_FAN_RUNNING: False, ATTR_FAN_OVERRIDE_SINCE: None, ATTR_FAN_RUNTIME: 0.0}
        )
        assert attrs["fan_running"] is False

    def test_attributes_fan_running_defaults_false_when_key_absent(self):
        """fan_running defaults to False when key is absent from coordinator data."""
        attrs = _fan_sensor_extra_state_attributes({ATTR_FAN_RUNTIME: 0.0})
        assert attrs["fan_running"] is False


_PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"


class TestMinFanRuntime:
    """Tests for the minimum fan runtime per hour rolling cycle (Issue #77)."""

    def test_cycle_on_activates_fan(self):
        """_fan_cycle_on activates fan and stores a cancel token when feature is enabled."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        cancel_mock = MagicMock()
        with patch(_PATCH_CALL_LATER, return_value=cancel_mock) as mock_later:
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_called()
        assert engine._fan_min_runtime_active is True
        mock_later.assert_called_once()
        assert engine._fan_min_cycle_cancel is cancel_mock

    def test_cycle_on_skips_when_zero(self):
        """_fan_cycle_on does nothing when min_runtime is 0."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 0,
            }
        )
        asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_min_runtime_active is False

    def test_cycle_on_skips_when_fan_mode_disabled(self):
        """_fan_cycle_on does nothing when CONF_FAN_MODE is disabled."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_DISABLED,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_min_runtime_active is False

    def test_cycle_on_skips_when_override_active(self):
        """_fan_cycle_on does nothing if fan override is active."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        engine._fan_override_active = True
        asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_min_runtime_active is False

    def test_cycle_on_retries_when_fan_already_running(self):
        """_fan_cycle_on schedules a 60-min retry without activation when fan is already on."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        engine._fan_active = True
        cancel_mock = MagicMock()
        with patch(_PATCH_CALL_LATER, return_value=cancel_mock) as mock_later:
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_min_runtime_active is False
        # Retry is scheduled for 60 * 60 seconds
        mock_later.assert_called_once()
        assert mock_later.call_args[0][1] == 60 * 60

    def test_cycle_on_no_deactivation_when_60_min(self):
        """_fan_cycle_on with min_runtime=60 activates fan and schedules no deactivation."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 60,
            }
        )
        with patch(_PATCH_CALL_LATER) as mock_later:
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_called()
        assert engine._fan_min_runtime_active is True
        mock_later.assert_not_called()
        assert engine._fan_min_cycle_cancel is None

    def test_cycle_off_deactivates_fan_and_schedules_next_on(self):
        """_fan_cycle_off deactivates fan and schedules next cycle after wait period."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        engine._fan_min_runtime_active = True
        engine._fan_active = True
        engine._fan_on_since = "2026-03-19T14:20:00"
        cancel_mock = MagicMock()
        with patch(_PATCH_CALL_LATER, return_value=cancel_mock) as mock_later:
            asyncio.run(engine._fan_cycle_off())
        assert engine._fan_min_runtime_active is False
        # Deactivation service call was made
        engine.hass.services.async_call.assert_called()
        # Next "on" is scheduled for (60 - 10) * 60 = 3000 seconds
        mock_later.assert_called_once()
        assert mock_later.call_args[0][1] == (60 - 10) * 60
        assert engine._fan_min_cycle_cancel is cancel_mock

    def test_override_stops_cycle(self):
        """handle_fan_manual_override cancels any pending cycle timer."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 10,
            }
        )
        cancel_mock = MagicMock()
        engine._fan_min_cycle_cancel = cancel_mock
        engine._fan_min_runtime_active = True
        engine.handle_fan_manual_override()
        cancel_mock.assert_called_once()
        assert engine._fan_min_cycle_cancel is None
        assert engine._fan_min_runtime_active is False

    def test_start_cycles_cancels_old_and_starts_new(self):
        """start_min_fan_runtime_cycles cancels existing timer before starting a new cycle."""
        engine = _make_automation_engine(
            {
                CONF_FAN_MODE: FAN_MODE_HVAC,
                CONF_FAN_MIN_RUNTIME_PER_HOUR: 5,
            }
        )
        old_cancel = MagicMock()
        engine._fan_min_cycle_cancel = old_cancel
        engine._fan_min_runtime_active = True
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.start_min_fan_runtime_cycles())
        old_cancel.assert_called_once()  # old timer cancelled


# ---------------------------------------------------------------------------
# Issue #91: Fan state cleanup when thermostat goes to off externally
# ---------------------------------------------------------------------------


def _apply_thermostat_off_fan_cleanup(ae, new_thermostat_state: str) -> None:
    """Mirror the fan-cleanup block added in coordinator._async_climate_entity_changed.

    Replicates:
        ae = self.automation_engine
        if new_state.state == "off" and ae._fan_active and not ae._fan_override_active:
            fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                ae._fan_active = False
    """
    if new_thermostat_state == "off" and ae._fan_active and not ae._fan_override_active:
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
            ae._fan_active = False


class TestFanStateCleanupOnThermostatOff:
    """Tests for fan _fan_active cleanup when thermostat is set to off externally.

    Issue #91: If the thermostat is manually set to 'off' while _fan_active=True,
    the coordinator must clear _fan_active to prevent stale 'active' status display.
    Only applies to HVAC-based fan modes (FAN_MODE_HVAC, FAN_MODE_BOTH).
    Whole-house fans are independent and must NOT be affected.
    """

    def test_hvac_fan_active_cleared_when_thermostat_off(self):
        """FAN_MODE_HVAC: _fan_active cleared when thermostat goes to off."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._fan_override_active = False

        _apply_thermostat_off_fan_cleanup(engine, "off")

        assert engine._fan_active is False

    def test_both_fan_active_cleared_when_thermostat_off(self):
        """FAN_MODE_BOTH: _fan_active cleared when thermostat goes to off."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_BOTH})
        engine._fan_active = True
        engine._fan_override_active = False

        _apply_thermostat_off_fan_cleanup(engine, "off")

        assert engine._fan_active is False

    def test_whole_house_fan_not_cleared_when_thermostat_off(self):
        """FAN_MODE_WHOLE_HOUSE: _fan_active NOT cleared — whole-house fan is independent."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
        engine._fan_active = True
        engine._fan_override_active = False

        _apply_thermostat_off_fan_cleanup(engine, "off")

        assert engine._fan_active is True  # unchanged

    def test_fan_override_active_skips_cleanup(self):
        """If fan override is active, _fan_active is NOT cleared."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._fan_override_active = True

        _apply_thermostat_off_fan_cleanup(engine, "off")

        assert engine._fan_active is True  # override protected

    def test_thermostat_heat_does_not_clear_fan_active(self):
        """No cleanup fires when thermostat transitions to 'heat' (not 'off')."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._fan_override_active = False

        _apply_thermostat_off_fan_cleanup(engine, "heat")

        assert engine._fan_active is True  # unchanged

    def test_fan_already_inactive_stays_inactive(self):
        """Cleanup is a no-op when fan is already inactive."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = False
        engine._fan_override_active = False

        _apply_thermostat_off_fan_cleanup(engine, "off")

        assert engine._fan_active is False


# ---------------------------------------------------------------------------
# Natural vent comfort-floor exit tests (TDD — feature not yet implemented)
# ---------------------------------------------------------------------------


def _make_nat_vent_engine(indoor_temp: float) -> AutomationEngine:
    """Create engine pre-configured for nat-vent comfort-floor-exit tests."""
    engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
    engine._natural_vent_active = True
    engine._paused_by_door = False
    engine._fan_active = True
    engine._fan_override_active = False
    engine._last_outdoor_temp = 62.0  # well below threshold (75+3=78) — outdoor alone won't exit
    engine._current_classification = _make_heat_classification()

    mock_cs = MagicMock()
    mock_cs.attributes = {"current_temperature": indoor_temp}
    mock_cs.state = "off"
    engine.hass.states.get.return_value = mock_cs

    return engine


class TestNatVentComfortFloorExit:
    """TDD tests for the comfort-floor exit condition in check_natural_vent_conditions().

    These tests FAIL until the comfort-floor exit feature is implemented in automation.py.
    When indoor temp drops to (or below) comfort_heat, natural vent should be deactivated
    and HVAC restored to the classification mode.
    """

    def test_nat_vent_exits_when_indoor_at_comfort_heat_floor(self):
        """Indoor exactly at comfort_heat floor (70) → nat vent exits, HVAC restored to heat."""
        engine = _make_nat_vent_engine(indoor_temp=70.0)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._paused_by_door is False

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

        hvac_calls = _get_service_calls(engine, "climate", "set_hvac_mode")
        assert len(hvac_calls) == 1
        assert hvac_calls[0][0][2]["hvac_mode"] == "heat"

    def test_nat_vent_exits_when_indoor_below_comfort_heat_floor(self):
        """Indoor strictly below comfort_heat floor (68 < 70) → nat vent exits, HVAC restored."""
        engine = _make_nat_vent_engine(indoor_temp=68.0)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False
        assert engine._paused_by_door is False

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

        hvac_calls = _get_service_calls(engine, "climate", "set_hvac_mode")
        assert len(hvac_calls) == 1
        assert hvac_calls[0][0][2]["hvac_mode"] == "heat"

    def test_nat_vent_continues_when_indoor_above_comfort_heat_floor(self):
        """Indoor above comfort_heat floor (72 > 70) → nat vent continues, no service calls."""
        engine = _make_nat_vent_engine(indoor_temp=72.0)
        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True

        assert len(_get_service_calls(engine, "climate", "set_fan_mode")) == 0
        assert len(_get_service_calls(engine, "climate", "set_hvac_mode")) == 0

    def test_comfort_floor_exit_takes_priority_over_outdoor_warmth(self):
        """Both comfort-floor AND outdoor-warm conditions true — comfort-floor path wins (no paused_by_door)."""
        engine = _make_nat_vent_engine(indoor_temp=70.0)
        engine._last_outdoor_temp = 80.0  # above threshold 78 too
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        # Comfort-floor path does NOT set paused_by_door; outdoor-warmth path does
        assert engine._paused_by_door is False
        assert engine._natural_vent_active is False

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

    def test_comfort_floor_exit_without_classification_only_deactivates_fan(self):
        """No current classification → fan deactivated but no set_hvac_mode call."""
        engine = _make_nat_vent_engine(indoor_temp=70.0)
        engine._current_classification = None
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

        assert len(_get_service_calls(engine, "climate", "set_hvac_mode")) == 0

    def test_comfort_floor_exit_skips_hvac_restore_when_classification_off(self):
        """Classification hvac_mode='off' → fan deactivated but no set_hvac_mode call."""
        cls_off = object.__new__(DayClassification)
        cls_off.day_type = DAY_TYPE_MILD
        cls_off.trend_direction = "stable"
        cls_off.trend_magnitude = 0.0
        cls_off.today_high = 65.0
        cls_off.today_low = 50.0
        cls_off.tomorrow_high = 65.0
        cls_off.tomorrow_low = 50.0
        cls_off.hvac_mode = "off"
        cls_off.pre_condition = False
        cls_off.pre_condition_target = 0.0
        cls_off.windows_recommended = False
        cls_off.window_open_time = None
        cls_off.window_close_time = None
        cls_off.setback_modifier = 0.0
        cls_off.window_opportunity_morning = False
        cls_off.window_opportunity_evening = False

        engine = _make_nat_vent_engine(indoor_temp=70.0)
        engine._current_classification = cls_off
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

        assert len(_get_service_calls(engine, "climate", "set_hvac_mode")) == 0

    def test_comfort_floor_exit_emits_event(self):
        """Comfort-floor exit fires the nat_vent_comfort_floor_exit event with indoor_temp payload.

        Note: _start_grace_period also fires a grace_started event, so call_count may be > 1.
        We assert the specific nat_vent_comfort_floor_exit event was emitted with correct payload.
        """
        engine = _make_nat_vent_engine(indoor_temp=70.0)
        engine._emit_event_callback = MagicMock()
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        # Extract all event names fired
        event_names = [call[0][0] for call in engine._emit_event_callback.call_args_list]
        assert "nat_vent_comfort_floor_exit" in event_names

        # Verify the payload of the nat_vent_comfort_floor_exit event
        comfort_floor_call = next(
            call for call in engine._emit_event_callback.call_args_list if call[0][0] == "nat_vent_comfort_floor_exit"
        )
        assert "indoor_temp" in comfort_floor_call[0][1]

    def test_comfort_floor_check_skipped_when_not_in_nat_vent(self):
        """_natural_vent_active=False → no service calls even when indoor is below floor."""
        engine = _make_nat_vent_engine(indoor_temp=65.0)
        engine._natural_vent_active = False
        engine._paused_by_door = False
        asyncio.run(engine.check_natural_vent_conditions())

        assert len(engine.hass.services.async_call.call_args_list) == 0
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# _set_hvac_mode("off") → assert fan_mode=auto (Issue #113)
# ---------------------------------------------------------------------------


class TestSetHvacModeOffFanAssert:
    """When CA sets hvac_mode=off, it should also assert fan_mode=auto for HVAC-
    controlled fans so post-heat blowdown fans don't linger in untracked state."""

    def _make_engine_with_fan_mode(self, fan_mode: str) -> AutomationEngine:
        engine = _make_automation_engine(
            config_overrides={
                CONF_FAN_MODE: fan_mode,
                "climate_entity": "climate.thermostat",
            }
        )
        engine.climate_entity = "climate.thermostat"
        return engine

    def test_set_hvac_off_asserts_fan_auto_for_hvac_fan_mode(self):
        """hvac_mode=off + fan_mode=hvac_fan → set_fan_mode('auto') is called."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_HVAC)
        asyncio.run(engine._set_hvac_mode("off", reason="warm day"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1, f"Expected 1 set_fan_mode call; got {fan_calls}"
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

    def test_set_hvac_off_asserts_fan_auto_for_both_fan_mode(self):
        """hvac_mode=off + fan_mode=both → set_fan_mode('auto') is called."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_BOTH)
        asyncio.run(engine._set_hvac_mode("off", reason="warm day"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"

    def test_set_hvac_off_no_fan_call_when_fan_disabled(self):
        """hvac_mode=off + fan_mode=disabled → no set_fan_mode call."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_DISABLED)
        asyncio.run(engine._set_hvac_mode("off", reason="warm day"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 0, "No fan call when fan feature is disabled"

    def test_set_hvac_off_no_fan_call_for_whole_house_fan(self):
        """hvac_mode=off + fan_mode=whole_house → no set_fan_mode on climate entity."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_WHOLE_HOUSE)
        asyncio.run(engine._set_hvac_mode("off", reason="warm day"))

        # Whole-house fans use a separate entity (fan.attic) — no climate set_fan_mode
        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 0

    def test_set_hvac_heat_does_not_assert_fan_auto(self):
        """hvac_mode=heat (not off) → no set_fan_mode call."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_HVAC)
        asyncio.run(engine._set_hvac_mode("heat", reason="morning restore"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 0, "set_fan_mode should only fire when mode=off"

    def test_nat_vent_flow_still_activates_fan_after_hvac_off(self):
        """Natural vent: _set_hvac_mode('off') then _activate_fan() → fan ends up on."""
        engine = self._make_engine_with_fan_mode(FAN_MODE_HVAC)

        async def _run():
            await engine._set_hvac_mode("off", reason="nat vent start")
            await engine._activate_fan(reason="nat vent circulation")

        asyncio.run(_run())

        # The last set_fan_mode call must be "on" (activate overrides the auto)
        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 2  # first "auto" from _set_hvac_mode, then "on" from _activate_fan
        assert fan_calls[0][0][2]["fan_mode"] == "auto"
        assert fan_calls[1][0][2]["fan_mode"] == "on"
        assert engine._fan_active is True
