"""The occupant-facing surfaces agree with the operation timeline for the same snapshot (Issue #1230 R3b, plan 11.8).

Occupant view: the Status "Next Action" card, the "Next Automation" card, the briefing and the timeline endpoint
used to be
four computations of "what is the plan"; Issue #1276 was the card telling a house at its heat floor to open up
while every
other surface said nothing of the kind. This replaces the R3a shadow-only restriction ("nothing reads the
timeline") with
the thing that restriction stood in for: one generated snapshot is turned into a timeline, every surface is asked
about it,
and the answers must agree. Property-style: day type x fan mode x occupancy x time of day x readings x window effect are
generated (hypothesis) and the same invariants are asserted for each.

The coordinator's ``_compute_next_action`` / ``_compute_next_automation_action`` call exactly ``window_card_text`` /
``next_automation_candidates`` with the live readings (see ``tests/test_timeline_surfaces_coordinator.py`` for the real
coordinator calls on the Tier A scenarios); asserting on those pure functions here is asserting on the surface rules.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from custom_components.climate_advisor.briefing import generate_briefing
from custom_components.climate_advisor.operation_timeline import (
    Actor,
    StepKind,
    build_timeline,
    detail_dict,
    to_dict,
)
from custom_components.climate_advisor.temperature import display_temp
from custom_components.climate_advisor.timeline_surfaces import (
    NEXT_AUTOMATION_LABELS,
    CardLive,
    advises_opening,
    card_user_step,
    next_automation,
    next_automation_candidates,
    timeline_api_payload,
    timeline_windows,
    window_card_text,
)
from tests.timeline_fixtures import CONFIG, MODEL, TZ, classification, make_snapshot

_BANNED = re.compile(r"\b(waiting|paused|grace|window)s?\b", re.IGNORECASE)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}\s?(AM|PM)\b", re.IGNORECASE)

DAY_TYPES = ("hot", "warm", "mild", "cool", "cold")
FAN_MODES = ("whole_house_fan", "hvac_fan", "disabled")  # never "both" (Issue #1175)
OCCUPANCY = ("home", "away", "vacation", "guest")


def _live(snap, windows_open: bool, direction_ok: bool) -> CardLive:
    o, i = snap.outdoor, snap.indoor
    return CardLive(
        unit=snap.unit,
        indoor=i,
        outdoor=o,
        windows_open=windows_open,
        direction_ok=direction_ok,
        heat_side_veto=o is not None and i is not None and o < i,
        armed_heat=CONFIG["comfort_heat"],
        armed_cool=CONFIG["comfort_cool"],
    )


def _briefing_windows_row(snap, tl, day_type: str) -> str | None:
    c = classification(day_type)
    text = generate_briefing(
        c,
        comfort_heat=CONFIG["comfort_heat"],
        comfort_cool=CONFIG["comfort_cool"],
        setback_heat=CONFIG["setback_heat"],
        setback_cool=CONFIG["setback_cool"],
        wake_time=snap.wake_time,
        sleep_time=snap.sleep_time,
        fan_mode=snap.fan_mode,
        occupancy_mode=snap.occupancy_mode,
        runtime_config=dict(CONFIG),
        now=snap.now,
        timeline=tl,
        verbosity="tldr_only",
    )
    return next((ln.strip() for ln in text.splitlines() if ln.strip().startswith("Windows:")), None)


def _check_all(snap, day_type: str, *, windows_open: bool, direction_ok: bool) -> None:
    tl = build_timeline(snap)
    now = snap.now
    today = now.date()

    # --- Next Action: the card is the timeline's window step, never a window the plan does not hold -------------
    got = window_card_text(tl, now, _live(snap, windows_open, direction_ok))
    step = card_user_step(tl, now, windows_open=windows_open)
    assert (got is None) == (step is None)
    if got is not None:
        text, used = got
        assert used in tl.steps  # it IS a step of the timeline, not a recomputation
        assert used.actor == Actor.YOU and used.ts.date() == today
        if advises_opening(text):
            assert used.kind == StepKind.WINDOWS_OPEN
            # "Open windows at 10:00 AM" carries the step's own time; "now" cards sit inside the step's period
            if "Open windows at " in text:
                assert text == f"Open windows at {used.ts.strftime('%I:%M %p').lstrip('0')}"
            else:
                assert used.ts <= now
        elif text.startswith("Close windows by "):
            assert used.kind == StepKind.WINDOWS_CLOSE
            assert text == f"Close windows by {used.ts.strftime('%I:%M %p').lstrip('0')}"
    you_today = [
        s for s in tl.steps if s.actor == Actor.YOU and s.ts.date() == today and s.ts > now - timedelta(days=1)
    ]
    if not [s for s in you_today if s.kind in (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE)]:
        assert got is None  # no window step today -> the plan invites nothing

    # --- Next Automation: the earliest automation-executed step still ahead, labelled from the table -----------------
    action, at, nstep = next_automation(tl, now, snap.unit)
    cands = next_automation_candidates(tl, now, snap.unit)
    if cands:
        assert nstep is not None and nstep in tl.steps and nstep.actor == Actor.CA
        assert action == cands[0][1] and at == cands[0][0].strftime("%I:%M %p").lstrip("0")
        assert all(cands[i][0] <= cands[i + 1][0] for i in range(len(cands) - 1))
        assert not _BANNED.search(action), action  # ontology: never window/waiting/paused/grace
        assert not _CLOCK.search(action), action  # ontology: the time is the card's second value
    else:
        assert (action, at, nstep) == ("No more actions today", "", None)
    assert all(c[2].kind not in (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE) for c in cands)

    # --- briefing: the window times it states are the timeline's ----------------------------------------------------
    row = _briefing_windows_row(snap, tl, day_type)
    periods = [p for p in timeline_windows(tl, today).periods if p[1] > now]
    if snap.occupancy_mode in ("away", "vacation"):
        assert not [
            p for p in periods if p[0] is not None
        ]  # no period is offered to an empty house (a window already open still gets its close)
    if row is not None and "Close by" in row:
        times = re.findall(r"\b\d{1,2}:\d{2} (?:AM|PM)\b", row)
        closes = {c.strftime("%-I:%M %p") for _o, c in periods}
        assert times and times[0] in closes, (row, closes)
    if day_type in ("warm", "mild") and not periods:
        assert row is None or "Closed all day" in row, row

    # --- endpoint: same steps, same order, units converted ---------------------------------------------------------
    for unit in ("fahrenheit", "celsius"):
        payload = timeline_api_payload(to_dict(tl), detail_dict(tl), unit, display_temp)
        assert payload["unit"] == unit
        assert [(s["ts"], s["kind"], s["actor"], s["reason"]) for s in payload["steps"]] == [
            (s.ts.isoformat(), s.kind.value, s.actor.value, s.reason) for s in tl.steps
        ]
        for raw, conv in zip(to_dict(tl)["steps"], payload["steps"], strict=True):
            if raw["target_f"] is not None:
                assert conv["target_f"] == display_temp(raw["target_f"], unit)


@pytest.mark.parametrize("occupancy", OCCUPANCY)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("day_type", DAY_TYPES)
def test_surfaces_agree_for_every_day_type_fan_mode_and_occupancy(day_type, fan_mode, occupancy) -> None:
    for hour in (5, 9, 13, 18, 21):
        now = datetime(2026, 7 if day_type in ("hot", "warm") else 11, 4, hour, 15, tzinfo=TZ)
        snap = make_snapshot(day_type, now=now, fan_mode=fan_mode, occupancy=occupancy)
        _check_all(snap, day_type, windows_open=False, direction_ok=True)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    day_type=st.sampled_from(DAY_TYPES),
    tomorrow=st.sampled_from(DAY_TYPES),
    fan_mode=st.sampled_from(FAN_MODES),
    occupancy=st.sampled_from(OCCUPANCY),
    hour=st.integers(0, 23),
    minute=st.sampled_from((0, 15, 30, 45)),
    indoor=st.floats(60.0, 84.0),
    windows_open=st.booleans(),
    direction_ok=st.booleans(),
    validated=st.booleans(),
    sleep_sensor=st.sampled_from((None, "bedroom sensor")),
)
def test_surfaces_agree_for_generated_snapshots(
    day_type, tomorrow, fan_mode, occupancy, hour, minute, indoor, windows_open, direction_ok, validated, sleep_sensor
) -> None:
    model = dict(MODEL) if validated else dict(MODEL, observation_count_vent_window=12)
    now = datetime(2026, 6, 15, hour, minute, tzinfo=TZ)
    snap = make_snapshot(
        day_type,
        now=now,
        tomorrow=tomorrow,
        fan_mode=fan_mode,
        occupancy=occupancy,
        indoor=round(indoor, 1),
        model=model,
        sleep_sensor=sleep_sensor,
        windows_physically_open=windows_open,
    )
    _check_all(snap, day_type, windows_open=windows_open, direction_ok=direction_ok)


def test_the_label_table_is_free_of_banned_words_and_clock_times() -> None:
    """Status Card Ontology: Next Automation never narrates mechanism state or tells the occupant to do something CA
    cannot do, and never embeds a clock time in the action (the card's second value carries it)."""
    for key, label in NEXT_AUTOMATION_LABELS.items():
        assert not _BANNED.search(label), (key, label)
        assert not _CLOCK.search(label), (key, label)
        assert key.split(".")[0] not in ("windows_open", "windows_close")


def test_an_engine_recommended_window_day_is_on_every_surface_even_when_unvalidated() -> None:
    """D1: the occupant keeps the window advice they get today; card, briefing and endpoint all say it."""
    model = dict(MODEL, observation_count_vent_window=12)
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=70.0, model=model)
    tl = build_timeline(snap)
    assert snap.windows_recommended
    got = window_card_text(tl, now, _live(snap, False, True))
    assert got is not None and got[0].startswith("Open windows at ")
    assert all(s.confidence == "low" for s in tl.steps if s.actor == Actor.YOU)
    row = _briefing_windows_row(snap, tl, "mild")
    assert row is not None and "Close by" in row


def test_a_direction_veto_removes_the_period_from_every_surface_together() -> None:
    """#1276-style contradiction: a house at its heat floor with cold air outside gets no window advice anywhere."""
    now = datetime(2026, 11, 4, 18, 11, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=68.0, outdoor=60.6, thermostat_mode="heat")
    tl = build_timeline(snap)
    got = window_card_text(tl, now, _live(snap, False, False))
    assert got is None or not advises_opening(got[0])
    row = _briefing_windows_row(snap, tl, "mild")
    periods = [p for p in timeline_windows(tl, now.date()).periods if p[1] > now]
    assert (row is None or "Close by" not in row) or periods


def test_live_direction_turns_an_open_now_step_into_keep_closed_never_an_invitation() -> None:
    now = datetime(2026, 11, 4, 11, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=72.0)
    tl = build_timeline(snap)
    step = card_user_step(tl, now, windows_open=False)
    if step is None or step.kind != StepKind.WINDOWS_OPEN or step.ts > now:
        pytest.skip("fixture has no active window period at this hour")
    bad = window_card_text(tl, now, _live(snap, False, False))
    assert bad is not None and not advises_opening(bad[0])
    assert time(0, 0) <= bad[1].ts.time() <= time(23, 59)
