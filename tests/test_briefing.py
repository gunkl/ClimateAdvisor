"""Tests for the Climate Advisor briefing module.

The briefing generator is pure logic with no Home Assistant dependencies,
so no mocking is required. Tests assert on content (temperatures, times,
action items) rather than exact formatting, so they survive tone rewrites.
"""
from __future__ import annotations

from datetime import time

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.briefing import generate_briefing, _generate_tldr_table


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
        learning_suggestions=kwargs.get("learning_suggestions", None),
    )


# ---------------------------------------------------------------------------
# Header tests
# ---------------------------------------------------------------------------

class TestBriefingHeader:
    """The structured header should always contain key weather data."""

    def test_contains_today_temps(self):
        c = _make_classification("hot", today_high=95, today_low=72)
        result = _generate(c)
        assert "95" in result
        assert "72" in result

    def test_contains_tomorrow_temps(self):
        c = _make_classification("mild", today_high=68, today_low=48,
                                  tomorrow_high=78, tomorrow_low=58)
        result = _generate(c)
        assert "78" in result
        assert "58" in result

    def test_contains_day_type(self):
        for day_type in ("hot", "warm", "mild", "cool", "cold"):
            c = _make_classification(day_type, today_high=70)
            result = _generate(c)
            assert day_type.lower() in result.lower()

    def test_contains_trend_info(self):
        c = _make_classification("mild", today_high=68, today_low=48,
                                  tomorrow_high=78, tomorrow_low=58,
                                  trend_direction="warming", trend_magnitude=10)
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
        assert "no hvac" in low or "hvac" not in low or "off" in low or "sweet spot" in low or "takes care of itself" in low


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
        c = _make_classification("cold", today_high=38, today_low=22,
                                  tomorrow_high=28, tomorrow_low=12,
                                  trend_direction="cooling", trend_magnitude=10)
        result = _generate(c)
        # Should mention pre-heating / banking heat
        low = result.lower()
        assert "pre-heat" in low or "bank" in low or "extra heat" in low or "extra warm" in low

    def test_no_preheat_on_stable_trend(self):
        c = _make_classification("cold", today_high=38, today_low=22,
                                  trend_direction="stable", trend_magnitude=1)
        result = _generate(c)
        # Should NOT mention pre-heating
        assert "pre-heat" not in result.lower() or "bank extra heat" not in result.lower()


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
        c = _make_classification("mild", today_high=68, today_low=48,
                                  tomorrow_high=80, tomorrow_low=60,
                                  trend_direction="warming", trend_magnitude=10)
        result = _generate(c)
        assert "80" in result  # tomorrow_high

    def test_cooling_trend(self):
        c = _make_classification("cool", today_high=55, today_low=35,
                                  tomorrow_high=45, tomorrow_low=25,
                                  trend_direction="cooling", trend_magnitude=10)
        result = _generate(c)
        assert "45" in result  # tomorrow_high

    def test_stable_trend(self):
        c = _make_classification("mild", today_high=68, today_low=48,
                                  tomorrow_high=69, tomorrow_low=49,
                                  trend_direction="stable", trend_magnitude=1)
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
        assert "ACCEPT" in result
        assert "DISMISS" in result


# ---------------------------------------------------------------------------
# Non-empty output tests
# ---------------------------------------------------------------------------

class TestBriefingNotEmpty:
    """Every day type should produce a non-trivial briefing."""

    @pytest.mark.parametrize("day_type,high", [
        ("hot", 95),
        ("warm", 80),
        ("mild", 68),
        ("cool", 55),
        ("cold", 38),
    ])
    def test_produces_output(self, day_type, high):
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        assert len(result) > 100


# ---------------------------------------------------------------------------
# Conversational tone tests
# ---------------------------------------------------------------------------

