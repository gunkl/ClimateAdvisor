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
from datetime import datetime, timedelta
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
from custom_components.climate_advisor.fan_status import (  # noqa: E402
    parse_remote_speed_event,
    parse_remote_timer_event,
)

_PATCH_CALL_LATER = "custom_components.climate_advisor.automation.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.automation.callback"
_PATCH_DT_NOW = "custom_components.climate_advisor.automation.dt_util.now"
_PATCH_COORDINATOR_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"
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
    ae._fan_remote_speed = None
    ae._fan_command_time = None  # Issue #567: default to "no recent CA command" for the echo guard
    ae.handle_fan_manual_override = MagicMock()
    ae.handle_fan_speed_observed = MagicMock()
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


def _make_coordinator_stub(config: dict | None = None, *, physical_on: bool | None = None) -> MagicMock:
    """Minimal coordinator stub sufficient for _async_fan_remote_changed (mirrors test_fan_cancel.py).

    Issue #519: binds the REAL burst-combining methods (not auto-mocked) so dispatch tests
    exercise the actual combine/classify logic, not a mock stand-in — per this project's
    "never mirror the logic under test" doctrine. `physical_on` seeds
    `_get_fan_physical_state()`, which the burst-open snapshot reads to classify a bare
    speed press as override vs. comfort-only.
    """
    config = config or {CONF_FAN_REMOTE_ENTITY: "event.quietcool_remote"}
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = MagicMock()
    coord.hass = hass
    coord.config = config
    coord.automation_engine = _make_mock_engine()
    coord.async_request_refresh = AsyncMock()
    # Issue #491: _async_fan_remote_changed now calls the real
    # _suppress_during_startup_coalescing() guard; coord being a bare MagicMock would
    # otherwise return a truthy MagicMock and silently suppress every dispatch test
    # below. These tests exercise post-coalescing (normal) dispatch behavior.
    coord._suppress_during_startup_coalescing = MagicMock(return_value=False)
    coord._last_fan_remote_event_ts = None
    coord._fan_remote_burst = None
    coord._fan_remote_burst_cancel = None
    coord._get_fan_physical_state = MagicMock(return_value=physical_on)

    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord._async_fan_remote_changed = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_remote_changed, coord)
    coord._arm_fan_remote_burst = types.MethodType(mod.ClimateAdvisorCoordinator._arm_fan_remote_burst, coord)
    coord._cancel_fan_remote_burst = types.MethodType(mod.ClimateAdvisorCoordinator._cancel_fan_remote_burst, coord)
    coord._flush_fan_remote_burst = types.MethodType(mod.ClimateAdvisorCoordinator._flush_fan_remote_burst, coord)
    # Issue #567: bind the REAL echo guard (not a MagicMock auto-attr, which would be
    # truthy and silently swallow every dispatch in this file) so it reads
    # automation_engine._fan_command_time exactly as production does.
    coord._is_recent_fan_command = types.MethodType(mod.ClimateAdvisorCoordinator._is_recent_fan_command, coord)
    return coord


async def _dispatch_and_flush(coord: MagicMock, event: MagicMock) -> None:
    """Issue #519: run dispatch then force the burst window to elapse immediately, instead
    of waiting out the real async_call_later timer — mirrors the pattern used in
    test_restart_coalescing_fan_guard.py."""
    await coord._async_fan_remote_changed(event)
    await coord._flush_fan_remote_burst()


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


