"""Tests for Issue #213 — Event log persistence (save/restore round-trip).
Extended by Issue #432 — age-based retention alongside the count-based backstop.

Covers:
  - _build_state_dict() serialises _event_log under the "event_log" key
  - async_restore_state() loads a saved event_log into _event_log
  - Oversized logs are capped to EVENT_LOG_CAP on restore
  - A system_restarted marker is always appended after restore
  - _event_source_label() classifies system_restarted events as "system"
  - Missing event_log key in persisted state defaults gracefully to one marker
  - Issue #432: events older than EVENT_LOG_MAX_AGE_HOURS are pruned even when
    well under EVENT_LOG_CAP in count
  - Issue #432: more than EVENT_LOG_CAP recent events are still trimmed to
    EVENT_LOG_CAP (count-based backstop)
  - Issue #432: restore path and live emit path share the identical
    _prune_event_log() function — no divergent re-cap logic
  - Issue #432: a >500-event, >12h-spanning restored log still returns
    everything actually within a 12h window afterward
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (  # noqa: E402
    EVENT_LOG_CAP,
    EVENT_LOG_MAX_AGE_HOURS,
)

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
    coord._vent_window_k_backfilled = False
    coord._vent_fan_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_window_k_backfill_v2 = False
    coord._vent_fan_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._solar_phase_ac_backfill = False  # Issue #312
    coord._last_solar_phase_fit_date = None  # Issue #310

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

    # ── _chart_log (Issue #543: async_restore_state() now loads it) ────────
    coord._chart_log = MagicMock()

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
    coord._vent_window_k_backfilled = False
    coord._vent_fan_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_window_k_backfill_v2 = False
    coord._vent_fan_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._solar_phase_ac_backfill = False  # Issue #312
    coord._last_solar_phase_fit_date = None  # Issue #310

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

    The async_add_executor_job calls inside async_restore_state are:
      1. self._chart_log.load           → return value unused
      2. self.learning.load_state       → returns None
      3. self._state_persistence.load   → returns state_data

    Keyed by function identity rather than call order, since call count/order
    isn't a contract worth pinning down in this stub.

    dt_util is patched on the coordinator module so that strftime("%Y-%m-%d")
    returns _TODAY_STR and the same-day restore branch is reached (not skipped).
    """

    async def _fake_executor(fn, *args):
        # Issue #812: async_restore_state() now routes executor offload
        # through coordinator._executor_job(), which wraps the target
        # callable with log_capture.bind_zone_for_executor() before handing
        # it to hass.async_add_executor_job() — so `fn` here is a wrapper,
        # not literally `coord._state_persistence.load`. Unwrap via
        # __wrapped__ (set by functools.wraps in bind_zone_for_executor) to
        # keep this identity check working.
        if getattr(fn, "__wrapped__", fn) is coord._state_persistence.load:
            return state_data
        return None

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
        """Oversized saved logs (all recent, within the age window) are truncated
        to EVENT_LOG_CAP as a count-based backstop, keeping the most recent."""
        coord = _make_restore_coordinator()
        count = EVENT_LOG_CAP + 100
        oversized = [
            {
                "type": "evt",
                "time": (_FIXED_NOW - timedelta(minutes=count - i)).isoformat(),
                "i": i,
            }
            for i in range(count)
        ]
        state = {
            "date": _TODAY_STR,
            "event_log": oversized,
        }

        _run_restore(coord, state_data=state)

        # After capping to EVENT_LOG_CAP + 1 system_restarted marker
        assert len(coord._event_log) <= EVENT_LOG_CAP + 1
        # Most recent events (highest "i") are kept, oldest dropped
        types_i = [e["i"] for e in coord._event_log if e["type"] == "evt"]
        assert min(types_i) >= 100

    def test_event_log_age_pruned_on_restore(self):
        """Events older than EVENT_LOG_MAX_AGE_HOURS are pruned on restore even
        when the list is well under EVENT_LOG_CAP in count."""
        coord = _make_restore_coordinator()
        old_event = {
            "type": "stale",
            "time": (_FIXED_NOW - timedelta(hours=EVENT_LOG_MAX_AGE_HOURS + 1)).isoformat(),
        }
        recent_event = {
            "type": "fresh",
            "time": (_FIXED_NOW - timedelta(hours=1)).isoformat(),
        }
        state = {
            "date": _TODAY_STR,
            "event_log": [old_event, recent_event],
        }

        _run_restore(coord, state_data=state)

        types_ = [e["type"] for e in coord._event_log]
        assert "stale" not in types_
        assert "fresh" in types_

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


