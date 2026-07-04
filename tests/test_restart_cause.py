"""Tests for Issue #403/#413: restart-cause diagnostics.

Covers:
- _persist_shutdown_diagnostics() (shared helper) persists clean_shutdown/
  last_shutdown_version/user_initiated_restart via learning.save_state(), called from
  both async_shutdown() and the EVENT_HOMEASSISTANT_STOP listener.
- async_restore_state() logs version, classifies restart cause
  (version_changed / user_restart / unknown), emits the cause on the
  system_restarted event (plus old/new version for version_changed), and
  resets clean_shutdown in memory afterward.
- The EVENT_CALL_SERVICE listener sets _user_initiated_shutdown only for
  homeassistant.restart/stop service calls.
- The EVENT_HOMEASSISTANT_STOP listener persists shutdown diagnostics even when
  async_unload_entry()/async_shutdown() never runs (Issue #413 regression — a real HA
  restart/deploy does NOT call async_unload_entry, so relying solely on async_shutdown()
  left every real restart classified as "unknown").
- LearningState persists and defensively validates the three new fields.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.climate_advisor.const import VERSION  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import LearningEngine, LearningState  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coord_stub():
    """Build a minimal coordinator-like object with real async_shutdown/async_restore_state bound."""
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    coord.hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    coord.config = {"climate_entity": "climate.thermostat"}
    coord._unsub_listeners = []
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._user_initiated_shutdown = False

    coord.learning = MagicMock()
    coord.learning._state = LearningState()
    coord.learning.save_state = MagicMock()
    coord.learning.load_state = MagicMock()

    coord.automation_engine = MagicMock()
    coord.automation_engine._natural_vent_active = False
    coord.automation_engine._fan_override_active = False
    coord.automation_engine.cleanup = MagicMock()

    coord._flush_hvac_runtime = MagicMock()
    coord._cancel_occupancy_away_timer = MagicMock()
    coord._unsubscribe_door_window_listeners = MagicMock()
    coord._async_save_state = AsyncMock()

    coord._rejection_log = {}
    coord._state_persistence = MagicMock()
    # Minimal same-day persisted state so async_restore_state() reaches the
    # restart-cause classification block (it returns early on falsy/other-day state).
    today_str = dt_util.now().strftime("%Y-%m-%d")
    coord._state_persistence.load = MagicMock(
        return_value={
            "date": today_str,
            "last_saved": today_str,
            "classification": None,
            "temp_history": {"outdoor": [], "indoor": []},
            "today_record": None,
            "briefing_state": {},
            "automation_state": {},
            "automation_enabled": True,
            "occupancy_mode": "home",
            "occupancy_away_since": None,
            "ai_stats": {},
            "pred_archive": {},
            "event_log": [],
        }
    )
    coord._event_log = []
    coord._emit_event = MagicMock()
    coord.claude_client = None

    coord.async_shutdown = types.MethodType(ClimateAdvisorCoordinator.async_shutdown, coord)
    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)
    coord._persist_shutdown_diagnostics = types.MethodType(
        ClimateAdvisorCoordinator._persist_shutdown_diagnostics, coord
    )

    return coord


# ---------------------------------------------------------------------------
# async_shutdown() persistence
# ---------------------------------------------------------------------------


class TestShutdownPersistsRestartCauseFields:
    def test_shutdown_sets_clean_shutdown_and_version(self):
        coord = _make_coord_stub()
        asyncio.run(coord.async_shutdown())

        assert coord.learning._state.clean_shutdown is True
        assert coord.learning._state.last_shutdown_version == VERSION
        coord.learning.save_state.assert_called_once()

    def test_shutdown_persists_user_initiated_flag_true(self):
        coord = _make_coord_stub()
        coord._user_initiated_shutdown = True
        asyncio.run(coord.async_shutdown())

        assert coord.learning._state.user_initiated_restart is True

    def test_shutdown_persists_user_initiated_flag_false(self):
        coord = _make_coord_stub()
        coord._user_initiated_shutdown = False
        asyncio.run(coord.async_shutdown())

        assert coord.learning._state.user_initiated_restart is False

    def test_shutdown_does_not_duplicate_save_state_call(self):
        """Only one _async_save_state() call — no second/duplicate save added."""
        coord = _make_coord_stub()
        asyncio.run(coord.async_shutdown())

        coord._async_save_state.assert_called_once()


# ---------------------------------------------------------------------------
# async_restore_state() cause classification
# ---------------------------------------------------------------------------


class TestRestoreStateClassifiesCause:
    def test_user_restart_cause_when_clean_shutdown_and_same_version(self):
        coord = _make_coord_stub()
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_version = VERSION
        coord.learning._state.user_initiated_restart = True

        asyncio.run(coord.async_restore_state())

        calls = coord._emit_event.call_args_list
        restarted_calls = [c for c in calls if c[0][0] == "system_restarted"]
        assert len(restarted_calls) == 1
        payload = restarted_calls[0][0][1]
        assert payload["cause"] == "user_restart"

    def test_version_changed_cause_and_separate_event(self):
        coord = _make_coord_stub()
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_version = "0.4.59"

        asyncio.run(coord.async_restore_state())

        calls = coord._emit_event.call_args_list
        event_types = [c[0][0] for c in calls]
        assert "version_changed" in event_types
        assert "system_restarted" in event_types

        vc_payload = next(c[0][1] for c in calls if c[0][0] == "version_changed")
        assert vc_payload == {"old_version": "0.4.59", "new_version": VERSION}

        restarted_payload = next(c[0][1] for c in calls if c[0][0] == "system_restarted")
        assert restarted_payload["cause"] == "version_changed"
        assert restarted_payload["old_version"] == "0.4.59"
        assert restarted_payload["new_version"] == VERSION

    def test_unknown_cause_when_no_clean_shutdown_ever_recorded(self):
        """Fresh LearningState defaults (never shut down cleanly) → unknown (crash residual)."""
        coord = _make_coord_stub()
        # LearningState() defaults: clean_shutdown=False, last_shutdown_version=None

        asyncio.run(coord.async_restore_state())

        calls = coord._emit_event.call_args_list
        restarted_calls = [c for c in calls if c[0][0] == "system_restarted"]
        payload = restarted_calls[0][0][1]
        assert payload["cause"] == "unknown"
        assert "old_version" not in payload
        assert "new_version" not in payload

    def test_clean_shutdown_reset_in_memory_after_classification(self):
        """clean_shutdown must be reset to False in memory so a subsequent crash reads unknown."""
        coord = _make_coord_stub()
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_version = VERSION

        asyncio.run(coord.async_restore_state())

        assert coord.learning._state.clean_shutdown is False


# ---------------------------------------------------------------------------
# EVENT_CALL_SERVICE listener behavior
# ---------------------------------------------------------------------------


class TestUserInitiatedRestartDetection:
    """Verify the EVENT_CALL_SERVICE handler logic registered in async_setup()."""

    def _make_handler(self, coord):
        """Extract the handler by replicating the closure logic in async_setup()."""

        def _async_call_service_event(event):
            if event.data.get("domain") == "homeassistant" and event.data.get("service") in (
                "restart",
                "stop",
            ):
                coord._user_initiated_shutdown = True

        return _async_call_service_event

    def test_restart_service_sets_flag(self):
        coord = _make_coord_stub()
        handler = self._make_handler(coord)
        event = MagicMock()
        event.data = {"domain": "homeassistant", "service": "restart"}

        handler(event)

        assert coord._user_initiated_shutdown is True

    def test_stop_service_sets_flag(self):
        coord = _make_coord_stub()
        handler = self._make_handler(coord)
        event = MagicMock()
        event.data = {"domain": "homeassistant", "service": "stop"}

        handler(event)

        assert coord._user_initiated_shutdown is True

    def test_other_homeassistant_service_does_not_set_flag(self):
        coord = _make_coord_stub()
        handler = self._make_handler(coord)
        event = MagicMock()
        event.data = {"domain": "homeassistant", "service": "check_config"}

        handler(event)

        assert coord._user_initiated_shutdown is False

    def test_other_domain_does_not_set_flag(self):
        coord = _make_coord_stub()
        handler = self._make_handler(coord)
        event = MagicMock()
        event.data = {"domain": "climate", "service": "restart"}

        handler(event)

        assert coord._user_initiated_shutdown is False


# ---------------------------------------------------------------------------
# EVENT_HOMEASSISTANT_STOP listener behavior (Issue #413)
# ---------------------------------------------------------------------------


class TestHomeAssistantStopPersistsShutdownDiagnostics:
    """Verify the EVENT_HOMEASSISTANT_STOP handler logic registered in async_setup().

    A real HA restart (user-clicked "Restart Home Assistant", `ha core restart`, or a
    HACS-deploy-triggered restart) fires EVENT_HOMEASSISTANT_STOP and then the process
    exits — it does NOT call async_unload_entry()/async_shutdown(). Before this fix, the
    three shutdown-diagnostics fields were only ever written inside async_shutdown(), so
    every real restart fell through to "unknown" in async_restore_state().
    """

    def _make_handler(self, coord):
        """Extract the handler by replicating the closure logic in async_setup()."""

        def _async_homeassistant_stop(_event):
            coord.hass.async_create_task(coord._persist_shutdown_diagnostics())

        return _async_homeassistant_stop

    def test_stop_event_schedules_persist_diagnostics_task(self):
        coord = _make_coord_stub()
        scheduled = []
        coord.hass.async_create_task = MagicMock(side_effect=lambda coro: scheduled.append(coro))
        handler = self._make_handler(coord)

        handler(MagicMock())

        assert len(scheduled) == 1
        asyncio.run(scheduled[0])
        assert coord.learning._state.clean_shutdown is True
        assert coord.learning._state.last_shutdown_version == VERSION
        coord.learning.save_state.assert_called_once()

    def test_stop_event_persists_user_initiated_flag(self):
        coord = _make_coord_stub()
        coord._user_initiated_shutdown = True
        scheduled = []
        coord.hass.async_create_task = MagicMock(side_effect=lambda coro: scheduled.append(coro))
        handler = self._make_handler(coord)

        handler(MagicMock())
        asyncio.run(scheduled[0])

        assert coord.learning._state.user_initiated_restart is True

    def test_stop_event_alone_without_unload_produces_user_restart_not_unknown(self):
        """Regression for #413: previously, if async_unload_entry() never ran (the normal
        case for a real HA restart), clean_shutdown/last_shutdown_version were never
        persisted and the classifier fell through to "unknown" even for a graceful
        restart. Now the EVENT_HOMEASSISTANT_STOP listener alone — WITHOUT
        async_shutdown() ever running — is sufficient to persist them correctly.
        """
        coord = _make_coord_stub()
        scheduled = []
        coord.hass.async_create_task = MagicMock(side_effect=lambda coro: scheduled.append(coro))
        handler = self._make_handler(coord)

        handler(MagicMock())  # STOP fires — async_unload_entry/async_shutdown NEVER called
        asyncio.run(scheduled[0])  # the scheduled persist task runs to completion

        asyncio.run(coord.async_restore_state())

        calls = coord._emit_event.call_args_list
        restarted_payload = next(c[0][1] for c in calls if c[0][0] == "system_restarted")
        assert restarted_payload["cause"] == "user_restart"

    def test_no_stop_event_and_no_unload_still_classifies_unknown(self):
        """A true crash/container kill fires neither EVENT_HOMEASSISTANT_STOP nor
        async_unload_entry — the classifier must still correctly report "unknown" for
        this case (not a regression to fix)."""
        coord = _make_coord_stub()
        # Neither the STOP listener nor async_shutdown() ran — fresh LearningState defaults.

        asyncio.run(coord.async_restore_state())

        calls = coord._emit_event.call_args_list
        restarted_payload = next(c[0][1] for c in calls if c[0][0] == "system_restarted")
        assert restarted_payload["cause"] == "unknown"


# ---------------------------------------------------------------------------
# LearningState persistence — new fields
# ---------------------------------------------------------------------------


class TestLearningStateRestartFields:
    def test_defaults(self):
        state = LearningState()
        assert state.last_shutdown_version is None
        assert state.clean_shutdown is False
        assert state.user_initiated_restart is False

    def test_save_and_load_round_trip(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine._state.last_shutdown_version = "0.4.60"
        engine._state.clean_shutdown = True
        engine._state.user_initiated_restart = True
        engine.save_state()

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()

        assert engine2._state.last_shutdown_version == "0.4.60"
        assert engine2._state.clean_shutdown is True
        assert engine2._state.user_initiated_restart is True

    def test_load_state_defensive_type_check_bad_types(self, tmp_path):
        """Corrupted JSON with wrong types resets fields to safe defaults."""
        import json

        db_path = tmp_path / "climate_advisor_learning.json"
        db_path.write_text(
            json.dumps(
                {
                    "last_shutdown_version": 123,  # wrong type — should become None
                    "clean_shutdown": "yes",  # wrong type — should become False
                    "user_initiated_restart": "no",  # wrong type — should become False
                }
            )
        )

        engine = LearningEngine(tmp_path)
        engine.load_state()

        assert engine._state.last_shutdown_version is None
        assert engine._state.clean_shutdown is False
        assert engine._state.user_initiated_restart is False


# ---------------------------------------------------------------------------
# ai_skills_activity rendering
# ---------------------------------------------------------------------------


class TestRenderSystemRestarted:
    def test_render_version_changed(self):
        from custom_components.climate_advisor.ai_skills_activity import (
            _render_system_restarted,
        )

        label, _ = _render_system_restarted(
            {"recovered_events": 5, "cause": "version_changed", "old_version": "0.4.59", "new_version": "0.4.60"},
            "fahrenheit",
        )
        assert "version_changed" in label
        assert "0.4.59->0.4.60" in label
        assert "5 prior events recovered" in label

    def test_render_user_restart(self):
        from custom_components.climate_advisor.ai_skills_activity import (
            _render_system_restarted,
        )

        label, _ = _render_system_restarted({"recovered_events": 2, "cause": "user_restart"}, "fahrenheit")
        assert "user_restart" in label
        assert "2 prior events recovered" in label

    def test_render_unknown_cause(self):
        from custom_components.climate_advisor.ai_skills_activity import (
            _render_system_restarted,
        )

        label, _ = _render_system_restarted({"recovered_events": 0, "cause": "unknown"}, "fahrenheit")
        assert "unknown" in label

    def test_render_missing_cause_defaults_to_unknown(self):
        """Backward compat: old persisted events without a cause key render as unknown."""
        from custom_components.climate_advisor.ai_skills_activity import (
            _render_system_restarted,
        )

        label, _ = _render_system_restarted({"recovered_events": 3}, "fahrenheit")
        assert "unknown" in label
        assert "3 prior events recovered" in label
