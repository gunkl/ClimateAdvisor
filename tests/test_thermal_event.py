"""Tests for the coordinator thermal event state machine (Issue #114).

Covers the PendingThermalEvent lifecycle:
  [pre-heat buffer] → [active] → [post_heat] → [stabilized] → commit
                                             ↘ [abandoned]

Tests bind the real coordinator thermal methods to a minimal stub object
so the physics pipeline runs without a full HA environment.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Give the HA dt stub a real parse_datetime so coordinator's local dt_util2 imports work.
# The coordinator does `from homeassistant.util import dt as dt_util2` locally inside
# _end_active_phase and _check_stabilization. This import resolves to
# sys.modules["homeassistant.util"].dt (an auto-generated MagicMock attribute on the
# "homeassistant.util" MagicMock), NOT sys.modules["homeassistant.util.dt"]. Without a
# real parse_datetime on the right object, arithmetic (now - parsed_dt) silently returns
# a MagicMock instead of a timedelta.
_ha_util = sys.modules.get("homeassistant.util")
if _ha_util is not None:
    _ha_util.dt.parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _parse_datetime_real(s: str) -> datetime | None:
    """Real datetime parser for use in dt_util mock."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime = _FAKE_NOW):
    """Build a dt_util mock with real now() and parse_datetime() behaviour."""
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    return mock_dt


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_thermal_coord(*, learning_enabled: bool = True, indoor_temp: float = 68.0):
    """Build a minimal coordinator stub with real thermal pipeline methods bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()

    # async_add_executor_job calls its first arg synchronously and returns result
    async def _exec_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec_job
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "learning_enabled": learning_enabled,
    }

    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []

    learning = MagicMock()
    learning.set_pending_thermal_event = MagicMock()
    learning.save_state = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value=(None, None, None))
    coord.learning = learning

    coord._today_record = MagicMock()
    coord._today_record.thermal_session_count = 0

    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._async_save_state = AsyncMock()

    # _get_current_sample needs weather state
    weather_state = MagicMock()
    weather_state.attributes = {"temperature": 45.0}
    coord.hass.states.get = MagicMock(return_value=weather_state)

    # Bind real thermal methods
    for method_name in (
        "_start_thermal_event",
        "_sample_thermal_event",
        "_end_active_phase",
        "_check_stabilization",
        "_commit_thermal_event",
        "_abandon_thermal_event",
        "_update_pre_heat_buffer",
        "_get_current_sample",
        "_get_outdoor_temp",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    return coord


# ---------------------------------------------------------------------------
# TestStartThermalEvent
# ---------------------------------------------------------------------------


class TestStartThermalEvent:
    """_start_thermal_event() creates a correctly structured active event."""

    def test_start_creates_active_event_heat(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        assert coord._pending_thermal_event is not None
        assert coord._pending_thermal_event["status"] == "active"
        assert coord._pending_thermal_event["session_mode"] == "heat"
        assert coord._pending_thermal_event["hvac_mode"] == "heat"

    def test_start_creates_active_event_cool(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("cool"))
        assert coord._pending_thermal_event["session_mode"] == "cool"

    def test_start_fan_only_creates_fan_only_event(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("fan_only"))
        assert coord._pending_thermal_event["session_mode"] == "fan_only"

    def test_start_calls_set_pending_and_save(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        coord.learning.set_pending_thermal_event.assert_called()
        coord.learning.save_state.assert_called()

    def test_start_snapshots_pre_heat_buffer(self):
        coord = _make_thermal_coord()
        # Seed pre-heat buffer with 3 samples
        for i in range(3):
            coord._pre_heat_sample_buffer.append(
                {
                    "timestamp": f"2026-04-19T11:5{i}:00+00:00",
                    "indoor_temp_f": 67.0,
                    "outdoor_temp_f": 45.0,
                    "elapsed_minutes": 0.0,
                }
            )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        assert len(coord._pending_thermal_event["pre_heat_samples"]) == 3

    def test_start_abandons_existing_event_first(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            first_id = coord._pending_thermal_event["event_id"]
            asyncio.run(coord._start_thermal_event("cool"))
        # New event was started
        assert coord._pending_thermal_event["event_id"] != first_id
        assert coord._pending_thermal_event["session_mode"] == "cool"

    def test_start_skipped_when_learning_disabled(self):
        coord = _make_thermal_coord(learning_enabled=False)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        assert coord._pending_thermal_event is None
        coord.learning.set_pending_thermal_event.assert_not_called()


# ---------------------------------------------------------------------------
# TestSampleThermalEvent
# ---------------------------------------------------------------------------


class TestSampleThermalEvent:
    """_sample_thermal_event() appends samples to the right list."""

    def test_sample_appends_to_active_list(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            initial_count = len(coord._pending_thermal_event["active_samples"])
            coord._sample_thermal_event()
        assert len(coord._pending_thermal_event["active_samples"]) == initial_count + 1

    def test_sample_appends_to_post_heat_list(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())
            assert coord._pending_thermal_event["status"] == "post_heat"
            coord._sample_thermal_event()
        assert len(coord._pending_thermal_event["post_heat_samples"]) == 1

    def test_sample_does_nothing_without_event(self):
        coord = _make_thermal_coord()
        # No event → no crash
        coord._sample_thermal_event()

    def test_sample_updates_peak_indoor(self):
        coord = _make_thermal_coord(indoor_temp=70.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        # Now simulate temp rising to 72°F
        coord._get_indoor_temp = MagicMock(return_value=72.0)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_thermal_event()
        assert coord._pending_thermal_event["peak_indoor_f"] == pytest.approx(72.0)


# ---------------------------------------------------------------------------
# TestEndActivePhase
# ---------------------------------------------------------------------------


class TestEndActivePhase:
    """_end_active_phase() transitions active → post_heat."""

    def test_end_active_transitions_to_post_heat(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())
        assert coord._pending_thermal_event["status"] == "post_heat"
        assert coord._pending_thermal_event["active_end"] is not None

    def test_end_active_sets_session_minutes(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())
        # session_minutes should be 0.0 since now() is mocked to the same value
        assert coord._pending_thermal_event["session_minutes"] is not None
        assert coord._pending_thermal_event["session_minutes"] >= 0.0

    def test_end_active_saves_pending_event(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            coord.learning.save_state.reset_mock()
            asyncio.run(coord._end_active_phase())
        coord.learning.save_state.assert_called()

    def test_end_active_noop_when_no_event(self):
        coord = _make_thermal_coord()
        # No crash when no event
        asyncio.run(coord._end_active_phase())

    def test_fan_after_heat_ends_active_phase(self):
        """hvac_action=fan while hvac_mode=heat triggers _end_active_phase, not a new event."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            event_id_before = coord._pending_thermal_event["event_id"]
            asyncio.run(coord._end_active_phase())
        # Same event, now in post_heat
        assert coord._pending_thermal_event["event_id"] == event_id_before
        assert coord._pending_thermal_event["status"] == "post_heat"


