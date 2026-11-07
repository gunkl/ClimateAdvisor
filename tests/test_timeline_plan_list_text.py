"""The "Next 24 hours" row wording (Issue #1399).

Occupant view: the plan list shows each step's time (with the weekday on another day) and who acts in their own
columns. The step's sentence opened with the same time again ("8:30 PM | CA | 8:30 PM: bedtime, CA switches ...").
The row now shows ``text_short`` (``timeline_surfaces.plan_list_text``): no leading time, no "CA" beside the CA badge,
a capital first letter. The canonical ``text`` keeps its time, because the briefing, the cards and the chart hover
show a step on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.climate_advisor.night_strategy import NIGHT_SENTENCES
from custom_components.climate_advisor.night_strategy import _sentence as night_say
from custom_components.climate_advisor.operation_timeline import (
    SENTENCES,
    build_timeline,
    detail_dict,
    say,
    to_dict,
)
from custom_components.climate_advisor.temperature import display_temp
from custom_components.climate_advisor.timeline_surfaces import plan_list_text, timeline_api_payload
from tests.timeline_fixtures import TZ, make_snapshot

NOW = datetime(2026, 11, 7, 20, 30, tzinfo=TZ)  # a Saturday
WAKE = datetime(2026, 11, 8, 6, 15, tzinfo=TZ)  # Sunday

# Every sentence that opens with its time or with "CA": the row's opening words, per unit-free key. A new template
# that opens with "{t}" or "CA" must be added here (the coverage test below), so its row wording is decided, not left
# to chance.
EXPECTED_OPENING: dict[str, tuple[str, str]] = {  # key -> (actor, how the row starts)
    "wake_band.default": ("ca", "Switches to the daytime comfort band, "),
    "wake_band.sensor_swap": ("ca", "Switches to the daytime comfort band, "),
    "sleep_band.default": ("ca", "Bedtime: switches to the sleep band, "),
    "sleep_band.sensor_swap": ("ca", "Bedtime: switches to the sleep band, "),
    "mode_change": ("ca", "The day plan changes from "),
    "occupancy_setback.away": ("ca", "Nobody is home: holds the away band, "),
    "occupancy_setback.vacation": ("ca", "Vacation mode: holds the vacation band, "),
    "pre_cool_start.weather": ("ca", "Pre-cools the house to "),
    "pre_cool_start.tou": ("ca", "Pre-cools to "),
    "pre_heat_start.tou": ("ca", "Pre-heats to "),
    "natvent_start": ("ca", "Once a window is open: runs the "),
    "natvent_stop": ("ca", "Stops the "),
    "resume_schedule": ("ca", "Your change ends: resumes the schedule ("),
    "setpoint_change.tou_expansion_start": ("ca", "High-cost period: widens the "),
    "setpoint_change.tou_expansion_end": ("ca", "The high-cost period ends: returns to the "),
    "briefing_send.default": ("ca", "Sends the daily briefing."),
    "ceiling_guard.default": ("ca", "Switches the AC on to hold the "),
    "natvent_start.predicted": ("ca", "Starts the "),
    "windows_open.free_cooling": ("you", "Open the windows. Outdoor is "),
    "windows_open.free_heating": ("you", "Open the windows. Outdoor is "),
    "windows_open.planned": ("you", "Open the windows. Outdoor air helps "),
    "windows_close.outdoor_rise": ("you", "Close the windows. Outdoor stops helping"),
    "windows_close.comfort_edge": ("you", "Close the windows before the house drifts below "),
    "windows_close.planned_end": ("you", "Close the windows, the planned window period ends."),
    "windows_close.bedtime": ("you", "Bedtime: close the windows before you turn in."),
    "windows_close.horizon": ("you", "Close the windows when you go to bed or leave."),
    "night_strategy.plan.closed": ("ca", "Holds the bedroom between "),
    "night_strategy.plan.active": ("ca", "Tonight's bedroom plan is below; CA holds the bedroom between "),
    "night_strategy.windows_open": ("you", "Open the bedroom windows; "),
    "night_strategy.windows_close": ("you", "Close the bedroom windows; CA keeps the bedroom at "),
    "night_strategy.windows_close_predicted": ("you", "Close the bedroom windows (about when the bedroom reaches "),
    "night_strategy.fan_on": ("you", "Turn the whole-house fan on with the windows open"),
    "night_strategy.fan_off": ("you", "Turn the whole-house fan off; CA takes over"),
}

_TEXT_FACTS = {
    "sensor": "bedroom sensor",
    "fan": "whole-house fan",
    "old": "warm",
    "new": "hot",
    "until": "",
    "base": "",
}


def _facts(spec: dict[str, str], ts: datetime) -> dict:
    out: dict = {}
    for name, kind in spec.items():
        if kind == "time":
            out[name] = ts if name == "t" else WAKE
        elif kind in ("temp",):
            out[name] = 68.0
        elif kind == "delta":
            out[name] = 6.0
        elif kind in ("int",):
            out[name] = 2
        elif kind == "minutes":
            out[name] = 30.0
        else:
            out[name] = _TEXT_FACTS.get(name, "away")
    return out


def _render(key: str, unit: str, ts: datetime) -> str:
    if key in SENTENCES:
        return say(key, unit, ref=NOW, **_facts(dict(SENTENCES[key].facts), ts))
    return night_say(key, unit, **_facts(dict(NIGHT_SENTENCES[key].facts), ts))


def _opening_templates() -> set[str]:
    tables = [(k, s.template) for k, s in SENTENCES.items()] + [(k, s.template) for k, s in NIGHT_SENTENCES.items()]
    return {k for k, t in tables if t.startswith("{t}") or t.startswith("CA ")}


def test_every_sentence_opening_with_a_time_or_ca_has_a_decided_row_wording() -> None:
    assert _opening_templates() == set(EXPECTED_OPENING)


@pytest.mark.parametrize("unit", ["fahrenheit", "celsius"])
@pytest.mark.parametrize("key", sorted(EXPECTED_OPENING))
@pytest.mark.parametrize("ts", [NOW, WAKE], ids=["same_day", "next_day"])
def test_the_row_drops_its_time_and_the_ca_word_and_starts_with_a_capital(key: str, unit: str, ts: datetime) -> None:
    actor, opening = EXPECTED_OPENING[key]
    text = _render(key, unit, ts)
    short = plan_list_text(text, actor)
    assert short.startswith(opening), (text, short)
    assert short[0].isupper()
    assert not short.startswith(("8:30", "Sun ", "Sat ", "6:15")), short
    if actor == "ca":
        assert not short.startswith("CA")
    # the rest of the sentence is untouched (same facts, same unit)
    sym = "°C" if unit == "celsius" else "°F"
    assert short.count(sym) == text.count(sym)
    assert text.endswith(short[-12:])


def test_the_owners_examples() -> None:
    bed = say("sleep_band.sensor_swap", ref=NOW, t=NOW, floor=66.0, ceiling=72.0, sensor="bedroom sensor", wake=WAKE)
    assert bed == (
        "8:30 PM: bedtime, CA switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until Sun 6:15 AM."
    )
    assert plan_list_text(bed, "ca") == (
        "Bedtime: switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until Sun 6:15 AM."
    )
    wake = say("wake_band.sensor_swap", ref=NOW, t=WAKE, floor=68.0, ceiling=74.0, sensor="thermostat")
    assert wake.startswith("Sun 6:15 AM: CA switches")
    assert plan_list_text(wake, "ca") == (
        "Switches to the daytime comfort band, 68°F to 74°F, and reads the thermostat again."
    )


def test_celsius_row() -> None:
    text = say("sleep_band.default", "celsius", ref=NOW, t=NOW, floor=66.0, ceiling=72.0)
    assert plan_list_text(text, "ca") == "Bedtime: switches to the sleep band, 19°C to 22°C."


def test_held_suffix_keeps_its_suffix_and_a_veto_or_summary_is_left_whole() -> None:
    base = say("sleep_band.default", ref=NOW, t=NOW, floor=66.0, ceiling=72.0)
    held = say("held.suffix", ref=NOW, base=base, until=WAKE)
    assert plan_list_text(held, "ca") == (
        "Bedtime: switches to the sleep band, 66°F to 72°F. It applies when CA resumes the schedule at Sun 6:15 AM."
    )
    veto = say("held.direction_veto.warm", outdoor=75.0, indoor=72.0)
    assert plan_list_text(veto, "you") == veto
    summary = say("summary.windows_closed", hours="24 h", checked=3)
    assert plan_list_text(summary, "ca") == summary


def test_a_you_step_keeps_the_ca_word_and_you_phrasing() -> None:
    # "CA " is dropped only beside the CA badge; an occupant sentence never opens with it, but must not lose it.
    assert plan_list_text("8:30 PM: CA something.", "you") == "CA something."


def test_the_rest_payload_adds_text_short_and_keeps_the_canonical_text() -> None:
    tl = build_timeline(make_snapshot("mild", now=datetime(2026, 11, 4, 9, 0, tzinfo=TZ), tomorrow="warm"))
    contract = to_dict(tl)
    payload = timeline_api_payload(contract, detail_dict(tl), "fahrenheit", display_temp)
    assert payload["steps"]
    for raw, s in zip(contract["steps"], payload["steps"], strict=True):
        assert s["text"] == raw["text"]  # canonical sentence untouched
        assert s["text_short"] == plan_list_text(raw["text"], raw["actor"])
        assert s["text_short"][0].isupper()
        when = datetime.fromisoformat(raw["ts"])
        clock = when.strftime("%I:%M %p").lstrip("0")
        assert not s["text_short"].startswith((clock, when.strftime("%a ") + clock)), s["text_short"]
        if s["actor"] == "ca":
            assert not s["text_short"].startswith("CA ")
    assert "text_short" not in contract["steps"][0]  # a view field, never in the stored contract


def test_a_late_step_on_another_day_drops_its_weekday_too() -> None:
    text = say("briefing_send.default", ref=NOW, t=NOW + timedelta(hours=12))
    assert text == "Sun 8:30 AM: CA sends the daily briefing."
    assert plan_list_text(text, "ca") == "Sends the daily briefing."
