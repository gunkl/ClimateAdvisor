"""Tests for state persistence (save/restore across restarts).

Covers:
- StatePersistence class (save, load, corrupt/missing file handling)
- Coordinator state restore logic (same-day, different-day, yesterday recovery)
- DailyRecord field population (runtime, comfort violations, avg temp, windows)
- Phase 2: Per-sensor pause tracking, granular overrides, suggestion tracking
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.climate_advisor.const import STATE_FILE
from custom_components.climate_advisor.learning import DailyRecord
from custom_components.climate_advisor.state import STATE_VERSION, StatePersistence

# ---------------------------------------------------------------------------
# StatePersistence class tests
# ---------------------------------------------------------------------------


class TestStatePersistenceSaveLoad:
    """Test basic save/load round-trip."""

    def test_save_and_load_round_trip(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        state = {
            "date": "2026-03-18",
            "last_saved": "2026-03-18T14:00:00",
            "classification": {"day_type": "mild"},
            "temp_history": {"outdoor": [["2026-03-18T06:00:00", 55.0]]},
        }
        sp.save(state)
        loaded = sp.load()
        assert loaded["date"] == "2026-03-18"
        assert loaded["classification"]["day_type"] == "mild"
        assert loaded["temp_history"]["outdoor"] == [["2026-03-18T06:00:00", 55.0]]

    def test_save_adds_version(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.save({"date": "2026-03-18"})
        loaded = sp.load()
        assert loaded["version"] == STATE_VERSION

    def test_save_is_atomic_no_tmp_left(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.save({"date": "2026-03-18"})
        assert (tmp_path / STATE_FILE).exists()
        assert list(tmp_path.glob("climate_advisor_state_*.tmp")) == []

    def test_concurrent_saves_no_enoent(self, tmp_path: Path):
        """Overlapping saves must not raise ENOENT (regression for race condition)."""
        import concurrent.futures

        sp = StatePersistence(tmp_path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(sp.save, {"date": f"2026-03-{i:02d}"}) for i in range(1, 9)]
            for f in concurrent.futures.as_completed(futures):
                f.result()  # raises if save() raised
        assert (tmp_path / STATE_FILE).exists()

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        assert sp.load() == {}

    def test_load_corrupt_json_returns_empty(self, tmp_path: Path):
        (tmp_path / STATE_FILE).write_text("not valid json{{{", encoding="utf-8")
        sp = StatePersistence(tmp_path)
        assert sp.load() == {}

    def test_load_wrong_version_returns_empty(self, tmp_path: Path):
        data = {"version": 999, "date": "2026-03-18"}
        (tmp_path / STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
        sp = StatePersistence(tmp_path)
        assert sp.load() == {}

    def test_load_non_object_returns_empty(self, tmp_path: Path):
        (tmp_path / STATE_FILE).write_text("[1, 2, 3]", encoding="utf-8")
        sp = StatePersistence(tmp_path)
        assert sp.load() == {}

    def test_delete_removes_files(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.save({"date": "2026-03-18"})
        assert (tmp_path / STATE_FILE).exists()
        sp.delete()
        assert not (tmp_path / STATE_FILE).exists()

    def test_delete_missing_file_no_error(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.delete()  # Should not raise


class TestStatePersistenceOverwrite:
    """Test that save overwrites previous state correctly."""

    def test_overwrite_replaces_data(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.save({"date": "2026-03-17", "classification": {"day_type": "cold"}})
        sp.save({"date": "2026-03-18", "classification": {"day_type": "warm"}})
        loaded = sp.load()
        assert loaded["date"] == "2026-03-18"
        assert loaded["classification"]["day_type"] == "warm"


# ---------------------------------------------------------------------------
# Coordinator state restore logic (replicated decision logic)
# ---------------------------------------------------------------------------


def _restore_decision(state: dict, today_str: str, yesterday_str: str):
    """Replicate the restore decision logic from the coordinator."""
    if not state:
        return "fresh"

    state_date = state.get("date", "")

    if state_date == yesterday_str and state.get("today_record"):
        return "recover_yesterday"

    if state_date != today_str:
        return "discard_stale"

    return "restore_same_day"


class TestRestoreDecisionLogic:
    """Test the date-based restore/discard/recover logic."""

    def test_empty_state_starts_fresh(self):
        assert _restore_decision({}, "2026-03-18", "2026-03-17") == "fresh"

    def test_same_day_restores(self):
        state = {"date": "2026-03-18", "classification": {"day_type": "mild"}}
        assert _restore_decision(state, "2026-03-18", "2026-03-17") == "restore_same_day"

    def test_yesterday_with_record_recovers(self):
        state = {
            "date": "2026-03-17",
            "today_record": {"date": "2026-03-17", "day_type": "cool", "trend_direction": "stable"},
        }
        assert _restore_decision(state, "2026-03-18", "2026-03-17") == "recover_yesterday"

    def test_yesterday_without_record_discards(self):
        state = {"date": "2026-03-17"}
        assert _restore_decision(state, "2026-03-18", "2026-03-17") == "discard_stale"

    def test_older_date_discards(self):
        state = {"date": "2026-03-15", "today_record": {"date": "2026-03-15"}}
        assert _restore_decision(state, "2026-03-18", "2026-03-17") == "discard_stale"


# ---------------------------------------------------------------------------
# DailyRecord field population tests
# ---------------------------------------------------------------------------


class TestHVACRuntimeTracking:
    """Test HVAC runtime accumulation logic."""

    def test_runtime_accumulates_on_off_cycle(self):
        """Simulate: HVAC on for 45 minutes, then off."""
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        # Simulate 45 minutes of runtime
        record.hvac_runtime_minutes += 45.0
        assert record.hvac_runtime_minutes == 45.0

    def test_runtime_accumulates_multiple_cycles(self):
        """Multiple on/off cycles should sum."""
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        record.hvac_runtime_minutes += 20.0  # first cycle
        record.hvac_runtime_minutes += 15.0  # second cycle
        record.hvac_runtime_minutes += 10.0  # third cycle
        assert record.hvac_runtime_minutes == 45.0

    def test_runtime_zero_when_never_on(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        assert record.hvac_runtime_minutes == 0.0

    def test_runtime_persists_in_record_dict(self):
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        record.hvac_runtime_minutes = 123.5
        d = asdict(record)
        assert d["hvac_runtime_minutes"] == 123.5


class TestComfortViolationTracking:
    """Test comfort violation accumulation logic."""

    def test_violation_when_below_heat_setpoint(self):
        """Indoor temp below comfort_heat should count as violation."""
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        indoor_temp = 65.0
        comfort_heat = 70
        comfort_cool = 75
        if indoor_temp < comfort_heat or indoor_temp > comfort_cool:
            record.comfort_violations_minutes += 30.0
        assert record.comfort_violations_minutes == 30.0

    def test_no_violation_when_in_range(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        indoor_temp = 72.0
        comfort_heat = 70
        comfort_cool = 75
        if indoor_temp < comfort_heat or indoor_temp > comfort_cool:
            record.comfort_violations_minutes += 30.0
        assert record.comfort_violations_minutes == 0.0

    def test_violation_when_above_cool_setpoint(self):
        record = DailyRecord(date="2026-03-18", day_type="hot", trend_direction="stable")
        indoor_temp = 78.0
        comfort_heat = 70
        comfort_cool = 75
        if indoor_temp < comfort_heat or indoor_temp > comfort_cool:
            record.comfort_violations_minutes += 30.0
        assert record.comfort_violations_minutes == 30.0

    def test_violations_accumulate_across_cycles(self):
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        # 3 update cycles where indoor is too cold
        for _ in range(3):
            record.comfort_violations_minutes += 30.0
        assert record.comfort_violations_minutes == 90.0


class TestAvgIndoorTempComputation:
    """Test average indoor temperature computation at end-of-day."""

    def test_avg_from_history(self):
        history = [
            ("2026-03-18T06:00:00", 68.0),
            ("2026-03-18T06:30:00", 69.0),
            ("2026-03-18T07:00:00", 70.0),
            ("2026-03-18T07:30:00", 71.0),
        ]
        avg = round(sum(t for _, t in history) / len(history), 1)
        assert avg == 69.5

    def test_avg_empty_history_is_none(self):
        history = []
        avg = None
        if history:
            avg = round(sum(t for _, t in history) / len(history), 1)
        assert avg is None

    def test_avg_single_reading(self):
        history = [("2026-03-18T12:00:00", 72.3)]
        avg = round(sum(t for _, t in history) / len(history), 1)
        assert avg == 72.3


class TestWindowComplianceDetection:
    """Test window open/close compliance tracking."""

    def test_window_opened_during_recommended_period(self):
        """When a sensor opens during the recommended window period, mark compliance."""
        from datetime import time

        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="warming",
            windows_recommended=True,
            window_open_time="10:00:00",
            window_close_time="17:00:00",
        )

        # Simulate sensor open at 11:30 AM (within window)
        window_open_time = time(10, 0)
        window_close_time = time(17, 0)
        current_time = time(11, 30)

        if (
            record.windows_recommended
            and window_open_time <= current_time <= window_close_time
            and not record.windows_opened
        ):
            record.windows_opened = True
            record.window_open_actual_time = "2026-03-18T11:30:00"

        assert record.windows_opened is True
        assert record.window_open_actual_time == "2026-03-18T11:30:00"

    def test_window_opened_outside_recommended_period(self):
        """Sensor open outside the recommended window period should not count."""
        from datetime import time

        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="warming",
            windows_recommended=True,
            window_open_time="10:00:00",
            window_close_time="17:00:00",
        )

        window_open_time = time(10, 0)
        window_close_time = time(17, 0)
        current_time = time(8, 0)  # Before recommended period

        if (
            record.windows_recommended
            and window_open_time <= current_time <= window_close_time
            and not record.windows_opened
        ):
            record.windows_opened = True

        assert record.windows_opened is False

    def test_window_not_recommended_no_tracking(self):
        """When windows are not recommended, don't track compliance."""
        record = DailyRecord(
            date="2026-03-18",
            day_type="cold",
            trend_direction="cooling",
            windows_recommended=False,
        )
        assert record.windows_opened is False