class TestPruneEventLog:
    """_prune_event_log() — Issue #432: age-based retention + count backstop."""

    def _prune_fn(self):
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        return mod._prune_event_log

    def test_age_based_eviction_under_cap(self):
        """An event older than EVENT_LOG_MAX_AGE_HOURS is pruned even though the
        list is nowhere near EVENT_LOG_CAP in count."""
        prune = self._prune_fn()
        old_event = {
            "type": "old",
            "time": (_FIXED_NOW - timedelta(hours=EVENT_LOG_MAX_AGE_HOURS + 1)).isoformat(),
        }
        recent_event = {
            "type": "recent",
            "time": (_FIXED_NOW - timedelta(hours=1)).isoformat(),
        }
        result = prune([old_event, recent_event], _FIXED_NOW)

        types_ = [e["type"] for e in result]
        assert "old" not in types_
        assert "recent" in types_

    def test_count_based_backstop_within_age_window(self):
        """More than EVENT_LOG_CAP events, all within the age window, are still
        trimmed down to EVENT_LOG_CAP, keeping the most recent."""
        prune = self._prune_fn()
        count = EVENT_LOG_CAP + 250
        events = [
            {"type": "evt", "i": i, "time": (_FIXED_NOW - timedelta(minutes=count - i)).isoformat()}
            for i in range(count)
        ]
        result = prune(events, _FIXED_NOW)

        assert len(result) == EVENT_LOG_CAP
        kept_i = [e["i"] for e in result]
        assert min(kept_i) == count - EVENT_LOG_CAP
        assert max(kept_i) == count - 1

    def test_restore_and_emit_share_identical_prune_function(self):
        """Both the live-emit path (_emit_event) and the restore path
        (async_restore_state) call the shared module-level _prune_event_log —
        no divergent re-cap logic in either path."""
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        calls: list[str] = []
        real_prune = mod._prune_event_log

        def _tracking_prune(event_log, now):
            calls.append("called")
            return real_prune(event_log, now)

        # ── live emit path ──────────────────────────────────────────────
        coord = _make_minimal_coordinator()
        dt_mock = _make_dt_mock()
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            patch("custom_components.climate_advisor.coordinator._prune_event_log", _tracking_prune),
        ):
            coord._emit_event("test_evt", {})
        assert calls == ["called"]

        # ── restore path ────────────────────────────────────────────────
        # async_restore_state() calls _prune_event_log directly on the restored
        # log, then _emit_event() (appending the system_restarted marker) calls
        # it again — both calls route through the same shared function.
        calls.clear()
        restore_coord = _make_restore_coordinator()
        state = {"date": _TODAY_STR, "event_log": [{"type": "x", "time": "t"}]}
        with patch("custom_components.climate_advisor.coordinator._prune_event_log", _tracking_prune):
            _run_restore(restore_coord, state_data=state)
        assert calls == ["called", "called"]

    def test_cross_layer_12h_window_after_restore(self):
        """A restored log seeded with >500 events spanning >12h (mixed ages) still
        returns everything actually within a 12h window when read back — the old
        count-only cap could silently evict hours of recent history on a busy day."""
        coord = _make_restore_coordinator()

        # 800 events spread evenly across the last 20 hours (well over the old
        # 500-count cap, and spanning more than the 12h dashboard window).
        total = 800
        span_hours = 20
        events = []
        for i in range(total):
            offset = timedelta(hours=span_hours) - timedelta(hours=span_hours * i / total)
            events.append({"type": "evt", "i": i, "time": (_FIXED_NOW - offset).isoformat()})
        state = {"date": _TODAY_STR, "event_log": events}

        _run_restore(coord, state_data=state)

        # Expected: every seeded event actually within the last 12h of _FIXED_NOW.
        window_cutoff = (_FIXED_NOW - timedelta(hours=12)).isoformat()
        expected_in_window = [e for e in events if e["time"] >= window_cutoff]

        actual_in_window = [e for e in coord._event_log if e.get("type") == "evt" and e["time"] >= window_cutoff]

        assert len(actual_in_window) == len(expected_in_window)


class TestEventSourceLabel:
    """_event_source_label() returns correct source strings."""

    def test_system_restarted_source_label(self):
        """system_restarted events are labelled as 'system'."""
        from custom_components.climate_advisor.ai_skills_context import (
            _event_source_label,
        )

        result = _event_source_label("system_restarted", {})

        assert result == "system"
