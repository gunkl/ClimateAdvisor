"""Issues #1355-#1360 (batch with #1349): what survives a restart on a later calendar day.

Occupant impact (all tests): Home Assistant went down before midnight and came back after it.
The rule of the batch: the date gate in ``async_restore_state()`` guards only data whose meaning
is "today"; settings, history, run-once flags and the audit trail come back on any restart.
These tests drive the real ``async_restore_state()``.

``async_setup()`` (#1354 occupancy sync) is not bound here: it wires dozens of listeners and timers, so
binding it on a stub would test the stub; the ``tests_ha/`` tier covers it with a real Home Assistant.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import logging
import sys
import textwrap
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import VERSION  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import LearningState  # noqa: E402

_NOW = datetime(2026, 11, 6, 0, 35, 0, tzinfo=UTC)
_TODAY = "2026-11-06"
_YESTERDAY = "2026-11-05"
_FOUR_DAYS = "2026-11-02"
_CROSS_DAY = pytest.mark.parametrize(
    "saved_date",
    [pytest.param(_YESTERDAY, id="yesterday"), pytest.param(_FOUR_DAYS, id="four_days_old")],
)
_COORD_LOGGER = "custom_components.climate_advisor.coordinator"

_BACKFILL_FLAGS = (
    "passive_k_backfilled",
    "vent_window_k_backfilled",
    "vent_fan_k_backfilled",
    "passive_k_backfill_v2",
    "vent_window_k_backfill_v2",
    "vent_fan_k_backfill_v2",
    "solar_phase_backfill",
    "solar_phase_ac_backfill",
)

# Structural guard (see TestEveryStateKeyIsClassified): a new persisted key must be put in one of the two sets.
ANY_RESTART_KEYS = frozenset(
    {
        "kept_forecast",
        "automation_state",
        "automation_enabled",
        "ai_stats",
        "pred_archive",
        *_BACKFILL_FLAGS,
        "last_solar_phase_fit_date",
        "event_log",
        "promise_stats",
        "last_update_error",
        "last_update_error_time",
        "consecutive_failure_count",
        "sensor_last_real_reading",
    }
)
SAME_DAY_KEYS = frozenset(
    {
        "classification",
        "classification_fetched_at",
        "temp_history",
        "today_record",
        "briefing_state",
        "occupancy_mode",
        "occupancy_away_since",
    }
)
_META_KEYS = frozenset({"date", "last_saved", "temp_unit", "version"})


def _epoch(delta: timedelta) -> int:
    return int((_NOW - delta).timestamp())


def _iso(delta: timedelta) -> str:
    return (_NOW - delta).isoformat()


def _coord(persisted_state: dict, *, claude_client=None, config=None):
    """Coordinator stub with the real async_restore_state() / _classify_restart_and_mark() / _emit_event() bound."""
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.is_running = False
    coord._executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    coord.config = config if config is not None else {"climate_entity": "climate.thermostat"}
    coord.learning = MagicMock()
    coord.learning._state = LearningState()
    coord.claude_client = claude_client
    coord._ai_client_config = None  # a MagicMock attribute would otherwise build a real API client
    coord._load_investigation_reports = MagicMock()
    coord._rejection_log = {}
    coord._state_persistence = MagicMock()
    coord._state_persistence.load = MagicMock(return_value=persisted_state)
    coord._event_log = []
    coord._nat_vent_was_active = False
    coord._get_indoor_temp = MagicMock(return_value=70.0)
    coord.last_update_error = None
    coord.last_update_error_time = None
    coord.consecutive_failure_count = 0
    coord._automation_enabled = True
    coord.automation_engine = MagicMock()
    coord.automation_engine.dry_run = False
    coord.automation_engine._natural_vent_active = False
    # Sentinels for the same-day-only fields: "not restored" means the sentinel is still there.
    coord._current_classification = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._today_record = None
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    for flag in _BACKFILL_FLAGS:
        setattr(coord, f"_{flag}", False)
    coord._last_solar_phase_fit_date = None
    coord._pred_archive = {}
    coord._daily_forecast_full = []
    coord._hourly_forecast_temps = []
    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)
    coord._classify_restart_and_mark = types.MethodType(ClimateAdvisorCoordinator._classify_restart_and_mark, coord)
    coord._emit_event = types.MethodType(ClimateAdvisorCoordinator._emit_event, coord)
    return coord


def _restore(state: dict, **kw):
    coord = _coord(state, **kw)
    with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
        asyncio.run(coord.async_restore_state())
    return coord


def _types(coord) -> list[str]:
    return [e["type"] for e in coord._event_log]


def _event(coord, event_type: str) -> dict:
    return next(e for e in coord._event_log if e["type"] == event_type)


def _base(saved_date: str, **extra) -> dict:
    return {"date": saved_date, "last_saved": f"{saved_date}T23:30:00+00:00", **extra}


_RECORD = {"date": _YESTERDAY, "day_type": "warm", "trend_direction": "stable"}


class TestAutomationStateRestored1358:
    @_CROSS_DAY
    def test_engine_restore_state_called_with_saved_state(self, saved_date):
        """Occupant impact: a fan or natural-ventilation hint the home was running on survives an overnight
        outage instead of the engine starting blind (and re-deciding the fan) after the clock rolled over."""
        auto = {"fan_active": True, "fan_on_since": "2026-11-05T22:00:00+00:00"}
        coord = _restore(_base(saved_date, automation_state=auto))
        coord.automation_engine.restore_state.assert_called_once_with(auto)

    def test_empty_automation_state_is_not_restored(self):
        """Occupant impact: nothing to restore means the engine stays on its clean-slate defaults."""
        coord = _restore(_base(_YESTERDAY, automation_state={}))
        coord.automation_engine.restore_state.assert_not_called()


class TestBackfillFlags1355:
    @_CROSS_DAY
    def test_all_eight_backfill_flags_and_fit_date_survive(self, saved_date):
        """Occupant impact: the one-time chart-log backfills do not run again after an overnight restart, so the
        thermal model is not fed the same observations twice (duplicates skew the learned rates)."""
        state = _base(saved_date, last_solar_phase_fit_date="2026-11-04", **dict.fromkeys(_BACKFILL_FLAGS, True))
        coord = _restore(state)
        for flag in _BACKFILL_FLAGS:
            assert getattr(coord, f"_{flag}") is True, flag
        assert coord._last_solar_phase_fit_date.isoformat() == "2026-11-04"

    def test_missing_flags_default_to_false(self):
        """Occupant impact: an old state file without the flags still lets the backfill run once."""
        coord = _restore(_base(_YESTERDAY))
        for flag in _BACKFILL_FLAGS:
            assert getattr(coord, f"_{flag}") is False, flag
        assert coord._last_solar_phase_fit_date is None


class TestKeptForecastAndPredArchive1359:
    @_CROSS_DAY
    def test_kept_forecast_daily_kept_and_past_hours_pruned(self, saved_date):
        """Occupant impact: if the weather service is still down after the restart, planning keeps the last good
        forecast; hours that are already over are not used as if they were still ahead."""
        daily = [{"datetime": "2026-11-06", "temperature": 60, "templow": 44}]
        hourly = [
            {"datetime": _iso(timedelta(hours=10)), "temperature": 50.0},  # long past
            {"datetime": _iso(timedelta(minutes=30)), "temperature": 52.0},  # within the 1 h grace
            {"datetime": (_NOW + timedelta(hours=3)).isoformat(), "temperature": 58.0},
        ]
        coord = _restore(_base(saved_date, kept_forecast={"daily": daily, "hourly": hourly}))
        assert coord._daily_forecast_full == daily
        assert [e["temperature"] for e in coord._hourly_forecast_temps] == [52.0, 58.0]
        assert coord.automation_engine._hourly_forecast_temps == coord._hourly_forecast_temps

    @_CROSS_DAY
    def test_pred_archive_kept_and_week_old_entries_dropped(self, saved_date):
        """Occupant impact: the prediction-accuracy history is not wiped by a restart, yet entries past the
        7-day expiry do not come back to be judged against."""
        recent = _epoch(timedelta(hours=5))
        old = _epoch(timedelta(days=8))
        archive = {str(recent): 70.5, str(old): 66.0, "junk": 1.0}
        coord = _restore(_base(saved_date, pred_archive=archive))
        assert coord._pred_archive == {recent: 70.5}

    @_CROSS_DAY
    def test_unit_change_drops_forecast_and_archive(self, saved_date):
        """Occupant impact: after switching F to C no forecast or prediction in the old unit is read as the new."""
        state = _base(
            saved_date,
            temp_unit="celsius",
            kept_forecast={"daily": [{"datetime": "2026-11-06", "temperature": 60}], "hourly": []},
            pred_archive={str(_epoch(timedelta(hours=5))): 70.5},
        )
        coord = _restore(state, config={"climate_entity": "c", "temp_unit": "fahrenheit"})
        assert coord._daily_forecast_full == []
        assert coord._pred_archive == {}


class TestEventLogAndRestartMarker1357:
    @_CROSS_DAY
    def test_event_log_kept_and_pruned_to_168_hours(self, saved_date):
        """Occupant impact: the activity report still shows what the system did before the outage, but not
        a month of stale rows."""
        fresh = {"time": _iso(timedelta(hours=3)), "type": "fresh"}
        stale = {"time": _iso(timedelta(hours=169)), "type": "stale"}
        coord = _restore(_base(saved_date, event_log=[stale, fresh]))
        assert "fresh" in _types(coord)
        assert "stale" not in _types(coord)

    @_CROSS_DAY
    def test_system_restarted_emitted_with_cause_and_recovered_events(self, saved_date):
        """Occupant impact: the activity report marks the restart (cause and how much history came back) even when
        the outage crossed midnight."""
        coord = _coord(_base(saved_date, event_log=[{"time": _iso(timedelta(hours=3)), "type": "fresh"}]))
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_version = VERSION
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        evt = _event(coord, "system_restarted")
        assert evt["cause"] == "user_restart"
        assert evt["recovered_events"] == 1
        assert _types(coord).index("fresh") < _types(coord).index("system_restarted")

    @_CROSS_DAY
    def test_shutdown_markers_reset(self, saved_date):
        """Occupant impact: a crash after this restart is reported as a crash, not as a stale clean shutdown."""
        coord = _coord(_base(saved_date))
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_kind = "unload"
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert coord.learning._state.clean_shutdown is False
        assert coord.learning._state.last_shutdown_kind is None

    def test_no_state_file_still_emits_system_restarted_and_resets_flags(self):
        """Occupant impact: a restart with no state file (first boot after a wipe, corrupt file) still shows up in
        the activity report, with zero recovered events."""
        coord = _coord({})
        coord.learning._state.clean_shutdown = True
        coord.learning._state.last_shutdown_kind = "unload"
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        evt = _event(coord, "system_restarted")
        assert evt["recovered_events"] == 0
        assert evt["cause"] == "reload"
        assert coord.learning._state.clean_shutdown is False
        assert coord.learning._state.last_shutdown_kind is None

    def test_version_changed_emitted_on_cross_day_restart(self):
        """Occupant impact: an update installed during the outage is still recorded as a version change."""
        coord = _coord(_base(_YESTERDAY))
        coord.learning._state.last_shutdown_version = "0.0.1"
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert "version_changed" in _types(coord)
        assert _event(coord, "system_restarted")["cause"] == "version_changed"


class TestInvestigationReports1356:
    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(_base(_YESTERDAY), id="yesterday"),
            pytest.param(_base(_FOUR_DAYS), id="four_days_old"),
            pytest.param(_base(_TODAY), id="same_day"),
            pytest.param({}, id="no_state"),
        ],
    )
    def test_reports_loaded_when_ai_is_on(self, state):
        """Occupant impact: the AI investigation history shown on the dashboard is not empty after an overnight
        restart or a missing state file."""
        coord = _restore(state, claude_client=MagicMock())
        coord._load_investigation_reports.assert_called_once_with()

    @pytest.mark.parametrize("state", [_base(_YESTERDAY), {}], ids=["cross_day", "no_state"])
    def test_reports_not_loaded_without_ai(self, state):
        """Occupant impact: an install without the AI subsystem never touches the reports file."""
        coord = _restore(state, claude_client=None)
        coord._load_investigation_reports.assert_not_called()


class TestUnitChangeAcrossDays1360:
    def test_yesterday_unit_change_clears_pending_and_emits_event_in_log(self, caplog):
        """Occupant impact: switching units during an overnight outage drops the old-unit thermal samples and the
        switch is visible in the activity report; the occupant's automation settings are untouched."""
        caplog.set_level(logging.WARNING, logger=_COORD_LOGGER)
        auto = {"fan_active": True}
        state = _base(
            _YESTERDAY,
            temp_unit="celsius",
            automation_enabled=False,
            automation_state=auto,
            event_log=[{"time": _iso(timedelta(hours=3)), "type": "earlier"}],
        )
        coord = _coord(state, config={"climate_entity": "c", "temp_unit": "fahrenheit"})
        coord.learning._state.pending_observations = {"obs1": {"x": 1}, "obs2": {"x": 2}}
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert coord.learning._state.pending_observations == {}
        evt = _event(coord, "unit_changed")  # in the restored log, i.e. emitted after the log restore
        assert evt["from"] == "celsius"
        assert evt["to"] == "fahrenheit"
        assert evt["dropped_pending_observations"] == 2
        assert _types(coord).index("earlier") < _types(coord).index("unit_changed")
        assert "Temperature unit changed" in caplog.text
        assert coord._automation_enabled is False
        coord.automation_engine.restore_state.assert_called_once_with(auto)