class TestParseRemoteSpeedEvent:
    """fan_status.parse_remote_speed_event() — single source of truth for speed tokens (Issue #519)."""

    def test_all_speed_tokens(self):
        assert parse_remote_speed_event("low") == "low"
        assert parse_remote_speed_event("medium") == "medium"
        assert parse_remote_speed_event("high") == "high"

    def test_non_speed_tokens_ignored(self):
        for token in ("on", "off", "timer_1h", "timer_none"):
            assert parse_remote_speed_event(token) is None

    def test_unknown_and_missing_tokens_ignored(self):
        assert parse_remote_speed_event("junk") is None
        assert parse_remote_speed_event("") is None
        assert parse_remote_speed_event(None) is None


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
            engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0, is_remote_event=True)
            # Issue #495: a genuine remote "no timer" (timer_none) press must be marked
            # is_remote_event=True to distinguish it from a plain non-remote re-detection —
            # the latter now preserves an already-active remote timer instead of clobbering it.
            engine.handle_fan_manual_override(duration_override=None, remote_timer_hours=None, is_remote_event=True)
        assert engine._grace_duration_seconds == 1800
        assert engine._fan_remote_timer_hours is None

    def test_non_remote_restamp_preserves_active_remote_timer(self):
        """Issue #495: a plain (non-remote) re-stamp of an already-active override — e.g.
        the WHF fan entity re-reporting "on" after a brief unavailable flap — must NOT
        clobber an active remote timer to None.

        Confirmed live: the status API returned fan_remote_timer_hours=None seconds after
        an 8h remote press while the persisted engine state still held 8.0 — the fan
        entity's own re-detection (is_remote_event=False, remote_timer_hours=None) had
        nulled it, so the dashboard's remote-timer card showed nothing during an active
        8h override.
        """
        engine = _make_automation_engine()
        with (
            patch(_PATCH_CALL_LATER) as mock_call_later,
            patch(_PATCH_CALLBACK, side_effect=lambda f: f),
            patch(_PATCH_DT_NOW, return_value=_FIXED_NOW),
        ):
            mock_call_later.return_value = MagicMock()
            engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0, is_remote_event=True)
            assert engine._fan_remote_timer_hours == 8.0

            # Plain fan-entity re-detection: NOT a remote event, supplies no timer info.
            engine.handle_fan_manual_override(fan_before="?", fan_after="on")

        assert engine._fan_remote_timer_hours == 8.0, (
            "A non-remote re-stamp must preserve the active remote timer, not clobber it to None"
        )


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

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_8h"})
        event = _make_fake_event(new_state)

        asyncio.run(_dispatch_and_flush(coord, event))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=28800.0,
            remote_timer_hours=8.0,
            remote_speed=None,
            is_remote_event=True,
        )

    def test_timer_none_event_drives_shared_override_with_none_duration(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_none"})
        event = _make_fake_event(new_state)

        asyncio.run(_dispatch_and_flush(coord, event))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=None,
            remote_timer_hours=None,
            remote_speed=None,
            is_remote_event=True,
        )

    def test_non_timer_event_is_a_noop(self):
        """Issue #519: "low"/"high" are deliberately NOT covered here anymore — they now
        open/extend a burst instead of being ignored outright (see
        TestFanRemoteBurstClassification for their real behavior). Only tokens that remain
        genuinely inert at the coordinator-dispatch level are covered here."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        for event_type in ("on", "off", None, "garbage"):
            new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": event_type})
            asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()

    def test_unavailable_state_is_a_noop(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        for state_str in ("unavailable", "unknown"):
            new_state = _make_fake_state(state_str, {"event_type": "timer_8h"})
            asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()

    def test_missing_new_state_is_a_noop(self):
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        ev = MagicMock()
        ev.data = {"new_state": None}
        asyncio.run(_dispatch_and_flush(coord, ev))

        ae.handle_fan_manual_override.assert_not_called()


# ---------------------------------------------------------------------------
# 7b. Issue #519: burst classification — override vs. comfort-only
# ---------------------------------------------------------------------------


class TestFanRemoteBurstClassification:
    """The full decision table from _flush_fan_remote_burst() (Issue #519):
    1. Timer selected (with or without speed) -> always override.
    2. Bare speed press, fan was OFF/unknown before this press -> override.
    3. Bare speed press, fan was ALREADY running before this press -> comfort-only.
    """

    def test_timer_alone_is_override(self):
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_4h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=14400.0,
            remote_timer_hours=4.0,
            remote_speed=None,
            is_remote_event=True,
        )
        ae.handle_fan_speed_observed.assert_not_called()

    def test_timer_and_speed_together_is_one_override_call(self):
        """A single physical interaction carrying both fields (speed + timer, transmitted
        as separate packets moments apart) must produce exactly ONE decision, not two."""
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        speed_state = _make_fake_state("2026-07-12T20:00:00.100+00:00", {"event_type": "high"})
        timer_state = _make_fake_state("2026-07-12T20:00:00.300+00:00", {"event_type": "timer_4h"})

        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(speed_state)))
        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(timer_state)))
        ae.handle_fan_manual_override.assert_not_called()  # still combining — window not elapsed
        asyncio.run(coord._flush_fan_remote_burst())

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=14400.0,
            remote_timer_hours=4.0,
            remote_speed="high",
            is_remote_event=True,
        )
        ae.handle_fan_speed_observed.assert_not_called()

    def test_bare_speed_press_fan_was_off_is_override(self):
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "high"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=None,
            remote_timer_hours=None,
            remote_speed="high",
            is_remote_event=True,
        )
        ae.handle_fan_speed_observed.assert_not_called()

    def test_bare_speed_press_fan_was_unknown_is_override(self):
        """physical_state unavailable (command-only mode) -> defaults to override, the
        safe/conservative direction, never less protective than pre-#519 behavior."""
        coord = _make_coordinator_stub(physical_on=None)
        coord.automation_engine._fan_active = False
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "low"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once()
        ae.handle_fan_speed_observed.assert_not_called()

    def test_bare_speed_press_fan_already_running_is_comfort_only(self):
        coord = _make_coordinator_stub(physical_on=True)
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "medium"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()
        ae.handle_fan_speed_observed.assert_called_once_with("medium", is_remote_event=True)

    def test_timing_bug_regression_fan_turns_on_mid_burst_still_classifies_as_override(self):
        """Issue #519 self-review catch: the classification MUST use the fan's state at
        burst-OPEN time, not a fresh read at flush time. Simulates the fan actually turning
        on partway through the interaction (the physical entity catching up) — a flush-time
        re-read would wrongly see "already running" and misclassify a genuine override as
        comfort-only."""
        coord = _make_coordinator_stub(physical_on=False)  # off when the burst opens
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "high"})
        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(new_state)))

        # Fan has now turned on, mid-burst, before the window elapses.
        coord._get_fan_physical_state = MagicMock(return_value=True)

        asyncio.run(coord._flush_fan_remote_burst())

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=None,
            remote_timer_hours=None,
            remote_speed="high",
            is_remote_event=True,
        )
        ae.handle_fan_speed_observed.assert_not_called()


