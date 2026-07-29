"""Tests for the Climate Advisor briefing module.

The briefing generator is pure logic with no Home Assistant dependencies,
so no mocking is required. Tests assert on content (temperatures, times,
action items) rather than exact formatting, so they survive tone rewrites.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.climate_advisor.briefing import _derive_warm_day_events, _generate_tldr_table, generate_briefing
from custom_components.climate_advisor.classifier import DayClassification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(
    day_type: str,
    today_high: float,
    today_low: float = 50.0,
    tomorrow_high: float | None = None,
    tomorrow_low: float | None = None,
    trend_direction: str = "stable",
    trend_magnitude: float = 1.0,
) -> DayClassification:
    """Build a DayClassification with convenient defaults."""
    return DayClassification(
        day_type=day_type,
        trend_direction=trend_direction,
        trend_magnitude=trend_magnitude,
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=tomorrow_high if tomorrow_high is not None else today_high,
        tomorrow_low=tomorrow_low if tomorrow_low is not None else today_low,
    )


DEFAULT_WAKE = time(6, 30)
DEFAULT_SLEEP = time(22, 30)
COMFORT_HEAT = 70.0
COMFORT_COOL = 75.0
SETBACK_HEAT = 60.0
SETBACK_COOL = 80.0


def _generate(classification: DayClassification, **kwargs) -> str:
    """Call generate_briefing with standard defaults."""
    return generate_briefing(
        classification=classification,
        comfort_heat=kwargs.get("comfort_heat", COMFORT_HEAT),
        comfort_cool=kwargs.get("comfort_cool", COMFORT_COOL),
        setback_heat=kwargs.get("setback_heat", SETBACK_HEAT),
        setback_cool=kwargs.get("setback_cool", SETBACK_COOL),
        wake_time=kwargs.get("wake_time", DEFAULT_WAKE),
        sleep_time=kwargs.get("sleep_time", DEFAULT_SLEEP),
        learning_suggestions=kwargs.get("learning_suggestions"),
        bedtime_setback_heat=kwargs.get("bedtime_setback_heat"),
        occupancy_mode=kwargs.get("occupancy_mode", "home"),
        predicted_indoor_future=kwargs.get("predicted_indoor_future"),
        predicted_outdoor_future=kwargs.get("predicted_outdoor_future"),
    )


def _make_indoor_curve(
    temps: list[float],
    start_hour_utc: int = 8,
) -> list[dict]:
    """Build a predicted_indoor curve (UTC ISO timestamps)."""
    base = datetime(2026, 5, 11, start_hour_utc, 0, 0, tzinfo=UTC)
    return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]


def _make_outdoor_curve(
    temps: list[float],
    start_hour_utc: int = 8,
) -> list[dict]:
    """Build a predicted_outdoor curve (UTC ISO timestamps)."""
    base = datetime(2026, 5, 11, start_hour_utc, 0, 0, tzinfo=UTC)
    return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]


# ---------------------------------------------------------------------------
# Header tests
# ---------------------------------------------------------------------------


class TestBriefingHeader:
    """The structured header should always contain key weather data."""

    def test_contains_today_high(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "95" in result  # Today's high appears in the TLDR table

    def test_contains_tomorrow_high(self):
        c = _make_classification("mild", today_high=68, today_low=48, tomorrow_high=78, tomorrow_low=58)
        result = _generate(c)
        assert "78" in result  # Tomorrow's high appears in the TLDR table

    def test_contains_day_type(self):
        for day_type in ("hot", "warm", "mild", "cool", "cold"):
            c = _make_classification(day_type, today_high=70)
            result = _generate(c)
            assert day_type.lower() in result.lower()

    def test_contains_trend_info(self):
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=78,
            tomorrow_low=58,
            trend_direction="warming",
            trend_magnitude=10,
        )
        result = _generate(c)
        assert "warm" in result.lower()


# ---------------------------------------------------------------------------
# Day-type content tests
# ---------------------------------------------------------------------------


class TestHotDayBriefing:
    """Hot day briefings should mention pre-cooling, sealed house, AC setpoint."""

    def test_mentions_precool_target(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        # Pre-cool target is comfort_cool - 2 = 73
        assert "73" in result

    def test_mentions_comfort_cool(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "75" in result

    def test_mentions_windows_closed(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "close" in result.lower() or "sealed" in result.lower()

    def test_mentions_blinds(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "blind" in result.lower()


class TestHotDayWindowOpportunities:
    """_hot_day_plan() branches based on morning/evening window opportunity flags."""

    # --- Both opportunities ---

    def test_hot_day_both_opportunities_mentions_morning_times(self):
        """today_low=72 (≤80) gives morning opportunity — start/end times appear."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        assert "6:00 AM" in result
        assert "9:00 AM" in result

    def test_hot_day_both_opportunities_mentions_evening(self):
        """tomorrow_low=70 (≤80) gives evening opportunity — evening start time appears."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        assert "5:00 PM" in result

    def test_hot_day_both_opportunities_mentions_threshold(self):
        """With opportunities, threshold (comfort_cool 75 + delta 3 = 78) appears."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        assert "78" in result

    # --- Morning only ---

    def test_hot_day_morning_only(self):
        """today_low=72 → morning opportunity; tomorrow_low=82 → no evening opportunity."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=82)
        result = _generate(c)
        assert "6:00 AM" in result
        assert "9:00 AM" in result
        assert "5:00 PM" not in result

    # --- Evening only ---

    def test_hot_day_evening_only(self):
        """today_low=82 → no morning opportunity; tomorrow_low=70 → evening opportunity."""
        c = _make_classification("hot", today_high=95, today_low=82, tomorrow_low=70)
        result = _generate(c)
        assert "5:00 PM" in result
        assert "6:00 AM" not in result

    # --- No opportunities ---

    def test_hot_day_no_opportunities_sealed(self):
        """today_low=82, tomorrow_low=82 → no opportunities — sealed/closed language, no time ranges."""
        c = _make_classification("hot", today_high=95, today_low=82, tomorrow_low=82)
        result = _generate(c)
        low = result.lower()
        assert "sealed" in low or "closed" in low
        assert "6:00 AM" not in result
        assert "9:00 AM" not in result
        assert "5:00 PM" not in result

    # --- Removed false notification promise ---

    def test_hot_day_no_false_notification_promise(self):
        """Old 'heads-up' / 'send' notification promise should not appear in the hot day body."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        # The old implementation promised to send a notification; that's been removed.
        # (Note: "hands-off" in grace sections is unrelated — we're checking the day plan body.
        #  Use a hot day with no grace to isolate the plan text.)
        assert "send" not in result.lower()

    # --- Persistent content regardless of opportunities ---

    def test_hot_day_still_mentions_precool(self):
        """Pre-cool target (comfort_cool - 2 = 73) should always appear on hot days."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        assert "73" in result

    def test_hot_day_still_mentions_blinds(self):
        """Blind/sun-facing guidance should appear on hot days regardless of opportunity branch."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = _generate(c)
        assert "blind" in result.lower()

    def test_hot_day_morning_only_still_mentions_blinds(self):
        """Blind guidance appears even in the morning-only branch."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=82)
        result = _generate(c)
        assert "blind" in result.lower()

    def test_hot_day_no_opportunities_still_mentions_blinds(self):
        """Blind guidance appears in the sealed/no-opportunity branch too."""
        c = _make_classification("hot", today_high=95, today_low=82, tomorrow_low=82)
        result = _generate(c)
        assert "blind" in result.lower()

    # --- Fan mode ---

    def test_hot_day_fan_mode_with_opportunities(self):
        """fan_mode != disabled AND opportunities exist → fan paragraph appears."""
        c = _make_classification("hot", today_high=95, today_low=72, tomorrow_low=70)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            fan_mode="whole_house_fan",
        )
        assert "fan" in result.lower()

    def test_hot_day_fan_mode_without_opportunities(self):
        """fan_mode != disabled but no opportunities → fan paragraph does NOT appear."""
        c = _make_classification("hot", today_high=95, today_low=82, tomorrow_low=82)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            fan_mode="whole_house_fan",
        )
        assert "fan" not in result.lower()


class TestWarmDayBriefing:
    """Warm day briefings should mention windows and optional AC safety net."""

    def test_mentions_window_open_time(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        # Window open time for warm days is 6:00 AM
        assert "6" in result

    def test_mentions_window_close_time(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        # Window close time for warm days is 10:00 AM
        assert "10" in result

    def test_mentions_comfort_cool_safety_net(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        assert "75" in result

    def test_mentions_cross_ventilation(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        assert "cross" in result.lower() or "opposite" in result.lower()


class TestMildDayBriefing:
    """Mild day briefings should celebrate the sweet spot and mention windows."""

    def test_mentions_comfort_heat(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c)
        assert "70" in result

    def test_mentions_window_open_time(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c)
        # Window open time for mild days is 10:00 AM
        assert "10" in result

    def test_mentions_window_close_time(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c)
        # Window close time for mild days is 5:00 PM
        assert "5" in result or "17" in result

    def test_mentions_no_hvac_needed(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c)
        low = result.lower()
        assert (
            "no hvac" in low
            or "hvac" not in low
            or "off" in low
            or "sweet spot" in low
            or "takes care of itself" in low
        )

    def test_uses_ode_close_time_when_forecast_available(self):
        """Issue #534: MILD-day close time uses the ODE-derived cutoff when a forecast curve
        is available, instead of always falling back to the static classifier hour (5 PM)."""
        c = _make_classification("mild", today_high=68, today_low=48)
        # Outdoor crosses indoor at hour 13 UTC (index 3) — well before the static 5 PM fallback.
        indoor = _make_indoor_curve([65.0, 66.0, 67.0, 68.0], start_hour_utc=10)
        outdoor = _make_outdoor_curve([55.0, 58.0, 62.0, 67.5], start_hour_utc=10)
        result = _generate(c, predicted_indoor_future=indoor, predicted_outdoor_future=outdoor)
        assert "1:00 PM" in result
        assert "5:00 PM" not in result

    def test_falls_back_to_static_close_time_without_forecast(self):
        """No forecast curve (fresh install / uncalibrated model) — unchanged static fallback."""
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c)
        assert "5" in result or "17" in result


class TestCoolDayBriefing:
    """Cool day briefings should mention heating and keeping sealed."""

    def test_mentions_comfort_heat(self):
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        assert "70" in result

    def test_mentions_bedtime_setback(self):
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        # Bedtime setback is comfort_heat - 4 = 66
        assert "66" in result

    def test_mentions_windows_closed(self):
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        assert "close" in result.lower() or "sealed" in result.lower() or "cool" in result.lower()


class TestColdDayBriefing:
    """Cold day briefings should mention conservation mode and heating strategy."""

    def test_mentions_comfort_heat(self):
        c = _make_classification("cold", today_high=38, today_low=22)
        result = _generate(c)
        assert "70" in result

    def test_mentions_conservative_setback(self):
        c = _make_classification("cold", today_high=38, today_low=22)
        result = _generate(c)
        # Conservative setback is comfort_heat - 3 = 67
        assert "67" in result

    def test_mentions_curtains(self):
        c = _make_classification("cold", today_high=38, today_low=22)
        result = _generate(c)
        assert "curtain" in result.lower()

    def test_preheat_on_cooling_trend(self):
        c = _make_classification(
            "cold",
            today_high=38,
            today_low=22,
            tomorrow_high=28,
            tomorrow_low=12,
            trend_direction="cooling",
            trend_magnitude=10,
        )
        result = _generate(c)
        # Should mention pre-heating / banking heat
        low = result.lower()
        assert "pre-heat" in low or "bank" in low or "extra heat" in low or "extra warm" in low

    def test_no_preheat_on_stable_trend(self):
        c = _make_classification("cold", today_high=38, today_low=22, trend_direction="stable", trend_magnitude=1)
        result = _generate(c)
        # Should NOT mention pre-heating
        assert "pre-heat" not in result.lower() or "bank extra heat" not in result.lower()

    def test_cold_day_plan_uses_adaptive_setback_heat(self):
        """_cold_day_plan uses bedtime_setback_heat when provided, not comfort_heat - 3."""
        c = _make_classification("cold", today_high=38, today_low=22)
        result = _generate(c, comfort_heat=70.0, bedtime_setback_heat=65.0)
        # The adaptive value 65 should appear; the default fallback 67 (70-3) should not
        assert "65" in result
        assert "67" not in result

    def test_cold_day_plan_falls_back_to_default_when_setback_none(self):
        """_cold_day_plan falls back to comfort_heat - 3 when bedtime_setback_heat is None."""
        c = _make_classification("cold", today_high=38, today_low=22)
        # bedtime_setback_heat defaults to None in _generate when not supplied
        result = _generate(c, comfort_heat=70.0)
        # Fallback: 70 - 3 = 67 should appear in the cold-day plan text
        assert "67" in result


# ---------------------------------------------------------------------------
# Universal section tests
# ---------------------------------------------------------------------------


class TestLeavingHomeSection:
    """Leaving home info should be present for all HVAC modes."""

    def test_heat_mode_mentions_setback(self):
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        assert "60" in result  # setback_heat
        assert "leave" in result.lower() or "head out" in result.lower()

    def test_cool_mode_mentions_setback(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "80" in result  # setback_cool

    def test_off_mode_present(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        low = result.lower()
        assert "leave" in low or "head out" in low or "hvac is off" in low or "nothing" in low


class TestLeavingHomeSectionOccupancy:
    """Issue #85: leaving home section should adapt to actual occupancy mode."""

    def test_away_mode_shows_away_text(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="away")
        low = result.lower()
        assert "currently away" in low or "you're away" in low

    def test_vacation_mode_shows_vacation_text(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="vacation")
        low = result.lower()
        assert "vacation" in low

    def test_guest_mode_shows_guest_text(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="guest")
        low = result.lower()
        assert "guest" in low

    def test_home_mode_shows_hypothetical(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="home")
        low = result.lower()
        assert "head out" in low or "leave" in low


