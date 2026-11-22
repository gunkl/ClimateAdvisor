"""Coverage for coordinator.py paths that the rest of the suite leaves unexercised (Silver test-coverage, #1644).

Each test drives the real coordinator method (``object.__new__`` partial instantiation, claude.md "Coordinator
methods") and checks what the person at home would see from it: a restored setting, a cleared override, a safe
fallback value on a display surface, or a log/event that tells them what happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as coord_mod  # noqa: E402
from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import LearningState  # noqa: E402
from tests.test_tou_away_vacation_coordinator import _build_coordinator as _build_tou_coordinator  # noqa: E402

_MOD = "custom_components.climate_advisor.coordinator"
_NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=UTC)


def _consume_coroutine(coro):
    coro.close()


def _engine() -> MagicMock:
    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae._manual_override_active = False
    ae._grace_active = False
    ae._grace_end_time = None
    ae._fan_command_pending = False
    return ae


def _coord(**attrs) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.config = {"climate_entity": "climate.thermostat", "weather_entity": "weather.home"}
    coord.automation_engine = _engine()
    coord._event_log = []
    coord._emit_event = MagicMock()
    for k, v in attrs.items():
        setattr(coord, k, v)
    return coord


# ---------------------------------------------------------------------------------------------- module helpers
class TestModuleHelpers:
    def test_run_extremum_keeps_current_when_reading_missing(self):
        assert coord_mod._run_extremum("hvac_cool", 71.0, None) == 71.0

    def test_label_text_plain_string_becomes_timeline_step(self):
        out = coord_mod._label_text("Open windows at 7 PM")
        assert out.code == "timeline_step"
        assert str(out) == "Open windows at 7 PM"

    def test_fan_status_compound_none_is_none(self):
        assert coord_mod._fan_status_compound(None) is None

    def test_fan_status_compound_unknown_token_appends_english_notes(self):
        base = coord_mod.CodedText("mystery", None, text="Mystery")
        out = coord_mod._fan_status_compound(base, rate=i18n.Msg("status.thermostat_notice.use_learned"))
        assert out.code == "mystery"
        assert str(out).startswith("Mystery")
        assert len(str(out)) > len("Mystery")

    def test_ventilation_row_states_naive_and_bad_timestamps(self):
        rows = coord_mod._ventilation_row_states(
            [
                {"ts": "2026-07-11T10:00:00", "windows_open": True, "fan_running": False},
                {"ts": "not-a-time", "windows_open": False, "fan_running": False},
            ]
        )
        assert len(rows) == 2
        assert all(isinstance(r, dict) for r in rows)

    def test_whf_request_check_without_requester_is_noop(self):
        assert coord_mod._whf_request_check(SimpleNamespace(), "fan_entity") is None  # no requester: must not raise

    def test_whf_after_sensor_event_unavailable_without_arm_still_requests(self):
        requested = []
        target = SimpleNamespace(_request_whf_safety_check=requested.append)
        event = SimpleNamespace(data={"new_state": SimpleNamespace(state="unavailable")})
        coord_mod._whf_after_sensor_event(target, event)
        assert requested == ["sensor_changed"]

    def test_tou_av_split_delta_none_and_fallback(self):
        assert ClimateAdvisorCoordinator._tou_av_split_delta(None) is None
        res = SimpleNamespace(split_key=None, target=None, expansion_f=1.5)
        assert ClimateAdvisorCoordinator._tou_av_split_delta(res) == 3.0


# ---------------------------------------------------------------------------------------------- restore state
def _restore_stub(persisted_state, **attrs):
    coord = MagicMock()
    coord.hass = MagicMock()
    coord._executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    coord.async_load_investigation_reports = AsyncMock()
    coord.config = {"climate_entity": "climate.thermostat"}
    coord.learning = MagicMock()
    coord.learning._state = LearningState()
    coord.claude_client = None
    coord._rejection_log = {}
    coord._state_persistence = MagicMock()
    coord._state_persistence.load = MagicMock(return_value=persisted_state)
    coord._event_log = []
    coord._emit_event = MagicMock()
    for k, v in attrs.items():
        setattr(coord, k, v)
    coord.async_restore_state = lambda: ClimateAdvisorCoordinator.async_restore_state(coord)
    return coord


def _same_day_state(**extra):
    base = {
        "date": _NOW.strftime("%Y-%m-%d"),
        "last_saved": _NOW.isoformat(),
        "temp_history": {"outdoor": [], "indoor": []},
        "briefing_state": {},
        "automation_enabled": True,
        "occupancy_mode": "home",
    }
    base.update(extra)
    return base


class TestRestoreState:
    def test_no_state_file_loads_reports_and_classifies_restart(self):
        """A first start (no state file) with AI configured still loads the zone's investigation history and
        records the restart; a malformed rejection log restores as empty."""
        coord = _restore_stub(None, claude_client=MagicMock())
        coord.learning._state.rejection_log = None
        with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        coord.async_load_investigation_reports.assert_awaited_once()
        coord._classify_restart_and_mark.assert_called_once()
        assert coord._rejection_log == {}

    def test_today_record_suggestion_sent_none_and_str_normalised(self):
        for sent, expected in ((None, []), ("windows_open", ["windows_open"])):
            rec = {"date": "2026-07-11", "day_type": "warm", "trend_direction": "stable", "suggestion_sent": sent}
            coord = _restore_stub(_same_day_state(today_record=rec))
            with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
                asyncio.run(coord.async_restore_state())
            assert coord._today_record.suggestion_sent == expected

    def test_bad_today_record_and_bad_times_are_dropped(self, caplog):
        """A hand-edited or corrupt state file never stops the restore: the bad parts are dropped."""
        state = _same_day_state(
            today_record={"date": "2026-07-11", "bogus_field": 1},
            briefing_state={"briefing_nat_vent_cutoff": "not-a-time", "briefing_evening_open_time": "nope"},
            occupancy_away_since="garbage",
            kept_forecast={"daily": "x", "hourly": None},
        )
        coord = _restore_stub(state)
        with caplog.at_level(logging.WARNING), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert "Failed to restore today's record" in caplog.text
        assert coord._briefing_nat_vent_cutoff is None
        assert coord._briefing_evening_open_time is None
        assert coord._occupancy_away_since is None

    def test_bad_classification_is_dropped(self, caplog):
        coord = _restore_stub(_same_day_state(classification={"day_type": "warm"}))
        with caplog.at_level(logging.WARNING), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert "Failed to restore classification" in caplog.text


# ---------------------------------------------------------------------------------------------- TOU away/vacation
class TestTouAwayVacationCycleSummary:
    def test_window_end_emits_runtime_summary(self):
        """After an away TOU window ends, the Activity log says how long the AC and the fan ran during it."""
        coord = _build_tou_coordinator("away")
        coord._today_record = SimpleNamespace(hvac_runtime_minutes=10.0)
        coord.automation_engine._get_fan_runtime_minutes = MagicMock(return_value=2.0)
        coord._emit_event = MagicMock()
        with patch(f"{_MOD}.dt_util.now", return_value=datetime(2026, 1, 5, 17, 0)):
            coord._resolve_tou_schedule_state()
        assert coord._tou_av_window_was_active is True
        coord._today_record.hvac_runtime_minutes = 55.0
        coord.automation_engine._get_fan_runtime_minutes.return_value = 12.0
        with patch(f"{_MOD}.dt_util.now", return_value=datetime(2026, 1, 5, 20, 30)):
            coord._resolve_tou_schedule_state()
        summary = [c for c in coord._emit_event.call_args_list if c.args[0] == "tou_precondition_cycle_summary"]
        assert len(summary) == 1
        payload = summary[0].args[1]
        assert payload["precool_runtime_minutes"] == 45.0
        assert payload["whf_runtime_minutes"] == 10.0
        assert payload["hvac_ran_during_window"] is True
        assert payload["schedule_id"] == "s1"
        assert coord._tou_av_cycle_start_hvac_runtime is None

    def test_no_engine_skips_engine_push(self):
        coord = _build_tou_coordinator("home")
        coord.automation_engine = None
        with patch(f"{_MOD}.dt_util.now", return_value=datetime(2026, 1, 5, 21, 0)):
            coord._sync_tou_away_vacation_state()
        assert coord._tou_av_window_was_active is False

    def test_fire_window_start_vacation_and_no_engine(self):
        coord = _build_tou_coordinator("vacation")
        coord.automation_engine.handle_occupancy_vacation = AsyncMock()
        coord._push_tou_bank_card_status = MagicMock()
        with patch(f"{_MOD}.dt_util.now", return_value=datetime(2026, 1, 5, 16, 0)):
            asyncio.run(coord._fire_tou_av_window_start("vacation"))
        coord.automation_engine.handle_occupancy_vacation.assert_awaited_once()
        coord.automation_engine = None
        asyncio.run(coord._fire_tou_av_window_start("away"))  # no engine: returns quietly
        assert coord._push_tou_bank_card_status.call_count == 1

    def test_window_in_progress_naive_vs_aware_is_false(self):
        coord = _coord()
        res = SimpleNamespace(schedule_start=datetime(2026, 1, 5, 16, 0), window_end=datetime(2026, 1, 5, 20, 0))
        assert coord._tou_window_in_progress(res, datetime(2026, 1, 5, 17, 0, tzinfo=UTC)) is False


# ---------------------------------------------------------------------------------------------- overrides and timers
class TestStuckGraceAndTimers:
    def test_stuck_grace_is_force_cleared(self, caplog):
        """An override whose grace already ended with no timer left is cleared, so automation resumes."""
        coord = _coord()
        ae = coord.automation_engine
        ae._manual_override_active = True
        ae._grace_end_time = (_NOW - timedelta(minutes=5)).isoformat()
        ae._manual_override_mode = "cool"
        ae._manual_override_time = "2026-07-11T12:00:00+00:00"
        with caplog.at_level(logging.ERROR), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            coord._check_stuck_grace()
        ae.clear_manual_override.assert_called_once_with(reason="stuck_grace_recovery")
        assert coord._emit_event.call_args.args[0] == "stuck_grace_recovered"
        assert coord._emit_event.call_args.args[1]["stale_mode"] == "cool"
        assert "Stuck grace detected" in caplog.text

    def test_grace_end_in_future_is_left_alone(self):
        coord = _coord()
        ae = coord.automation_engine
        ae._manual_override_active = True
        ae._grace_end_time = (_NOW + timedelta(minutes=5)).isoformat()
        with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            coord._check_stuck_grace()
        ae.clear_manual_override.assert_not_called()

    def test_cancel_all_debounce_timers(self):
        cancels = [MagicMock(), MagicMock()]
        coord = _coord(
            _door_open_timers={"binary_sensor.a": cancels[0], "binary_sensor.b": cancels[1]},
            _door_open_timer_expiry={"binary_sensor.a": _NOW, "binary_sensor.b": _NOW},
        )
        coord._cancel_all_debounce_timers()
        assert all(c.call_count == 1 for c in cancels)
        assert coord._door_open_timers == {} and coord._door_open_timer_expiry == {}

    def test_arm_whf_unknown_sensor_check_cancels_previous(self):
        prev = MagicMock()
        coord = _coord(_whf_unknown_sensor_cancel=prev)
        with patch(f"{_MOD}.async_call_later", return_value=MagicMock()):
            coord._arm_whf_unknown_sensor_check()
        prev.assert_called_once()

    def test_request_whf_safety_check_failures_never_raise(self, caplog):
        coord = _coord()
        coord._whf_rule2_overlap_for_s = MagicMock(side_effect=RuntimeError("boom"))
        coord.automation_engine.request_whf_safety_check = MagicMock(side_effect=RuntimeError("boom"))
        with caplog.at_level(logging.WARNING):
            coord._request_whf_safety_check("fan_entity")
        assert "WHF safety check request failed" in caplog.text
        coord.automation_engine = None
        coord._request_whf_safety_check("fan_entity")  # no engine: quiet no-op

    def test_unsubscribe_tou_condition_listeners(self):
        unsub = MagicMock()
        coord = _coord(_unsub_tou_condition=[unsub])
        coord._unsubscribe_tou_condition_listeners()
        unsub.assert_called_once()
        assert coord._unsub_tou_condition == []


# ---------------------------------------------------------------------------------------------- investigation reports
class TestInvestigationReports:
    def _coord(self, tmp_path, entry_id=None):
        coord = _coord(_entry_id=entry_id, _investigation_report_history=[])
        coord.hass.config.config_dir = str(tmp_path)
        return coord

    def test_store_caps_history(self, tmp_path):
        coord = self._coord(tmp_path)
        coord._investigation_report_history = [{"n": i} for i in range(coord_mod.INVESTIGATION_REPORT_HISTORY_CAP)]
        coord._executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_store_investigation_report({"summary": "ok"}))
        assert len(coord._investigation_report_history) == coord_mod.INVESTIGATION_REPORT_HISTORY_CAP
        assert coord._investigation_report_history[-1]["result"] == {"summary": "ok"}
        saved = json.loads(coord._investigation_reports_path().read_text())
        assert saved[-1]["result"] == {"summary": "ok"}

    def test_save_failure_logged_and_no_tmp_left(self, tmp_path, caplog):
        coord = self._coord(tmp_path / "missing_dir")
        coord._investigation_report_history = [{"a": 1}]
        with caplog.at_level(logging.ERROR):
            coord._save_investigation_reports()
        assert "Failed to save investigation reports" in caplog.text
        assert not (tmp_path / "missing_dir").exists()

    def test_load_unexpected_format_starts_fresh(self, tmp_path, caplog):
        coord = self._coord(tmp_path)
        coord._investigation_reports_path().write_text(json.dumps({"not": "a list"}))
        coord._investigation_report_history = [{"stale": True}]
        with caplog.at_level(logging.WARNING):
            coord._load_investigation_reports()
        assert coord._investigation_report_history == []
        assert "unexpected format" in caplog.text

    def test_load_corrupt_file_starts_fresh(self, tmp_path, caplog):
        coord = self._coord(tmp_path)
        coord._investigation_reports_path().write_text("{corrupt")
        with caplog.at_level(logging.ERROR):
            coord._load_investigation_reports()
        assert coord._investigation_report_history == []
        assert "Failed to load investigation reports" in caplog.text

    def test_split_failure_still_loads_own_file(self, tmp_path, caplog):
        coord = self._coord(tmp_path, entry_id="entry1")
        recent = datetime.now(UTC).isoformat()
        coord._investigation_reports_path().write_text(json.dumps([{"timestamp": recent, "result": {}}]))
        with caplog.at_level(logging.ERROR), patch(f"{_MOD}.split_shared_list_file", side_effect=OSError("disk")):
            coord._load_investigation_reports(["entry1"], True)
        assert "Failed to split" in caplog.text
        assert len(coord._investigation_report_history) == 1


class TestRestoreYesterdayRecord:
    def _yesterday_state(self, rec):
        return {"date": (_NOW - timedelta(days=1)).strftime("%Y-%m-%d"), "today_record": rec}

    def test_yesterday_record_recovered_with_legacy_suggestion(self):
        """Yesterday's day (restored after an overnight restart) still reaches learning, old string format and all."""
        rec = {"date": "2026-07-10", "day_type": "warm", "trend_direction": "stable", "suggestion_sent": "x"}
        coord = _restore_stub(self._yesterday_state(rec))
        with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        recovered = coord.learning.record_day.call_args.args[0]
        assert recovered.suggestion_sent == ["x"]
        coord.learning.save_state.assert_called_once()

    def test_bad_yesterday_record_is_logged_not_raised(self, caplog):
        coord = _restore_stub(self._yesterday_state({"date": "2026-07-10", "nope": 1}))
        with caplog.at_level(logging.WARNING), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            asyncio.run(coord.async_restore_state())
        assert "Failed to recover yesterday's record" in caplog.text
        coord.learning.record_day.assert_not_called()