# ---------------------------------------------------------------------------
# 7c. Issue #519: burst window combining/cancellation mechanics
# ---------------------------------------------------------------------------


class TestFanRemoteBurstWindow:
    def test_mid_window_event_extends_without_double_flushing(self):
        """A second event within the window must extend the burst, not flush the first
        one prematurely — flushing happens exactly once, when explicitly triggered."""
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        first = _make_fake_state("2026-07-12T20:00:00.100+00:00", {"event_type": "high"})
        second = _make_fake_state("2026-07-12T20:00:00.300+00:00", {"event_type": "timer_2h"})

        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(first)))
        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(second)))
        ae.handle_fan_manual_override.assert_not_called()

        asyncio.run(coord._flush_fan_remote_burst())
        ae.handle_fan_manual_override.assert_called_once()

    def test_off_mid_burst_cancels_without_flushing(self):
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        speed_state = _make_fake_state("2026-07-12T20:00:00.100+00:00", {"event_type": "high"})
        off_state = _make_fake_state("2026-07-12T20:00:00.300+00:00", {"event_type": "off"})

        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(speed_state)))
        assert coord._fan_remote_burst is not None
        asyncio.run(coord._async_fan_remote_changed(_make_fake_event(off_state)))
        assert coord._fan_remote_burst is None

        # Nothing left to flush — a late-firing timer callback must be a no-op.
        asyncio.run(coord._flush_fan_remote_burst())
        ae.handle_fan_manual_override.assert_not_called()
        ae.handle_fan_speed_observed.assert_not_called()

    def test_timer_none_in_burst_passes_none_hours_correctly(self):
        """A real timer_none press must still pass remote_timer_hours=None with
        is_remote_event=True (clears any previously-stored value) — not be confused with
        "no timer packet arrived at all" (which must NOT clear a stored value, per
        handle_fan_manual_override's own guarded-overwrite logic)."""
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_none"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=None,
            remote_timer_hours=None,
            remote_speed=None,
            is_remote_event=True,
        )


# ---------------------------------------------------------------------------
# 8. Issue #495: dedup on stale unavailable->restore re-announcing an old event
# ---------------------------------------------------------------------------


