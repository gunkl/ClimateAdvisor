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
        """fan_mode=both → calls fan.turn_on, suppresses HVAC, then sets fan_mode 'on'.

        Fix C (Issue #277): FAN_MODE_BOTH includes a whole-house fan, so HVAC is
        suppressed to 'off' first (which asserts fan_mode='auto'), then the HVAC
        fan-only mode is activated (fan_mode='on').  Two set_fan_mode calls total:
        1. 'auto'  — asserted by _set_hvac_mode('off') fan-assertion guard
        2. 'on'    — activated by the FAN_MODE_HVAC branch in _activate_fan
        """
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
        # Two calls: 'auto' (from HVAC-off fan assertion) then 'on' (from fan-only activation)
        assert len(hvac_fan_calls) == 2, f"Expected 2 set_fan_mode calls; got {hvac_fan_calls}"
        assert hvac_fan_calls[0][0][2]["fan_mode"] == "auto"
        assert hvac_fan_calls[1][0][2]["fan_mode"] == "on"

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
        # Fix 1c idempotency guard: _deactivate_fan() is now a no-op unless
        # _fan_active is already True (mirrors _activate_fan()'s equivalent guard).
        engine._fan_active = True

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
        # Fix 1c idempotency guard requires _fan_active=True before deactivate is real.
        engine._fan_active = True

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
        # Fix 1c idempotency guard requires _fan_active=True before deactivate is real.
        engine._fan_active = True

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
        # Fix 1c idempotency guard requires _fan_active=True before deactivate is real.
        engine._fan_active = True

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
        # Fix 1c idempotency guard: fan must already be active for deactivate to be real
        # (mirrors production — the economizer only ever stops a fan it previously started).
        engine._fan_active = True

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
        # Issue #327: fan override is CLEARED on restart (clean slate, matching HVAC override).
        # Restoring it perpetuated a stale override with no grace timer → permanent fan lockout.
        # Restart now reclaims fan control; startup coalesce reconciles the real fan disposition.
        assert engine._fan_override_active is False
        assert engine._fan_override_time is None

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
        # Issue #327: _activate_fan now also schedules the fan thermostatic backstop timer (300s)
        # alongside the 30s post-fan verify; _fan_cycle_on adds the min_runtime deactivation (600s).
        delays = [c.args[1] for c in mock_later.call_args_list]
        assert 30.0 in delays  # post-fan setpoint verify
        assert 10 * 60 in delays  # min-runtime deactivation
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
        # Always-on (min_runtime>=60): _fan_cycle_on schedules NO deactivation timer, so
        # _fan_min_cycle_cancel stays None. _activate_fan still schedules the 30s verify and the
        # Issue #327 backstop timer (300s) — assert the verify is present, not a brittle total count.
        delays = [c.args[1] for c in mock_later.call_args_list]
        assert 30.0 in delays
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
        # Next "on" is scheduled for (60 - 10) * 60 = 3000 seconds; also 30s verify
        assert mock_later.call_count == 2  # 30s verify + next-cycle scheduling
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
        assert "fan_device" in comfort_floor_call[0][1], "Issue #402: exit events must identify the fan mechanism"

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


# ---------------------------------------------------------------------------
# Fix 1 (Issue #134): _set_hvac_mode("off") must not clobber active nat-vent
# ---------------------------------------------------------------------------