# ---------------------------------------------------------------------------------------------- display fallbacks
_BOOM = RuntimeError("boom")


class TestDisplaySurfacesNeverRaise:
    """A failing display/diagnostic layer answers a documented fallback; the house is never affected."""

    def test_engine_id_fallbacks(self):
        coord = _coord()
        with (
            patch.object(coord_mod.prediction_engines, "primary_id", side_effect=_BOOM),
            patch.object(coord_mod.prediction_engines, "shadow_id", side_effect=_BOOM),
        ):
            assert coord.primary_engine_id() == "v4"
            assert coord.shadow_engine_id() == "none"

    def test_prediction_engines_payload_fallbacks(self):
        coord = _coord()
        with patch.object(coord_mod.prediction_engines, "engine_state", side_effect=_BOOM):
            out = coord.prediction_engines_payload()
        assert out["engines"] == [] and out["fallback"] is False
        coord.learning = SimpleNamespace()  # no _state: the cycle-model block falls back
        with patch.object(
            coord_mod.prediction_engines, "engine_state", return_value={"fallback": False, "primary": "v4"}
        ):
            out = coord.prediction_engines_payload()
        assert out["cycle_model"] == {"source": "cycle", "heat": None, "cool": None}

    def test_prediction_refit_note_fallback(self):
        coord = _coord()
        coord.prediction_engines_payload = MagicMock(side_effect=_BOOM)
        assert coord.prediction_refit_note() == ""

    def test_control_thermal_model_fallback_uses_blank_model(self):
        coord = _coord()
        with patch.object(coord_mod.prediction_engines, "control_model", side_effect=_BOOM):
            out = coord.control_thermal_model({"k_passive": -0.1})
        assert out["k_passive"] == coord_mod.prediction_engines.CONTROL_BLANK_MODEL.get("k_passive", -0.1)
        for k, v in coord_mod.prediction_engines.CONTROL_BLANK_MODEL.items():
            assert out[k] == v

    def test_decision_rates_summary_fallback(self):
        coord = _coord(_thermal_model={})
        coord.control_thermal_model = MagicMock(side_effect=_BOOM)
        assert coord.decision_rates_summary() == []

    def test_night_strategy_payload_error(self):
        coord = _coord(_timeline_night={"strategy": object()})
        with patch("custom_components.climate_advisor.night_strategy.api_payload", side_effect=_BOOM):
            assert coord.night_strategy_payload() == (None, "error")

    def test_surface_timeline_error_is_none(self):
        latch = MagicMock()
        type(latch).timeline = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
        coord = _coord(_timeline_latch=latch, _shut_down=False, _current_classification=object())
        assert coord._surface_timeline() is None

    def test_timeline_debug_error(self):
        latch = SimpleNamespace(timeline=object())
        coord = _coord(_timeline_latch=latch, _timeline_last={})
        with patch(f"{_MOD}.timeline_to_dict", side_effect=_BOOM):
            assert coord.timeline_debug() == {"shadow": True, "error": True}

    def test_v4_chart_series_error_reports_reason(self):
        coord = _coord(_v4_store=SimpleNamespace(v4_model=None), _v4_archive=object(), _entry_id="e1")
        coord.engine_running = MagicMock(return_value=True)
        with patch(f"{_MOD}.load_backtest", side_effect=_BOOM):
            out = coord.v4_chart_series(
                now=_NOW,
                start=0.0,
                end=1.0,
                range_str="24h",
                unit="fahrenheit",
                historical=False,
                sensor_ids=[],
                raw_log=[],
            )
        assert out["ready"] is False and out["reason"] == "error"

    def test_indoor_friendly_name(self):
        coord = _coord()
        assert coord._indoor_friendly_name(None) == "unknown"
        coord.hass.states.get = MagicMock(return_value=None)
        assert coord._indoor_friendly_name("sensor.bedroom") == "sensor.bedroom"

    def test_indoor_zone_title_falls_back_to_log_id(self):
        coord = _coord(_entry_id="e1")
        coord._zone_log_id = MagicMock(return_value="zone-e1")
        with patch("custom_components.climate_advisor.zone_registry.resolve_zone_display_name", side_effect=_BOOM):
            assert coord._indoor_zone_title() == "zone-e1"

    def test_indoor_snapshot_failure_is_empty(self):
        coord = _coord()
        with patch(f"{_MOD}.read_indoor_sensors_f", side_effect=_BOOM):
            assert coord._indoor_snapshot_f() == {}

    def test_thermostat_target_temp_step_unreadable(self):
        coord = _coord()
        coord.hass.states.get = MagicMock(side_effect=_BOOM)
        assert coord._thermostat_target_temp_step() is None

    def test_read_chart_hold_bad_values(self):
        coord = _coord()
        coord._target_band_lower_upper_now = MagicMock(return_value=(70.0, 76.0))
        coord._chart_hold_lower_upper_now = MagicMock(return_value=("x", "y"))
        assert coord._read_chart_hold() == {}

    def test_chart_away_pending_band_failure_keeps_schedule(self):
        band = [{"ts": _NOW.isoformat(), "lower": 60.0, "upper": 80.0}]
        coord = _coord(_occupancy_mode="away", _occupancy_away_timer_cancel=MagicMock())
        coord._build_target_band_for = MagicMock(side_effect=_BOOM)
        assert coord._chart_away_pending_band(_NOW, None, None, band) is band

    def test_chart_widened_band_bad_ts_keeps_band(self):
        res = SimpleNamespace(expansion_f=3.0, schedule_start=_NOW, window_end=_NOW + timedelta(hours=4))
        band = [{"ts": "garbage", "lower": 60.0, "upper": 80.0}]
        coord = _coord(
            _tou_phase_resolution=res,
            _current_classification=SimpleNamespace(hvac_mode="cool"),
            _occupancy_mode="away",
        )
        assert coord._chart_widened_band(band, _NOW) is band

    def test_chart_bank_band_guards(self):
        band = [{"ts": _NOW.isoformat(), "lower": 70.0, "upper": 76.0}]
        coord = _coord()
        coord._tou_precondition_window_tuple = MagicMock(return_value=(None, None, 72.0, "cool"))
        assert coord._chart_bank_band(_NOW, None, None, band) is band
        coord._tou_precondition_window_tuple = MagicMock(return_value=(_NOW, _NOW + timedelta(hours=1), 72.0, "cool"))
        coord._build_target_band_for = MagicMock(return_value=[{"ts": "bad", "lower": 70.0, "upper": 76.0}])
        coord.current_hold = MagicMock(return_value=None)
        assert coord._chart_bank_band(_NOW, None, None, [{"ts": "bad", "lower": 70.0}]) == [
            {"ts": "bad", "lower": 70.0}
        ]

    def test_chart_bank_band_hour_without_base_is_kept(self):
        coord = _coord()
        coord._tou_precondition_window_tuple = MagicMock(return_value=(_NOW, _NOW + timedelta(hours=1), 72.0, "cool"))
        coord._build_target_band_for = MagicMock(return_value=[])
        coord.current_hold = MagicMock(return_value=None)
        band = [{"ts": _NOW.isoformat(), "lower": 70.0, "upper": 76.0}]
        assert coord._chart_bank_band(_NOW, None, None, band) == band