class TestStaleRemoteEventDedup:
    """The QuietCool event.* entity's `state` field IS the firmware event timestamp.
    The entity flaps to `unavailable` at arbitrary times (not just at restart) and
    restores its STALE last event_type with the SAME timestamp — confirmed live: a
    phantom fan_manual_override(remote_timer_hours=2.0) fired with zero user action,
    exactly when the entity restored to a timer_2h state frozen from hours earlier.

    Occupant effect: without this dedup, a flapping remote entity can spuriously start
    (and, since every flap re-stamps the grace, indefinitely extend) a fan override that
    suppresses CA's own fan control — with no user action at all.
    """

    def test_same_timestamp_resent_is_deduped(self):
        """A genuine press is processed once; the SAME state re-announced (the
        unavailable->stale-restore pattern) must NOT re-trigger the override."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        new_state = _make_fake_state("2026-07-12T13:41:10.440+00:00", {"event_type": "timer_2h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))
        assert ae.handle_fan_manual_override.call_count == 1

        # Same entity `state` (== timestamp) re-announced, e.g. after an unavailable
        # flap restores the stale last value — must be ignored.
        stale_restore = _make_fake_state("2026-07-12T13:41:10.440+00:00", {"event_type": "timer_2h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(stale_restore)))
        assert ae.handle_fan_manual_override.call_count == 1, (
            "A re-announced identical event timestamp must not trigger a second override"
        )

    def test_two_genuinely_different_timestamps_both_fire(self):
        """Two real presses at different times must both be processed — the dedup guard
        must not become a blanket suppressor."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        first = _make_fake_state("2026-07-12T20:45:32.622+00:00", {"event_type": "timer_2h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(first)))
        second = _make_fake_state("2026-07-12T20:48:40.960+00:00", {"event_type": "timer_8h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(second)))

        assert ae.handle_fan_manual_override.call_count == 2


# ---------------------------------------------------------------------------
# 9. Feature-off regression: subscription gate condition
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


# ---------------------------------------------------------------------------
# 10. Issue #519: ambient speed-sensor discovery via entity/device registry
# ---------------------------------------------------------------------------


class TestFanRemoteSpeedSensorDiscovery:
    """coordinator._resolve_fan_remote_speed_sensor()/_read_fan_remote_speed() (Issue #519).

    Auto-discovery via HA's entity/device registry, keyed off the already-configured
    fan_remote_entity — no new user-facing config. This is the first feature in this
    codebase needing entity/device registry stubs (see tools/sim_harness/ha_stubs.py).
    """

    def _make_coord(self, *, remote_entity: str | None = "event.quietcool_remote") -> MagicMock:
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = MagicMock()
        coord.config = {CONF_FAN_REMOTE_ENTITY: remote_entity} if remote_entity else {}
        coord.hass = MagicMock()
        coord._fan_remote_speed_sensor_eid = None
        coord._resolve_fan_remote_speed_sensor = types.MethodType(
            mod.ClimateAdvisorCoordinator._resolve_fan_remote_speed_sensor, coord
        )
        coord._read_fan_remote_speed = types.MethodType(mod.ClimateAdvisorCoordinator._read_fan_remote_speed, coord)
        return coord

    def test_no_remote_entity_configured_returns_none(self):
        coord = self._make_coord(remote_entity=None)
        assert coord._resolve_fan_remote_speed_sensor() is None

    def test_remote_entity_not_in_registry_returns_none(self):
        coord = self._make_coord()
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        mock_registry = MagicMock()
        er_mod.async_get = MagicMock(return_value=mock_registry)
        mock_registry.async_get = MagicMock(return_value=None)

        assert coord._resolve_fan_remote_speed_sensor() is None

    def test_sibling_sensor_discovered_and_cached(self):
        coord = self._make_coord()
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        mock_registry = MagicMock()
        er_mod.async_get = MagicMock(return_value=mock_registry)
        remote_entry = MagicMock()
        remote_entry.device_id = "device123"
        mock_registry.async_get = MagicMock(return_value=remote_entry)
        sibling = MagicMock()
        sibling.domain = "sensor"
        sibling.entity_id = "sensor.basement_quietcool_speed"
        er_mod.async_entries_for_device = MagicMock(return_value=[sibling])

        result = coord._resolve_fan_remote_speed_sensor()
        assert result == "sensor.basement_quietcool_speed"

        # Cached: a second call must NOT re-scan the registry.
        er_mod.async_entries_for_device.reset_mock()
        assert coord._resolve_fan_remote_speed_sensor() == "sensor.basement_quietcool_speed"
        er_mod.async_entries_for_device.assert_not_called()

    def test_non_matching_sibling_ignored(self):
        coord = self._make_coord()
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        mock_registry = MagicMock()
        er_mod.async_get = MagicMock(return_value=mock_registry)
        remote_entry = MagicMock()
        remote_entry.device_id = "device123"
        mock_registry.async_get = MagicMock(return_value=remote_entry)
        unrelated = MagicMock()
        unrelated.domain = "sensor"
        unrelated.entity_id = "sensor.basement_quietcool_uptime"
        er_mod.async_entries_for_device = MagicMock(return_value=[unrelated])

        assert coord._resolve_fan_remote_speed_sensor() is None

    def test_miss_is_not_cached_self_corrects_on_later_call(self):
        """A miss (e.g. registry not yet populated at startup) must NOT be cached
        permanently — a later call, once the sibling appears, must succeed."""
        coord = self._make_coord()
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        mock_registry = MagicMock()
        er_mod.async_get = MagicMock(return_value=mock_registry)
        remote_entry = MagicMock()
        remote_entry.device_id = "device123"
        mock_registry.async_get = MagicMock(return_value=remote_entry)
        er_mod.async_entries_for_device = MagicMock(return_value=[])

        assert coord._resolve_fan_remote_speed_sensor() is None

        sibling = MagicMock()
        sibling.domain = "sensor"
        sibling.entity_id = "sensor.basement_quietcool_speed"
        er_mod.async_entries_for_device = MagicMock(return_value=[sibling])
        assert coord._resolve_fan_remote_speed_sensor() == "sensor.basement_quietcool_speed"

    def test_read_returns_none_when_sensor_unknown_or_unavailable(self):
        coord = self._make_coord()
        coord._fan_remote_speed_sensor_eid = "sensor.basement_quietcool_speed"
        for bad_state in ("unknown", "unavailable"):
            state = MagicMock()
            state.state = bad_state
            coord.hass.states.get = MagicMock(return_value=state)
            assert coord._read_fan_remote_speed() is None

    def test_read_returns_none_when_sensor_missing_entirely(self):
        coord = self._make_coord()
        coord._fan_remote_speed_sensor_eid = "sensor.basement_quietcool_speed"
        coord.hass.states.get = MagicMock(return_value=None)
        assert coord._read_fan_remote_speed() is None

    def test_read_returns_live_value_when_known(self):
        coord = self._make_coord()
        coord._fan_remote_speed_sensor_eid = "sensor.basement_quietcool_speed"
        state = MagicMock()
        state.state = "high"
        coord.hass.states.get = MagicMock(return_value=state)
        assert coord._read_fan_remote_speed() == "high"


class TestFanRemoteStatusFieldsWiring:
    """coordinator._compute_fan_remote_status_fields() (Issue #524).

    Before this fix, fan_remote_speed/fan_remote_timer_hours/fan_remote_timer_ends existed
    ONLY inside get_debug_state() (debug endpoint + diagnostics download) and never reached
    coordinator.data -- so the dashboard's WHF status card was unconditionally dark regardless
    of firmware/remote activity. This class tests the extracted shared helper directly (the
    real production computation, not a mirror); coordinator.py's own
    _async_update_data_impl()/get_debug_state() call sites are reviewed directly in the diff
    rather than additionally covered by a full-pipeline test here, consistent with this
    codebase's existing granularity for this class of method (no other _async_update_data_impl()
    field has a full-pipeline test either -- see test_coordinator_health.py, which mocks
    _async_update_data_impl() wholesale rather than exercising its real body).
    """

    def _make_coord(self) -> MagicMock:
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = MagicMock()
        coord._compute_fan_remote_status_fields = types.MethodType(
            mod.ClimateAdvisorCoordinator._compute_fan_remote_status_fields, coord
        )
        coord._read_fan_remote_speed = MagicMock(return_value=None)
        return coord

    def test_live_read_wins_over_engine_fallback(self):
        coord = self._make_coord()
        coord._read_fan_remote_speed = MagicMock(return_value="high")
        coord.automation_engine._fan_remote_speed = "low"
        coord.automation_engine._fan_remote_timer_hours = None

        result = coord._compute_fan_remote_status_fields()
        assert result["fan_remote_speed"] == "high"

    def test_falls_back_to_engine_value_when_live_read_unavailable(self):
        coord = self._make_coord()
        coord._read_fan_remote_speed = MagicMock(return_value=None)
        coord.automation_engine._fan_remote_speed = "low"
        coord.automation_engine._fan_remote_timer_hours = None

        result = coord._compute_fan_remote_status_fields()
        assert result["fan_remote_speed"] == "low"

    def test_timer_fields_present_when_timer_active(self):
        coord = self._make_coord()
        coord.automation_engine._fan_remote_speed = None
        coord.automation_engine._fan_remote_timer_hours = 4.0
        coord.automation_engine._grace_end_time = "2026-07-26T12:00:00"

        result = coord._compute_fan_remote_status_fields()
        assert result["fan_remote_timer_hours"] == 4.0
        assert result["fan_remote_timer_ends"] == "2026-07-26T12:00:00"

    def test_timer_fields_none_when_no_timer(self):
        """A stale/unrelated grace_end_time must not leak into fan_remote_timer_ends when
        no remote timer is actually active -- the two fields are gated together."""
        coord = self._make_coord()
        coord.automation_engine._fan_remote_speed = None
        coord.automation_engine._fan_remote_timer_hours = None
        coord.automation_engine._grace_end_time = "2026-07-26T12:00:00"

        result = coord._compute_fan_remote_status_fields()
        assert result["fan_remote_timer_hours"] is None
        assert result["fan_remote_timer_ends"] is None


# ---------------------------------------------------------------------------
# 11. Issue #567: CA-command echo guard on _async_fan_remote_changed
# ---------------------------------------------------------------------------


class TestFanRemoteEchoGuard:
    """The QuietCool device transmits AND receives on the same RF channel, so a CA-issued
    fan command can be heard back by this same receive-side entity and misread as a fresh
    manual press. Mirrors the existing _is_recent_fan_command() echo guard already used by
    _async_fan_entity_changed() (Fix #239) -- see the sibling-site list at
    _is_recent_fan_command()'s definition in coordinator.py.
    """

    def test_event_within_30s_of_ca_command_is_ignored(self):
        coord = _make_coordinator_stub(physical_on=True)
        ae = coord.automation_engine
        ae._fan_command_time = _FIXED_NOW - timedelta(seconds=5)  # CA just issued a command

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "low"})
        with patch(_PATCH_COORDINATOR_DT_NOW, return_value=_FIXED_NOW):
            asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()
        ae.handle_fan_speed_observed.assert_not_called()

    def test_timer_event_within_30s_of_ca_command_is_also_ignored(self):
        """The guard applies before any burst-combining logic runs -- a timer press
        (normally an unconditional override, see TestFanRemoteBurstClassification) must be
        suppressed too when it lands inside the echo window."""
        coord = _make_coordinator_stub(physical_on=True)
        ae = coord.automation_engine
        ae._fan_command_time = _FIXED_NOW - timedelta(seconds=1)

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_8h"})
        with patch(_PATCH_COORDINATOR_DT_NOW, return_value=_FIXED_NOW):
            asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_not_called()

    def test_event_outside_30s_window_still_classifies_normally(self):
        """Proves the guard doesn't over-suppress -- a remote press well clear of any CA
        command still drives the shared override entry point as before."""
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        ae._fan_command_time = _FIXED_NOW - timedelta(seconds=45)

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "high"})
        with patch(_PATCH_COORDINATOR_DT_NOW, return_value=_FIXED_NOW):
            asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once_with(
            fan_before="?",
            fan_after="on",
            duration_override=None,
            remote_timer_hours=None,
            remote_speed="high",
            is_remote_event=True,
        )

    def test_no_recent_command_still_classifies_normally(self):
        """Default state (no CA command ever issued) must behave exactly as before this
        fix -- the guard is opt-in via a real recent timestamp, not opt-out."""
        coord = _make_coordinator_stub(physical_on=False)
        ae = coord.automation_engine
        assert ae._fan_command_time is None

        new_state = _make_fake_state("2026-07-12T20:00:00+00:00", {"event_type": "timer_2h"})
        asyncio.run(_dispatch_and_flush(coord, _make_fake_event(new_state)))

        ae.handle_fan_manual_override.assert_called_once()
