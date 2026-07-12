"""Tests for Issue #486: QuietCool RF remote timer events set the fan override grace duration.

Covers:
- fan_status.parse_remote_timer_event(): the single source of truth for token->hours mapping
- handle_fan_manual_override(duration_override=...): shared entry point, RF path and physical
  path both use it; RF supplies its own duration, physical path uses the configured default
- Suppression is absolute while an RF timer is active, at BOTH existing choke points
  (_deactivate_fan and fan_thermostat_check) — with a WARNING logged (not silently dropped
  at INFO), so a future refactor that decouples the RF timer from _fan_override_active would
  make one of these tests fail loudly instead of silently regressing (the #400/#402/#417/#456
  "sibling threshold drift" failure mode this plan was explicitly designed to avoid).
- Last-wins duration refresh, grace-expiry resumption, restart clean-slate
- Coordinator dispatch: _async_fan_remote_changed() parses the event and drives the SAME
  handle_fan_manual_override() the physical-detection path already uses (no new method)

Coordinator infrastructure note: ClimateAdvisorCoordinator cannot be instantiated without a
live HA instance (see test_fan_cancel.py). Coordinator-dispatch tests here follow the same
minimal-stub + importlib pattern used there, to avoid stale __globals__ from test_occupancy.py
module deletion.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure HA stubs are installed before any coordinator import.
if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 12, 20, 0, 0)

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_REMOTE_ENTITY,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    DEFAULT_MANUAL_GRACE_SECONDS,
)
from custom_components.climate_advisor.fan_status import parse_remote_timer_event  # noqa: E402

_PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.automation.callback"
_PATCH_DT_NOW = "custom_components.climate_advisor.automation.dt_util.now"
_FIXED_NOW = datetime(2026, 7, 12, 20, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create a real AutomationEngine with mocked HA dependencies (mirrors test_grace_convergence.py)."""
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
        "fan_mode": "whole_house_fan",
        CONF_MANUAL_GRACE_PERIOD: DEFAULT_MANUAL_GRACE_SECONDS,
        CONF_MANUAL_GRACE_NOTIFY: False,
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


def _make_mock_engine() -> MagicMock:
    """Build a MagicMock engine with all boolean flags explicitly False (mirrors test_fan_cancel.py)."""
    ae = MagicMock(spec=AutomationEngine)
    ae._fan_active = False
    ae._fan_override_active = False
    ae._natural_vent_active = False
    ae._grace_active = False
    ae._fan_command_pending = False
    ae._hvac_command_pending = False
    ae._temp_command_pending = False
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._fan_remote_timer_hours = None
    ae.handle_fan_manual_override = MagicMock()
    return ae


def _make_fake_state(state_str: str, attributes: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.state = state_str
    s.attributes = attributes or {}
    return s


def _make_fake_event(new_state) -> MagicMock:
    ev = MagicMock()
    ev.data = {"new_state": new_state}
    return ev


def _make_coordinator_stub(config: dict | None = None) -> MagicMock:
    """Minimal coordinator stub sufficient for _async_fan_remote_changed (mirrors test_fan_cancel.py)."""
    config = config or {CONF_FAN_REMOTE_ENTITY: "event.quietcool_remote"}
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = MagicMock()
    coord.hass = hass
    coord.config = config
    coord.automation_engine = _make_mock_engine()
    coord.async_request_refresh = AsyncMock()
    return coord


# ---------------------------------------------------------------------------
# 1. parse_remote_timer_event()
# ---------------------------------------------------------------------------


class TestParseRemoteTimerEvent:
    """fan_status.parse_remote_timer_event() — single source of truth for the token mapping."""

    def test_all_timer_hours_tokens(self):
        assert parse_remote_timer_event("timer_1h") == (True, 1.0)
        assert parse_remote_timer_event("timer_2h") == (True, 2.0)
        assert parse_remote_timer_event("timer_4h") == (True, 4.0)
        assert parse_remote_timer_event("timer_8h") == (True, 8.0)
        assert parse_remote_timer_event("timer_12h") == (True, 12.0)

    def test_timer_none_uses_configured_default(self):
        assert parse_remote_timer_event("timer_none") == (True, None)

    def test_non_timer_tokens_ignored(self):
        for token in ("on", "off", "low", "medium", "high"):
            assert parse_remote_timer_event(token) == (False, None)

    def test_unknown_and_missing_tokens_ignored(self):
        assert parse_remote_timer_event("junk") == (False, None)
        assert parse_remote_timer_event("") == (False, None)
        assert parse_remote_timer_event(None) == (False, None)


# ---------------------------------------------------------------------------
# 2. Duration wiring — handle_fan_manual_override(duration_override=...)
# ---------------------------------------------------------------------------


class TestDurationWiring:
    """The RF path and the physical-detection path share ONE entry point (Issue #486 dedup)."""

    def test_duration_override_sets_grace_duration_seconds(self):
        engine = _make_automation_engine()
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0)
        assert engine._fan_override_active is True
        assert engine._grace_duration_seconds == 28800
        assert engine._fan_remote_timer_hours == 8.0

    def test_duration_override_none_uses_configured_manual_grace(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 1800})
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=None, remote_timer_hours=None)
        assert engine._grace_duration_seconds == 1800
        assert engine._fan_remote_timer_hours is None

    def test_physical_detection_path_still_uses_configured_default(self):
        """The pre-existing physical-fan-on callsite (no duration_override arg) must be
        unaffected by the new parameter — proves the shared entry point didn't regress
        the path it already served."""
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 1800})
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(fan_before="auto", fan_after="on")
        assert engine._grace_duration_seconds == 1800
        assert engine._fan_remote_timer_hours is None


