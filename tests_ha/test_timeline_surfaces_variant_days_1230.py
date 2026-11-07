"""Issue #1230 R3b (real HA): recorded variant days through the coordinator, every surface agrees with the timeline.

Occupant story: three real days from Zone 1 (a warm windows day the occupant followed, a mild windows day the occupant
followed, and a hot day with no window advice) are replayed through the real coordinator on real Home Assistant: the
recorded outdoor and indoor temperatures hour by hour, the occupant's own window openings and closings at the times they
happened. At every half hour the person asks the same four questions the dashboard answers (what should I do, what does
CA do next, what does today's briefing say about windows, what is the plan) and the answers must be one answer: the card
is the timeline's window step, Next Automation is the timeline's earliest CA step, the briefing's window times are the
timeline's, the REST body is the timeline.

The recorded inputs are temperatures and window edges only (``tests_ha/data/zone1_variant_days.json``): what CA did on
those days came from code versions that no longer exist, so it is not asserted here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.climate_advisor.timeline_surfaces import (
    advises_opening,
    card_user_step,
    next_automation,
    timeline_windows,
)

pytestmark = pytest.mark.house

_DATA = json.loads((Path(__file__).parent / "data" / "zone1_variant_days.json").read_text(encoding="utf-8"))
_DAYS = {d["date"]: d for d in _DATA["days"]}
_TOTALS: dict = {"advised": 0, "window_steps": 0, "briefing_close": 0, "plan_has_window_steps": 0, "cards": set()}


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _assert_surfaces_agree(house, *, day_label: str, seen: dict) -> None:
    c = house.coordinator
    tl = c._surface_timeline()
    assert tl is not None, f"{day_label}: the coordinator has no timeline"
    from homeassistant.util import dt as dt_util

    now = dt_util.now()
    cls = c._current_classification
    assert cls is not None
    windows_open = c._any_sensor_open()

    # --- Next Action: the timeline's window step, never a window the plan does not hold --------------------------
    card = c._compute_next_action(
        cls,
        indoor_temp=c._get_indoor_temp(),
        outdoor_temp=getattr(c, "_last_outdoor_temp", None),
        windows_physically_open=windows_open,
        ae=c.automation_engine,
    )
    step = card_user_step(tl, now, windows_open=windows_open)
    open_steps = [s for s in tl.steps if s.kind.value == "windows_open" and s.ts.date() == now.date()]
    if advises_opening(card):
        assert open_steps, f"{day_label} {now:%H:%M}: card invites a window the plan does not hold: {card!r}"
        seen["advised"] += 1
    if card.startswith("Open windows at "):
        assert step is not None and card == f"Open windows at {_clock(step.ts)}", (card, step)
    if card.startswith("Close windows by "):
        assert step is not None and step.kind.value == "windows_close" and card == f"Close windows by {_clock(step.ts)}"
    if step is not None:
        seen["window_steps"] += 1
    if [x for x in tl.steps if x.actor.value == "you"]:
        seen["plan_has_window_steps"] += 1  # today's or tomorrow's engine plan: the plan holds window advice
    seen["cards"].add(re.sub(r"\d{1,2}:\d{2} (AM|PM)", "T", re.sub(r"\(\d+.F vs \d+.F\)", "(o vs i)", card)))

    # --- Next Automation: the timeline's earliest automation-executed step still ahead -----------------------------
    action, at = c._compute_next_automation_action(cls)
    want_action, want_at, _used = next_automation(tl, now, c.config.get("temp_unit", "fahrenheit"))
    if action not in ("Startup coalescing", "Waiting for classification..."):
        assert (action, at) == (want_action, want_at), f"{day_label} {now:%H:%M}: next automation {action!r} @ {at}"
        assert not re.search(r"\b(waiting|paused|grace|window)\b", action, re.IGNORECASE), action

    # --- briefing: the window times it states are the timeline's -----------------------------------------------------
    _full, short = c._build_briefing_text(cls, suggestions=[])
    row = next((ln.strip() for ln in short.splitlines() if ln.strip().startswith("Windows:")), "")
    periods = [p for p in timeline_windows(tl, now.date()).periods if p[1] > now]
    if "Close by" in row:
        times = re.findall(r"\b\d{1,2}:\d{2} (?:AM|PM)\b", row)
        assert times and times[0] in {_clock(e) for _o, e in periods}, (day_label, now, row, periods)
        seen["briefing_close"] += 1
    elif cls.day_type in ("warm", "mild") and not periods:
        assert "Closed all day" in row or row == "", row

    # --- endpoint: the same plan, in the display unit --------------------------------------------------------------
    payload = c.timeline_payload()
    assert payload is not None and payload["unit"] == c.config.get("temp_unit", "fahrenheit")
    assert [(s["ts"], s["kind"]) for s in payload["steps"]] == [(s.ts.isoformat(), s.kind.value) for s in tl.steps]
    assert payload["detail"]["days"][0]["day_type"] == cls.day_type


@pytest.mark.parametrize("day", ["2026-10-15", "2026-11-01", "2026-10-03"])
async def test_every_surface_agrees_with_the_timeline_on_a_recorded_zone_1_day(make_house, day) -> None:
    rec = _DAYS[day]
    y, m, d = (int(x) for x in day.split("-"))
    hourly = {h["h"]: h for h in rec["hourly"]}
    edges = [(tuple(int(x) for x in t.split(":")), kind) for t, kind in rec["occupant_window_edges"]]
    start = datetime(y, m, d, 5, 45, tzinfo=UTC)
    first = hourly[5]
    house = await make_house(
        fan="whf",
        contacts=["window"],
        startup="settled",
        start=start,
        profile="scripted",
        initial_indoor_f=float(first["indoor"]),
        outdoor_f=float(first["outdoor"]),
        high_f=float(rec["forecast_high_f"]),
        low_f=float(rec["forecast_low_f"]),
        initial_hvac_mode="off",
        ca_config={"sleep_time": "20:30", "wake_time": "06:30", "sensor_debounce_seconds": 60},
    )
    seen = {"advised": 0, "window_steps": 0, "briefing_close": 0, "plan_has_window_steps": 0, "cards": set()}
    pending = [(hm, kind) for hm, kind in edges if hm >= (5, 45)]
    window_is_open = any(hm < (5, 45) and kind == "open" for hm, kind in edges) and not any(
        hm < (5, 45) and kind == "close" for hm, kind in edges
    )
    if window_is_open:
        await house.open("window")

    minutes_total = 0
    for half_hour in range(36):  # 06:00 .. 23:30
        slot = 6 * 60 + 30 * half_hour
        hour = slot // 60
        rec_h = hourly[hour]
        house.set_outdoor(
            float(rec_h["outdoor"]), high_f=float(rec["forecast_high_f"]), low_f=float(rec["forecast_low_f"])
        )
        house.set_indoor(float(rec_h["indoor"]))
        # the occupant's own window edges that fall before the end of this half hour
        while pending and pending[0][0] <= (hour, (slot % 60) + 29):
            (_hm, kind) = pending.pop(0)
            if kind == "open":
                await house.open("window")
            else:
                await house.close_contact("window")
        target = datetime(y, m, d, hour, slot % 60, tzinfo=UTC)
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        gap = (target - now).total_seconds()
        if gap > 0:
            await house.advance(seconds=gap)
        minutes_total += 30
        _assert_surfaces_agree(house, day_label=day, seen=seen)

    assert minutes_total == 18 * 60
    for key in ("advised", "window_steps", "briefing_close", "plan_has_window_steps"):
        _TOTALS[key] += seen[key]
    _TOTALS["cards"] |= seen["cards"]


def test_the_three_variant_days_were_not_vacuous() -> None:
    """Runs after the three days above (file order): across them the surfaces really did talk about windows.

    A day on the sim house does not reproduce what the recorded engine said (the live outdoor gate and the sim forecast
    differ), so each day is held to the agreement invariants at every checkpoint, and the three together to having
    exercised the card's open and close wording, the briefing's close time and the plan's window steps.
    """
    assert _TOTALS["plan_has_window_steps"] > 0, _TOTALS
    assert _TOTALS["advised"] > 0, f"the card never invited a window on any variant day: {_TOTALS}"
    assert _TOTALS["window_steps"] > 0 and _TOTALS["briefing_close"] > 0, _TOTALS
    assert any(c.startswith("Open windows") for c in _TOTALS["cards"]), _TOTALS
    assert any(c.startswith("Keep windows") for c in _TOTALS["cards"]), _TOTALS