class TestSameDayOnlyFieldsStayGated:
    @staticmethod
    def _full_state(saved_date: str) -> dict:
        return _base(
            saved_date,
            classification={
                "day_type": "warm",
                "trend_direction": "stable",
                "trend_magnitude": 0,
                "today_high": 80.0,
                "today_low": 59.5,
                "tomorrow_high": 78.0,
                "tomorrow_low": 58.0,
            },
            classification_fetched_at=f"{saved_date}T08:00:00+00:00",
            temp_history={"outdoor": [[f"{saved_date}T08:00:00", 61.0]], "indoor": [[f"{saved_date}T08:00:00", 71.0]]},
            today_record=dict(_RECORD, date=saved_date),
            briefing_state={"sent_today": True, "last_text": "Good morning", "briefing_day_type": "warm"},
            occupancy_mode="vacation",
            occupancy_away_since=f"{saved_date}T09:00:00+00:00",
        )

    @_CROSS_DAY
    def test_today_data_is_not_restored_across_days(self, saved_date):
        """Occupant impact: today's plan starts fresh. Yesterday's day type, briefing, temperature trace and
        away/vacation mode are not presented as today's (the live toggles decide the mode at setup)."""
        coord = _restore(self._full_state(saved_date))
        assert coord._current_classification is None
        assert coord._outdoor_temp_history == []
        assert coord._indoor_temp_history == []
        assert coord._today_record is None
        assert coord._briefing_sent_today is False
        assert coord._last_briefing == ""
        assert coord._occupancy_mode == "home"
        assert coord._occupancy_away_since is None
        coord.automation_engine.set_occupancy_mode.assert_not_called()

    def test_yesterdays_record_goes_to_learning(self):
        """Occupant impact: yesterday's finished day still reaches the learning history instead of being lost."""
        coord = _restore(self._full_state(_YESTERDAY))
        coord.learning.record_day.assert_called_once()
        assert coord._today_record is None

    def test_info_line_says_history_restored_and_plan_starts_fresh(self, caplog):
        """Occupant impact: the log says plainly what came back and what starts over."""
        caplog.set_level(logging.INFO, logger=_COORD_LOGGER)
        _restore(self._full_state(_YESTERDAY))
        assert "settings and history restored; today's plan starts fresh" in caplog.text
        assert "Restoring same-day state" not in caplog.text

    def test_same_day_control_restores_everything(self):
        """Occupant impact (control): a restart within the same day still resumes today's plan exactly."""
        coord = _restore(self._full_state(_TODAY))
        assert coord._current_classification is not None
        assert coord._current_classification.day_type == "warm"
        assert len(coord._outdoor_temp_history) == 1
        assert len(coord._indoor_temp_history) == 1
        assert coord._today_record is not None
        assert coord._briefing_sent_today is True
        assert coord._last_briefing == "Good morning"
        assert coord._occupancy_mode == "vacation"
        assert coord._occupancy_away_since is not None
        coord.automation_engine.set_occupancy_mode.assert_called_once_with("vacation")
        coord.learning.record_day.assert_not_called()


