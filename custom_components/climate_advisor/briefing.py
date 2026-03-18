"""Daily briefing generator for Climate Advisor."""
from __future__ import annotations

import logging
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

    # Header
    trend_desc = _trend_description(c)
    lines.append("🏠 Your Home Climate Plan for Today")
    lines.append(f"{'=' * 40}")
    lines.append("")
    lines.append(f"Today: High {c.today_high:.0f}°F / Low {c.today_low:.0f}°F")
    lines.append(f"Tomorrow: High {c.tomorrow_high:.0f}°F / Low {c.tomorrow_low:.0f}°F")
    lines.append(f"Day Type: {c.day_type.title()} | Trend: {trend_desc}")
    lines.append("")

    # Day-type specific plan
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

    # Universal sections
    lines.append("")
    lines.extend(_leaving_home_section(c, setback_heat, setback_cool))
    lines.append("")
    lines.extend(_door_window_section())

    # Trend-based preview of tonight/tomorrow
    lines.append("")
    lines.extend(_tonight_preview(c, comfort_heat, comfort_cool, sleep_time))

    # Learning suggestions if any
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
    """Plan for hot days (85°F+)."""
    return [
        "☀️  HOT DAY PLAN",
        "-" * 40,
        "",
        f"🌅 Early Morning (before {wake_time.strftime('%I:%M %p')})",
        f"  The AC pre-cooled the house to {comfort_cool - 2:.0f}°F while outdoor air",
        "  was still cool. This banking strategy saves significant energy.",
        "  ✅ Already handled automatically.",
        "",
        "🏠 All Day — Keep the house sealed up",
        "  • Keep ALL windows and doors closed.",
        "  • Close blinds on sun-facing windows, especially west-facing after noon.",
        f"  • The AC will maintain your comfort at {comfort_cool:.0f}°F.",
        "",
        "🌆 Evening",
        f"  If outdoor temps drop below {comfort_cool:.0f}°F after sunset, you'll get a",
        "  notification that it's safe to open windows and turn off the AC.",
        "  ✅ Automation will handle the AC shutoff if you open windows.",
    ]


def _warm_day_plan(c, comfort_cool, wake_time, sleep_time) -> list[str]:
    """Plan for warm days (75-85°F)."""
    lines = [
        "🌤️  WARM DAY PLAN",
        "-" * 40,
        "",
        "🌅 Morning",
        "  HVAC is off. The house should be comfortable from overnight.",
        "",
    ]
    if c.windows_recommended and c.window_open_time:
        lines.extend([
            f"🪟 By {c.window_open_time.strftime('%I:%M %p')} — Open windows for a breeze",
            "  Outdoor air will be pleasant. Cross-ventilation will keep things",
            "  comfortable without AC. Open windows on opposite sides of the house.",
            "",
        ])
    lines.extend([
        "☀️  Afternoon",
        "  Temps may climb into the upper range. If it gets too warm inside,",
        f"  the AC will kick in automatically above {comfort_cool:.0f}°F as a safety net.",
        "  But with windows and airflow, you likely won't need it.",
        "",
    ])
    if c.window_close_time:
        lines.extend([
            f"🌆 By {c.window_close_time.strftime('%I:%M %p')} — Close windows",
            "  Lock in the comfortable air before evening temps shift.",
        ])
    return lines


def _mild_day_plan(c, comfort_heat, wake_time, sleep_time) -> list[str]:
    """Plan for mild days (60-74°F)."""
    lines = [
        "😊 MILD DAY PLAN — The Sweet Spot!",
        "-" * 40,
        "",
        f"🌅 Early Morning ({wake_time.strftime('%I:%M %p')})",
        f"  The heater warmed the house to {comfort_heat:.0f}°F before sunrise.",
        "  It's now off for the day — outdoor temps will do the rest.",
        "  ✅ Already handled automatically.",
        "",
    ]
    if c.windows_recommended and c.window_open_time:
        lines.extend([
            f"🪟 By {c.window_open_time.strftime('%I:%M %p')} — Open windows (south and east side first)",
            "  Outside air will be around 60°F and climbing. Natural cross-breeze",
            "  freshens the air and gently warms the house for free.",
            "",
        ])
    lines.extend([
        "🏠 Midday through Afternoon",
        "  This is the sweet spot. No HVAC needed at all. Enjoy it!",
        "",
    ])
    if c.window_close_time:
        lines.extend([
            f"🌆 By {c.window_close_time.strftime('%I:%M %p')} — Close the windows",
            "  Temps will start dropping with the sun. Close up to trap the warmth.",
            "  The house should coast comfortably through dinner.",
            "",
            f"🌙 Evening ({sleep_time.strftime('%I:%M %p')})",
            f"  If indoor temp drops below {comfort_heat - 2:.0f}°F, the heater will",
            "  gently kick on. Otherwise, you're coasting on stored warmth.",
            "  ✅ Handled automatically.",
        ])
    return lines