class TestSetHvacModeOffNatVentGuard:
    """When nat-vent is already active, _set_hvac_mode("off") must NOT assert
    fan_mode=auto — doing so silently stops the fan while CA believes it's on."""

    def _make_engine_nat_vent_active(self, fan_mode: str) -> AutomationEngine:
        engine = _make_automation_engine(
            config_overrides={
                CONF_FAN_MODE: fan_mode,
                "climate_entity": "climate.thermostat",
            }
        )
        engine.climate_entity = "climate.thermostat"
        engine._natural_vent_active = True
        engine._fan_active = True
        return engine

    def test_nat_vent_active_hvac_off_does_not_assert_fan_auto(self):
        """nat-vent active + _set_hvac_mode('off') → NO set_fan_mode call (Issue #134)."""
        engine = self._make_engine_nat_vent_active(FAN_MODE_HVAC)
        asyncio.run(engine._set_hvac_mode("off", reason="daily classification"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 0, (
            f"_set_hvac_mode('off') must not assert fan_mode=auto when nat-vent is active; got {fan_calls}"
        )
        assert engine._natural_vent_active is True

    def test_nat_vent_active_fan_mode_both_no_clobber(self):
        """nat-vent active + fan_mode=both + hvac_mode=off → no set_fan_mode call."""
        engine = self._make_engine_nat_vent_active(FAN_MODE_BOTH)
        asyncio.run(engine._set_hvac_mode("off", reason="daily classification"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 0
        assert engine._natural_vent_active is True

    def test_nat_vent_inactive_hvac_off_still_asserts_fan_auto(self):
        """nat-vent NOT active + _set_hvac_mode('off') → set_fan_mode('auto') fires as before."""
        engine = _make_automation_engine(
            config_overrides={
                CONF_FAN_MODE: FAN_MODE_HVAC,
                "climate_entity": "climate.thermostat",
            }
        )
        engine.climate_entity = "climate.thermostat"
        engine._natural_vent_active = False

        asyncio.run(engine._set_hvac_mode("off", reason="daily classification"))

        fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["fan_mode"] == "auto"


# ---------------------------------------------------------------------------
# Fix 2 (Issue #134): comfort-ceiling override in check_natural_vent_conditions
# ---------------------------------------------------------------------------


def _make_grace_nat_vent_engine(indoor_temp: float, grace_active: bool = True) -> AutomationEngine:
    """Engine with both nat-vent flags False (fan exited at comfort floor) and grace active."""
    engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
    engine._natural_vent_active = False
    engine._paused_by_door = False
    engine._grace_active = grace_active
    engine._fan_active = False
    engine._fan_override_active = False
    engine._last_outdoor_temp = 65.0  # cool enough for nat-vent (< comfort_cool + delta)

    mock_cs = MagicMock()
    mock_cs.attributes = {"current_temperature": indoor_temp}
    mock_cs.state = "off"
    engine.hass.states.get.return_value = mock_cs

    return engine


class TestCheckNatVentGraceComfortCeiling:
    """Fix 2: grace period must not block nat-vent re-evaluation when indoor > comfort_cool.

    Before the fix, check_natural_vent_conditions() returned immediately when both
    _paused_by_door and _natural_vent_active were False — even if indoor was overheating.
    """

    def test_grace_active_indoor_above_comfort_cool_blocks_nat_vent_for_hvac_fan(self):
        """grace=True, indoor=76 > comfort_cool=75, FAN_MODE_HVAC → nat-vent does NOT activate.

        Issue #392 Fix 1: for FAN_MODE_HVAC, the ceiling threshold (comfort_cool, since
        aggressive_savings is off here) is still a valid handoff point to AC — fan and
        compressor coexist safely for this archetype (band stays armed, Issue #249).
        Once indoor exceeds the ceiling, reactivation must be BLOCKED so the compressor
        (not the fan) handles the excess heat, matching the ODE ceiling guard's own
        dormancy condition (`indoor <= ceiling_threshold`). This is the mirror image of
        the FAN_MODE_WHOLE_HOUSE case (test_grace_active_whole_house_fan_ignores_ceiling
        below), where the ceiling does not apply because WHF is direction-only.

        Before Issue #392's archetype-aware ceiling fix, this test asserted the opposite
        (nat-vent activates) — that was correct only for the (untested-for-archetype)
        general case, but wrong once FAN_MODE_HVAC's ceiling-escalation contract is
        applied to the four reactivation gates, not just the ODE guard.
        """
        engine = _make_grace_nat_vent_engine(indoor_temp=76.0, grace_active=True)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "FAN_MODE_HVAC: indoor (76) > ceiling_threshold (comfort_cool=75) must block "
            "nat-vent reactivation — compressor should take over, not the fan"
        )

    def test_grace_active_whole_house_fan_ignores_ceiling(self):
        """grace=True, indoor=76 > comfort_cool=75, FAN_MODE_WHOLE_HOUSE, outdoor cool → activates.

        Issue #392 Fix 1: WHF's ceiling_threshold is always None (mutually exclusive with
        AC, direction-only convergence) — the comfort ceiling never blocks WHF reactivation,
        only the outdoor/indoor direction primary gate does. Outdoor (65) < indoor (76), so
        nat-vent must activate despite indoor being well past the comfort ceiling.
        """
        engine = _make_grace_nat_vent_engine(indoor_temp=76.0, grace_active=True)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE

        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, (
            "FAN_MODE_WHOLE_HOUSE: ceiling_threshold is None, so indoor > comfort_cool must "
            "NOT block reactivation as long as outdoor < indoor"
        )

    def test_grace_active_outdoor_too_warm_no_nat_vent(self):
        """grace=True, indoor=76 > comfort_cool, but outdoor=74 is above threshold (75+3=78) — activates."""
        engine = _make_grace_nat_vent_engine(indoor_temp=76.0, grace_active=True)
        engine._last_outdoor_temp = 79.0  # above threshold — nat-vent should not fire
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False

    def test_grace_active_indoor_at_comfort_cool_no_nat_vent(self):
        """grace=True, indoor=75 == comfort_cool=75 → comfort ceiling not breached → no nat-vent."""
        engine = _make_grace_nat_vent_engine(indoor_temp=75.0, grace_active=True)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "Comfort ceiling is not breached at exactly comfort_cool — grace should hold"
        )

    def test_grace_active_indoor_below_comfort_cool_no_nat_vent(self):
        """grace=True, indoor=73 < comfort_cool=75 → comfort ceiling not breached → no nat-vent."""
        engine = _make_grace_nat_vent_engine(indoor_temp=73.0, grace_active=True)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False

    def test_no_grace_indoor_above_comfort_cool_no_fallthrough(self):
        """grace=False, indoor=76 — no grace, so normal early-return applies → no nat-vent."""
        engine = _make_grace_nat_vent_engine(indoor_temp=76.0, grace_active=False)
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False


def _make_idle_reactivation_engine(
    hvac_state: str, hvac_action: str, indoor_temp: float = 70.0, outdoor_temp: float = 60.0
) -> AutomationEngine:
    """Engine with neither nat-vent flag set, a sensor open, and a given thermostat state.

    comfort_heat=68, comfort_cool=74 (default overrides) so indoor=70 > comfort_heat and
    outdoor=60 < indoor - hysteresis, satisfying the reactivation condition itself — the
    only variable under test is whether the HVAC-state gate permits re-evaluation to run.
    """
    engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, "comfort_heat": 68.0, "comfort_cool": 74.0})
    engine._natural_vent_active = False
    engine._paused_by_door = False
    engine._grace_active = False
    engine._fan_active = False
    engine._fan_override_active = False
    engine._last_outdoor_temp = outdoor_temp
    engine._sensor_check_callback = lambda: True
    engine._activate_fan = AsyncMock()
    engine._apply_nat_vent_hvac_state = AsyncMock()
    engine._emit_event_callback = MagicMock()

    mock_cs = MagicMock()
    mock_cs.attributes = {"current_temperature": indoor_temp, "hvac_action": hvac_action}
    mock_cs.state = hvac_state
    engine.hass.states.get.return_value = mock_cs

    return engine