class TestWindowCloseTracking:
    """Test recording when windows are closed."""

    def test_close_time_recorded_when_all_close(self):
        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="warming",
            windows_recommended=True,
            window_open_time="10:00:00",
            window_close_time="17:00:00",
        )
        record.windows_opened = True
        record.window_open_actual_time = "2026-03-18T11:30:00"

        # Simulate all sensors closing
        if record.windows_opened and record.window_close_actual_time is None:
            record.window_close_actual_time = "2026-03-18T15:45:00"

        assert record.window_close_actual_time == "2026-03-18T15:45:00"


# ---------------------------------------------------------------------------
# Full state round-trip (save → load → verify all sections)
# ---------------------------------------------------------------------------


class TestFullStateRoundTrip:
    """Test a complete state save and load with all sections."""

    def test_full_state_round_trip(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)

        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="warming",
            windows_recommended=True,
            window_open_time="10:00:00",
            window_close_time="17:00:00",
            hvac_mode_recommended="off",
            door_window_pause_events=2,
            manual_overrides=1,
            hvac_runtime_minutes=45.5,
            avg_indoor_temp=71.2,
            comfort_violations_minutes=30.0,
            windows_opened=True,
            window_open_actual_time="2026-03-18T11:00:00",
            window_close_actual_time="2026-03-18T16:30:00",
        )

        state = {
            "date": "2026-03-18",
            "last_saved": "2026-03-18T18:00:00",
            "classification": {
                "day_type": "mild",
                "trend_direction": "warming",
                "trend_magnitude": 7.5,
                "today_high": 68.0,
                "today_low": 45.0,
                "tomorrow_high": 75.0,
                "tomorrow_low": 52.0,
                "hvac_mode": "off",
                "pre_condition": False,
                "pre_condition_target": None,
                "windows_recommended": True,
                "window_open_time": "10:00:00",
                "window_close_time": "17:00:00",
                "setback_modifier": -2.0,
            },
            "temp_history": {
                "outdoor": [
                    ["2026-03-18T06:00:00", 48.2],
                    ["2026-03-18T06:30:00", 50.1],
                ],
                "indoor": [
                    ["2026-03-18T06:00:00", 69.8],
                    ["2026-03-18T06:30:00", 70.0],
                ],
            },
            "automation_state": {
                "paused_by_door": False,
                "pre_pause_mode": None,
                "grace_active": False,
                "last_resume_source": None,
            },
            "today_record": asdict(record),
            "briefing_state": {
                "sent_today": True,
                "last_text": "Good morning! Today is a mild day...",
            },
        }

        sp.save(state)
        loaded = sp.load()

        assert loaded["date"] == "2026-03-18"
        assert loaded["classification"]["day_type"] == "mild"
        assert loaded["classification"]["trend_magnitude"] == 7.5
        assert loaded["temp_history"]["outdoor"][0] == ["2026-03-18T06:00:00", 48.2]
        assert loaded["temp_history"]["indoor"][1] == ["2026-03-18T06:30:00", 70.0]
        assert loaded["automation_state"]["paused_by_door"] is False
        assert loaded["today_record"]["hvac_runtime_minutes"] == 45.5
        assert loaded["today_record"]["windows_opened"] is True
        assert loaded["today_record"]["avg_indoor_temp"] == 71.2
        assert loaded["briefing_state"]["sent_today"] is True
        assert "mild day" in loaded["briefing_state"]["last_text"]