# ---------------------------------------------------------------------------
# TestStabilization
# ---------------------------------------------------------------------------


class TestStabilization:
    """_check_stabilization() commits on stable temps or abandons on timeout."""

    def _make_stable_post_samples(self, count: int, base_temp: float = 68.0):
        """Build post_heat samples all within 0.1°F of base_temp."""
        samples = []
        # All samples timestamped to within the stabilization window (last 5 min)
        # Space them 20 seconds apart (all within 5-minute window for count <= 15)
        for i in range(count):
            total_seconds = i * 20
            ts = datetime(2026, 4, 19, 12, total_seconds // 60, total_seconds % 60, tzinfo=UTC).isoformat()
            samples.append(
                {
                    "timestamp": ts,
                    "indoor_temp_f": base_temp + (0.05 * (i % 2)),  # alternates +0 / +0.05
                    "outdoor_temp_f": 45.0,
                    "elapsed_minutes": float(i),
                }
            )
        return samples

    def test_stabilization_commits_observation(self):
        """Post-heat samples within threshold → event committed."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 30, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        # Inject 12 stable post_heat samples within the stabilization window
        stable_samples = self._make_stable_post_samples(12)
        coord._pending_thermal_event["post_heat_samples"] = stable_samples
        # Simulate: active_end is "now" so elapsed_post is small (no timeout)
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()

        # check_stabilization: now=12:00:30, samples have ts up to 12:00:10 → within 5-min window
        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        # Event should have been committed (pending cleared)
        assert coord._pending_thermal_event is None

    def test_timeout_abandons_event(self):
        """Post-heat timeout exceeded → event abandoned."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        # active_end 46 minutes ago (exceeds THERMAL_POST_HEAT_TIMEOUT_MINUTES=45)
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 11, 14, 0, tzinfo=UTC).isoformat()

        dt_now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
        dt_mock2 = _make_dt_mock(dt_now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        assert coord._pending_thermal_event is None

    def test_too_few_post_samples_does_not_commit(self):
        """Below THERMAL_MIN_POST_HEAT_SAMPLES (10) → no commit attempt."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        # Only 5 post samples
        coord._pending_thermal_event["post_heat_samples"] = self._make_stable_post_samples(5)
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()

        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        # Event should still be active (not committed)
        assert coord._pending_thermal_event is not None
        assert coord._pending_thermal_event["status"] == "post_heat"


# ---------------------------------------------------------------------------
# TestAbandonThermalEvent
# ---------------------------------------------------------------------------


class TestAbandonThermalEvent:
    """_abandon_thermal_event() clears event and notifies learning."""

    def test_abandon_clears_pending_event(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        assert coord._pending_thermal_event is not None

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._abandon_thermal_event("test reason"))

        assert coord._pending_thermal_event is None

    def test_abandon_calls_set_pending_none(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            coord.learning.set_pending_thermal_event.reset_mock()
            asyncio.run(coord._abandon_thermal_event("test"))

        coord.learning.set_pending_thermal_event.assert_called_once_with(None)

    def test_hvac_restart_abandons_and_starts_new(self):
        """Starting a new session mid-post_heat abandons old and creates fresh event."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())
            old_id = coord._pending_thermal_event["event_id"]
            # New session starts — _start_thermal_event abandons old first
            asyncio.run(coord._start_thermal_event("cool"))

        assert coord._pending_thermal_event is not None
        assert coord._pending_thermal_event["event_id"] != old_id
        assert coord._pending_thermal_event["session_mode"] == "cool"
        assert coord._pending_thermal_event["status"] == "active"


# ---------------------------------------------------------------------------
# TestPreHeatBuffer
# ---------------------------------------------------------------------------


class TestPreHeatBuffer:
    """_update_pre_heat_buffer() maintains a rolling 15-sample buffer."""

    def test_pre_heat_buffer_populated(self):
        """When no active event, samples are appended to the buffer."""
        coord = _make_thermal_coord()
        assert coord._pending_thermal_event is None

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            for _ in range(5):
                coord._update_pre_heat_buffer()

        assert len(coord._pre_heat_sample_buffer) == 5

    def test_pre_heat_buffer_rolls_at_cap(self):
        """Buffer is capped at 15 samples; oldest entries are dropped."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            for _ in range(20):
                coord._update_pre_heat_buffer()

        assert len(coord._pre_heat_sample_buffer) <= 15

    def test_pre_heat_buffer_not_updated_when_event_active(self):
        """Buffer is frozen once a thermal event is active."""
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            for _ in range(3):
                coord._update_pre_heat_buffer()
            asyncio.run(coord._start_thermal_event("heat"))
            # Buffer updates must stop now
            count_before = len(coord._pre_heat_sample_buffer)
            for _ in range(3):
                coord._update_pre_heat_buffer()

        assert len(coord._pre_heat_sample_buffer) == count_before

    def test_start_event_snapshots_buffer_into_pre_heat_samples(self):
        """Buffer contents become event.pre_heat_samples on session start."""
        coord = _make_thermal_coord()
        # Manually inject buffer entries with real timestamps
        for i in range(10):
            ts = datetime(2026, 4, 19, 11, 55 + i // 60, i % 60, tzinfo=UTC)
            coord._pre_heat_sample_buffer.append(
                {
                    "timestamp": ts.isoformat(),
                    "indoor_temp_f": 67.0,
                    "outdoor_temp_f": 45.0,
                    "elapsed_minutes": 0.0,
                }
            )

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))

        assert len(coord._pending_thermal_event["pre_heat_samples"]) == 10


# ---------------------------------------------------------------------------
# TestCommitThermalEvent
# ---------------------------------------------------------------------------


class TestCommitThermalEvent:
    """_commit_thermal_event() calls _commit_event_from_dict and clears state."""

    def test_commit_calls_learning(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._commit_thermal_event())

        coord.learning._commit_event_from_dict.assert_called_once()
        assert coord._pending_thermal_event is None

    def test_commit_clears_pending_event(self):
        coord = _make_thermal_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
        coord.learning._commit_event_from_dict.return_value = ({"hvac_mode": "heat", "k_active": 3.0}, None, None)
        asyncio.run(coord._commit_thermal_event())

        assert coord._pending_thermal_event is None
        coord.learning.set_pending_thermal_event.assert_called_with(None)

    def test_commit_increments_session_count_on_success(self):
        coord = _make_thermal_coord()
        coord.learning._commit_event_from_dict.return_value = ({"hvac_mode": "heat"}, None, None)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._commit_thermal_event())

        assert coord._today_record.thermal_session_count == 1

    def test_commit_skipped_when_learning_disabled(self):
        coord = _make_thermal_coord(learning_enabled=False)
        coord._pending_thermal_event = {"event_id": "x", "status": "stabilized"}
        asyncio.run(coord._commit_thermal_event())

        coord.learning._commit_event_from_dict.assert_not_called()
        assert coord._pending_thermal_event is None


# ---------------------------------------------------------------------------
# TestCheckStabilizationPlateauGuard
# ---------------------------------------------------------------------------


class TestCheckStabilizationPlateauGuard:
    """Plateau guard: stabilized events with < THERMAL_MIN_DECAY_F decay are abandoned."""

    def _make_stable_post_samples(self, count: int, base_temp: float = 68.0):
        """Build post_heat samples all within 0.1°F of base_temp."""
        samples = []
        for i in range(count):
            total_seconds = i * 20
            ts = datetime(2026, 4, 19, 12, total_seconds // 60, total_seconds % 60, tzinfo=UTC).isoformat()
            samples.append(
                {
                    "timestamp": ts,
                    "indoor_temp_f": base_temp + (0.05 * (i % 2)),
                    "outdoor_temp_f": 45.0,
                    "elapsed_minutes": float(i),
                }
            )
        return samples

    def test_plateau_guard_abandons_when_decay_below_min(self):
        """peak_indoor_f - end_indoor_f < THERMAL_MIN_DECAY_F → abandon, not commit."""
        coord = _make_thermal_coord()
        coord._async_save_state = AsyncMock()
        dt_mock = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 30, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        # Stable samples near base_temp (all within stabilization threshold)
        base_temp = 68.0
        stable_samples = self._make_stable_post_samples(12, base_temp=base_temp)
        coord._pending_thermal_event["post_heat_samples"] = stable_samples
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()
        # peak barely above end: decay = 0.4°F < THERMAL_MIN_DECAY_F (1.0)
        coord._pending_thermal_event["peak_indoor_f"] = base_temp + 0.4

        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        # Guard fired → event abandoned (pending cleared) and no commit to learning
        assert coord._pending_thermal_event is None
        coord.learning._commit_event_from_dict.assert_not_called()

    def test_plateau_guard_commits_when_decay_meets_min(self):
        """peak_indoor_f - end_indoor_f >= THERMAL_MIN_DECAY_F → commit proceeds normally."""
        coord = _make_thermal_coord()
        coord._async_save_state = AsyncMock()
        dt_mock = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 30, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        base_temp = 68.0
        stable_samples = self._make_stable_post_samples(12, base_temp=base_temp)
        coord._pending_thermal_event["post_heat_samples"] = stable_samples
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()
        # peak 2°F above end: decay = 2.0°F >= THERMAL_MIN_DECAY_F (1.0)
        coord._pending_thermal_event["peak_indoor_f"] = base_temp + 2.0

        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        # Guard passed → commit called, event cleared
        assert coord._pending_thermal_event is None
        coord.learning._commit_event_from_dict.assert_called_once()

    def test_plateau_guard_commits_when_decay_exactly_at_min(self):
        """Decay exactly at THERMAL_MIN_DECAY_F boundary → guard passes, commit proceeds."""
        coord = _make_thermal_coord()
        coord._async_save_state = AsyncMock()
        dt_mock = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 30, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        base_temp = 68.0
        stable_samples = self._make_stable_post_samples(12, base_temp=base_temp)
        coord._pending_thermal_event["post_heat_samples"] = stable_samples
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()
        # decay exactly at THERMAL_MIN_DECAY_F (1.0) — end_f = base_temp + 0.05 (last alternating
        # sample has i%2=1), so peak must be >= base_temp + 1.05 to yield decay >= 1.0.
        # Use base_temp + 1.06 → decay = 1.01°F ≥ 1.0 → guard passes, commit proceeds.
        coord._pending_thermal_event["peak_indoor_f"] = base_temp + 1.06

        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        assert coord._pending_thermal_event is None
        coord.learning._commit_event_from_dict.assert_called_once()

    def test_plateau_guard_skipped_when_peak_indoor_missing(self):
        """If peak_indoor_f is None (shouldn't happen), guard is skipped and commit proceeds."""
        coord = _make_thermal_coord()
        coord._async_save_state = AsyncMock()
        dt_mock = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 30, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_thermal_event("heat"))
            asyncio.run(coord._end_active_phase())

        base_temp = 68.0
        stable_samples = self._make_stable_post_samples(12, base_temp=base_temp)
        coord._pending_thermal_event["post_heat_samples"] = stable_samples
        coord._pending_thermal_event["active_end"] = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC).isoformat()
        # Explicitly null out peak_indoor_f
        coord._pending_thermal_event["peak_indoor_f"] = None

        dt_mock2 = _make_dt_mock(datetime(2026, 4, 19, 12, 2, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock2):
            asyncio.run(coord._check_stabilization())

        # Guard condition not met (peak_f is None) → commit proceeds
        assert coord._pending_thermal_event is None
        coord.learning._commit_event_from_dict.assert_called_once()
