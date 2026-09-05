"""Tests for the Issue #587 vent-split trigger/abort behavior (coordinator.py).

Covers:
  1. vent_window_decay triggers when sensor open + fan off
  2. vent_fan_decay triggers when sensor open + fan on
  3. Fan-state toggle mid-window commits (not discards) and lets the sibling retrigger
  4. A toggle with insufficient accumulated samples falls through to abandon
  5. fan_only_decay (the old fan-on/windows-closed regime) no longer triggers at all —
     regression confirming clean retirement (not silently rerouted to vent_fan_decay)

Reuses the fixture-coordinator harness pattern from tests/test_thermal_observations.py
(``_make_obs_coord``) — a minimal partially-instantiated coordinator with the real
observation-pipeline methods bound via ``types.MethodType``.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_ha_util = sys.modules.get("homeassistant.util")
if _ha_util is not None:
    _ha_util.dt.parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

# ---------------------------------------------------------------------------
# Imports after stubs are in place
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENT_FAN_DECAY,
    OBS_TYPE_VENT_WINDOW_DECAY,
    THERMAL_MIN_DECAY_SAMPLES,
    THERMAL_ROLLING_MIN_DELTA_T_F,
    THERMAL_ROLLING_MIN_WINDOW_MINUTES,
    THERMAL_VENTILATED_MIN_DELTA_F,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_thermal_observations.py::_make_obs_coord)
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _parse_datetime_real(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime = _FAKE_NOW):
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    return mock_dt


def _get_coordinator_class():
    """Import coordinator freshly to avoid stale module references."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_obs_coord(
    *,
    indoor_temp: float = 75.0,
    outdoor_temp: float = 55.0,
    hvac_action: str = "idle",
    fan_active: bool = False,
    nat_vent_active: bool = False,
    any_sensor_open: bool = False,
    learning_enabled: bool = True,
):
    """Build a minimal coordinator stub with the v3 observation methods bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    def _exec_job(fn, *args):
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return fn(*args)
        fut = loop.create_future()
        fut.set_result(fn(*args))
        return fut

    hass.async_add_executor_job = _exec_job

    climate_state = MagicMock()
    climate_state.state = "heat" if hvac_action in ("heating",) else "idle"
    climate_state.attributes = {"hvac_action": hvac_action}
    weather_state = MagicMock()
    weather_state.attributes = {"temperature": outdoor_temp}

    def _states_get(entity_id: str):
        if "climate" in entity_id:
            return climate_state
        if "weather" in entity_id:
            return weather_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "learning_enabled": learning_enabled,
    }

    ae = MagicMock()
    ae._fan_active = fan_active
    ae._natural_vent_active = nat_vent_active
    coord.automation_engine = ae

    learning = MagicMock()
    learning.set_pending_thermal_event = MagicMock()
    learning.save_state = MagicMock()
    learning.record_thermal_observation = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value={"hvac_mode": "heat"})
    coord.learning = learning

    coord._pending_observations = {}
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._last_outdoor_temp = outdoor_temp
    coord._chart_log = None  # no chart_log wired — _run_vent_*_chart_log_fit no-ops cleanly

    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._any_sensor_open = MagicMock(return_value=any_sensor_open)
    coord._async_save_state = AsyncMock()

    def _get_current_sample(elapsed: float) -> dict:
        return {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": elapsed,
        }

    coord._get_current_sample = _get_current_sample

    for method_name in (
        "_ensure_pending_observations",
        "_start_hvac_observation",
        "_start_decay_observation",
        "_sample_all_observations",
        "_evaluate_vent_split_observation",
        "_evaluate_rolling_window",
        "_commit_rolling_window_obs",
        "_abandon_observation",
        "_commit_observation_if_sufficient",
        "_commit_observation",
        "_check_hvac_stabilization",
        "_end_hvac_active_phase",
        "_run_vent_window_chart_log_fit",
        "_run_vent_fan_chart_log_fit",
        "_run_vent_chart_log_fit_impl",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    return coord


def _make_stale_obs(obs_type: str, n_samples: int, indoor_temp: float, outdoor_temp: float, minutes_ago: int) -> dict:
    """Build a monitoring observation dict that started ``minutes_ago`` minutes before _FAKE_NOW."""
    from datetime import timedelta

    start = _FAKE_NOW - timedelta(minutes=minutes_ago)
    samples = [
        {
            "timestamp": start.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": float(i),
        }
        for i in range(n_samples)
    ]
    return {
        "obs_type": obs_type,
        "obs_id": "test-vent-split-1",
        "start_time": start.isoformat(),
        "status": "monitoring",
        "samples": samples,
        "flags_at_start": {},
        "schema_version": 1,
    }


# ---------------------------------------------------------------------------
# 1-2: trigger split
# ---------------------------------------------------------------------------


class TestVentSplitTrigger:
    def test_vent_window_triggers_when_sensor_open_fan_off(self):
        """vent_window_decay starts: sensor open, fan off, HVAC idle, delta >= threshold."""
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=70.0,  # delta = 2.0 >= THERMAL_VENTILATED_MIN_DELTA_F (1.0)
            fan_active=False,
            any_sensor_open=True,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert OBS_TYPE_VENT_WINDOW_DECAY in coord._pending_observations, (
            "vent_window_decay should start when sensor open, fan off, delta >= threshold"
        )
        assert OBS_TYPE_VENT_FAN_DECAY not in coord._pending_observations, (
            "vent_fan_decay must not start when fan is off"
        )

    def test_vent_fan_triggers_when_sensor_open_fan_on(self):
        """vent_fan_decay starts: sensor open, fan on, HVAC idle, delta >= threshold."""
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=70.0,
            fan_active=True,
            any_sensor_open=True,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert OBS_TYPE_VENT_FAN_DECAY in coord._pending_observations, (
            "vent_fan_decay should start when sensor open, fan on, delta >= threshold"
        )
        assert OBS_TYPE_VENT_WINDOW_DECAY not in coord._pending_observations, (
            "vent_window_decay must not start when fan is on"
        )

    def test_fan_only_decay_no_longer_triggers(self):
        """Regression: fan-on + windows-closed no longer starts ANY decay observation.

        Confirms clean retirement — fan_only_decay is not accidentally rerouted to
        vent_fan_decay (which requires windows OPEN, not closed).
        """
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
            any_sensor_open=False,  # windows closed — the old fan_only_decay regime
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert OBS_TYPE_VENT_WINDOW_DECAY not in coord._pending_observations
        assert OBS_TYPE_VENT_FAN_DECAY not in coord._pending_observations
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations, (
            "passive_decay also must not start — fan is active"
        )
        assert OBS_TYPE_SOLAR_GAIN not in coord._pending_observations, "solar_gain also must not start — fan is active"
        assert coord._pending_observations == {}, (
            f"no observation should be pending at all; got {list(coord._pending_observations.keys())}"
        )


# ---------------------------------------------------------------------------
# 3-4: fan-state toggle mid-window
# ---------------------------------------------------------------------------


class TestVentSplitToggle:
    def test_fan_toggle_mid_window_commits_and_retriggers(self):
        """Fan activating mid-vent_window_decay commits accumulated samples (not discard),
        and the same poll cycle starts vent_fan_decay to continue under the new regime.
        """
        # Enough samples + enough indoor movement to satisfy _evaluate_rolling_window's
        # min-window/signal gates and _commit_observation_if_sufficient's min_samples,
        # so the toggle path actually reaches "commit" rather than being swallowed by
        # the shared rolling-window early-return.
        n = 25
        indoor_start, indoor_end, outdoor = 72.0, 74.0, 70.0
        step = (indoor_end - indoor_start) / (n - 1)
        samples = [
            {
                "timestamp": (_FAKE_NOW).isoformat(),
                "indoor_temp_f": round(indoor_start + i * step, 2),
                "outdoor_temp_f": outdoor,
                "elapsed_minutes": float(i * 2),
            }
            for i in range(n)
        ]
        obs = {
            "obs_type": OBS_TYPE_VENT_WINDOW_DECAY,
            "obs_id": "toggle-1",
            "start_time": (_FAKE_NOW).isoformat(),
            "status": "monitoring",
            "samples": samples,
            "flags_at_start": {},
            "schema_version": 1,
        }
        # Started > THERMAL_ROLLING_MIN_WINDOW_MINUTES ago so _evaluate_rolling_window's
        # "too early, no signal" branch does not swallow the toggle before it's checked.
        from datetime import timedelta

        start_ts = _FAKE_NOW - timedelta(minutes=THERMAL_ROLLING_MIN_WINDOW_MINUTES + 5)
        obs["start_time"] = start_ts.isoformat()

        coord = _make_obs_coord(
            indoor_temp=indoor_end,
            outdoor_temp=outdoor,
            fan_active=True,  # fan just activated — toggled from the window's fan_off state
            any_sensor_open=True,
            hvac_action="idle",
        )
        coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] = obs

        committed_types: list[str] = []

        def _fake_async_create_task(coro):
            name = getattr(coro, "__name__", getattr(coro, "__qualname__", ""))
            if "_commit_observation" in name:
                committed_types.append(OBS_TYPE_VENT_WINDOW_DECAY)
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # vent_window_decay must have been committed (queued) or already popped as
        # "committing" — either way, not silently discarded.
        vw_obs = coord._pending_observations.get(OBS_TYPE_VENT_WINDOW_DECAY)
        was_committing = vw_obs is not None and vw_obs.get("status") == "committing"
        was_queued = len(committed_types) > 0
        assert was_committing or was_queued, (
            "vent_window_decay should commit (not discard) accumulated samples on fan-state toggle"
        )

        # vent_fan_decay should start on the SAME poll cycle to continue the observation
        # under the new (fan-on) regime.
        assert OBS_TYPE_VENT_FAN_DECAY in coord._pending_observations, (
            "vent_fan_decay should start in the same cycle the fan toggled on"
        )

    def test_fan_toggle_with_insufficient_samples_falls_through_to_abandon(self):
        """A toggle with too few accumulated samples abandons rather than commits.

        _commit_observation_if_sufficient already falls back to _abandon_observation
        internally when the accumulated count is below minimum — this test confirms
        that fallback fires (not a silent no-op) for the vent-split toggle path.
        """
        # Below THERMAL_MIN_DECAY_SAMPLES + 1, and started long enough ago that
        # _evaluate_rolling_window's max-window hard cap independently would also
        # abandon it — pick a duration that reaches the toggle branch first.
        obs = _make_stale_obs(
            OBS_TYPE_VENT_WINDOW_DECAY,
            n_samples=2,
            indoor_temp=72.0,
            outdoor_temp=70.0,
            minutes_ago=THERMAL_ROLLING_MIN_WINDOW_MINUTES + 5,
        )
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=70.0,
            fan_active=True,  # toggled on
            any_sensor_open=True,
            hvac_action="idle",
        )
        coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] = obs

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert OBS_TYPE_VENT_WINDOW_DECAY not in coord._pending_observations, (
            "insufficient-sample toggle must not leave the stale obs pending"
        )


# Sanity checks that the constants used in this module still hold their documented values
# (keeps this file's assumptions falsifiable against const.py drift).
def test_constants_sanity():
    assert THERMAL_VENTILATED_MIN_DELTA_F == 1.0
    assert THERMAL_MIN_DECAY_SAMPLES == 4
    assert THERMAL_ROLLING_MIN_DELTA_T_F == 0.2