class TestIdleReactivationGate:
    """Issue #244 idle re-eval gate, widened in Issue #402 to check hvac_action instead of

    the literal armed mode. _apply_comfort_band() legitimately arms 'cool' mode as a ceiling
    backstop once nat-vent releases HVAC ownership — but that permanently blocked this
    reactivation path even when the compressor was never actually running (hvac_action
    stayed 'idle' because indoor never reached the armed ceiling). Occupant impact: WHF
    silently stopped controlling the home for hours overnight once this armed-but-idle state
    was reached, despite outdoor conditions remaining ideal for free cooling.
    """

    def test_reactivates_when_mode_off(self):
        """REGRESSION GUARD: the original case — thermostat mode literally 'off' — still works."""
        engine = _make_idle_reactivation_engine(hvac_state="off", hvac_action="off")
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        engine._activate_fan.assert_awaited()
        assert engine._natural_vent_active is True

    def test_reactivates_when_mode_cool_but_action_idle(self):
        """THE FIX: mode='cool' (ceiling backstop armed) but hvac_action='idle' (compressor

        not actually running) must still permit reactivation — this is exactly the state
        _apply_comfort_band() leaves the thermostat in after a nat-vent floor-exit.
        """
        engine = _make_idle_reactivation_engine(hvac_state="cool", hvac_action="idle")
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        engine._activate_fan.assert_awaited()
        assert engine._natural_vent_active is True

    def test_does_not_reactivate_when_mode_cool_and_action_cooling(self):
        """REGRESSION GUARD: if the compressor is ACTIVELY cooling (hvac_action='cooling'),

        reactivation must still be blocked — WHF must never fight a running compressor.
        """
        engine = _make_idle_reactivation_engine(hvac_state="cool", hvac_action="cooling")
        with patch(_PATCH_CALL_LATER):
            asyncio.run(engine.check_natural_vent_conditions())

        engine._activate_fan.assert_not_awaited()
        assert engine._natural_vent_active is False

        # Without grace the early-return fires normally (neither flag is True)
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# Issue #327: fan_thermostat_check + reconcile_fan_on_startup
# ---------------------------------------------------------------------------