class TestTldrTableOccupancy:
    """Issue #85: TLDR table should reflect occupancy state."""

    def test_away_shows_setback_in_hvac_row(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="away")
        assert "setback" in result.lower()

    def test_away_shows_occupancy_row(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="away")
        assert "Occupancy:" in result
        assert "Away" in result

    def test_vacation_shows_occupancy_row(self):
        c = _make_classification("cold", today_high=40, today_low=20)
        result = _generate(c, occupancy_mode="vacation")
        assert "Occupancy:" in result
        assert "Vacation" in result

    def test_guest_shows_occupancy_row(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="guest")
        assert "Occupancy:" in result
        assert "Guest" in result

    def test_home_no_occupancy_row(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c, occupancy_mode="home")
        assert "Occupancy:" not in result


class TestFreshAirSection:
    """Fresh air section should affirm user's choice and explain impact."""

    def test_affirms_opening_window(self):
        """All modes should say opening a window is fine."""
        for day_type, high in [("hot", 95), ("cool", 55), ("warm", 80)]:
            c = _make_classification(day_type, today_high=high)
            result = _generate(c)
            low = result.lower()
            assert "window" in low
            assert "no problem" in low or "go for it" in low

    def test_cool_mode_mentions_ac_pause(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        low = result.lower()
        assert "ac" in low
        # Pause threshold appears as the configured debounce duration
        assert "minutes" in low or "few minutes" in low

    def test_heat_mode_mentions_heat_pause(self):
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        low = result.lower()
        assert "heat" in low
        # Pause threshold appears as the configured debounce duration
        assert "minutes" in low or "few minutes" in low

    def test_fresh_air_uses_configured_debounce(self):
        """The pause threshold in the fresh air section should reflect debounce config."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            debounce_seconds=180,  # 3 minutes
        )
        assert "3 minutes" in result.lower()

    def test_fresh_air_default_debounce_shown(self):
        """Default debounce (5 minutes) should appear in fresh air section."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            debounce_seconds=300,  # 5 minutes (default)
        )
        assert "5 minutes" in result.lower()

    def test_off_mode_no_energy_impact(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        low = result.lower()
        assert "no energy impact" in low or "hvac is off" in low

    def test_includes_minimize_impact_tip(self):
        """Active HVAC modes should suggest how to minimize impact."""
        for day_type, high in [("hot", 95), ("cool", 55)]:
            c = _make_classification(day_type, today_high=high)
            result = _generate(c)
            low = result.lower()
            assert "minimize" in low or "shorter" in low or "quick" in low


class TestTonightPreview:
    """Tonight/tomorrow preview should mention tomorrow's conditions."""

    def test_warming_trend(self):
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=80,
            tomorrow_low=60,
            trend_direction="warming",
            trend_magnitude=10,
        )
        result = _generate(c)
        assert "80" in result  # tomorrow_high

    def test_cooling_trend(self):
        c = _make_classification(
            "cool",
            today_high=55,
            today_low=35,
            tomorrow_high=45,
            tomorrow_low=25,
            trend_direction="cooling",
            trend_magnitude=10,
        )
        result = _generate(c)
        assert "45" in result  # tomorrow_high

    def test_stable_trend(self):
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=69,
            tomorrow_low=49,
            trend_direction="stable",
            trend_magnitude=1,
        )
        result = _generate(c)
        assert "69" in result  # tomorrow_high