class TestTouNaiveAwareGuards:
    """A naive/aware clock mix (hand-edited schedule, odd provider) never raises out of the time-of-use code."""

    def test_freeze_split_not_frozen_on_clock_mix(self):
        coord = _coord(_tou_av_frozen="old")
        res = SimpleNamespace(
            split_key=(1, 2, 3, 4, 5, 6),
            precondition_start=datetime(2026, 1, 5, 15, 0),
            window_end=datetime(2026, 1, 5, 20, 0),
            frozen=False,
        )
        coord._freeze_tou_av_split(res, datetime(2026, 1, 5, 16, 0, tzinfo=UTC))
        assert coord._tou_av_frozen is None

    def test_bank_in_force_clock_mix_is_none(self):
        res = SimpleNamespace(
            phase=coord_mod.TOUPhase.PRECONDITIONING,
            target=72.0,
            mode="cool",
            schedule_start=datetime(2026, 1, 5, 16, 0),
            precondition_start=datetime(2026, 1, 5, 14, 0),
        )
        coord = _coord(_tou_phase_resolution=res)
        assert coord._tou_bank_in_force(datetime(2026, 1, 5, 15, 0, tzinfo=UTC)) is None

    def test_thermostat_holds_bank_unreadable(self):
        coord = _coord()
        coord.hass.states.get = MagicMock(return_value=None)
        assert coord._thermostat_holds_tou_bank(SimpleNamespace(target=72.0)) is False


