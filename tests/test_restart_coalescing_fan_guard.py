"""Regression tests for Issue #491.

Two independent restart-time bugs, both surfaced by a real HA restart after
deploying #489 (0.5.20), neither caused by #489 itself:

1. False WHF manual override + grace period: `_async_fan_entity_changed()` and
   `_async_fan_remote_changed()` had no guard against firing during the 5-minute
   startup-coalescing window, unlike `_async_thermostat_changed()` (Issue #321). A
   physical device (WHF entity, or the QuietCool RF remote's `event.*` entity) can
   report/re-announce state while HA is still settling right after restart, which was
   misread as a fresh manual override. Fixed via one shared
   `_suppress_during_startup_coalescing()` helper used by all three listeners, instead
   of copy-pasting the guard (the "sibling copies drift" defect class this codebase has
   paid for repeatedly).

2. Coordinator crash / "Climate Advisor unavailable" banner: `_abandon_observation()`
   wrapped `hass.async_add_executor_job(...)` (already a scheduled awaitable) in
   `hass.async_create_task(...)` (which requires a coroutine), raising
   `TypeError: a coroutine was expected, got <Future ...>` on every restart that hit
   this abandonment path.

Occupant framing: without fix 1, a user could see the dashboard falsely claim they (or
someone) manually turned on the whole-house fan and started a 2-hour grace period,
right after every restart, blocking automation for no reason and misrepresenting what
actually happened in their home. Without fix 2, the dashboard could show a scary
"Climate Advisor unavailable" error after routine restarts/deploys, with no real
problem to fix.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 12, 17, 0, 0)

from custom_components.climate_advisor.const import CONF_FAN_REMOTE_ENTITY  # noqa: E402


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = attributes or {}
    return s


def _make_event(data: dict) -> MagicMock:
    event = MagicMock()
    event.data = data
    return event


# ---------------------------------------------------------------------------
# Shared helper: _suppress_during_startup_coalescing()
# ---------------------------------------------------------------------------


class TestSuppressDuringStartupCoalescing:
    """Direct unit tests for the shared helper — single source of truth for all
    three override-detection listeners."""

    def _make_coord(self, *, startup_coalesce_active: bool) -> object:
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._startup_coalesce_active = startup_coalesce_active
        coord._suppress_during_startup_coalescing = types.MethodType(
            ClimateAdvisorCoordinator._suppress_during_startup_coalescing, coord
        )
        return coord

    def test_returns_true_and_logs_when_active(self):
        coord = self._make_coord(startup_coalesce_active=True)
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            result = coord._suppress_during_startup_coalescing("test description")
        assert result is True
        mock_logger.debug.assert_called_once()
        assert "test description" in mock_logger.debug.call_args[0]

    def test_returns_false_when_inactive(self):
        coord = self._make_coord(startup_coalesce_active=False)
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            result = coord._suppress_during_startup_coalescing("test description")
        assert result is False
        mock_logger.debug.assert_not_called()


# ---------------------------------------------------------------------------
# _async_fan_entity_changed() — coalescing suppression
# ---------------------------------------------------------------------------


def _make_fan_entity_coord(*, startup_coalesce_active: bool) -> object:
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass = hass
    coord.config = {"fan_state_feedback": True}

    ae = MagicMock()
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._fan_active = False
    ae._fan_command_context_id = None
    ae.handle_fan_manual_override = MagicMock()
    ae.on_fan_turned_off = MagicMock()
    coord.automation_engine = ae

    coord._is_recent_fan_command = MagicMock(return_value=False)
    coord._startup_coalesce_active = startup_coalesce_active
    coord.async_request_refresh = AsyncMock()

    coord._suppress_during_startup_coalescing = types.MethodType(
        ClimateAdvisorCoordinator._suppress_during_startup_coalescing, coord
    )
    coord._async_fan_entity_changed = types.MethodType(ClimateAdvisorCoordinator._async_fan_entity_changed, coord)
    return coord


class TestFanEntityChangedCoalescingGuard:
    def test_no_override_during_coalesce_window(self):
        """A real WHF entity state blip during the restart window must NOT be
        detected as a manual override.

        Occupant impact: without this fix, a restart-time state blip on the whole-
        house fan entity triggered a spurious manual override and grace period,
        blocking automation for no reason.
        """
        coord = _make_fan_entity_coord(startup_coalesce_active=True)
        event = _make_event({"old_state": _make_state("off"), "new_state": _make_state("on")})

        asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_override_detection_works_after_coalesce(self):
        """After the coalescing window closes, a real fan-on is still detected.

        Occupant impact: genuine manual fan use after startup must still be
        recognised and start the grace period.
        """
        coord = _make_fan_entity_coord(startup_coalesce_active=False)
        event = _make_event({"old_state": _make_state("off"), "new_state": _make_state("on")})

        asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()


# ---------------------------------------------------------------------------
# _async_fan_remote_changed() — coalescing suppression
# ---------------------------------------------------------------------------


def _make_fan_remote_coord(*, startup_coalesce_active: bool) -> object:
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass = hass
    coord.config = {CONF_FAN_REMOTE_ENTITY: "event.quietcool_remote"}

    ae = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._startup_coalesce_active = startup_coalesce_active
    coord.async_request_refresh = AsyncMock()

    coord._suppress_during_startup_coalescing = types.MethodType(
        ClimateAdvisorCoordinator._suppress_during_startup_coalescing, coord
    )
    coord._async_fan_remote_changed = types.MethodType(ClimateAdvisorCoordinator._async_fan_remote_changed, coord)
    return coord


class TestFanRemoteChangedCoalescingGuard:
    def test_no_override_during_coalesce_window(self):
        """A stale QuietCool RF timer event re-announced at restart must NOT be acted
        on — this is the exact incident: "Fan manual override: whf: ?->on" and a
        120-minute grace period appeared at the restart boundary with no real remote
        press and the whole-house fan never physically turning on.

        Occupant impact: without this fix, every restart risked a false "you turned
        the fan on" grace period blocking automation for up to the RF timer's full
        duration (up to 12 hours), even though nobody touched the remote and the fan
        never ran.
        """
        coord = _make_fan_remote_coord(startup_coalesce_active=True)
        new_state = _make_state("2026-07-12T16:58:00+00:00", {"event_type": "timer_2h"})
        event = _make_event({"new_state": new_state})

        asyncio.run(coord._async_fan_remote_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_timer_event_works_after_coalesce(self):
        """After the coalescing window closes, a real remote press is still honored.

        Occupant impact: genuine remote use after startup must still set the fan
        override grace duration as documented in docs/fan-remote-spec.md.
        """
        coord = _make_fan_remote_coord(startup_coalesce_active=False)
        new_state = _make_state("2026-07-12T16:58:00+00:00", {"event_type": "timer_2h"})
        event = _make_event({"new_state": new_state})

        asyncio.run(coord._async_fan_remote_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once_with(
            fan_before="?", fan_after="on", duration_override=7200.0, remote_timer_hours=2.0
        )


# ---------------------------------------------------------------------------
# _abandon_observation() — executor-job crash
# ---------------------------------------------------------------------------


class TestAbandonObservationExecutorJob:
    def test_does_not_wrap_executor_job_in_create_task(self):
        """_abandon_observation() must call hass.async_add_executor_job() directly,
        not wrap it in hass.async_create_task() — the latter raised
        'TypeError: a coroutine was expected, got <Future ...>' on every restart that
        hit this abandonment path, crashing the whole coordinator update
        (surfaced to the user as the "Climate Advisor unavailable" status banner).

        Occupant impact: without this fix, the dashboard showed a scary "Climate
        Advisor unavailable" error after routine restarts/deploys, even though
        nothing was actually broken.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        hass = MagicMock()
        # A bare MagicMock (not a coroutine/Future) — if _abandon_observation ever
        # wraps this in hass.async_create_task() again, asyncio.iscoroutine() inside
        # HA's real async_create_task would reject it exactly like production does;
        # here we assert the wrapper is never called at all.
        hass.async_add_executor_job = MagicMock(return_value=MagicMock())
        hass.async_create_task = MagicMock()
        coord.hass = hass

        learning = MagicMock()
        learning.save_state = MagicMock()
        learning._state = MagicMock()
        learning._state.rejection_log = {}
        coord.learning = learning
        coord._rejection_log = {}
        coord._pending_observations = {
            "passive_decay": {
                "obs_type": "passive_decay",
                "start_time": "2026-07-12T16:30:00+00:00",
                "samples": [],
            }
        }

        coord._abandon_observation = types.MethodType(ClimateAdvisorCoordinator._abandon_observation, coord)

        # Must not raise — the original bug crashed here with a TypeError.
        coord._abandon_observation("passive_decay", "test_reason")

        hass.async_add_executor_job.assert_called_once_with(learning.save_state)
        hass.async_create_task.assert_not_called()