# ---------------------------------------------------------------------------
# Learning suggestions tests
# ---------------------------------------------------------------------------


class TestLearningSuggestions:
    """Learning suggestions should appear when provided."""

    def test_suggestions_present(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        suggestions = ["You might want to adjust setpoints", "Windows rarely opened"]
        result = _generate(c, learning_suggestions=suggestions)
        assert "adjust setpoints" in result
        assert "Windows rarely opened" in result

    def test_no_suggestions_when_none(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        result = _generate(c, learning_suggestions=None)
        assert "Suggestions" not in result or "suggestion" not in result.lower()

    def test_accept_dismiss_instructions(self):
        c = _make_classification("mild", today_high=68, today_low=48)
        suggestions = ["Test suggestion"]
        result = _generate(c, learning_suggestions=suggestions)
        assert "AI tab" in result


# ---------------------------------------------------------------------------
# Non-empty output tests
# ---------------------------------------------------------------------------


class TestBriefingNotEmpty:
    """Every day type should produce a non-trivial briefing."""

    @pytest.mark.parametrize(
        "day_type,high",
        [
            ("hot", 95),
            ("warm", 80),
            ("mild", 68),
            ("cool", 55),
            ("cold", 38),
        ],
    )
    def test_produces_output(self, day_type, high):
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        assert len(result) > 100


# ---------------------------------------------------------------------------
# Conversational tone tests
# ---------------------------------------------------------------------------


class TestConversationalTone:
    """Verify the briefing uses conversational prose, not structured headers."""

    @pytest.mark.parametrize(
        "day_type,high",
        [
            ("hot", 95),
            ("warm", 80),
            ("mild", 68),
            ("cool", 55),
            ("cold", 38),
        ],
    )
    def test_no_section_header_dashes(self, day_type, high):
        """Body text should not have lines that are just dashes (old header style)."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        # Split into lines after the structured header (first "========" line)
        body_started = False
        for line in result.split("\n"):
            if "========" in line:
                body_started = True
                continue
            if body_started and line.strip():
                # Lines of just dashes indicate old header format
                assert line.strip() != "-" * 40, f"Found header dash line in {day_type} briefing"

    @pytest.mark.parametrize(
        "day_type,high",
        [
            ("hot", 95),
            ("warm", 80),
            ("mild", 68),
            ("cool", 55),
            ("cold", 38),
        ],
    )
    def test_first_person_voice(self, day_type, high):
        """Body should use first-person voice (I'll, I'm, I've)."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        low = result.lower()
        assert "i'll" in low or "i'm" in low or "i've" in low, f"Expected first-person voice in {day_type} briefing"

    @pytest.mark.parametrize(
        "day_type,high",
        [
            ("hot", 95),
            ("warm", 80),
            ("mild", 68),
            ("cool", 55),
            ("cold", 38),
        ],
    )
    def test_no_checkbox_markers(self, day_type, high):
        """Body should not use old-style checkbox markers."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        # The structured header + learning section may have emoji,
        # but the body should not have ✅ markers
        body = result.split("\n\n", 2)[-1]  # skip header block
        if "Suggestions" not in body:
            assert "✅" not in body, f"Found checkbox marker in {day_type} briefing body"

    @pytest.mark.parametrize(
        "day_type,high",
        [
            ("hot", 95),
            ("warm", 80),
            ("mild", 68),
            ("cool", 55),
            ("cold", 38),
        ],
    )
    def test_no_system_third_person(self, day_type, high):
        """Should say 'I'll' not 'the system will'."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        assert "the system will" not in result.lower()
        assert "climate advisor will" not in result.lower()


# ---------------------------------------------------------------------------
# Grace period section tests
# ---------------------------------------------------------------------------


class TestGracePeriodSection:
    """Grace period section should only appear when a grace period is active."""

    def test_no_grace_section_by_default(self):
        """No grace period info when nothing is active."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = _generate(c)
        assert "grace" not in result.lower()
        assert "hands-off" not in result.lower()
        assert "settling period" not in result.lower()

    def test_manual_grace_active_shown(self):
        """When a manual grace period is active, briefing explains the hands-off window."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            grace_active=True,
            grace_source="manual",
            manual_grace_seconds=1800,
        )
        low = result.lower()
        assert "hands-off" in low
        assert "30 minutes" in low
        # Door/window sensing is suppressed during grace
        assert "door" in low or "window" in low

    def test_automation_grace_active_shown(self):
        """When an automation grace period is active, briefing explains the settling period."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            grace_active=True,
            grace_source="automation",
            automation_grace_seconds=300,
        )
        low = result.lower()
        assert "settling period" in low
        assert "5 minutes" in low

    def test_grace_section_duration_reflects_config(self):
        """Grace period duration shown should match what was configured."""
        c = _make_classification("mild", today_high=68, today_low=48)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            grace_active=True,
            grace_source="manual",
            manual_grace_seconds=600,  # 10 minutes
        )
        assert "10 minutes" in result.lower()

    def test_grace_inactive_with_source_is_suppressed(self):
        """grace_active=False suppresses section even if grace_source is set."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            grace_active=False,
            grace_source="manual",
        )
        assert "hands-off" not in result.lower()
        assert "settling period" not in result.lower()


# ---------------------------------------------------------------------------
# TLDR table tests
# ---------------------------------------------------------------------------


def _make_config(
    comfort_heat: float = COMFORT_HEAT,
    comfort_cool: float = COMFORT_COOL,
    setback_heat: float = SETBACK_HEAT,
    setback_cool: float = SETBACK_COOL,
    sleep_time: time = DEFAULT_SLEEP,
    wake_time: time = DEFAULT_WAKE,
) -> dict:
    return {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": setback_heat,
        "setback_cool": setback_cool,
        "sleep_time": sleep_time,
        "wake_time": wake_time,
    }


class TestTldrTable:
    """_generate_tldr_table() should produce accurate rows for each day type."""

    def test_tldr_hot_day_type_row(self):
        c = _make_classification("hot", today_high=92, today_low=70)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Hot" in table
        assert "92" in table

    def test_tldr_hot_day_hvac_mode_row(self):
        c = _make_classification("hot", today_high=92, today_low=70)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Cool at 75" in table

    def test_tldr_hot_day_windows_row(self):
        """Hot days with both opportunities show time ranges and threshold in Windows row."""
        # today_low=70 (≤80) → morning; tomorrow_low=70 (≤80) → evening
        c = _make_classification("hot", today_high=92, today_low=70, tomorrow_low=70)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        # Should contain morning start time, morning end time, evening start time, and threshold
        assert "6:00 AM" in table or "6" in table
        assert "9:00 AM" in table or "9" in table
        assert "5:00 PM" in table or "5" in table
        # Threshold: comfort_cool (75) + ECONOMIZER_TEMP_DELTA (3) = 78
        assert "78" in table

    def test_tldr_hot_day_windows_morning_only(self):
        """Hot day with morning-only opportunity shows morning time range and threshold, no evening."""
        # today_low=72 (≤80) → morning; tomorrow_low=82 (>80) → no evening
        c = _make_classification("hot", today_high=92, today_low=72, tomorrow_low=82)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "6:00 AM" in table or "6" in table
        assert "9:00 AM" in table or "9" in table
        assert "78" in table
        assert "5:00 PM" not in table

    def test_tldr_hot_day_windows_evening_only(self):
        """Hot day with evening-only opportunity shows evening start time and threshold."""
        # today_low=82 (>80) → no morning; tomorrow_low=70 (≤80) → evening
        c = _make_classification("hot", today_high=92, today_low=82, tomorrow_low=70)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "5:00 PM" in table or "5" in table
        assert "78" in table
        assert "6:00 AM" not in table

    def test_tldr_hot_day_windows_neither(self):
        """Hot day with no opportunities shows 'Closed all day'."""
        # today_low=82 (>80) → no morning; tomorrow_low=82 (>80) → no evening
        c = _make_classification("hot", today_high=92, today_low=82, tomorrow_low=82)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Closed all day" in table

    def test_tldr_warm_day_windows_open_times(self):
        """Warm days should show open/close times in the Windows row."""
        c = _make_classification("warm", today_high=80, today_low=60)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        # warm day window_open_time=06:00, window_close_time=10:00
        assert "Open" in table
        assert "6" in table  # open time hour (6:00 AM)
        assert "10" in table  # close time (10:00 AM)

    def test_tldr_cold_day_hvac_mode_row(self):
        c = _make_classification("cold", today_high=38, today_low=22)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Heat at 70" in table

    def test_tldr_cold_day_windows_row(self):
        """Cold days have no window recommendation — should say 'Closed all day'."""
        c = _make_classification("cold", today_high=38, today_low=22)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Closed all day" in table

    def test_tldr_contains_bedtime_heat_info(self):
        """Heat mode: bedtime setback should be comfort_heat - 4."""
        c = _make_classification("cool", today_high=55, today_low=35)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        # COMFORT_HEAT=70, setback = 70-4 = 66
        assert "66" in table
        assert "10:30 PM" in table  # DEFAULT_SLEEP

    def test_tldr_contains_bedtime_cool_info(self):
        """Cool mode: bedtime setback should be comfort_cool + 3."""
        c = _make_classification("hot", today_high=95, today_low=72)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        # COMFORT_COOL=75, setback = 75+3 = 78
        assert "78" in table

    def test_tldr_off_mode_no_setback(self):
        """Off mode days should show 'No setback'."""
        c = _make_classification("warm", today_high=80, today_low=60)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "No setback" in table

    def test_tldr_contains_tomorrow_trend(self):
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=80,
            tomorrow_low=60,
            trend_direction="warming",
            trend_magnitude=12,
        )
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "80" in table  # tomorrow high
        assert "warm" in table.lower()

    def test_tldr_stable_tomorrow(self):
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=69,
            tomorrow_low=49,
            trend_direction="stable",
            trend_magnitude=1,
        )
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Stable" in table
        assert "69" in table

    def test_tldr_is_plain_text_format(self):
        """Output should use plain-text colon-aligned format, not markdown pipe table."""
        c = _make_classification("hot", today_high=92, today_low=70)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        # Must contain colon-delimited rows
        assert "Day Type:" in table
        # Must NOT contain markdown pipe syntax
        assert "|" not in table, f"Pipe chars found in tldr table: {table!r}"