class TestCardPushes:
    def test_tou_bank_card_refresh_failure_does_not_notify(self):
        coord = _coord(data={"x": 1}, _current_classification=None)
        coord._compute_automation_status = MagicMock(side_effect=_BOOM)
        coord.async_update_listeners = MagicMock()
        coord._push_tou_bank_card_status()
        coord.async_update_listeners.assert_not_called()

    def test_override_card_push_without_data_is_noop(self):
        coord = _coord(data=None)
        coord._compute_automation_status = MagicMock()
        coord._push_override_card_status()
        coord._compute_automation_status.assert_not_called()

    def test_planned_window_exit_failure_is_logged(self, caplog):
        coord = _coord()
        coord.automation_engine.async_sync_after_planned_window_exit = AsyncMock(side_effect=_BOOM)
        with caplog.at_level(logging.WARNING):
            asyncio.run(coord._async_planned_window_exit_sync())
        assert "Planned-window exit reconcile failed" in caplog.text

    def test_current_hvac_mode(self):
        coord = _coord()
        coord.automation_engine.climate_entity = "climate.thermostat"
        coord.hass.states.get = MagicMock(return_value=SimpleNamespace(state="cool"))
        assert coord._current_hvac_mode() == "cool"
        coord.hass.states.get = MagicMock(return_value=None)
        assert coord._current_hvac_mode() is None

    def test_occupant_fan_stamp_needs_engine(self):
        coord = _coord()
        coord.automation_engine = None
        assert coord._occupant_fan_on_at_from_ground_truth(True, 30.0) is None


