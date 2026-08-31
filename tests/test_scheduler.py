"""Tests for the Time-of-Use scheduler module (Issue #786).

Pure unit tests — scheduler.py is a stateless helper, no HA coordinator or real hass
needed for the resolver portion. The banking-target/lead-time portion uses a lightweight
thermal-model dict fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from custom_components.climate_advisor.scheduler import (
    COST_TAG_HIGH,
    COST_TAG_LOW,
    MAX_SCHEDULES,
    Schedule,
    TOUPhase,
    is_schedule_active_at,
    resolve_active_schedules,
    resolve_tou_phase,
)

_BASE_CONFIG = {
    "comfort_heat": 68.0,
    "comfort_cool": 76.0,
    "sleep_heat": 64.0,
    "sleep_cool": 72.0,
    "wake_time": "07:00",
    "sleep_time": "22:30",
}


def _dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _midnight_spanning_schedule(days=("fri",)) -> Schedule:
    return Schedule(id="s1", name="test", days=tuple(days), start="23:00", end="01:00", cost_tag=COST_TAG_HIGH)


class TestIsScheduleActiveAt:
    def test_midnight_spanning_schedule_resolves_via_start_day_weekday(self):
        """Friday 23:00->01:00, evaluated at Saturday 00:30 -> active (Friday's window)."""
        schedule = _midnight_spanning_schedule()
        # 2026-01-02 is a Friday; 2026-01-03 is a Saturday.
        saturday_0030 = _dt(2026, 1, 3, 0, 30)
        assert is_schedule_active_at(schedule, saturday_0030) is True

    def test_midnight_spanning_schedule_inactive_outside_window(self):
        schedule = _midnight_spanning_schedule()
        saturday_0200 = _dt(2026, 1, 3, 2, 0)
        friday_2200 = _dt(2026, 1, 2, 22, 0)
        assert is_schedule_active_at(schedule, saturday_0200) is False
        assert is_schedule_active_at(schedule, friday_2200) is False

    def test_midnight_spanning_active_before_midnight_on_start_day(self):
        schedule = _midnight_spanning_schedule()
        friday_2330 = _dt(2026, 1, 2, 23, 30)
        assert is_schedule_active_at(schedule, friday_2330) is True

    def test_empty_days_list_never_active(self):
        schedule = Schedule(id="s2", name="empty", days=(), start="09:00", end="17:00", cost_tag=COST_TAG_HIGH)
        for day in range(1, 8):
            assert is_schedule_active_at(schedule, _dt(2026, 1, day, 12, 0)) is False

    def test_all_days_sentinel_active_every_weekday(self):
        schedule = Schedule(id="s3", name="all", days=("all",), start="09:00", end="17:00", cost_tag=COST_TAG_LOW)
        for day in range(1, 8):
            assert is_schedule_active_at(schedule, _dt(2026, 1, day, 12, 0)) is True

    def test_non_midnight_schedule_respects_day_and_time_bounds(self):
        schedule = Schedule(
            id="s4",
            name="weekday peak",
            days=("mon", "tue", "wed", "thu", "fri"),
            start="16:15",
            end="21:00",
            cost_tag=COST_TAG_HIGH,
        )
        # 2026-01-05 is a Monday.
        assert is_schedule_active_at(schedule, _dt(2026, 1, 5, 17, 0)) is True
        assert is_schedule_active_at(schedule, _dt(2026, 1, 5, 15, 59)) is False
        # 2026-01-10 is a Saturday.
        assert is_schedule_active_at(schedule, _dt(2026, 1, 10, 17, 0)) is False

    def test_hh_mm_ss_format_parsed_same_as_hh_mm(self):
        """HA's TimeSelector returns "HH:MM:SS" (config-flow storage shape) — must resolve
        identically to the plain "HH:MM" shape used elsewhere in these tests."""
        hh_mm_ss = Schedule(id="s5", name="s5", days=("mon",), start="16:15:00", end="21:00:00", cost_tag=COST_TAG_HIGH)
        assert is_schedule_active_at(hh_mm_ss, _dt(2026, 1, 5, 17, 0)) is True
        assert is_schedule_active_at(hh_mm_ss, _dt(2026, 1, 5, 15, 59)) is False


class TestResolveActiveSchedules:
    def test_no_covering_schedule_yields_implicit_normal(self):
        schedule = Schedule(id="s1", name="peak", days=("mon",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        # 2026-01-06 is a Tuesday.
        resolution = resolve_active_schedules([schedule], _dt(2026, 1, 6, 17, 0))
        assert resolution.cost_tag is None
        assert resolution.active_schedule_ids == ()

    def test_covering_schedule_reports_tag_and_id(self):
        schedule = Schedule(id="peak1", name="peak", days=("mon",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_active_schedules([schedule], _dt(2026, 1, 5, 17, 0))
        assert resolution.cost_tag == COST_TAG_HIGH
        assert resolution.active_schedule_ids == ("peak1",)
        assert resolution.schedule_end == _dt(2026, 1, 5, 21, 0)

    def test_midnight_spanning_active_schedule_end_is_next_day(self):
        schedule = _midnight_spanning_schedule()
        friday_2330 = _dt(2026, 1, 2, 23, 30)
        resolution = resolve_active_schedules([schedule], friday_2330)
        assert resolution.schedule_end == _dt(2026, 1, 3, 1, 0)

    def test_multiple_overlapping_schedules_both_reported(self):
        s1 = Schedule(id="a", name="a", days=("mon",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        s2 = Schedule(id="b", name="b", days=("mon",), start="17:00", end="18:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_active_schedules([s1, s2], _dt(2026, 1, 5, 17, 30))
        assert set(resolution.active_schedule_ids) == {"a", "b"}


class TestResolveTouPhase:
    def test_no_indoor_temp_yields_none(self):
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), None, "cool", {"k_active_cool": -2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.NONE

    def test_off_hvac_mode_yields_none(self):
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 72.0, "off", {"k_active_cool": -2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.NONE

    def test_low_tag_schedule_never_triggers_preconditioning(self):
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_LOW)
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 15, 0), 72.0, "cool", {"k_active_cool": -2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.NONE

    def test_preconditioning_targets_comfort_heat_for_cooling_bank(self):
        """cost_tag=high, hvac_mode=cool: banks toward comfort_heat (68.0), mode='cool'."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        # indoor=76, target=68 -> delta_t=8; rate=-2 F/hr -> (8/2)*60*1.3=312, clamped to 240.
        # lead window: [16:00 - 240min, 16:00) = [12:00, 16:00). now=13:00 is inside it.
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 13, 0), 76.0, "cool", {"k_active_cool": -2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.target == 68.0
        assert resolution.mode == "cool"
        assert resolution.schedule_id == "s1"

    def test_preconditioning_targets_comfort_cool_for_heating_bank(self):
        """cost_tag=high, hvac_mode=heat: banks toward comfort_cool (76.0), mode='heat'."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 13, 0), 68.0, "heat", {"k_active_heat": 2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.target == 76.0
        assert resolution.mode == "heat"

    def test_preconditioning_falls_back_to_sleep_heat_when_overlapping_sleep_window(self):
        """Schedule starting at 23:30 (inside the configured 22:30-07:00 sleep window):
        the banking target must follow resolve_comfort_heat()'s sleep-aware fallback
        (sleep_heat=64.0), not the plain comfort_heat=68.0."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="23:30", end="23:59", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_phase(
            [schedule], _dt(2026, 1, 5, 22, 0), 76.0, "cool", {"k_active_cool": -2.0}, _BASE_CONFIG
        )
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.target == 64.0

    def test_lead_minutes_falls_back_when_rate_missing(self):
        """No thermal model rate -> fallback lead time (120 min, clamped [30,240])."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        # Fallback lead = 120 min -> window [14:00, 16:00). now=14:30 is inside it.
        resolution = resolve_tou_phase([schedule], _dt(2026, 1, 5, 14, 30), 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution.phase == TOUPhase.PRECONDITIONING
        # now=13:00 is before the fallback window opens (14:00) -> NONE.
        resolution2 = resolve_tou_phase([schedule], _dt(2026, 1, 5, 13, 0), 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution2.phase == TOUPhase.NONE

    def test_earliest_of_multiple_high_schedules_wins(self):
        later = Schedule(id="later", name="later", days=("all",), start="20:00", end="22:00", cost_tag=COST_TAG_HIGH)
        earlier = Schedule(
            id="earlier", name="earlier", days=("all",), start="16:00", end="18:00", cost_tag=COST_TAG_HIGH
        )
        resolution = resolve_tou_phase([later, earlier], _dt(2026, 1, 5, 14, 30), 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution.phase == TOUPhase.PRECONDITIONING
        assert resolution.schedule_id == "earlier"

    def test_after_schedule_start_no_longer_preconditioning(self):
        """Once now >= schedule_start, phase reverts to NONE (coast — see module docstring:
        confirmed by test_tou_precondition.py that no new code is needed here)."""
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        resolution = resolve_tou_phase([schedule], _dt(2026, 1, 5, 16, 0), 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution.phase == TOUPhase.NONE

    def test_window_fields_populated_even_when_phase_is_none(self):
        """target/mode/schedule_start/precondition_start are populated whenever a
        qualifying schedule is found in the lookahead — regardless of whether `now` is
        currently inside the window — so a chart-rendering caller can show the full
        upcoming window shape for future timestamps, not just answer "act now or not".
        """
        schedule = Schedule(id="s1", name="s", days=("all",), start="16:00", end="21:00", cost_tag=COST_TAG_HIGH)
        # now=13:00 is within the lookahead (schedule starts within 4h) but before the
        # fallback lead window opens (14:00) -> phase=NONE, but the window fields (needed
        # for chart rendering of the upcoming window, not just "act now") must still resolve.
        resolution = resolve_tou_phase([schedule], _dt(2026, 1, 5, 13, 0), 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution.phase == TOUPhase.NONE
        assert resolution.target == 68.0
        assert resolution.mode == "cool"
        assert resolution.schedule_id == "s1"
        assert resolution.schedule_start == _dt(2026, 1, 5, 16, 0)
        assert resolution.precondition_start == _dt(2026, 1, 5, 14, 0)

    def test_dst_spring_forward_nonexistent_hour_does_not_crash(self):
        """A schedule window inside the local hour that US spring-forward skips
        (2026-03-08: 2:00-3:00am local does not exist) must not raise. This module does
        plain wall-clock arithmetic on whatever `datetime` it's given — it has no way to
        know an instant was "skipped" locally, so it simply computes on `.hour`/`.minute`
        as-is. Avoiding construction of a nonexistent local time in the first place is
        `dt_util`'s responsibility upstream, not this module's; this test only guards
        against this module crashing if one slips through.

        Uses a fixed-offset `timezone` object rather than `ZoneInfo`, matching this
        project's existing DST-test convention (test_outdoor_temp_interpolation.py) —
        avoids depending on the platform having an IANA tzdata database installed (not
        guaranteed on Windows without the `tzdata` package).
        """
        est = timezone(timedelta(hours=-5))
        schedule = Schedule(id="s1", name="s", days=("all",), start="02:15", end="02:45", cost_tag=COST_TAG_HIGH)
        nonexistent_local = datetime(2026, 3, 8, 2, 30, tzinfo=est)
        # Must not raise.
        active = is_schedule_active_at(schedule, nonexistent_local)
        assert active in (True, False)
        resolution = resolve_tou_phase([schedule], nonexistent_local, 76.0, "cool", {}, _BASE_CONFIG)
        assert resolution.phase in (TOUPhase.NONE, TOUPhase.PRECONDITIONING)


class TestFiveScheduleCapIsAConfigFlowConcern:
    def test_resolver_does_not_truncate_a_six_item_list(self):
        """MAX_SCHEDULES exists as a documented constant for config_flow.py to enforce —
        the resolver itself takes an arbitrary-length list and must not silently drop
        entries past the cap, keeping the boundary clearly config-flow's job alone."""
        assert MAX_SCHEDULES == 5
        schedules = [
            Schedule(id=f"s{i}", name=f"s{i}", days=("all",), start="00:00", end="01:00", cost_tag=COST_TAG_LOW)
            for i in range(6)
        ]
        resolution = resolve_active_schedules(schedules, _dt(2026, 1, 5, 0, 30))
        assert len(resolution.active_schedule_ids) == 6