class TestFanThermostatCheck:
    """Thermostatic fast-loop that stops a running CA fan (Issue #327)."""

    def _engine(self, **cfg):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, **cfg})
        engine._deactivate_fan = AsyncMock()
        engine._emit_event_callback = MagicMock()
        engine._current_classification = None
        return engine

    def test_keeps_fan_when_outdoor_below_indoor(self):
        """REGRESSION GUARD: outdoor 71 / indoor 72 (1°F favorable gradient) must KEEP the fan.

        The original Check-1 used ``outdoor >= indoor - hysteresis`` (71 >= 71) and stopped here,
        killing nat-vent the instant it activated. The stop must fire only at outdoor >= indoor.
        """
        engine = self._engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=71.0, trigger="test"))

        engine._deactivate_fan.assert_not_awaited()
        assert engine._natural_vent_active is True

    def test_stops_natvent_and_emits_outdoor_rise_exit(self):
        """outdoor >= indoor while nat-vent active → deactivate + nat_vent_outdoor_rise_exit."""
        engine = self._engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=72.5, trigger="test"))

        engine._deactivate_fan.assert_awaited()
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        events = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "nat_vent_outdoor_rise_exit" in events
        payload = next(
            c.args[1] for c in engine._emit_event_callback.call_args_list if c.args[0] == "nat_vent_outdoor_rise_exit"
        )
        assert "fan_device" in payload, "Issue #402: exit events must identify the fan mechanism"

    def test_stops_non_natvent_fan_without_natvent_event(self):
        """A non-nat-vent running fan stops on outdoor>=indoor with a generic reason (no nat-vent event)."""
        engine = self._engine()
        engine._natural_vent_active = False
        engine._fan_active = True

        asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=73.0, trigger="test"))

        engine._deactivate_fan.assert_awaited()
        events = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "nat_vent_outdoor_rise_exit" not in events

    def test_noop_when_override_active(self):
        """Manual fan override in effect → fast loop is a no-op (user has control)."""
        engine = self._engine()
        engine._fan_active = True
        engine._natural_vent_active = True
        engine._fan_override_active = True

        asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=80.0, trigger="test"))

        engine._deactivate_fan.assert_not_awaited()

    def test_noop_when_no_fan_active(self):
        """No CA fan active → no-op even if outdoor is warm."""
        engine = self._engine()
        engine._fan_active = False
        engine._natural_vent_active = False

        asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=80.0, trigger="test"))

        engine._deactivate_fan.assert_not_awaited()

    # -----------------------------------------------------------------
    # Issue #402: Check 2's hard floor must be sleep-aware, mirroring the
    # #374 fix already applied to check_natural_vent_conditions(). Before this
    # fix, this tick-level check always used the flat comfort_heat floor and —
    # because it fires far more often than the 30-min classification cycle —
    # always preempted nat_vent_temperature_check()'s correct sleep-window
    # cycling, permanently ending nat-vent sessions at comfort_heat overnight
    # instead of letting them cycle around the (lower) sleep floor.
    # -----------------------------------------------------------------

    _DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"
    _SLEEP_NOW = datetime(2026, 7, 21, 2, 0, 0)  # 02:00 — inside 22:30-07:00 sleep window

    def _sleep_engine(self, **cfg):
        engine = self._engine(
            comfort_heat=68.0,
            comfort_cool=74.0,
            sleep_heat=64.0,
            sleep_time="22:30",
            wake_time="07:00",
            nat_vent_hysteresis_f=1.0,
            **cfg,
        )
        return engine

    def test_sleep_window_does_not_stop_at_comfort_heat(self):
        """Occupant: overnight, WHF must keep running past comfort_heat (68°F) — it should

        only stop at the sleep floor (sleep_heat - hysteresis = 63°F), not the flat daytime
        comfort_heat floor. This is the exact regression from Issue #402 — before the fix,
        indoor=68 during the sleep window incorrectly ended the whole nat-vent session here.
        """
        engine = self._sleep_engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        with patch(self._DT_NOW_PATH, return_value=self._SLEEP_NOW):
            asyncio.run(engine.fan_thermostat_check(indoor=68.0, outdoor=60.0, trigger="test"))

        engine._deactivate_fan.assert_not_awaited()
        assert engine._natural_vent_active is True

    def test_sleep_window_stops_at_sleep_floor(self):
        """Occupant: WHF should still stop if indoor genuinely reaches the sleep floor

        (sleep_heat - hysteresis = 64-1 = 63°F) — the safety net still works, just at the
        correct sleep-aware threshold instead of the daytime one.
        """
        engine = self._sleep_engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        with patch(self._DT_NOW_PATH, return_value=self._SLEEP_NOW):
            asyncio.run(engine.fan_thermostat_check(indoor=63.0, outdoor=60.0, trigger="test"))

        engine._deactivate_fan.assert_awaited()
        assert engine._natural_vent_active is False

    def test_daytime_still_stops_at_comfort_heat(self):
        """REGRESSION GUARD: daytime behavior is unchanged — comfort_heat (68°F) still ends

        the session outside the sleep window.
        """
        engine = self._sleep_engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        with patch("custom_components.climate_advisor.automation._in_sleep_window", return_value=False):
            asyncio.run(engine.fan_thermostat_check(indoor=68.0, outdoor=60.0, trigger="test"))

        engine._deactivate_fan.assert_awaited()
        assert engine._natural_vent_active is False