class TestForecastFetch:
    def test_missing_weather_entity_returns_empty(self):
        coord = _coord()
        coord.hass.states.get = MagicMock(return_value=None)
        assert asyncio.run(coord._get_forecast_data()) == []

    def test_weather_entity_gone_after_failed_service_call(self):
        coord = _coord()
        coord.hass.states.get = MagicMock(side_effect=[SimpleNamespace(attributes={}), None])
        coord.hass.services.async_call = AsyncMock(side_effect=_BOOM)
        assert asyncio.run(coord._get_forecast_data()) == []

    def test_hourly_non_dict_entries_pass_through(self):
        coord = _coord()
        coord.hass.states.get = MagicMock(return_value=SimpleNamespace(attributes={}))
        coord.hass.services.async_call = AsyncMock(
            return_value={"weather.home": {"forecast": ["odd", {"datetime": "2026-07-11T15:00:00", "temperature": 80}]}}
        )
        out = asyncio.run(coord._get_hourly_forecast_data())
        assert out[0] == "odd"
        assert out[1]["temperature"] == 80.0


class TestForecastSnapshotBias:
    def _coord(self, bias, history=()):
        coord = _coord(_outdoor_temp_history=list(history))
        coord.config.update({"temp_unit": "fahrenheit", "learning_enabled": True})
        coord._get_indoor_temp = MagicMock(return_value=72.0)
        coord.learning = MagicMock()
        coord.learning.get_weather_bias.return_value = bias
        return coord

    def _forecast(self):
        return [
            {"datetime": "garbage"},
            {"datetime": "2026-07-11T00:00:00+00:00", "temperature": 90, "templow": 65},
            {"datetime": "2026-07-12T00:00:00+00:00", "temperature": 88, "templow": 64},
        ]

    def test_learned_bias_moves_only_the_significant_edge(self):
        """The learned forecast bias corrects tomorrow's high (2 F off) but ignores a negligible low bias."""
        bias = {"confidence": "medium", "high_bias": 2.0, "low_bias": 0.01}
        coord = self._coord(bias)
        with patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            snap = coord._build_forecast_snapshot(self._forecast(), 80.0, {}, from_kept_forecast=False)
        assert snap.tomorrow_high == 90.0
        assert snap.tomorrow_low == 64.0

    def test_observed_extreme_warned_once_per_sample(self, caplog):
        history = [(_NOW.isoformat(), 120.0)]
        coord = self._coord({"confidence": "none"}, history)
        with caplog.at_level(logging.WARNING), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            coord._build_forecast_snapshot(self._forecast(), 80.0, {}, from_kept_forecast=False)
            caplog.clear()
            snap = coord._build_forecast_snapshot(self._forecast(), 80.0, {}, from_kept_forecast=False)
        assert "Observed outdoor extreme" not in caplog.text
        assert snap.today_high == 120.0