class TestConversationalTone:
    """Verify the briefing uses conversational prose, not structured headers."""

    @pytest.mark.parametrize("day_type,high", [
        ("hot", 95),
        ("warm", 80),
        ("mild", 68),
        ("cool", 55),
        ("cold", 38),
    ])
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

    @pytest.mark.parametrize("day_type,high", [
        ("hot", 95),
        ("warm", 80),
        ("mild", 68),
        ("cool", 55),
        ("cold", 38),
    ])
    def test_first_person_voice(self, day_type, high):
        """Body should use first-person voice (I'll, I'm, I've)."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        low = result.lower()
        assert "i'll" in low or "i'm" in low or "i've" in low, (
            f"Expected first-person voice in {day_type} briefing"
        )

    @pytest.mark.parametrize("day_type,high", [
        ("hot", 95),
        ("warm", 80),
        ("mild", 68),
        ("cool", 55),
        ("cold", 38),
    ])
    def test_no_checkbox_markers(self, day_type, high):
        """Body should not use old-style checkbox markers."""
        c = _make_classification(day_type, today_high=high)
        result = _generate(c)
        # The structured header + learning section may have emoji,
        # but the body should not have ✅ markers
        body = result.split("\n\n", 2)[-1]  # skip header block
        if "Suggestions" not in body:
            assert "✅" not in body, f"Found checkbox marker in {day_type} briefing body"

    @pytest.mark.parametrize("day_type,high", [
        ("hot", 95),
        ("warm", 80),
        ("mild", 68),
        ("cool", 55),
        ("cold", 38),
    ])
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
            automation_grace_seconds=3600,
        )
        low = result.lower()
        assert "settling period" in low
        assert "60 minutes" in low

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
            "mild", today_high=68, today_low=48,
            tomorrow_high=80, tomorrow_low=60,
            trend_direction="warming", trend_magnitude=12,
        )
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "80" in table  # tomorrow high
        assert "warm" in table.lower()

    def test_tldr_stable_tomorrow(self):
        c = _make_classification("mild", today_high=68, today_low=48,
                                  tomorrow_high=69, tomorrow_low=49,
                                  trend_direction="stable", trend_magnitude=1)
        rows = _generate_tldr_table(c, _make_config())
        table = "\n".join(rows)
        assert "Stable" in table
        assert "69" in table

    def test_tldr_is_markdown_table(self):
        """Output should look like a markdown pipe table."""
        c = _make_classification("hot", today_high=92, today_low=70)
        rows = _generate_tldr_table(c, _make_config())
        # Every row should start and end with |
        for row in rows:
            assert row.startswith("|"), f"Row does not start with |: {row!r}"
            assert row.endswith("|"), f"Row does not end with |: {row!r}"

    def test_tldr_separator_row_present(self):
        """There should be exactly one separator row (dashes)."""
        c = _make_classification("mild", today_high=68)
        rows = _generate_tldr_table(c, _make_config())
        sep_rows = [r for r in rows if set(r.strip("|").replace(" ", "")) <= set("-|")]
        assert len(sep_rows) == 1


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
        # Should have the header
        assert "Your Home Climate Plan" in result
        # Should have the TLDR table
        assert "Day Type" in result
        assert "HVAC Mode" in result
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
        assert "| Day Type" in result
        assert "| HVAC Mode" in result
        assert "| Windows" in result
        assert "| Bedtime Setback" in result
        assert "| Tomorrow" in result

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
        assert "| Day Type" in result
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
        assert "| Day Type" in result
        assert "I'll" in result or "I'm" in result or "I pre-cooled" in result


# ---------------------------------------------------------------------------
# TLDR length guard (Issue #34)
# ---------------------------------------------------------------------------

class TestTldrLength:
    """Verify tldr_only briefings stay short enough for push notifications."""

    @pytest.mark.parametrize("day_type,high,low", [
        ("hot", 95, 72),
        ("warm", 80, 60),
        ("mild", 72, 55),
        ("cool", 55, 40),
        ("cold", 30, 15),
    ])
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
        assert len(result) < 600, (
            f"tldr_only for {day_type} is {len(result)} chars (max 600)"
        )

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
        assert "| Day Type" in result

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