def _cool_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Plan for cool days (45-59°F)."""
    return [
        "🍂 COOL DAY PLAN",
        "-" * 40,
        "",
        f"🌅 Morning ({wake_time.strftime('%I:%M %p')})",
        f"  The heater will maintain {comfort_heat:.0f}°F through the morning.",
        "  Keep windows closed — it's too cool for natural ventilation today.",
        "  ✅ Running automatically.",
        "",
        "☀️  Midday Break (11:00 AM – 3:00 PM)",
        "  During the warmest hours, the heater setpoint drops a few degrees",
        "  to take advantage of whatever solar gain the house gets.",
        "  ✅ Handled automatically. You won't notice the difference.",
        "",
        "🌆 Evening",
        f"  Heater returns to full {comfort_heat:.0f}°F setpoint after 3:00 PM",
        "  as outdoor temps fall.",
        "  ✅ Automatic.",
        "",
        "🌙 Bedtime",
        f"  Setpoint drops to {comfort_heat - 4:.0f}°F for sleeping comfort.",
        "  ✅ Automatic.",
    ]


def _cold_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Plan for cold days (below 45°F)."""
    lines = [
        "🥶 COLD DAY PLAN — Conservation Mode",
        "-" * 40,
        "",
        "The heater is your best friend today. Help it out:",
        "",
        f"🌅 All Day — Heat runs at {comfort_heat:.0f}°F",
        "  • Keep ALL doors and windows CLOSED.",
        "  • Minimize how long exterior doors stay open.",
        "  • Close curtains on north-facing windows for insulation.",
        "  • Open curtains on south-facing windows to capture solar heat.",
        "",
    ]
    if c.pre_condition and c.trend_direction == "cooling":
        lines.extend([
            "🌡️  Pre-Heating Tonight",
            "  Tomorrow is even colder. Starting at 7:00 PM, the system will",
            f"  bank extra heat by raising the setpoint to {comfort_heat + (c.pre_condition_target or 3):.0f}°F",
            "  for a couple hours, then coast into the night.",
            "  ✅ Automatic — the house will feel extra cozy this evening.",
            "",
        ])
    lines.extend([
        "🌙 Bedtime",
        f"  Conservative setback tonight — dropping to {comfort_heat - 3:.0f}°F instead of the",
        f"  usual {setback_heat:.0f}°F, since recovery is harder in extreme cold.",
        "  ✅ Automatic.",
    ])
    return lines


def _leaving_home_section(c, setback_heat, setback_cool) -> list[str]:
    """Section about what happens when they leave."""
    if c.hvac_mode == "cool":
        setback_desc = f"let it drift up to {setback_cool:.0f}°F"
        recovery_desc = "pull it back down"
    elif c.hvac_mode == "heat":
        setback_desc = f"drop to {setback_heat:.0f}°F"
        recovery_desc = "warm back up"
    else:
        return [
            "🚗 If You Leave the House",
            "-" * 40,
            "  HVAC is off today, so no changes needed. If the system was running",
            "  as a safety net (too hot or too cold), it will set back automatically.",
        ]

    return [
        "🚗 If You Leave the House",
        "-" * 40,
        f"  15 minutes after you leave, the system will {setback_desc}.",
        f"  When you return, it will {recovery_desc} immediately.",
        "  Expect about 20–30 minutes to feel normal depending on how",
        "  long you were away. No need to touch the thermostat.",
    ]


def _door_window_section() -> list[str]:
    """Section about door/window behavior."""
    return [
        "🚪 Doors & Windows",
        "-" * 40,
        "  If any monitored door or window stays open for more than 3 minutes,",
        "  the HVAC will pause and you'll get a notification. It resumes",
        "  automatically once everything is closed.",
    ]


def _tonight_preview(c, comfort_heat, comfort_cool, sleep_time) -> list[str]:
    """Preview of tonight and tomorrow based on trend."""
    lines = ["🔮 Looking Ahead", "-" * 40]

    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        lines.extend([
            f"  Tomorrow is warmer (high of {c.tomorrow_high:.0f}°F), so tonight's setback",
            "  will be a bit more aggressive than usual. The warming trend",
            "  means less heating needed overnight — saving energy while you sleep.",
        ])
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        lines.extend([
            f"  Tomorrow is cooler (high of {c.tomorrow_high:.0f}°F), so the system will",
            "  bank extra warmth this evening and use a gentler setback overnight.",
            "  You might notice the house feeling a touch warmer than usual",
            "  before bed — that's intentional.",
        ])
    else:
        lines.extend([
            f"  Tomorrow looks similar to today (high of {c.tomorrow_high:.0f}°F).",
            "  No special adjustments needed tonight.",
        ])

    return lines