class TestDroppedChangeRecheck:
    def _state(self, state="cool", **attrs):
        return SimpleNamespace(state=state, attributes=attrs)

    def test_too_many_attempts_gives_up(self, caplog):
        coord = _coord(_dropped_change=None)
        event = SimpleNamespace(recheck_attempt=coord_mod.DROPPED_CHANGE_RECHECK_MAX_ATTEMPTS, context=None)
        with caplog.at_level(logging.WARNING):
            coord._note_dropped_change(event, self._state("heat"), self._state("cool"), ["state"])
        assert coord._dropped_change is None
        assert "not re-checked" in caplog.text

    def test_scheduling_failure_leaves_no_timer(self):
        coord = _coord(_dropped_change=None, _dropped_change_recheck_cancel=None)
        event = SimpleNamespace(recheck_attempt=0, context=None)
        with patch(f"{_MOD}._call_later", side_effect=_BOOM):
            coord._note_dropped_change(event, self._state("heat"), self._state("cool"), ["state"])
        assert coord._dropped_change_recheck_cancel is None
        assert coord._dropped_change["after"] == {"state": "cool"}

    def test_recheck_without_drop_or_live_state_is_noop(self):
        coord = _coord(_dropped_change=None, _shut_down=False)
        asyncio.run(coord._async_recheck_dropped_change())
        coord.hass.states.get.assert_not_called()
        coord._dropped_change = {"before": {"state": "heat"}, "after": {"state": "cool"}}
        coord.hass.states.get = MagicMock(return_value=None)
        asyncio.run(coord._async_recheck_dropped_change())
        assert coord._dropped_change is None
        coord.hass.states.get.assert_called_once()