# ---------------------------------------------------------------------------
# Verbosity parameter tests
# ---------------------------------------------------------------------------


class TestVerbosity:
    """generate_briefing() verbosity parameter should control output length/content."""

    def test_verbosity_tldr_only_no_body(self):
        """tldr_only should omit all conversational body paragraphs."""
        c = _make_classification("hot", today_high=92, today_low=70)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        # Should NOT have the header (push notification title handles that)
        assert "Your Home Climate Plan" not in result
        # Should have the TLDR table
        assert "Day Type:" in result
        assert "HVAC Mode:" in result
        # Should NOT have conversational body phrases
        assert "I pre-cooled" not in result
        assert "head out" not in result

    def test_verbosity_tldr_only_has_table(self):
        """tldr_only must include the TLDR table."""
        c = _make_classification("cold", today_high=38, today_low=22)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        assert "Day Type:" in result
        assert "HVAC Mode:" in result
        assert "Windows:" in result
        assert "Bedtime Setback:" in result
        assert "Tomorrow:" in result

    def test_verbosity_normal_has_tldr_and_body(self):
        """normal verbosity should include both the TLDR table and body text."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="normal",
        )
        # TLDR table present
        assert "Day Type:" in result
        # Conversational body present
        assert "I'll" in result or "I'm" in result

    def test_verbosity_normal_is_default(self):
        """Omitting verbosity should produce the same output as verbosity='normal'."""
        c = _make_classification("mild", today_high=68, today_low=48)
        default_result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
        )
        normal_result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="normal",
        )
        assert default_result == normal_result

    def test_verbosity_tldr_only_shorter_than_normal(self):
        """tldr_only output should always be shorter than normal output."""
        c = _make_classification("warm", today_high=80, today_low=60)
        tldr = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        normal = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="normal",
        )
        assert len(tldr) < len(normal)


# ---------------------------------------------------------------------------
# Phase 5G: adaptive thermal briefing tests
# ---------------------------------------------------------------------------


class TestAdaptiveThermalBriefing:
    """Tests for bedtime_setback_heat and adaptive_thermal_active parameters."""

    def test_tldr_table_uses_passed_bedtime_setback_heat(self):
        """generate_briefing with bedtime_setback_heat=62 on a heat day → 62 appears in output."""
        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            bedtime_setback_heat=62.0,
        )
        assert "62" in result

    def test_tldr_table_falls_back_to_default_when_none(self):
        """generate_briefing with bedtime_setback_heat=None → uses comfort_heat - 4."""
        from custom_components.climate_advisor.const import DEFAULT_SETBACK_DEPTH_F

        c = _make_classification("cool", today_high=55, today_low=35)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            bedtime_setback_heat=None,
        )
        # Default: comfort_heat (70) - DEFAULT_SETBACK_DEPTH_F (4) = 66
        expected = str(int(COMFORT_HEAT - DEFAULT_SETBACK_DEPTH_F))
        assert expected in result

    def test_adaptive_thermal_active_adds_sentence(self):
        """adaptive_thermal_active=True → 'tuned to your home's actual heating performance' appears."""
        c = _make_classification(
            "cool",
            today_high=55,
            today_low=35,
            trend_direction="stable",
            trend_magnitude=1.0,
        )
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            adaptive_thermal_active=True,
        )
        assert "tuned to your home's actual heating performance" in result

    def test_verbosity_verbose_has_body(self):
        """verbose should include body text (same as normal for now)."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="verbose",
        )
        assert "Day Type:" in result
        assert "I'll" in result or "I'm" in result or "I pre-cooled" in result


# ---------------------------------------------------------------------------
# TLDR length guard (Issue #34)
# ---------------------------------------------------------------------------


class TestTldrLength:
    """Verify tldr_only briefings stay short enough for push notifications."""

    @pytest.mark.parametrize(
        "day_type,high,low",
        [
            ("hot", 95, 72),
            ("warm", 80, 60),
            ("mild", 72, 55),
            ("cool", 55, 40),
            ("cold", 30, 15),
        ],
    )
    def test_tldr_under_300_chars(self, day_type, high, low):
        """tldr_only output must stay under 300 chars for all day types."""
        c = _make_classification(day_type, today_high=high, today_low=low)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        assert len(result) <= 250, f"tldr_only for {day_type} is {len(result)} chars (max 250)"

    def test_tldr_contains_day_type_table(self):
        """tldr_only should include the TLDR table with Day Type row."""
        c = _make_classification("mild", today_high=72, today_low=55)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        assert "Day Type:" in result

    def test_tldr_excludes_conversational_body(self):
        """tldr_only should NOT include conversational plan sections."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        # Conversational body includes phrases like "tonight" or "fresh air"
        # TLDR should be just header + table
        normal = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="normal",
        )
        assert len(result) < len(normal), "tldr_only should be shorter than normal"