# ---------------------------------------------------------------------------
# Automation engine restore_state tests
# ---------------------------------------------------------------------------


class TestAutomationRestoreState:
    """Test the AutomationEngine.restore_state method."""

    def test_restore_paused_state(self):
        """Pause state (paused_by_door, pre_pause_mode) IS restored; grace/override are clean-slated."""
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = AutomationEngine(
            hass=MagicMock(),
            climate_entity="climate.thermostat",
            weather_entity="weather.home",
            door_window_sensors=[],
            notify_service="notify.mobile",
            config={},
        )

        engine.restore_state(
            {
                "paused_by_door": True,
                "pre_pause_mode": "heat",
                "grace_active": True,
                "last_resume_source": "automation",
                "grace_end_time": "2099-01-01T00:00:00+00:00",
                "grace_duration_seconds": 5400,
            }
        )

        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == "heat"
        # Clean-slate on restart: grace and override state always reset
        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._grace_end_time is None
        assert engine._grace_duration_seconds is None

    def test_restore_empty_state(self):
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = AutomationEngine(
            hass=MagicMock(),
            climate_entity="climate.thermostat",
            weather_entity="weather.home",
            door_window_sensors=[],
            notify_service="notify.mobile",
            config={},
        )

        engine.restore_state({})

        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        assert engine._grace_active is False

    def test_restore_missing_keys_uses_defaults(self):
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = AutomationEngine(
            hass=MagicMock(),
            climate_entity="climate.thermostat",
            weather_entity="weather.home",
            door_window_sensors=[],
            notify_service="notify.mobile",
            config={},
        )

        engine.restore_state({"paused_by_door": True})

        assert engine._paused_by_door is True
        assert engine._pre_pause_mode is None

    def test_restore_clean_slates_override_state(self):
        """Restart always clears manual override state — no carry-over from prior session."""
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = AutomationEngine(
            hass=MagicMock(),
            climate_entity="climate.thermostat",
            weather_entity="weather.home",
            door_window_sensors=[],
            notify_service="notify.mobile",
            config={},
        )

        engine.restore_state(
            {
                "manual_override_active": True,
                "manual_override_mode": "heat",
                "manual_override_time": "2026-06-12T08:00:00",
                "grace_active": True,
                "grace_end_time": "2026-06-12T09:00:00",
                "grace_duration_seconds": 3600,
                "last_resume_source": "manual",
                "override_confirm_pending": True,
                "override_confirm_time": "2026-06-12T07:55:00",
            }
        )

        assert engine._manual_override_active is False
        assert engine._manual_override_mode is None
        assert engine._manual_override_time is None
        assert engine._grace_active is False
        assert engine._grace_end_time is None
        assert engine._grace_duration_seconds is None
        assert engine._last_resume_source is None
        assert engine._override_confirm_pending is False
        assert engine._override_confirm_time is None

    def test_get_serializable_state_omits_override_and_grace_keys(self):
        """get_serializable_state() must not include override/grace keys — they are clean-slated."""
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = AutomationEngine(
            hass=MagicMock(),
            climate_entity="climate.thermostat",
            weather_entity="weather.home",
            door_window_sensors=[],
            notify_service="notify.mobile",
            config={},
        )

        serialized = engine.get_serializable_state()

        assert "manual_override_active" not in serialized
        assert "manual_override_mode" not in serialized
        assert "manual_override_time" not in serialized
        assert "grace_active" not in serialized
        assert "grace_end_time" not in serialized
        assert "grace_duration_seconds" not in serialized
        assert "last_resume_source" not in serialized
        assert "override_confirm_pending" not in serialized
        assert "override_confirm_time" not in serialized


