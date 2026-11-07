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

import dataclasses
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
    apply_direction_veto,
    card_user_step,
    is_direction_vetoed,
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
    now = snap.now
    today = now.date()
    live = _live(snap, windows_open, direction_ok)
    # every surface reads the plan with the live direction veto applied (what the coordinator's _surface_timeline does)
    tl = apply_direction_veto(build_timeline(snap), now, live)

    # --- Next Action: the card is the timeline's window step, never a window the plan does not hold -------------
    got = window_card_text(tl, now, live)
    step = card_user_step(tl, now, windows_open=windows_open)
    assert (got is None) == (step is None)
    if got is not None:
        text, used = got
        assert used in tl.steps  # it IS a step of the timeline, not a recomputation
        assert used.actor == Actor.YOU and used.ts.date() == today
        if is_direction_vetoed(used):
            assert text == used.text and not advises_opening(text)  # one sentence on the card and in the plan list
            assert used.status == "held" and used.meta["hold"].startswith("held.direction_veto.")
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
    if row is not None and "lose by" in row:
        times = re.findall(r"\b\d{1,2}:\d{2} (?:AM|PM)\b", row)
        closes = {c.strftime("%-I:%M %p") for _o, c in periods}
        assert times and times[-1] in closes, (row, closes)  # the last time of the row is the close
        if row.startswith("Windows: Open ") and " \u2013 close by " in row:  # both times while the open is ahead
            opens = {o.strftime("%-I:%M %p") for o, _c in periods if o is not None and o > now}
            assert times[0] in opens, (row, opens)
    if day_type in ("warm", "mild") and not periods:
        assert row is None or "Closed all day" in row or "Closed for now" in row, row
    if timeline_windows(tl, today).vetoed:
        assert row is None or "lose by" not in row or periods, row  # a vetoed period is never "Close by"

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
    assert row is not None and "lose by" in row  # "Open 8:00 AM \u2013 close by ..." while the open is still ahead


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


# ---------------------------------------------------------------------------
# The live direction veto is a property of the plan step (R3b redesign, owner decision 2026-11-06)
# ---------------------------------------------------------------------------


def _running_period():
    """A mild-day timeline and a clock five minutes into its first window period (the windows still shut)."""
    snap = make_snapshot("mild", now=datetime(2026, 11, 4, 8, 0, tzinfo=TZ), indoor=68.0)
    tl = build_timeline(snap)
    opened = next(s for s in tl.steps if s.kind == StepKind.WINDOWS_OPEN and s.ts.date() == snap.now.date())
    return snap, tl, opened, opened.ts + timedelta(minutes=5)


def _veto_live(snap, *, outdoor: float, indoor: float, direction_ok: bool = False) -> CardLive:
    return CardLive(
        unit=snap.unit,
        indoor=indoor,
        outdoor=outdoor,
        windows_open=False,
        direction_ok=direction_ok,
        heat_side_veto=outdoor < indoor,
        armed_heat=68.0,
        armed_cool=74.0,
    )


def test_the_veto_holds_the_open_step_with_the_cards_sentence_in_the_plan_list_and_the_endpoint() -> None:
    snap, tl, opened, now = _running_period()
    live = _veto_live(snap, outdoor=60.6, indoor=68.0)
    vetoed = apply_direction_veto(tl, now, live)
    held = next(s for s in vetoed.steps if s.id == opened.id)
    sentence = "Keep windows closed to hold heat — outdoor (61°F) would cool the house below its 68°F floor."
    assert held.status == "held" and held.meta["hold"] == "held.direction_veto.heat" and held.text == sentence
    assert is_direction_vetoed(held)
    card, used = window_card_text(vetoed, now, live)
    assert card == sentence and used.text == sentence  # the card and the list row are one sentence
    payload = timeline_api_payload(to_dict(vetoed), detail_dict(vetoed), "fahrenheit", display_temp)
    row = next(s for s in payload["steps"] if s["meta"]["id"] == opened.id)
    assert row["text"] == sentence and row["meta"]["status"] == "held"
    # the latched plan itself is untouched, and the veto is idempotent
    assert next(s for s in tl.steps if s.id == opened.id).status == "planned"
    assert apply_direction_veto(vetoed, now, live) is vetoed
    # the unmarked timeline gives the card the same answer
    assert window_card_text(tl, now, live)[0] == sentence


def test_the_vetoed_period_is_not_close_by_in_the_briefing() -> None:
    snap, tl, opened, now = _running_period()
    vetoed = apply_direction_veto(tl, now, _veto_live(snap, outdoor=60.6, indoor=68.0))
    windows = timeline_windows(vetoed, now.date())
    assert windows.vetoed == "heat" and not [p for p in windows.periods if p[0] == opened.ts]
    snap2 = make_snapshot("mild", now=now, indoor=68.0, outdoor=60.6)
    row = _briefing_windows_row(snap2, vetoed, "mild")
    assert row == "Windows: Closed for now — outdoor air would cool the house below its floor"


def test_the_not_cooler_variant_is_held_with_its_own_sentence() -> None:
    snap, tl, opened, now = _running_period()
    live = _veto_live(snap, outdoor=69.0, indoor=68.0)  # outdoor warmer than indoor, no heat-side veto
    vetoed = apply_direction_veto(tl, now, live)
    held = next(s for s in vetoed.steps if s.id == opened.id)
    assert held.meta["hold"] == "held.direction_veto.warm"
    assert held.text == "Keep windows closed for now — outdoor (69°F) isn't cooler than indoor (68°F) yet."
    assert timeline_windows(vetoed, now.date()).vetoed == "warm"


@pytest.mark.parametrize(
    "case",
    ["direction_ok_cooler", "unknown_outdoor", "period_not_started", "windows_already_open"],
)
def test_the_veto_leaves_the_plan_alone_when_nothing_is_vetoed(case: str) -> None:
    snap, tl, opened, now = _running_period()
    live = _veto_live(snap, outdoor=60.0, indoor=72.0, direction_ok=True)
    if case == "unknown_outdoor":
        live = CardLive("fahrenheit", 68.0, None, False, False, False, 68.0, 74.0)
    if case == "period_not_started":
        now = opened.ts - timedelta(minutes=30)
        live = _veto_live(snap, outdoor=60.6, indoor=68.0)
    if case == "windows_already_open":
        live = CardLive("fahrenheit", 68.0, 60.6, True, False, True, 68.0, 74.0)
    assert apply_direction_veto(tl, now, live) is tl


def test_the_windows_row_gives_both_times_while_the_open_is_still_ahead_and_only_the_close_after() -> None:
    """Occupant view: at 8 AM the card says "Open windows at T"; a reader of the briefing alone must be told to open."""
    snap = make_snapshot("mild", now=datetime(2026, 11, 4, 8, 0, tzinfo=TZ), indoor=68.0)
    tl = build_timeline(snap)
    opened = next(s for s in tl.steps if s.kind == StepKind.WINDOWS_OPEN and s.ts.date() == snap.now.date())
    closed = next(s for s in tl.steps if s.kind == StepKind.WINDOWS_CLOSE and s.ts > opened.ts)
    o, c = opened.ts.strftime("%-I:%M %p"), closed.ts.strftime("%-I:%M %p")
    assert opened.ts > snap.now
    assert _briefing_windows_row(snap, tl, "mild") == f"Windows: Open {o} – close by {c}"
    later = dataclasses.replace(snap, now=opened.ts + timedelta(minutes=10))  # same plan, the open now behind us
    assert _briefing_windows_row(later, tl, "mild") == f"Windows: Close by {c}"