class TestThermoBackstopTask:
    """Issue #402 follow-up: the existing 5-minute backstop timer (Issue #327) must also

    re-evaluate nat-vent cycling (nat_vent_temperature_check), not just the coarser hard
    floor (fan_thermostat_check). nat_vent_temperature_check has no timer of its own —
    it's only invoked when the thermostat's current_temperature attribute produces a
    distinct change event — so without this, indoor could sit below the cycling
    off-threshold for minutes with nothing re-checking. Confirmed against live production
    data: the fan ran ~4+ minutes past its cycling off-threshold before a fresh temperature
    tick happened to arrive.
    """

    def _engine(self, **cfg):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, **cfg})
        engine.fan_thermostat_check = AsyncMock()
        engine.nat_vent_temperature_check = AsyncMock()
        engine._start_fan_thermo_backstop = MagicMock()
        engine._get_indoor_temp_f = MagicMock(return_value=69.0)
        engine._last_outdoor_temp = 65.0
        return engine

    def test_backstop_invokes_nat_vent_cycling_check_when_active(self):
        """When nat-vent is active, the backstop tick must also call

        nat_vent_temperature_check with the current indoor reading.
        """
        engine = self._engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        asyncio.run(engine._thermo_backstop_task())

        engine.nat_vent_temperature_check.assert_awaited_once_with(69.0)

    def test_backstop_skips_nat_vent_check_when_not_active(self):
        """REGRESSION GUARD: when nat-vent is not active (e.g. a plain HVAC-fan-only

        session), the backstop must not call nat_vent_temperature_check at all.
        """
        engine = self._engine()
        engine._natural_vent_active = False
        engine._fan_active = True

        asyncio.run(engine._thermo_backstop_task())

        engine.nat_vent_temperature_check.assert_not_awaited()

    def test_backstop_still_calls_fan_thermostat_check(self):
        """REGRESSION GUARD: the existing fan_thermostat_check() backstop call must be

        unaffected by this addition.
        """
        engine = self._engine()
        engine._natural_vent_active = True
        engine._fan_active = True

        asyncio.run(engine._thermo_backstop_task())

        engine.fan_thermostat_check.assert_awaited_once_with(indoor=69.0, outdoor=65.0, trigger="timer")

    def test_backstop_skips_nat_vent_check_when_indoor_unavailable(self):
        """nat_vent_temperature_check requires a float — must not be called with None."""
        engine = self._engine()
        engine._get_indoor_temp_f = MagicMock(return_value=None)
        engine._natural_vent_active = True
        engine._fan_active = True

        asyncio.run(engine._thermo_backstop_task())

        engine.nat_vent_temperature_check.assert_not_awaited()


class TestReconcileFanOnStartup:
    """Startup fan reconciliation — no running fan is ever left in limbo (Issue #327)."""

    def _engine(self, fan_mode=FAN_MODE_WHOLE_HOUSE):
        engine = _make_automation_engine({CONF_FAN_MODE: fan_mode})
        engine._deactivate_fan = AsyncMock()
        return engine

    def test_no_fan_when_thermostat_fan_not_running(self):
        """thermostat fan off → decision no-fan; stale flags cleared, no deactivate call."""
        engine = self._engine()
        engine._fan_active = True  # stale

        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=75.0, outdoor=60.0, thermostat_fan_running=False, any_sensor_open=True
            )
        )

        engine._deactivate_fan.assert_not_awaited()
        assert engine._fan_active is False
        assert engine._natural_vent_active is False

    def test_adopt_on_when_nat_vent_eligible(self):
        """Fan running + window open + outdoor cooler → adopt as CA nat-vent."""
        engine = self._engine()
        engine._emit_event_callback = MagicMock()

        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=75.0, outdoor=65.0, thermostat_fan_running=True, any_sensor_open=True
            )
        )

        assert engine._fan_active is True
        assert engine._natural_vent_active is True
        engine._deactivate_fan.assert_not_awaited()

    def test_adopt_on_records_activity_log_entry(self):
        """Issue #402 follow-up: the adopt-on path must not silently adopt the fan with

        zero activity log trace — before this fix it set flags and started the backstop
        but never called _record_action() or emitted a fan_activated event, unlike the
        turn-off branch which does emit one.
        """
        engine = self._engine()
        engine._emit_event_callback = MagicMock()
        engine._record_action = MagicMock()

        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=75.0, outdoor=65.0, thermostat_fan_running=True, any_sensor_open=True
            )
        )

        engine._record_action.assert_called_once()
        assert engine._record_action.call_args.args[0] == "Fan activated"
        reason = engine._record_action.call_args.args[1]
        assert "75.0" in reason and "65.0" in reason, f"reason must state real temps; got: {reason!r}"

        engine._emit_event_callback.assert_called_once()
        event_type, payload = engine._emit_event_callback.call_args.args
        assert event_type == "fan_activated"
        assert payload.get("reason"), "fan_activated event must carry a non-empty reason"
        assert "fan_device" in payload

    def test_turn_off_when_not_eligible_hvac(self):
        """Fan running, sensors closed (not nat-vent eligible), HVAC-fan archetype → turn off."""
        engine = self._engine(fan_mode=FAN_MODE_HVAC)

        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=75.0, outdoor=65.0, thermostat_fan_running=True, any_sensor_open=False
            )
        )

        engine._deactivate_fan.assert_awaited()
        assert engine._natural_vent_active is False

    def test_turn_off_when_outdoor_warmer_whole_house(self):
        """Fan running, window open but outdoor warmer than indoor → not eligible → turn off."""
        engine = self._engine(fan_mode=FAN_MODE_WHOLE_HOUSE)

        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=75.0, outdoor=80.0, thermostat_fan_running=True, any_sensor_open=True
            )
        )

        engine._deactivate_fan.assert_awaited()