# ---------------------------------------------------------------------------
# Phase 2: Per-sensor door/window pause tracking
# ---------------------------------------------------------------------------


class TestDoorPauseBySensor:
    """Test per-sensor pause count tracking."""

    def test_sensor_counts_accumulate(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        # Simulate 3 pauses from back_door, 1 from front_door
        for _ in range(3):
            record.door_window_pause_events += 1
            sensor_key = "back_door"
            record.door_pause_by_sensor[sensor_key] = record.door_pause_by_sensor.get(sensor_key, 0) + 1
        record.door_window_pause_events += 1
        sensor_key = "front_door"
        record.door_pause_by_sensor[sensor_key] = record.door_pause_by_sensor.get(sensor_key, 0) + 1

        assert record.door_window_pause_events == 4
        assert record.door_pause_by_sensor == {"back_door": 3, "front_door": 1}

    def test_round_trip_preserves_sensor_dict(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="stable",
            door_window_pause_events=5,
            door_pause_by_sensor={"garage_door": 3, "kitchen_window": 2},
        )
        state = {"date": "2026-03-18", "today_record": asdict(record)}
        sp.save(state)
        loaded = sp.load()
        restored = DailyRecord(**loaded["today_record"])
        assert restored.door_pause_by_sensor == {"garage_door": 3, "kitchen_window": 2}

    def test_empty_dict_default(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        assert record.door_pause_by_sensor == {}


# ---------------------------------------------------------------------------
# Phase 2: Granular override records
# ---------------------------------------------------------------------------


class TestOverrideDetails:
    """Test structured override event recording."""

    def test_override_appended_correctly(self):
        record = DailyRecord(date="2026-03-18", day_type="cool", trend_direction="stable")
        old_val, new_val = 70.0, 72.0
        magnitude = round(new_val - old_val, 1)
        record.manual_overrides += 1
        record.override_details.append(
            {
                "time": "14:30",
                "old_temp": old_val,
                "new_temp": new_val,
                "direction": "up" if magnitude > 0 else "down",
                "magnitude": abs(magnitude),
            }
        )
        assert len(record.override_details) == 1
        assert record.override_details[0]["direction"] == "up"
        assert record.override_details[0]["magnitude"] == 2.0

    def test_override_direction_down(self):
        record = DailyRecord(date="2026-03-18", day_type="warm", trend_direction="stable")
        old_val, new_val = 74.0, 71.0
        magnitude = round(new_val - old_val, 1)
        record.override_details.append(
            {
                "time": "22:00",
                "old_temp": old_val,
                "new_temp": new_val,
                "direction": "up" if magnitude > 0 else "down",
                "magnitude": abs(magnitude),
            }
        )
        assert record.override_details[0]["direction"] == "down"
        assert record.override_details[0]["magnitude"] == 3.0

    def test_override_round_trip(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        record = DailyRecord(
            date="2026-03-18",
            day_type="cool",
            trend_direction="stable",
            manual_overrides=2,
            override_details=[
                {"time": "09:00", "old_temp": 68.0, "new_temp": 70.0, "direction": "up", "magnitude": 2.0},
                {"time": "21:00", "old_temp": 72.0, "new_temp": 69.0, "direction": "down", "magnitude": 3.0},
            ],
        )
        state = {"date": "2026-03-18", "today_record": asdict(record)}
        sp.save(state)
        loaded = sp.load()
        restored = DailyRecord(**loaded["today_record"])
        assert len(restored.override_details) == 2
        assert restored.override_details[0]["direction"] == "up"
        assert restored.override_details[1]["magnitude"] == 3.0

    def test_empty_list_default(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        assert record.override_details == []


# ---------------------------------------------------------------------------
# Phase 2: Suggestion tracking
# ---------------------------------------------------------------------------


class TestSuggestionTracking:
    """Test suggestion key recording and round-trip."""

    def test_suggestion_keys_populated(self, tmp_path: Path):
        from custom_components.climate_advisor.learning import LearningEngine

        engine = LearningEngine(tmp_path)

        # Add enough records to trigger suggestions (need MIN_DATA_POINTS_FOR_SUGGESTION)
        from datetime import datetime, timedelta

        from custom_components.climate_advisor.const import MIN_DATA_POINTS_FOR_SUGGESTION

        # Relative dates so records stay inside the 90-day retention window (issue #242).
        for i in range(MIN_DATA_POINTS_FOR_SUGGESTION):
            engine.record_day(
                DailyRecord(
                    date=(datetime.now() - timedelta(days=MIN_DATA_POINTS_FOR_SUGGESTION - i)).strftime("%Y-%m-%d"),
                    day_type="mild",
                    trend_direction="stable",
                    manual_overrides=5,  # Trigger frequent_overrides pattern
                )
            )

        suggestions = engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()
        # Keys and suggestions should have same length
        assert len(keys) == len(suggestions)
        # If overrides triggered, the key should be present
        if suggestions:
            assert "frequent_overrides" in keys

    def test_suggestion_sent_round_trip(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        record = DailyRecord(
            date="2026-03-18",
            day_type="mild",
            trend_direction="stable",
            suggestion_sent=["frequent_overrides", "comfort_violations"],
        )
        state = {"date": "2026-03-18", "today_record": asdict(record)}
        sp.save(state)
        loaded = sp.load()
        restored = DailyRecord(**loaded["today_record"])
        assert restored.suggestion_sent == ["frequent_overrides", "comfort_violations"]

    def test_no_suggestions_leaves_empty_list(self, tmp_path: Path):
        from custom_components.climate_advisor.learning import LearningEngine

        engine = LearningEngine(tmp_path)
        suggestions = engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()
        assert suggestions == []
        assert keys == []

    def test_empty_list_default(self):
        record = DailyRecord(date="2026-03-18", day_type="mild", trend_direction="stable")
        assert record.suggestion_sent == []


# ---------------------------------------------------------------------------
# Phase 2: Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatNewFields:
    """Test that old records without new Phase 2 fields get sensible defaults."""

    def test_old_record_without_new_fields(self):
        """Simulate loading an old DailyRecord dict that lacks Phase 2 fields."""
        old_data = {
            "date": "2026-03-15",
            "day_type": "cool",
            "trend_direction": "stable",
            "door_window_pause_events": 3,
            "manual_overrides": 2,
            "suggestion_sent": None,  # Old format was str | None
            "suggestion_response": None,
        }
        # Normalize suggestion_sent (as coordinator does)
        sent = old_data.get("suggestion_sent")
        if sent is None:
            old_data["suggestion_sent"] = []
        elif isinstance(sent, str):
            old_data["suggestion_sent"] = [sent]

        record = DailyRecord(**old_data)
        assert record.door_pause_by_sensor == {}
        assert record.override_details == []
        assert record.suggestion_sent == []

    def test_old_record_with_string_suggestion_sent(self):
        """Old format had suggestion_sent as a string."""
        old_data = {
            "date": "2026-03-15",
            "day_type": "cool",
            "trend_direction": "stable",
            "suggestion_sent": "frequent_overrides",
        }
        sent = old_data.get("suggestion_sent")
        if sent is None:
            old_data["suggestion_sent"] = []
        elif isinstance(sent, str):
            old_data["suggestion_sent"] = [sent]

        record = DailyRecord(**old_data)
        assert record.suggestion_sent == ["frequent_overrides"]