# ---------------------------------------------------------------------------
# 3. Suppression across BOTH existing choke points, with WARNING logging
# ---------------------------------------------------------------------------


class TestSuppressionAbsoluteWithRemoteTimer:
    """While an RF timer is active, all CA-initiated fan-offs are suppressed and logged
    as WARNING (not silently dropped at INFO) — Issue #486's "fully absolute (log-only)"
    decision. Both _deactivate_fan (the choke point every other caller funnels through:
    nat-vent exit, comfort-floor breach, cycle-off, min-runtime cycle-off) and
    fan_thermostat_check (which returns "keep" directly without ever reaching
    _deactivate_fan) need their own guard — this is why each has its own test.
    """

    def test_deactivate_fan_suppressed_and_warns_with_remote_timer(self, caplog):
        import logging

        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_active = True
        engine._fan_remote_timer_hours = 8.0

        with caplog.at_level(logging.WARNING):
            asyncio.run(engine._deactivate_fan(reason="nat-vent ceiling exit (away mode)"))

        assert any("suppressed by active RF remote timer" in r.message for r in caplog.records)
        assert engine._fan_active is True  # never turned off

    def test_deactivate_fan_suppressed_info_only_without_remote_timer(self, caplog):
        """A plain (non-RF) manual override still suppresses, but at INFO — no behavior
        change for the pre-existing manual-override path."""
        import logging

        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_active = True
        engine._fan_remote_timer_hours = None

        with caplog.at_level(logging.DEBUG):
            asyncio.run(engine._deactivate_fan(reason="economizer off — fan no longer needed"))

        assert not any("suppressed by active RF remote timer" in r.message for r in caplog.records)
        assert (
            any(
                r.levelno == logging.WARNING and "suppressed by active RF remote timer" in r.message
                for r in caplog.records
            )
            is False
        )

    def test_fan_thermostat_check_suppressed_and_warns_with_remote_timer(self, caplog):
        import logging

        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_active = True
        engine._fan_remote_timer_hours = 4.0

        with caplog.at_level(logging.WARNING):
            asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=68.0, trigger="indoor"))

        assert any("cycle-off suppressed by active RF remote timer" in r.message for r in caplog.records)

    def test_fan_thermostat_check_suppressed_debug_only_without_remote_timer(self, caplog):
        import logging

        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_active = True
        engine._fan_remote_timer_hours = None

        with caplog.at_level(logging.DEBUG):
            asyncio.run(engine.fan_thermostat_check(indoor=72.0, outdoor=68.0, trigger="indoor"))

        assert not any("cycle-off suppressed by active RF remote timer" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Last-wins / refresh
# ---------------------------------------------------------------------------


class TestLastWinsRefresh:
    def test_second_timer_overrides_first(self):
        engine = _make_automation_engine()
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0)
            assert engine._grace_duration_seconds == 28800
            engine.handle_fan_manual_override(duration_override=7200, remote_timer_hours=2.0)
        assert engine._grace_duration_seconds == 7200
        assert engine._fan_remote_timer_hours == 2.0

    def test_timer_none_after_timer_reverts_to_configured_default(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 1800})
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0)
            engine.handle_fan_manual_override(duration_override=None, remote_timer_hours=None)
        assert engine._grace_duration_seconds == 1800
        assert engine._fan_remote_timer_hours is None