class TestFanEventEmission:
    """Issue #331 follow-up: _activate_fan/_deactivate_fan emit fan_activated/fan_deactivated."""

    def test_activate_emits_fan_activated_with_reason(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._emit_event_callback = MagicMock()
        asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
        calls = {c.args[0]: c.args[1] for c in engine._emit_event_callback.call_args_list}
        assert "fan_activated" in calls
        assert calls["fan_activated"]["reason"] == "min_runtime_cycle"
        # Issue #392 Fix 2: payload carries fan_device so the renderer can label the
        # archetype (hvac_fan/whf/both) instead of a generic "fan" string.
        assert calls["fan_activated"]["fan_device"] == "hvac_fan"

    def test_activate_emits_fan_activated_with_whf_device_label(self):
        """FAN_MODE_WHOLE_HOUSE -> fan_activated payload carries fan_device='whf'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.attic"})
        engine._emit_event_callback = MagicMock()
        asyncio.run(engine._activate_fan(reason="natural ventilation"))
        calls = {c.args[0]: c.args[1] for c in engine._emit_event_callback.call_args_list}
        assert calls["fan_activated"]["fan_device"] == "whf"

    def test_deactivate_emits_fan_deactivated(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._emit_event_callback = MagicMock()
        asyncio.run(engine._deactivate_fan(reason="economizer off -- fan no longer needed"))
        calls = {c.args[0]: c.args[1] for c in engine._emit_event_callback.call_args_list}
        assert "fan_deactivated" in calls
        # Issue #392 Fix 2: payload carries fan_device so the renderer can label the archetype.
        assert calls["fan_deactivated"]["fan_device"] == "hvac_fan"

    def test_emit_event_false_suppresses_event(self):
        """nat-vent cycler passes emit_event=False so it does not double with nat_vent_fan_on."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._emit_event_callback = MagicMock()
        asyncio.run(engine._activate_fan(reason="nat_vent_cycling_on", emit_event=False))
        types = [c.args[0] for c in engine._emit_event_callback.call_args_list]
        assert "fan_activated" not in types


# ---------------------------------------------------------------------------
# Issue #359: on_fan_turned_off() — fan-OFF dispatch (new method)
# ---------------------------------------------------------------------------


class TestFanTurnedOff:
    """Tests for on_fan_turned_off() — the fan-OFF path added by Issue #359.

    on_fan_turned_off() is called when the thermostat reports fan_mode on→auto.
    It must:
      - Clear _fan_active (fan is physically off)
      - Clear _natural_vent_active (nat-vent session ends with the fan)
      - NOT set _fan_override_active (that flag means "user turned fan ON")
      - Clear stale _fan_override_active if it was somehow True (log + clear)
      - Emit a fan_cancel event
      - Start a grace period (trigger="fan_off") to gate nat-vent re-activation

    Occupant impact: without on_fan_turned_off(), CA previously called
    handle_fan_manual_override() on a fan-off event — setting _fan_override_active=True,
    which blocked CA from re-activating nat-vent the next morning. The occupant woke
    to a warming home (71°F) with no automatic nat-vent despite ideal outdoor conditions.
    """

    def test_on_fan_turned_off_clears_fan_active(self):
        """on_fan_turned_off clears _fan_active — fan is physically off."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._fan_on_since = "2026-06-28T07:00:00"
        engine._natural_vent_active = False

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        assert engine._fan_active is False

    def test_on_fan_turned_off_clears_natural_vent_active(self):
        """on_fan_turned_off clears _natural_vent_active — nat-vent session ends."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._natural_vent_active = True

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        assert engine._natural_vent_active is False

    def test_on_fan_turned_off_does_not_set_override(self):
        """on_fan_turned_off does NOT set _fan_override_active — that is the fan-ON path."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._fan_override_active = False

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        assert engine._fan_override_active is False

    def test_on_fan_turned_off_clears_stale_override(self):
        """on_fan_turned_off clears _fan_override_active when it is stale-True."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = False
        engine._natural_vent_active = False
        engine._fan_override_active = True  # stale state
        engine._fan_override_time = "2026-06-28T06:00:00"

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        assert engine._fan_override_active is False
        assert engine._fan_override_time is None

    def test_on_fan_turned_off_emits_fan_cancel_event(self):
        """on_fan_turned_off emits a fan_cancel event with fan_before/fan_after."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._natural_vent_active = False

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        cancel_events = [e for e in events if e[0] == "fan_cancel"]
        assert len(cancel_events) == 1, f"Expected fan_cancel event; got events: {events}"
        assert cancel_events[0][1]["fan_before"] == "on"
        assert cancel_events[0][1]["fan_after"] == "auto"

    def test_on_fan_turned_off_starts_grace_with_fan_off_trigger(self):
        """on_fan_turned_off starts a grace period with trigger='fan_off'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._fan_active = True
        engine._natural_vent_active = False

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch(_PATCH_CALL_LATER):
            engine.on_fan_turned_off(fan_before="on", fan_after="auto")

        assert engine._grace_active is True
        grace_events = [e for e in events if e[0] == "grace_started"]
        assert len(grace_events) >= 1, f"Expected grace_started event; got events: {events}"
        assert grace_events[0][1]["trigger"] == "fan_off"

    def test_post_grace_callback_attribute_exists_and_is_none_initially(self):
        """_post_grace_fan_check_callback is initialized to None on a fresh engine."""
        engine = _make_automation_engine()
        assert hasattr(engine, "_post_grace_fan_check_callback")
        assert engine._post_grace_fan_check_callback is None


class TestDeactivateFanRestoresStrandedHvacSuppression:
    """Issue #402 follow-up: _deactivate_fan's idempotency guard must not strand

    _pre_fan_hvac_mode when the fan is already physically off.

    nat_vent_temperature_check()'s cycling-off path calls
    _deactivate_fan(restore_hvac=False) to stop the fan motor while intentionally
    keeping HVAC suppressed (_pre_fan_hvac_mode stays set) so the nat-vent session
    survives the cycle. If sensors close (or any other caller) subsequently invokes
    _deactivate_fan(restore_hvac=True) while the fan is already inactive, the
    idempotency guard must still release the HVAC suppression — otherwise
    _pre_fan_hvac_mode is stranded forever and _whf_owns_hvac() blocks every future
    HVAC write with hvac_write_blocked_whf_active, even though nat-vent has ended.

    Occupant impact without this fix: after natural ventilation cycles the fan off
    right before windows close, the home's HVAC never re-arms — the occupant is left
    with the thermostat permanently suppressed to "off" with no way to recover short
    of a restart.
    """

    def test_restore_hvac_true_while_fan_already_inactive_clears_pre_fan_hvac_mode(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whole_house"})
        engine._fan_active = False  # already cycled off
        engine._pre_fan_hvac_mode = "cool"  # stranded suppression latch

        import asyncio

        asyncio.run(engine._deactivate_fan(reason="door/window closed", restore_hvac=True))

        assert engine._pre_fan_hvac_mode is None

    def test_restore_hvac_true_while_fan_already_inactive_calls_set_hvac_mode(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whole_house"})
        engine._fan_active = False
        engine._pre_fan_hvac_mode = "cool"
        engine._set_hvac_mode = AsyncMock()

        import asyncio

        asyncio.run(engine._deactivate_fan(reason="door/window closed", restore_hvac=True))

        engine._set_hvac_mode.assert_awaited_once()
        call_args = engine._set_hvac_mode.await_args
        assert call_args.args[0] == "cool"

    def test_restore_hvac_false_while_fan_already_inactive_stays_stranded_by_design(self):
        """restore_hvac=False (mid-cycle) must NOT clear the latch — session continuity."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whole_house"})
        engine._fan_active = False
        engine._pre_fan_hvac_mode = "cool"

        import asyncio

        asyncio.run(engine._deactivate_fan(reason="nat_vent_cycling_off", restore_hvac=False))

        assert engine._pre_fan_hvac_mode == "cool"

    def test_fan_already_inactive_with_no_pending_restore_is_true_noop(self):
        """No stranded latch, no fan to stop — genuinely nothing to do."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whole_house"})
        engine._fan_active = False
        engine._pre_fan_hvac_mode = None
        engine._set_hvac_mode = AsyncMock()

        import asyncio

        asyncio.run(engine._deactivate_fan(reason="redundant call", restore_hvac=True))

        engine._set_hvac_mode.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dual fan status tests (Issue #374)
# ---------------------------------------------------------------------------


def _make_coordinator_for_fan_status(
    fan_mode: str,
    fan_active: bool = False,
    fan_override_active: bool = False,
    natural_vent_active: bool = False,
    physical_state: bool | None = None,
    climate_fan_mode: str = "auto",
    climate_hvac_action: str = "",
):
    """Build a minimal coordinator stub for testing _compute_whf_status / _compute_hvac_fan_status.

    Uses object.__new__ to bypass DataUpdateCoordinator.__init__, then attaches only the
    attributes required by the two status methods and _compute_fan_status.
    """
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    cs = MagicMock()
    cs.attributes = {"fan_mode": climate_fan_mode, "hvac_action": climate_hvac_action}
    hass.states.get.return_value = cs
    coord.hass = hass

    coord.config = {"climate_entity": "climate.thermostat"}

    ae = MagicMock()
    ae.config = {CONF_FAN_MODE: fan_mode}
    ae._fan_active = fan_active
    ae._fan_override_active = fan_override_active
    ae._natural_vent_active = natural_vent_active
    coord.automation_engine = ae

    # Wire _get_fan_physical_state to return the supplied physical_state value
    coord._get_fan_physical_state = MagicMock(return_value=physical_state)

    return coord


class TestDualFanStatus:
    """Tests for _compute_whf_status() and _compute_hvac_fan_status() (Issue #374).

    Both methods are on the coordinator. Tests use a minimal coordinator stub built via
    object.__new__ to avoid DataUpdateCoordinator.__init__ requirements.
    """

    def test_whf_status_returns_none_when_not_configured(self):
        """_compute_whf_status returns None when fan_mode is FAN_MODE_HVAC."""
        coord = _make_coordinator_for_fan_status(fan_mode=FAN_MODE_HVAC)
        assert coord._compute_whf_status() is None

    def test_whf_status_returns_none_when_disabled(self):
        """_compute_whf_status returns None when fan_mode is FAN_MODE_DISABLED."""
        coord = _make_coordinator_for_fan_status(fan_mode=FAN_MODE_DISABLED)
        assert coord._compute_whf_status() is None

    def test_hvac_fan_status_returns_none_when_not_configured(self):
        """_compute_hvac_fan_status returns None when fan_mode is FAN_MODE_WHOLE_HOUSE."""
        coord = _make_coordinator_for_fan_status(fan_mode=FAN_MODE_WHOLE_HOUSE)
        assert coord._compute_hvac_fan_status() is None

    def test_hvac_fan_status_returns_none_when_disabled(self):
        """_compute_hvac_fan_status returns None when fan_mode is FAN_MODE_DISABLED."""
        coord = _make_coordinator_for_fan_status(fan_mode=FAN_MODE_DISABLED)
        assert coord._compute_hvac_fan_status() is None

    def test_whf_status_active_when_fan_active_and_physical_on(self):
        """_compute_whf_status returns 'active' when _fan_active=True and physical state=True."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=True,
            physical_state=True,
        )
        assert coord._compute_whf_status() == "active"

    def test_whf_status_active_unconfirmed_when_physical_off(self):
        """_compute_whf_status returns 'active (unconfirmed)' when _fan_active=True but physical=False."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=True,
            physical_state=False,
        )
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            result = coord._compute_whf_status()
        assert result == "active (unconfirmed)"
        mock_logger.warning.assert_called_once()

    def test_hvac_fan_status_active_when_fan_active(self):
        """_compute_hvac_fan_status returns 'active' when _fan_active=True."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_active=True,
        )
        assert coord._compute_hvac_fan_status() == "active"

    def test_both_mode_returns_whf_active_and_hvac_active(self):
        """FAN_MODE_BOTH: both _compute_whf_status and _compute_hvac_fan_status return 'active'."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_BOTH,
            fan_active=True,
            physical_state=True,
        )
        assert coord._compute_whf_status() == "active"
        assert coord._compute_hvac_fan_status() == "active"

    def test_compute_fan_status_warns_on_stale_flag(self):
        """_compute_fan_status returns 'active (unconfirmed)' + WARNING when _fan_active=True but physical=False."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=True,
            physical_state=False,
        )
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            result = coord._compute_fan_status()
        assert result == "active (unconfirmed)"
        mock_logger.warning.assert_called_once()

    def test_whf_status_nat_vent_idle(self):
        """_compute_whf_status returns 'nat-vent (session active, fan idle)' when nat-vent active, fan idle."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=False,
            natural_vent_active=True,
            physical_state=False,
        )
        assert coord._compute_whf_status() == "nat-vent (session active, fan idle)"

    def test_hvac_fan_status_nat_vent_idle(self):
        """_compute_hvac_fan_status returns 'nat-vent (session active, fan idle)' when nat-vent active, fan idle."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_active=False,
            natural_vent_active=True,
        )
        assert coord._compute_hvac_fan_status() == "nat-vent (session active, fan idle)"

    def test_whf_status_running_untracked_when_physical_on(self):
        """_compute_whf_status returns 'running (untracked)' when physical state=True but CA flags clear."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=False,
            physical_state=True,
        )
        assert coord._compute_whf_status() == "running (untracked)"

    def test_hvac_fan_status_running_untracked_via_thermostat(self):
        """_compute_hvac_fan_status returns 'running (untracked)' when thermostat fan_mode='on'."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_active=False,
            climate_fan_mode="on",
        )
        assert coord._compute_hvac_fan_status() == "running (untracked)"

    def test_whf_status_override_running(self):
        """_compute_whf_status returns 'running (manual override)' when override active and physical on."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_override_active=True,
            fan_active=False,
            physical_state=True,
        )
        assert coord._compute_whf_status() == "running (manual override)"

    def test_whf_status_override_off(self):
        """_compute_whf_status returns 'off (manual override)' when override active but physical off."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_override_active=True,
            fan_active=False,
            physical_state=False,
        )
        assert coord._compute_whf_status() == "off (manual override)"

    def test_hvac_fan_status_override_running(self):
        """_compute_hvac_fan_status returns 'running (manual override)' when override active and fan active."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_override_active=True,
            fan_active=True,
        )
        assert coord._compute_hvac_fan_status() == "running (manual override)"

    def test_hvac_fan_status_override_off(self):
        """_compute_hvac_fan_status returns 'off (manual override)' when override active but fan inactive."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_override_active=True,
            fan_active=False,
        )
        assert coord._compute_hvac_fan_status() == "off (manual override)"

    def test_whf_status_inactive(self):
        """_compute_whf_status returns 'inactive' when all flags clear and physical off."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_WHOLE_HOUSE,
            fan_active=False,
            physical_state=False,
        )
        assert coord._compute_whf_status() == "inactive"

    def test_hvac_fan_status_inactive(self):
        """_compute_hvac_fan_status returns 'inactive' when all flags clear and thermostat fan=auto."""
        coord = _make_coordinator_for_fan_status(
            fan_mode=FAN_MODE_HVAC,
            fan_active=False,
            climate_fan_mode="auto",
        )
        assert coord._compute_hvac_fan_status() == "inactive"