# ---------------------------------------------------------------------------
# No markdown in briefing output (Issue #21)
# ---------------------------------------------------------------------------


class TestNoMarkdownInBriefing:
    """Verify briefing output contains no markdown syntax."""

    @pytest.mark.parametrize(
        "day_type,high,low",
        [
            ("hot", 95, 72),
            ("warm", 80, 60),
            ("mild", 72, 55),
            ("cool", 55, 40),
            ("cold", 30, 15),
        ],
    )
    def test_no_pipe_table_in_tldr(self, day_type, high, low):
        """TLDR table rows must not contain markdown pipe table syntax."""
        c = _make_classification(day_type, today_high=high, today_low=low)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            verbosity="tldr_only",
        )
        # Check table lines (indented with 2 spaces) don't use pipe syntax
        table_lines = [line for line in result.splitlines() if line.startswith("  ")]
        for line in table_lines:
            assert "|" not in line, f"Pipe char in table line: {line}"
        # Verify no markdown separator row
        assert "---" not in result or "===" in result, "Markdown separator found"

    @pytest.mark.parametrize("occupancy", ["home", "away", "guest", "vacation"])
    def test_no_bold_markers_in_full_briefing(self, occupancy):
        """Full briefing must not contain markdown bold markers."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            occupancy_mode=occupancy,
        )
        assert "**" not in result, f"Bold markers found in output: {result}"


# ---------------------------------------------------------------------------
# Celsius display tests
# ---------------------------------------------------------------------------


class TestCelsiusBriefing:
    """Tests that briefing output uses Celsius when temp_unit='celsius'."""

    def test_tldr_table_shows_celsius_not_fahrenheit(self):
        """TLDR table temperatures appear in °C when unit is celsius."""
        c = _make_classification("hot", today_high=95, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            temp_unit="celsius",
        )
        assert "°C" in result
        assert "°F" not in result

    def test_hot_day_threshold_displayed_in_celsius(self):
        """Hot day classification shows correct Celsius value in briefing body.

        today_high=90°F → (90-32)*5/9 = 32°C.
        """
        c = _make_classification("hot", today_high=90, today_low=72)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            temp_unit="celsius",
        )
        # 90°F → 32°C
        assert "32°C" in result

    def test_trend_delta_displayed_in_celsius(self):
        """Trend magnitude is shown in °C (scale-only conversion, no offset).

        A warming trend of 9°F → 9*5/9 = 5°C.
        """
        c = _make_classification(
            "mild",
            today_high=68,
            today_low=48,
            tomorrow_high=77,
            tomorrow_low=57,
            trend_direction="warming",
            trend_magnitude=9,
        )
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            temp_unit="celsius",
        )
        # 9°F delta → 5°C delta
        assert "5°C" in result

    def test_fahrenheit_default_unchanged(self):
        """Without temp_unit arg, output is identical to passing temp_unit='fahrenheit'.

        This is a regression guard to ensure the default remains Fahrenheit.
        """
        c = _make_classification("cool", today_high=55, today_low=35)
        default_result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
        )
        explicit_f_result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            temp_unit="fahrenheit",
        )
        assert default_result == explicit_f_result

    def test_celsius_output_contains_degree_c_symbol(self):
        """Basic check: output has °C when celsius unit is specified."""
        c = _make_classification("mild", today_high=68, today_low=48)
        result = generate_briefing(
            classification=c,
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=DEFAULT_WAKE,
            sleep_time=DEFAULT_SLEEP,
            temp_unit="celsius",
        )
        assert "°C" in result


# ---------------------------------------------------------------------------
# Phase 3: ODE warm-day event derivation tests
# ---------------------------------------------------------------------------


class TestDeriveWarmDayEvents:
    """Tests for _derive_warm_day_events() helper."""

    def test_nat_vent_cutoff_when_outdoor_crosses_indoor(self):
        """First hour outdoor >= indoor-1 F is the nat-vent cutoff."""
        # indoor: 72, 73, 74, 75 (hour 8-11 UTC)
        # outdoor: 65, 68, 73.0, 76 — outdoor crosses (74-1=73) at index 2 (UTC hour 10)
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.0], start_hour_utc=8)
        outdoor = _make_outdoor_curve([65.0, 68.0, 73.0, 76.0], start_hour_utc=8)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=75.0,
        )
        # outdoor[2]=73.0 >= indoor[2](74.0)-1.0=73.0 -> cutoff at UTC hour 10
        assert events["nat_vent_cutoff"] is not None
        assert events["nat_vent_cutoff"].hour == 10

    def test_ceiling_breach_detected(self):
        """ceiling_breach_time is first indoor > comfort_cool."""
        indoor = _make_indoor_curve([72.0, 73.0, 75.5], start_hour_utc=8)
        outdoor = _make_outdoor_curve([65.0, 70.0, 76.0], start_hour_utc=8)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=75.0,
        )
        # breach at hour 10 UTC (index 2, temp 75.5 > 75.0)
        assert events["ceiling_breach_time"] is not None
        assert events["ceiling_breach_time"].hour == 10

    def test_returns_none_fields_when_no_data(self):
        """Empty or None curves return None for all fields."""
        events = _derive_warm_day_events(
            predicted_indoor=None,
            predicted_outdoor=None,
            comfort_cool=75.0,
        )
        assert events["nat_vent_cutoff"] is None
        assert events["ceiling_breach_time"] is None
        assert events["precool_start_time"] is None
        assert events["nat_vent_recovers"] is False

    def test_nat_vent_recovers_when_outdoor_drops_back(self):
        """nat_vent_recovers True when outdoor drops below indoor after cutoff."""
        indoor = _make_indoor_curve([72.0, 73.0, 75.0, 74.0, 71.0], start_hour_utc=8)
        outdoor = _make_outdoor_curve([65.0, 73.0, 76.0, 73.0, 68.0], start_hour_utc=8)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=75.0,
        )
        # outdoor drops below indoor in evening -> nat_vent_recovers=True
        assert events["nat_vent_recovers"] is True

    def test_precool_start_uses_fallback_when_k_active_cool_none(self):
        """precool_start_time = ceiling_breach_time - 120 min when k_active_cool=None."""
        # breach at hour 14 UTC (start_hour=10, index 4 => temp 75.5 > 75)
        indoor = _make_indoor_curve([72.0, 72.5, 73.0, 74.0, 75.5], start_hour_utc=10)
        outdoor = _make_outdoor_curve([75.0, 76.0, 77.0, 78.0, 79.0], start_hour_utc=10)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=75.0,
            k_active_cool=None,
        )
        breach = events["ceiling_breach_time"]
        precool = events["precool_start_time"]
        assert breach is not None
        assert precool is not None
        # fallback = 120 min -> precool = breach - 2h
        assert abs((breach - precool).total_seconds() - 7200) < 60  # within 1 min

    def test_misaligned_curves_pair_by_timestamp_not_index(self):
        """Issue #528 regression: indoor/outdoor curves that start at different hours
        (the real production shape — indoor cached from an earlier cycle, outdoor
        rebuilt fresh) must be paired by matching ISO timestamp, not list position.

        Indoor covers hours 8-11 UTC; outdoor covers hours 10-13 UTC (only 10 and 11
        overlap). The old zip()-by-index implementation would pair indoor[0] (hour 8,
        72F) against outdoor[0] (hour 10, 73F) — two hours apart — and (wrongly)
        report a cutoff at hour 8, since 73 >= 72-1. The timestamp-correct
        implementation has no outdoor entry for hour 8 at all, so it must skip
        straight to the real overlap and report the cutoff at hour 10, where
        outdoor(73) >= indoor(74)-1=73.
        """
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.0], start_hour_utc=8)
        outdoor = _make_outdoor_curve([73.0, 76.0, 78.0, 80.0], start_hour_utc=10)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=100.0,  # keep ceiling_breach out of the way for this test
        )
        assert events["nat_vent_cutoff"] is not None
        assert events["nat_vent_cutoff"].hour == 10
        # The old buggy behavior would have reported hour 8 here.
        assert events["nat_vent_cutoff"].hour != 8

    def test_misaligned_curves_recovery_time_after_true_cutoff(self):
        """Same misalignment shape, extended to prove recovery_time also uses the
        timestamp-correct cutoff as its `after` boundary, not an index-drifted one."""
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.0, 74.0, 70.0], start_hour_utc=8)
        outdoor = _make_outdoor_curve([73.0, 76.0, 71.0, 68.0], start_hour_utc=10)
        events = _derive_warm_day_events(
            predicted_indoor=indoor,
            predicted_outdoor=outdoor,
            comfort_cool=100.0,
        )
        assert events["nat_vent_cutoff"] is not None
        assert events["nat_vent_cutoff"].hour == 10
        assert events["nat_vent_recovers"] is True
        assert events["recovery_time"] is not None
        assert events["recovery_time"] > events["nat_vent_cutoff"]
        assert events["recovery_time"].hour == 12


class TestWarmDayBriefWithPrediction:
    """Enriched warm-day brief includes action/consequence language."""

    def test_includes_window_close_instruction_with_time(self):
        """With predicted data, brief says 'Close up at HH AM/PM'."""
        c = _make_classification("warm", today_high=80, today_low=60)
        # nat-vent cutoff at hour 9 UTC (outdoor crosses indoor-1 at index 1)
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.5], start_hour_utc=8)
        outdoor = _make_outdoor_curve([65.0, 72.0, 75.0, 77.0], start_hour_utc=8)
        result = _generate(c, predicted_indoor_future=indoor, predicted_outdoor_future=outdoor)
        # Should mention closing windows (no jargon like "nat-vent")
        assert "close" in result.lower() or "Close" in result
        assert "nat-vent" not in result

    def test_includes_ac_notice_when_breach_predicted(self):
        """With breach in curve, brief mentions AC running (action + consequence)."""
        c = _make_classification("warm", today_high=80, today_low=60)
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.5], start_hour_utc=8)
        outdoor = _make_outdoor_curve([73.0, 74.0, 76.0, 78.0], start_hour_utc=8)
        result = _generate(c, predicted_indoor_future=indoor, predicted_outdoor_future=outdoor, comfort_cool=75.0)
        # Should mention the AC will run
        assert "AC" in result or "ac" in result.lower() or "cool" in result.lower()
        assert "nat-vent" not in result

    def test_no_model_fallback_to_generic_language(self):
        """No predicted data -> brief falls back to current generic language (no regression)."""
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        # Generic warm-day language must still be present
        assert "75" in result  # comfort_cool mentioned

    def test_no_nat_vent_jargon_in_any_warm_day_brief(self):
        """The phrase 'nat-vent' must never appear in a user-facing briefing."""
        c = _make_classification("warm", today_high=80, today_low=60)
        indoor = _make_indoor_curve([72.0, 73.0, 74.5], start_hour_utc=8)
        outdoor = _make_outdoor_curve([73.0, 75.0, 77.0], start_hour_utc=8)
        result = _generate(c, predicted_indoor_future=indoor, predicted_outdoor_future=outdoor)
        assert "nat-vent" not in result
        assert "nat_vent" not in result

    def test_no_bold_markers_with_prediction(self):
        """Enriched warm-day brief with prediction data must not contain markdown bold markers."""
        c = _make_classification("warm", today_high=80, today_low=60)
        indoor = _make_indoor_curve([72.0, 73.0, 74.0, 75.5], start_hour_utc=8)
        outdoor = _make_outdoor_curve([65.0, 73.0, 75.0, 77.0], start_hour_utc=8)
        result = _generate(c, predicted_indoor_future=indoor, predicted_outdoor_future=outdoor)
        assert "**" not in result, f"Bold markers found in enriched warm-day brief: {result}"