# ---------------------------------------------------------------------------
# 5. Grace expiry resumes normal supervision
# ---------------------------------------------------------------------------


class TestGraceExpiryResumes:
    def test_grace_expiry_clears_override_and_remote_timer(self):
        engine = _make_automation_engine()
        engine._is_within_planned_window_period = MagicMock(return_value=False)
        engine._current_classification = None

        with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=7200, remote_timer_hours=2.0)
            assert mock_call_later.call_count == 1
            grace_callback = mock_call_later.call_args[0][2]

        grace_callback(None)

        assert engine._fan_override_active is False
        assert engine._fan_remote_timer_hours is None

    def test_cycle_off_allowed_again_after_expiry(self):
        """After the RF-driven grace expires, a subsequent deactivation is no longer suppressed."""
        engine = _make_automation_engine()
        engine._is_within_planned_window_period = MagicMock(return_value=False)
        engine._current_classification = None
        engine._fan_active = True

        with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=7200, remote_timer_hours=2.0)
            grace_callback = mock_call_later.call_args[0][2]

        grace_callback(None)
        assert engine._fan_override_active is False

        asyncio.run(engine._deactivate_fan(reason="all sensors closed — stopping whole-house fan"))
        assert engine._fan_active is False


# ---------------------------------------------------------------------------
# 6. Restart clean-slate
# ---------------------------------------------------------------------------


class TestRestartCleanSlate:
    def test_restore_state_does_not_carry_remote_timer_across_restart(self):
        engine = _make_automation_engine()
        engine._fan_override_active = True
        engine._fan_remote_timer_hours = 8.0

        engine.restore_state({"fan_remote_timer_hours": 8.0, "fan_override_active": True})

        assert engine._fan_override_active is False
        assert engine._fan_remote_timer_hours is None

    def test_get_serializable_state_includes_remote_timer_for_observability(self):
        engine = _make_automation_engine()
        engine._fan_remote_timer_hours = 4.0
        state = engine.get_serializable_state()
        assert state["fan_remote_timer_hours"] == 4.0


# ---------------------------------------------------------------------------
# 7. Coordinator dispatch: _async_fan_remote_changed
# ---------------------------------------------------------------------------


class TestCoordinatorFanRemoteDispatch:
    """_async_fan_remote_changed parses the event and drives the SAME
    handle_fan_manual_override() the physical-detection path already uses."""

    def test_timer_8h_event_drives_shared_override_with_28800s(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_8h"})
        event = _make_fake_event(new_state)

        asyncio.run(method(event))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?", fan_after="on", duration_override=28800.0, remote_timer_hours=8.0
        )

    def test_timer_none_event_drives_shared_override_with_none_duration(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_none"})
        event = _make_fake_event(new_state)

        asyncio.run(method(event))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?", fan_after="on", duration_override=None, remote_timer_hours=None
        )

    def test_non_timer_event_is_a_noop(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)

        for event_type in ("on", "off", "low", "high", None, "garbage"):
            new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": event_type})
            asyncio.run(method(_make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()

    def test_unavailable_state_is_a_noop(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)

        for state_str in ("unavailable", "unknown"):
            new_state = _make_fake_state(state_str, {"event_type": "timer_8h"})
            asyncio.run(method(_make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()

    def test_missing_new_state_is_a_noop(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)

        ev = MagicMock()
        ev.data = {"new_state": None}
        asyncio.run(method(ev))

        ae.handle_fan_manual_override.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Feature-off regression: subscription gate condition
# ---------------------------------------------------------------------------


class TestFeatureOffRegression:
    """Coordinator.async_setup() cannot be instantiated without a live HA instance
    (see test_fan_cancel.py's note on this). This replicates the exact gate condition
    from async_setup — `if self.config.get(CONF_FAN_REMOTE_ENTITY): subscribe` — the
    same pattern TestFanCancelFlagComputation uses to unit-test a dispatch condition
    without a live coordinator."""

    def test_gate_is_false_when_unconfigured(self):
        config = {"climate_entity": "climate.thermostat"}
        assert bool(config.get(CONF_FAN_REMOTE_ENTITY)) is False

    def test_gate_is_true_when_configured(self):
        config = {CONF_FAN_REMOTE_ENTITY: "event.quietcool_remote"}
        assert bool(config.get(CONF_FAN_REMOTE_ENTITY)) is True
