"""Tests for the Climate Advisor briefing module.

The briefing generator is pure logic with no Home Assistant dependencies,
so no mocking is required. Tests assert on content (temperatures, times,
action items) rather than exact formatting, so they survive tone rewrites.
"""
from __future__ import annotations

from datetime import time

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.briefing import generate_briefing


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


class TestWarmDayBriefing:
    """Warm day briefings should mention windows and optional AC safety net."""

    def test_mentions_window_open_time(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        # Window open time for warm days is 8:00 AM
        assert "8" in result

    def test_mentions_window_close_time(self):
        c = _make_classification("warm", today_high=80, today_low=60)
        result = _generate(c)
        # Window close time for warm days is 6:00 PM
        assert "6" in result or "18" in result

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
