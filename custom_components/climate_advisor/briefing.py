"""Daily briefing generator for Climate Advisor.

Voice rules for the conversational body:
- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- Cause-and-effect: explain *why* before asking the user to do something
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times
- No emoji in body text (emoji only in the structured header and learning section)
"""
from __future__ import annotations

import logging
import platform
from datetime import time

from .classifier import DayClassification
from .const import (
    DAY_TYPE_HOT,
    DAY_TYPE_WARM,
    DAY_TYPE_MILD,
    DAY_TYPE_COOL,
    DAY_TYPE_COLD,
)

_LOGGER = logging.getLogger(__name__)

# strftime format for 12-hour without leading zero (platform-dependent)
_FMT_HOUR = "%#I:%M %p" if platform.system() == "Windows" else "%-I:%M %p"


def generate_briefing(
    classification: DayClassification,
    comfort_heat: float,
    comfort_cool: float,
    setback_heat: float,
    setback_cool: float,
    wake_time: time,
    sleep_time: time,
    learning_suggestions: list[str] | None = None,
) -> str:
    """Generate the daily climate briefing message.

    Args:
        classification: Today's day classification and recommendations.
        comfort_heat / comfort_cool: User's comfort setpoints.
        setback_heat / setback_cool: User's setback setpoints.
        wake_time / sleep_time: User's schedule.
        learning_suggestions: Any pending suggestions from the learning system.

    Returns:
        Formatted briefing string suitable for email or notification.
    """
    c = classification
    lines: list[str] = []

    # Structured header (kept for quick scanning)
    trend_desc = _trend_description(c)
    lines.append("🏠 Your Home Climate Plan for Today")
    lines.append(f"{'=' * 40}")
    lines.append("")
    lines.append(f"Today: High {c.today_high:.0f}°F / Low {c.today_low:.0f}°F")
    lines.append(f"Tomorrow: High {c.tomorrow_high:.0f}°F / Low {c.tomorrow_low:.0f}°F")
    lines.append(f"Day Type: {c.day_type.title()} | Trend: {trend_desc}")
    lines.append("")

    # Conversational body
    if c.day_type == DAY_TYPE_HOT:
        lines.extend(_hot_day_plan(c, comfort_cool, setback_cool, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_WARM:
        lines.extend(_warm_day_plan(c, comfort_cool, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_MILD:
        lines.extend(_mild_day_plan(c, comfort_heat, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_COOL:
        lines.extend(_cool_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_COLD:
        lines.extend(_cold_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time))

    lines.append("")
    lines.extend(_leaving_home_section(c, setback_heat, setback_cool))
    lines.append("")
    lines.extend(_fresh_air_section(c, comfort_heat, comfort_cool))
    lines.append("")
    lines.extend(_tonight_preview(c, comfort_heat, comfort_cool, sleep_time))

    # Learning suggestions (kept structured for accept/dismiss clarity)
    if learning_suggestions:
        lines.append("")
        lines.append("💡 Suggestions Based on Recent Patterns")
        lines.append("-" * 40)
        for suggestion in learning_suggestions:
            lines.append(f"  • {suggestion}")
        lines.append("")
        lines.append("Reply ACCEPT or DISMISS to any suggestion, or ignore to keep current behavior.")

    return "\n".join(lines)


def _trend_description(c: DayClassification) -> str:
    """Human-readable trend description."""
    if c.trend_direction == "warming":
        if c.trend_magnitude >= 10:
            return f"Significantly warmer tomorrow (+{c.trend_magnitude:.0f}°F)"
        return f"Warming trend (+{c.trend_magnitude:.0f}°F)"
    elif c.trend_direction == "cooling":
        if c.trend_magnitude >= 10:
            return f"Significant cold front coming (-{c.trend_magnitude:.0f}°F)"
        return f"Cooling trend (-{c.trend_magnitude:.0f}°F)"
    return "Stable"


def _hot_day_plan(c, comfort_cool, setback_cool, wake_time, sleep_time) -> list[str]:
    """Conversational plan for hot days (85°F+)."""
    return [
        f"I got a head start on the heat this morning. The AC pre-cooled the house"
        f" to {comfort_cool - 2:.0f}°F while the outdoor air was still cool — that"
        f" banking strategy saves a lot of energy over the course of the day.",
        "",
        f"Today's a keep-it-sealed kind of day. Keep all windows and doors closed,"
        f" and if you can, close the blinds on sun-facing windows (especially"
        f" west-facing ones after noon). I'll hold things at {comfort_cool:.0f}°F"
        f" all day — you shouldn't need to touch anything.",
        "",
        f"If outdoor temps drop below {comfort_cool:.0f}°F after sunset, I'll send"
        f" you a heads-up that it's safe to open windows and give the AC a rest."
        f" If you do open up, I'll handle shutting the AC off automatically.",
    ]


def _warm_day_plan(c, comfort_cool, wake_time, sleep_time) -> list[str]:
    """Conversational plan for warm days (75-85°F)."""
    lines = [
        "The HVAC is off this morning — the house held its temperature"
        " nicely overnight.",
    ]

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        lines[0] += (
            f" Around {open_t}, it'll be a great time to open some windows."
            " Opening on opposite sides gives you a nice cross-breeze that"
            " keeps things comfortable without the AC."
        )

    lines.append("")

    if c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        lines.append(
            f"You'll want to close things up by {close_t} though — I'll be ready"
            f" to kick on the AC if temps push above {comfort_cool:.0f}°F, and it"
            f" works much better with the house sealed up."
        )
    else:
        lines.append(
            f"If it gets too warm inside, the AC will step in automatically above"
            f" {comfort_cool:.0f}°F as a safety net. But with good airflow, you"
            f" probably won't need it."
        )

    return lines


def _mild_day_plan(c, comfort_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for mild days (60-74°F)."""
    lines = [
        f"This is the good stuff — a day where the house practically takes care"
        f" of itself. I ran the heater to {comfort_heat:.0f}°F before sunrise,"
        f" and now it's off for the day. The weather does the rest.",
    ]

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"By {open_t}, the outside air will be around 60°F and climbing."
            f" If you open windows on the south and east side first, you'll get a"
            f" natural cross-breeze that freshens the air and warms the house for free."
        )

    lines.append("")
    lines.append(
        "Through the afternoon, no HVAC needed at all. Enjoy the break!"
    )

    if c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Close up the windows by {close_t} to trap the warmth before the"
            f" sun drops. The house should coast comfortably through dinner. If"
            f" it dips below {comfort_heat - 2:.0f}°F later tonight, I'll gently"
            f" kick the heater back on — but that's all automatic."
        )

    return lines


def _cool_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for cool days (45-59°F)."""
    return [
        f"It's a heater day. I'll keep the house at {comfort_heat:.0f}°F through"
        f" the morning — it's too cool outside for windows today, so we're"
        f" staying sealed up.",
        "",
        "Between about 11am and 3pm, I'll ease the setpoint back a couple"
        " degrees to ride whatever solar gain the house picks up through the"
        " windows. You won't notice the difference, but it saves a bit of energy.",
        "",
        f"After 3pm I'll bring it back to {comfort_heat:.0f}°F as the sun drops."
        f" At bedtime, I'll set things to {comfort_heat - 4:.0f}°F for sleeping"
        f" — most people sleep better a little cooler.",
    ]


def _cold_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for cold days (below 45°F)."""
    lines = [
        f"It's going to be cold out there. The heater runs all day at"
        f" {comfort_heat:.0f}°F, and you can help it out — keep doors and windows"
        f" closed, minimize how long you hold exterior doors open, and close"
        f" curtains on the north side. If you have south-facing windows, open"
        f" those curtains to grab some free solar heat.",
    ]

    if c.pre_condition and c.trend_direction == "cooling":
        target = comfort_heat + (c.pre_condition_target or 3)
        lines.append("")
        lines.append(
            f"Tomorrow's even colder, so I'm going to bank some extra heat this"
            f" evening. Starting around 7pm, I'll bump the setpoint up to"
            f" {target:.0f}°F for a couple hours, then coast into the night. The"
            f" house will feel extra warm before bed — that's on purpose."
        )

    lines.append("")
    lines.append(
        f"Tonight I'm using a conservative setback — {comfort_heat - 3:.0f}°F"
        f" instead of the usual {setback_heat:.0f}°F. When it's this cold, a"
        f" deeper setback takes too long to recover from in the morning."
    )

    return lines


def _leaving_home_section(c, setback_heat, setback_cool) -> list[str]:
    """Conversational section about what happens when they leave."""
    if c.hvac_mode == "cool":
        return [
            f"If you head out, no worries. After about 15 minutes I'll let the"
            f" house drift up to {setback_cool:.0f}°F to save energy. When you're"
            f" back, I'll pull it right back down — give it 20 to 30 minutes to"
            f" feel normal again.",
        ]
    elif c.hvac_mode == "heat":
        return [
            f"If you head out, I'll drop to {setback_heat:.0f}°F after about"
            f" 15 minutes. When you get back, I'll warm things right up — should"
            f" take 20 to 30 minutes depending on how long you were gone.",
        ]
    else:
        return [
            "If you head out, nothing really changes today — the HVAC is off."
            " If it was running as a safety net, it'll set back on its own.",
        ]


def _fresh_air_section(c, comfort_heat, comfort_cool) -> list[str]:
    """User-centric section about opening windows/doors for fresh air.

    Affirms the user's choice first, then explains impact and recovery.
    Varies by HVAC mode since the impact differs significantly.
    """
    if c.hvac_mode == "cool":
        return [
            "If you want to crack a window for some fresh air, no problem —"
            " it's your house. I'll keep the AC running for a few minutes in"
            " case it's just a quick thing, but if it stays open past 3 minutes"
            " I'll shut the AC off so you're not cooling the outdoors. Once you"
            " close up, I'll fire the AC back up right away. Just know that on a"
            f" day like today it may take a bit longer to pull back down to"
            f" {comfort_cool:.0f}°F, so if you want to minimize the impact, shorter"
            " is better — and try to keep other windows and doors shut while"
            " you've got one open.",
        ]
    elif c.hvac_mode == "heat":
        return [
            "If you want to open a window for some fresh air, no problem —"
            " go for it. I'll keep the heat running for a few minutes in case"
            " you're just airing things out, but if it stays open past 3 minutes"
            " I'll turn the heat off so we're not heating the neighborhood. Once"
            " you close up, the heat kicks right back on. It'll take a little"
            " extra energy to warm back up, so if you want to minimize the"
            " impact, a quick burst of fresh air works great — and closing doors"
            " to the room with the open window helps keep the rest of the house"
            " comfortable while you do it.",
        ]
    else:
        return [
            "If you want to open a window for some fresh air, go for it —"
            " the HVAC is off today so there's no energy impact at all."
            " Enjoy the breeze. If the system does need to kick on as a safety"
            " net later and a window is still open, I'll give it a few minutes"
            " and then pause until you close up.",
        ]


def _tonight_preview(c, comfort_heat, comfort_cool, sleep_time) -> list[str]:
    """Conversational preview of tonight and tomorrow based on trend."""
    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        return [
            f"Looking ahead — tomorrow's warmer at {c.tomorrow_high:.0f}°F, so"
            f" I'm going to set back a bit more aggressively tonight. Less"
            f" heating needed means energy saved while you sleep.",
        ]
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        return [
            f"Looking ahead — tomorrow's cooler at {c.tomorrow_high:.0f}°F, so"
            f" I'll bank some extra warmth this evening and go easy on the"
            f" overnight setback. If the house feels a touch warmer than usual"
            f" before bed, that's intentional.",
        ]
    else:
        return [
            f"Tomorrow looks pretty similar to today — {c.tomorrow_high:.0f}°F"
            f" for a high. Nothing special planned overnight.",
        ]