class TestBlowerLeadIn:
    def _run_later(self, coord):
        captured = {}

        def _fake_call_later(obj, delay, fn):
            captured["fn"] = fn
            return MagicMock()

        with patch(f"{_MOD}._call_later", side_effect=_fake_call_later):
            coord._defer_fan_reconcile_for_lead_in(1000.0)
        return captured["fn"]

    def test_lead_in_recheck_skips_when_shut_down_or_no_state(self):
        coord = _coord(_lead_in_recheck_cancel=None, _shut_down=False, _last_compressor_start_ts=None)
        coord._fan_reconcile_347 = AsyncMock()
        later = self._run_later(coord)
        coord.hass.states.get = MagicMock(return_value=None)
        asyncio.run(later(None))
        coord._shut_down = True
        asyncio.run(later.__wrapped__(None))  # the inner check (the zone wrapper would refuse first)
        coord._fan_reconcile_347.assert_not_awaited()

    def test_scheduling_failure_is_swallowed(self):
        coord = _coord(_lead_in_recheck_cancel=None)
        with patch(f"{_MOD}._call_later", side_effect=_BOOM):
            coord._defer_fan_reconcile_for_lead_in(1000.0)
        assert coord._lead_in_recheck_cancel is None


class TestLiveDirectionVeto:
    def test_no_classification_returns_plan(self):
        coord = _coord(_current_classification=None)
        tl = object()
        assert coord._apply_live_direction_veto(tl) is tl

    def test_failure_returns_plan_as_latched(self):
        coord = _coord(_current_classification=object(), _last_outdoor_temp=70.0, _occupancy_mode="home")
        coord._get_indoor_temp = MagicMock(return_value=72.0)
        coord.automation_engine = None
        tl = object()
        with patch(f"{_MOD}.resolve_active_band", side_effect=_BOOM) as rab:
            assert coord._apply_live_direction_veto(tl) is tl
        rab.assert_called_once()


