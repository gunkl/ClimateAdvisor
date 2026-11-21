"""hvac_season: which season a day belongs to, by what the HVAC did (Issue #1230 R1, Silver coverage).

Occupant view: the prediction scorecard groups a day as heating, cooling/fan ("summer") or shoulder by what the
house actually ran, so a cold spring morning with the heat on is scored with the heating days.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta, timezone

from custom_components.climate_advisor.hvac_season import (
    HEATING,
    SHOULDER,
    SUMMER,
    day_seasons,
    season_of_day,
)


class TestSeasonOfDay:
    def test_heating_wins_over_cooling(self):
        assert season_of_day(True, True) == HEATING

    def test_cooling_or_fan_is_summer(self):
        assert season_of_day(False, True) == SUMMER

    def test_neither_is_shoulder(self):
        assert season_of_day(False, False) == SHOULDER


class TestDaySeasons:
    def test_heating_and_cooling_same_day_is_heating(self):
        rows = [
            {"ts": "2026-03-01T06:00:00+00:00", "hvac": "heating"},
            {"ts": "2026-03-01T15:00:00+00:00", "hvac": "cooling"},
        ]
        assert day_seasons(rows, UTC) == {date(2026, 3, 1): HEATING}

    def test_cooling_day_is_summer_and_idle_day_absent(self):
        rows = [
            {"ts": "2026-07-01T15:00:00+00:00", "hvac": "Cooling"},
            {"ts": "2026-07-02T15:00:00+00:00", "hvac": "idle"},
        ]
        assert day_seasons(rows, UTC) == {date(2026, 7, 1): SUMMER}

    def test_whole_house_fan_with_nat_vent_is_summer(self):
        rows = [{"ts": "2026-06-01T20:00:00+00:00", "hvac": "idle", "fan_running": True, "nat_vent_active": True}]
        assert day_seasons(rows, UTC) == {date(2026, 6, 1): SUMMER}

    def test_legacy_fan_key_with_hvac_fan_action_is_summer(self):
        rows = [{"ts": "2026-06-02T20:00:00+00:00", "hvac": "fan", "fan": True}]
        assert day_seasons(rows, UTC) == {date(2026, 6, 2): SUMMER}

    def test_fan_running_without_nat_vent_or_fan_action_is_not_summer(self):
        rows = [{"ts": "2026-06-03T20:00:00+00:00", "hvac": "idle", "fan_running": True}]
        assert day_seasons(rows, UTC) == {}

    def test_bad_rows_and_timestamps_are_skipped(self):
        rows = [
            "not a mapping",
            {"ts": None, "hvac": "heating"},
            {"ts": "garbage", "hvac": "heating"},
            {"hvac": "heating"},
        ]
        assert day_seasons(rows, UTC) == {}

    def test_naive_ts_read_in_tz_and_aware_ts_converted_to_local_day(self):
        tz = timezone(timedelta(hours=-8))
        rows = [
            {"ts": "2026-01-05T23:30:00", "hvac": "heating"},  # naive: already local
            {"ts": "2026-01-07T03:00:00+00:00", "hvac": "cooling"},  # 19:00 local on the 6th
        ]
        assert day_seasons(rows, tz) == {date(2026, 1, 5): HEATING, date(2026, 1, 6): SUMMER}
