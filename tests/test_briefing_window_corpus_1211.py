"""Issue #1211: the briefing may only talk about windows when windows actually help.

Every case is built with the REAL classifier (``classify_day``), never a hand-built
``DayClassification``, so the briefing is exercised on exactly what production hands it. Each case
carries a hand-written expectation (``windows_day``) taken from the documented rule
(outside air helps when the day's high reaches the comfort floor and its low is at most
``comfort_cool - 3``; never while away; never from an estimated low), not computed with the rule.

Invariants asserted on every rendered briefing:

* the HVAC row says "windows day" and the Windows row says "Close by" exactly when windows are recommended,
  and a home day with an armed band says so ("Comfort band") instead of "Off";
* no window sentence, fan-ventilation sentence, "close up" or "reopen" line without a recommendation, and
  none at all while away or on vacation;
* no "HVAC is off today / no energy impact" on a warm or mild day (a comfort band is armed);
* a near-miss mild day says outside air may help later, other non-windows days say nothing about windows;
* nothing promised that no code performs ("bump to X around 7pm", "bank some extra warmth", "ease back a
  couple degrees midday");
* no temperature quoted that is not a configured/armed value, a forecast value or a documented fallback
  (catches the old hard-coded ``comfort_heat - 2``).

Issue #1231 extends the corpus to every day type (hot, warm, mild, cool, cold) x home/guest/away/vacation x
both fan modes (never ``both``, which is unsupported) and adds the briefing-facts rules:

* every quoted temperature is a configured/armed/forecast value for the occupancy (the armed band comes from
  ``select_comfort_band``, the normal bedtime setpoint is the edge the engine arms at bedtime: ``sleep_heat`` on a
  heat day, ``sleep_cool`` on a cool day) -- and at home the setbacks are quoted only in the "if you head out"
  paragraph, never as the at-home value (the old cold-day "instead of the usual 60");
* the body and the TLDR quote the same bedtime number and the same hold number;
* every time quoted is a classifier/schedule time and the TLDR close time reappears in the body;
* away/vacation, on EVERY day type: no at-home temperature/bedtime/pre-cool promise, no window advice, no time,
  the Windows row says "Closed all day" and the Bedtime row says "away setback held".

Before the fix this whole corpus was committed as strict xfail (proving it catches the bug); the briefing
commit removed the marks once every case rendered correctly.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import time

import pytest

from custom_components.climate_advisor.automation import (
    compute_pre_cool_target,
    resolve_pre_cool_modifier,
    select_comfort_band,
)
from custom_components.climate_advisor.briefing import generate_briefing
from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day
from custom_components.climate_advisor.const import (
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

# (comfort_heat, comfort_cool, setback_heat, setback_cool). The two sets are chosen so that
# comfort_heat - 2 is not itself a legitimate quoted value in either (68 -> 66, 70 -> 68).
_CONFIGS = {
    "default": (68.0, 74.0, 63.0, 79.0),
    "custom": (70.0, 78.0, 61.0, 84.0),
}
# (sleep_heat, sleep_cool): chosen distinct from every comfort / setback / forecast value in the corpus so a
# bedtime number quoted from anywhere else (comfort - 2, the old setback_heat) cannot hide among legitimate ones.
_SLEEP = {"default": (67.0, 78.0), "custom": (64.0, 82.0)}
_SLEEP_TIME = time(22, 30)
_WAKE_TIME = time(6, 30)
_FANS = (FAN_MODE_DISABLED, FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC)
_OCCUPANCY = ("home", "guest", "away", "vacation")

_WINDOW_PHRASES = (
    "open south and east windows",
    "windows are open to catch",
    "close up by",
    "close up at",
    "close up now",
    "reopen windows",
    "boost cross-ventilation",
    "good airflow",
    "windows day",
)
_NO_ENERGY_PHRASES = ("no energy impact", "the hvac is off today", "hvac is off this morning")
_PROMISE_PHRASES = ("around 7pm", "bank some extra warmth", "ease back a couple degrees midday")
_NEAR_MISS_PHRASE = "may help later"
# Sentences that SAY something about whether outside air helps the house today.
_AIR_CLAIM_PHRASES = ("outside air won't do much", "outside air isn't going to warm", "outside air may help")


@dataclass(frozen=True)
class Case:
    id: str
    day_type: str
    high: float
    low: float
    config: str
    fan: str
    occupancy: str
    windows_day: bool  # hand-written: should windows be recommended to an occupant who is home?
    near_miss: bool = False  # hand-written: mild forecast within ~5 F below the floor
    low_estimated: bool = False
    tomorrow_high: float | None = None


def _cases() -> list[Case]:
    out: list[Case] = []
    # mild: (high, low, windows_day, near_miss) per config -- floors 68 (default) / 70 (custom).
    mild = {
        "default": [(65, 49, False, True), (61, 45, False, False), (72, 50, True, False), (68, 52, True, False)],
        "custom": [(67, 50, False, True), (62, 45, False, False), (73, 52, True, False)],
    }
    # warm: ceilings 74 (needs low <= 71) / 78 (needs low <= 75)
    warm = {
        "default": [(80, 60, True), (80, 74, False), (80, 71, True)],
        "custom": [(80, 74, True), (80, 76, False)],
    }
    for cfg in _CONFIGS:
        for high, low, wd, nm in mild[cfg]:
            for fan in _FANS:
                for occ in _OCCUPANCY:
                    out.append(Case(f"mild-{high}-{low}-{cfg}-{fan}-{occ}", "mild", high, low, cfg, fan, occ, wd, nm))
        for high, low, wd in warm[cfg]:
            for fan in _FANS:
                for occ in _OCCUPANCY:
                    out.append(Case(f"warm-{high}-{low}-{cfg}-{fan}-{occ}", "warm", high, low, cfg, fan, occ, wd))
    # missing data: the forecast low is only an estimate -> make no window claim
    for dt, high, low in (("mild", 72, 50), ("warm", 80, 55)):
        for fan in _FANS:
            for occ in ("home", "away"):
                out.append(
                    Case(
                        f"{dt}-{high}-{low}-default-{fan}-{occ}-estimated",
                        dt,
                        high,
                        low,
                        "default",
                        fan,
                        occ,
                        False,
                        low_estimated=True,
                    )
                )
    # other day types, every occupancy and both configs. Tomorrow much colder to trigger the cooling-trend
    # sentences; the hot day also gets a warming-tomorrow variant so tonight's pre-cool sentence is exercised.
    for cfg in _CONFIGS:
        for dt, high, low in (("hot", 92, 70), ("cool", 52, 40), ("cold", 35, 25)):
            for fan in _FANS:
                for occ in _OCCUPANCY:
                    out.append(
                        Case(
                            f"{dt}-{high}-{low}-{cfg}-{fan}-{occ}",
                            dt,
                            high,
                            low,
                            cfg,
                            fan,
                            occ,
                            False,
                            tomorrow_high=high - 12,
                        )
                    )
        for fan in _FANS:
            for occ in _OCCUPANCY:
                out.append(
                    Case(f"hot-92-70-{cfg}-{fan}-{occ}-warming", "hot", 92, 70, cfg, fan, occ, False, tomorrow_high=97)
                )
    return out


CASES = _cases()


def _runtime_config(case: Case) -> dict:
    heat, cool, set_heat, set_cool = _CONFIGS[case.config]
    sleep_heat, sleep_cool = _SLEEP[case.config]
    return {
        "comfort_heat": heat,
        "comfort_cool": cool,
        "setback_heat": set_heat,
        "setback_cool": set_cool,
        "sleep_heat": sleep_heat,
        "sleep_cool": sleep_cool,
        "sleep_time": "22:30",
        "wake_time": "06:30",
    }


@functools.cache
def _render(case: Case):
    heat, cool, set_heat, set_cool = _CONFIGS[case.config]
    snap = ForecastSnapshot(
        today_high=case.high,
        today_low=case.low,
        tomorrow_high=case.tomorrow_high if case.tomorrow_high is not None else case.high,
        tomorrow_low=case.low,
        current_outdoor_temp=case.low,
        low_is_estimated=case.low_estimated,
    )
    c = classify_day(snap, comfort_heat=heat, comfort_cool=cool)
    assert c.day_type == case.day_type, "corpus case does not classify as intended"
    text = generate_briefing(
        c,
        heat,
        cool,
        set_heat,
        set_cool,
        _WAKE_TIME,
        _SLEEP_TIME,
        fan_mode=case.fan,
        occupancy_mode=case.occupancy,
        runtime_config=_runtime_config(case),
    )
    hvac_row = next(line for line in text.splitlines() if "HVAC Mode:" in line)
    windows_row = next(line for line in text.splitlines() if line.strip().startswith("Windows:"))
    return text, hvac_row, windows_row, c


_TEMP_RE = re.compile(r"(?<![+\-\d])(\d+)(?:\u2013(\d+))?\s*\u00b0F")
_TIME_RE = re.compile(r"\d{1,2}:\d{2} [AP]M")
# Phrases that promise an at-home temperature / bedtime / pre-cool / window action. An empty house gets none of them.
_AT_HOME_PROMISES = (
    "i'll hold",
    "at bedtime",
    "pre-cool",
    "cool the home to",
    "tonight i",
    "conservative setback",
    "pull back down",
    "keep-it-sealed",
    "blinds",
    "curtains",
    "close up for",
    "close up by",
    "close up at",
    "close up now",
    "reopen",
    "enjoy the cross-breeze",
)


def _independent_facts(case: Case, c) -> tuple[set[int], set[int]]:
    """(temps quoted anywhere, temps additionally allowed in the 'if you head out' paragraph) from config values
    and the engine's own band selector -- never from the briefing's own facts."""
    heat, cool, set_heat, set_cool = _CONFIGS[case.config]
    sleep_heat, sleep_cool = _SLEEP[case.config]
    cfg = _runtime_config(case)
    tomorrow = case.tomorrow_high if case.tomorrow_high is not None else case.high
    band = select_comfort_band(c, cfg, occupancy_mode=case.occupancy, in_sleep_window=False, aggressive_savings=False)
    anywhere = {case.high, tomorrow, band.floor, band.ceiling}
    if case.occupancy in ("home", "guest"):
        # at home the thermostat is armed with the sleep band's edge on this day's side at bedtime
        asleep = select_comfort_band(c, cfg, occupancy_mode="home", in_sleep_window=True, aggressive_savings=False)
        if c.hvac_mode == "heat":
            anywhere.add(asleep.floor)
        elif c.hvac_mode == "cool":
            anywhere.add(asleep.ceiling)
        anywhere.add(cool + ECONOMIZER_TEMP_DELTA)  # the "(<77F)" outdoor threshold on a hot windows row
        modifier = resolve_pre_cool_modifier(c, cfg)
        if modifier is not None:
            anywhere.add(compute_pre_cool_target(cfg, modifier))
    # "If you head out ... I'll drop to X": a hypothetical away setback, labelled as such.
    leaving = {set_heat, set_cool, band.floor, band.ceiling}
    return {round(v) for v in anywhere}, {round(v) for v in leaving}


def _violations(case: Case) -> list[str]:
    text, hvac_row, windows_row, c = _render(case)
    low = text.lower()
    heat, cool, set_heat, set_cool = _CONFIGS[case.config]
    sleep_heat, sleep_cool = _SLEEP[case.config]
    away = case.occupancy in ("away", "vacation")
    expect_windows = case.windows_day and not away
    problems: list[str] = []
    rows = {
        line.strip().split(":", 1)[0]: line.strip().split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("  ") and ":" in line
    }

    if case.day_type in ("warm", "mild"):
        if ("windows day" in hvac_row.lower()) != expect_windows:
            problems.append(f"HVAC row windows-day mismatch (expected {expect_windows}): {hvac_row.strip()!r}")
        if not away and "comfort band" not in hvac_row.lower():
            problems.append(f"home day with an armed band should say so: {hvac_row.strip()!r}")
        if away and ("away" not in hvac_row.lower() and "vacation" not in hvac_row.lower()):
            problems.append(f"away day lost its away wording: {hvac_row.strip()!r}")
        if ("close by" in windows_row.lower()) != expect_windows:
            problems.append(f"Windows row mismatch (expected recommended={expect_windows}): {windows_row.strip()!r}")
        if not expect_windows:
            for phrase in _WINDOW_PHRASES:
                if phrase in low:
                    problems.append(f"window sentence {phrase!r} without a recommendation")
        if case.fan != FAN_MODE_DISABLED and not expect_windows and "fan" in low and "cross-ventilation" in low:
            problems.append("fan-ventilation sentence without a recommendation")
        for phrase in _NO_ENERGY_PHRASES:
            if phrase in low:
                problems.append(f"{phrase!r} while a comfort band is armed")
        wants_near_miss = case.near_miss and not away and not case.low_estimated
        if (_NEAR_MISS_PHRASE in low) != wants_near_miss:
            problems.append(f"near-miss wording mismatch (expected {wants_near_miss})")
        # Away/vacation: the lead line says nothing about the air and quotes no awake band (the away section
        # quotes the setbacks); an estimated low: neither "helps" nor "won't help" is known, so neither is said.
        if away or case.low_estimated:
            for phrase in _AIR_CLAIM_PHRASES:
                if phrase in low and not (phrase == "outside air may help" and not away and wants_near_miss):
                    problems.append(f"claim about outside air {phrase!r} while away or on an estimated low")
        # A real forecast low and no windows day at home: the briefing says air will not help (not silence).
        says_wont_help = "won't do much" in low or "isn't going to warm" in low
        if not away and not case.low_estimated and not expect_windows and not wants_near_miss and not says_wont_help:
            problems.append("a home non-windows day on a real low should say outside air won't help")

    # --- #1231: facts rules, every day type -------------------------------------------------------------
    anywhere, leaving = _independent_facts(case, c)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    for para in paragraphs:
        is_leaving = "head out" in para or "you're currently away" in para.lower() or "vacation" in para.lower()
        ok = anywhere | (leaving if is_leaving else set())
        for lo, hi in _TEMP_RE.findall(para):
            for num in (lo, hi):
                if num and int(num) not in ok:
                    problems.append(
                        f"quoted temperature {num}\u00b0F is not a configured/armed/forecast value here: {para[:70]!r}"
                    )

    if away:
        awake_band = f"{heat:.0f}\u2013{cool:.0f}\u00b0f"
        if awake_band in low:
            problems.append("awake comfort band quoted while away or on vacation")
        body = "\n".join(line for line in text.splitlines() if "Bedtime Setback:" not in line).lower()
        for phrase in _AT_HOME_PROMISES:
            if phrase in body:
                problems.append(f"at-home promise {phrase!r} while away or on vacation")
        for lone in (sleep_heat, sleep_cool):
            if f"{lone:.0f}\u00b0f" in low and round(lone) not in {
                round(case.high),
                round(case.tomorrow_high or case.high),
            }:
                problems.append(f"at-home bedtime value {lone:.0f}\u00b0F quoted while away or on vacation")
        if _TIME_RE.findall(text):
            problems.append(f"time quoted while away or on vacation: {_TIME_RE.findall(text)}")
        if "closed all day" not in windows_row.lower():
            problems.append(f"Windows row should give no advice while away: {windows_row.strip()!r}")
        if rows.get("Bedtime Setback", "").lower() != "away setback held":
            problems.append(f"Bedtime row should say the away setback is held: {rows.get('Bedtime Setback')!r}")
    else:
        # home/guest: setbacks are never the at-home value (the old cold-day "instead of the usual 60")
        if "instead of the usual" in low:
            problems.append("'instead of the usual X' without an adaptive bedtime value to compare")
        times = set(_TIME_RE.findall(text))
        sched = {
            t.strftime("%-I:%M %p")
            for t in (c.window_close_time, c.window_opportunity_morning_end, c.window_opportunity_evening_start)
            if t is not None
        }
        if case.day_type in ("hot", "cool", "cold"):
            sched.add(_SLEEP_TIME.strftime("%-I:%M %p"))
        for t in times - sched:
            problems.append(f"time {t} is not a classifier/schedule time")
        # the TLDR windows row's times reappear in the body (header agrees with body)
        body_only = text.split("Tomorrow:", 1)[-1]
        for t in _TIME_RE.findall(windows_row):
            if t not in body_only:
                problems.append(f"TLDR window time {t} does not reappear in the body")
        # bedtime / hold numbers: body and TLDR quote the same one
        bed_row = _TEMP_RE.findall(rows.get("Bedtime Setback", ""))
        for pat in (r"conservative setback \u2014 (\d+)", r"set back to (\d+)"):
            for m in re.findall(pat, body_only):
                if not bed_row or m != bed_row[0][0]:
                    problems.append(f"body bedtime {m} disagrees with TLDR row {rows.get('Bedtime Setback')!r}")
        want_bed = {"heat": sleep_heat, "cool": sleep_cool}.get(c.hvac_mode)
        if want_bed is not None and bed_row and int(bed_row[0][0]) != round(want_bed):
            problems.append(f"Bedtime row {rows.get('Bedtime Setback')!r} is not the engine's bedtime edge {want_bed}")
        hvac_num = _TEMP_RE.findall(hvac_row)
        for m in re.findall(r"hold(?: things)?(?: at)? (\d+)\s*\u00b0F", body_only):
            if hvac_num and m != hvac_num[0][0]:
                problems.append(f"body holds {m} but the HVAC row says {hvac_row.strip()!r}")

    for phrase in _PROMISE_PHRASES:
        if phrase in low:
            problems.append(f"promises an action no code performs: {phrase!r}")
    if case.day_type == "cold" and "bump to" in low:
        problems.append("cold-day pre-heat promise")
    return problems


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in CASES])
def test_briefing_only_talks_about_windows_when_they_help(case: Case) -> None:
    assert _violations(case) == []