class TestV4Guards:
    def test_display_fit_without_model_is_noop(self):
        coord = _coord(_v4_store=SimpleNamespace(v4_model={"confidence": "none"}))
        coord._executor_job = AsyncMock()
        asyncio.run(coord._v4_run_display_fit(_NOW, "p", [], (), None))
        coord._executor_job.assert_not_awaited()

    def test_control_cycle_without_model_leaves_future_unset(self):
        coord = _coord(_v4_store=SimpleNamespace(v4_model=None), _v4_control_future="old")
        asyncio.run(coord._v4_control_cycle())
        assert coord._v4_control_future is None

    def test_shadow_cycle_after_shutdown_is_noop(self):
        coord = _coord(_shut_down=True)
        coord._executor_job = AsyncMock()
        asyncio.run(coord._v4_shadow_cycle_impl())
        coord._executor_job.assert_not_awaited()

    def test_night_replay_spawn_failure_resets_flag(self):
        coord = _coord(_shut_down=False, _v4_night_replay_running=False)
        coord.engine_running = MagicMock(return_value=True)
        coord._spawn = MagicMock(side_effect=_BOOM)
        with patch.object(coord_mod.v4_shadow, "predictor_available", return_value=True), pytest.raises(RuntimeError):
            coord._v4_start_night_replay("upgrade")
        assert coord._v4_night_replay_running is False


class TestSmallPaths:
    def test_save_learning_state_runs_in_executor(self):
        coord = _coord()
        coord.learning = MagicMock()
        coord._executor_job = AsyncMock()
        asyncio.run(coord._save_learning_state())
        coord._executor_job.assert_awaited_once_with(coord.learning.save_state)

    def test_pre_cool_trigger_already_passed_is_not_scheduled(self, caplog):
        coord = _coord(_pre_cool_trigger_scheduled=False)
        coord._compute_pre_cool_trigger_time = MagicMock(return_value=_NOW - timedelta(minutes=10))
        coord._pre_cool_modifier_for_night = MagicMock()
        with caplog.at_level(logging.INFO), patch(f"{_MOD}.dt_util.now", return_value=_NOW):
            coord._maybe_schedule_pre_cool()
        assert "already passed" in caplog.text
        assert coord._pre_cool_trigger_scheduled is False
        coord._pre_cool_modifier_for_night.assert_not_called()

    def test_pre_cool_reschedule_without_classification(self):
        coord = _coord(_current_classification=None)
        coord._compute_pre_cool_trigger_time = MagicMock()
        coord._maybe_reschedule_pre_cool_on_nat_vent_exit()
        coord._compute_pre_cool_trigger_time.assert_not_called()


class TestRecordOnlyPaths:
    def test_v3_archive_skips_bad_points_and_never_raises(self, caplog):
        archive = MagicMock()
        archive.record = MagicMock(side_effect=_BOOM)
        coord = _coord(
            _v3_archive=archive,
            _last_predicted_indoor=[{"ts": "bad", "temp": 70.0}, {"ts": _NOW.isoformat(), "temp": 71.0}],
        )
        with caplog.at_level(logging.WARNING):
            asyncio.run(coord._record_v3_archive(_NOW))
        curve = archive.record.call_args.args[1]["_ca"]
        assert curve == [(_NOW, 71.0)]
        assert "v3 archive: could not record" in caplog.text

    def test_occupancy_chart_row_without_chart_log(self):
        coord = _coord(_chart_log=None)
        coord._read_chart_indoor_sensors = MagicMock()
        coord._append_occupancy_chart_row("occupancy_away")
        coord._read_chart_indoor_sensors.assert_not_called()

    def test_self_heal_keeps_ownership_when_thermostat_unreadable(self):
        coord = _coord()
        coord.automation_engine._fan_active = True
        coord.automation_engine._fan_min_runtime_active = False
        coord._is_recent_fan_command = MagicMock(return_value=False)
        coord.config[coord_mod.CONF_FAN_MODE] = coord_mod.FAN_MODE_BOTH
        coord.hass.states.get = MagicMock(return_value=None)
        coord._self_heal_fan_ownership()
        coord.automation_engine.clear_stale_fan_ownership.assert_not_called()
        coord._emit_event.assert_not_called()