def _returned_state_keys() -> set[str]:
    src = textwrap.dedent(inspect.getsource(ClimateAdvisorCoordinator._build_state_dict))
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("_build_state_dict no longer returns a dict literal; update this guard")


class TestEveryStateKeyIsClassified:
    def test_every_persisted_key_has_a_restore_decision(self):
        """Occupant impact: a new persisted setting cannot silently be left below the date gate (it would vanish
        after an overnight outage, which is how #1349 and #1354-#1360 happened). Add the key to ANY_RESTART_KEYS
        (restored above the gate in async_restore_state) or SAME_DAY_KEYS (below it) in this file."""
        written = _returned_state_keys()
        classified = ANY_RESTART_KEYS | SAME_DAY_KEYS | _META_KEYS
        assert written - classified == set(), "unclassified state key(s): decide any-restart vs same-day"
        assert ANY_RESTART_KEYS.isdisjoint(SAME_DAY_KEYS)
        # Stale entries would hide a removed key; every classified key (bar the optional meta key) must still exist.
        assert (ANY_RESTART_KEYS | SAME_DAY_KEYS) - written == set()

    def test_any_restart_keys_really_restore_across_days(self):
        """Occupant impact: the classification above is true, not a label. Every any-restart key marked
        here has a ``state.get("<key>")`` in the part of async_restore_state() before the date gate."""
        src = inspect.getsource(ClimateAdvisorCoordinator.async_restore_state)
        gate = src.index("if state_date != today_str:")
        before, after = src[:gate], src[gate:]
        for key in ANY_RESTART_KEYS:
            assert f'"{key}"' in before, f"{key} is not read above the date gate"
        for key in SAME_DAY_KEYS:  # the behaviour tests above prove they are NOT restored across days
            assert f'"{key}"' in after, f"{key} is not restored at all"


class TestAiInitLogging:
    def _restore_with_ai(self, caplog, *, fails: bool):
        coord = _coord(_base(_TODAY))
        coord._ai_client_config = {"ai_provider": "fake", "fake_model": "m1"}
        if fails:
            coord._executor_job = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            caplog.at_level(logging.INFO, logger=_COORD_LOGGER),
            patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW),
            contextlib.suppress(RuntimeError),
        ):
            asyncio.run(coord.async_restore_state())
        return caplog.text

    def test_initialized_is_not_logged_after_a_failed_init(self, caplog):
        text = self._restore_with_ai(caplog, fails=True)
        assert "could not be initialized" in text
        assert "AI subsystem initialized" not in text
