"""Tests for Issue #213 — Event log persistence (save/restore round-trip).

Covers:
  - _build_state_dict() serialises _event_log under the "event_log" key
  - async_restore_state() loads a saved event_log into _event_log
  - Oversized logs are capped to EVENT_LOG_CAP on restore
  - A system_restarted marker is always appended after restore
  - _event_source_label() classifies system_restarted events as "system"
  - Missing event_log key in persisted state defaults gracefully to one marker
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Fixed datetime for tests — used via dt_mock injected into coordinator module scope
_FIXED_NOW = datetime(2026, 6, 3, 10, 0, 0)
_TODAY_STR = _FIXED_NOW.strftime("%Y-%m-%d")  # "2026-06-03"


def _make_dt_mock():
    """Build a dt_util mock that returns _FIXED_NOW from .now() and delegates
    isoformat/strftime to the real datetime object."""
    dt_mock = MagicMock()
    dt_mock.now.return_value = _FIXED_NOW
    dt_mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
    return dt_mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EVENT_LOG_CAP = 500  # must match const.py


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class via importlib.

    Using importlib each time prevents stale __globals__ when test_occupancy.py
    deletes and re-imports the coordinator module.
    """
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_minimal_coordinator(*, initial_event_log: list | None = None):
    """Build the smallest coordinator stub needed for event_log tests.

    Only populates attrs required by _build_state_dict() and _emit_event().
    """
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass

    # Attrs consumed by _build_state_dict()
    coord._event_log = list(initial_event_log) if initial_event_log is not None else []
    coord._current_classification = None
    coord._today_record = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord.claude_client = None
    coord._pred_archive = {}
    coord._passive_k_backfilled = False
    coord._vent_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._solar_phase_ac_backfill = False  # Issue #312

    # automation_engine — MagicMock (NOT AsyncMock) per project convention
    ae = MagicMock()
    ae.get_serializable_state = MagicMock(return_value={})
    coord.automation_engine = ae

    # Bind real methods so __globals__ point to the live module
    coord._build_state_dict = types.MethodType(ClimateAdvisorCoordinator._build_state_dict, coord)
    coord._emit_event = types.MethodType(ClimateAdvisorCoordinator._emit_event, coord)

    return coord


def _make_restore_coordinator():
    """Build a coordinator stub wired for async_restore_state().

    Injects executor calls so load_state() and _state_persistence.load() can be
    controlled, and stubs out everything async_restore_state() touches.
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    # ── learning ──────────────────────────────────────────────────────────
    learning = MagicMock()
    learning.load_state = MagicMock()
    learning._state = MagicMock()
    learning._state.rejection_log = {}
    coord.learning = learning

    # ── _state_persistence ────────────────────────────────────────────────
    coord._state_persistence = MagicMock()

    # ── runtime state ─────────────────────────────────────────────────────
    coord._event_log = []
    coord._rejection_log = {}
    coord._current_classification = None
    coord._today_record = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord.claude_client = None
    coord._pred_archive = {}
    coord._passive_k_backfilled = False
    coord._vent_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._solar_phase_ac_backfill = False  # Issue #312

    ae = MagicMock()
    ae.restore_state = MagicMock()
    ae.set_occupancy_mode = MagicMock()
    ae.dry_run = False
    coord.automation_engine = ae

    # Bind real methods
    coord._emit_event = types.MethodType(ClimateAdvisorCoordinator._emit_event, coord)
    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)

    return coord


def _run_restore(coord, *, state_data: dict):
    """Drive async_restore_state() with controlled executor responses.

    The two async_add_executor_job calls inside async_restore_state are:
      1. self.learning.load_state        → returns None
      2. self._state_persistence.load   → returns state_data

    dt_util is patched on the coordinator module so that strftime("%Y-%m-%d")
    returns _TODAY_STR and the same-day restore branch is reached (not skipped).
    """
    executor_results = iter([None, state_data])

    async def _fake_executor(fn, *args):
        return next(executor_results, None)

    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = _fake_executor

    dt_mock = _make_dt_mock()
    with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
        asyncio.run(coord.async_restore_state())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventLogSerialization:
    """_build_state_dict() correctly serialises the event log."""

    def test_event_log_saved_in_state_dict(self):
        """event_log key is present and non-empty after seeding _event_log."""
        event = {"type": "test_evt", "time": "2026-01-01T00:00:00"}
        coord = _make_minimal_coordinator(initial_event_log=[event])

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            result = coord._build_state_dict()

        assert "event_log" in result
        assert len(result["event_log"]) >= 1
        assert result["event_log"][0]["type"] == "test_evt"


class TestEventLogRestore:
    """async_restore_state() correctly restores and caps the event log."""

    def test_event_log_restored_from_state(self):
        """Events from persisted state are loaded into _event_log."""
        coord = _make_restore_coordinator()
        state = {
            "date": _TODAY_STR,
            "event_log": [{"type": "x", "time": "t"}],
        }

        _run_restore(coord, state_data=state)

        # At minimum the single saved event + the system_restarted marker
        assert len(coord._event_log) >= 1
        types_ = [e["type"] for e in coord._event_log]
        assert "x" in types_

    def test_event_log_capped_on_restore(self):
        """Oversized saved logs are truncated to EVENT_LOG_CAP before appending marker."""
        coord = _make_restore_coordinator()
        oversized = [{"type": "evt", "time": "t", "i": i} for i in range(600)]
        state = {
            "date": _TODAY_STR,
            "event_log": oversized,
        }

        _run_restore(coord, state_data=state)

        # After capping to 500 + 1 system_restarted marker
        assert len(coord._event_log) <= EVENT_LOG_CAP + 1

    def test_system_restarted_event_emitted_after_restore(self):
        """system_restarted marker is always the last entry after restore."""
        coord = _make_restore_coordinator()
        state = {
            "date": _TODAY_STR,
            "event_log": [{"type": "x", "time": "t"}],
        }

        _run_restore(coord, state_data=state)

        assert coord._event_log[-1]["type"] == "system_restarted"
        assert coord._event_log[-1]["recovered_events"] == 1

    def test_event_log_missing_from_old_state_defaults_gracefully(self):
        """If event_log key is absent in persisted state no exception is raised
        and exactly one system_restarted marker is appended."""
        coord = _make_restore_coordinator()
        state = {"date": _TODAY_STR}  # no event_log key

        _run_restore(coord, state_data=state)

        assert len(coord._event_log) == 1
        assert coord._event_log[0]["type"] == "system_restarted"
        assert coord._event_log[0]["recovered_events"] == 0


class TestEventSourceLabel:
    """_event_source_label() returns correct source strings."""

    def test_system_restarted_source_label(self):
        """system_restarted events are labelled as 'system'."""
        from custom_components.climate_advisor.ai_skills_activity import (
            _event_source_label,
        )

        result = _event_source_label("system_restarted", {})

        assert result == "system"
