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

Cases the current tree still gets wrong are selected by ``_known_bad`` and marked strict xfail (so the corpus
is proven to catch the bug before the fix lands); the commit that fixes a behaviour narrows that predicate.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import time

import pytest

from custom_components.climate_advisor.briefing import generate_briefing
from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day
from custom_components.climate_advisor.const import (
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
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
_FANS = (FAN_MODE_DISABLED, FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC)
_OCCUPANCY = ("home", "guest", "away", "vacation")

_WINDOW_PHRASES = (
    "open south and east windows",
    "windows are open to catch",
    "close up",
    "reopen windows",
    "boost cross-ventilation",
    "good airflow",
    "windows day",
)
_NO_ENERGY_PHRASES = ("no energy impact", "the hvac is off today", "hvac is off this morning")
_PROMISE_PHRASES = ("around 7pm", "bank some extra warmth", "ease back a couple degrees midday")
_NEAR_MISS_PHRASE = "may help later"


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
    # other day types: no windows-day branch; promises / band wording only. Tomorrow much colder to
    # trigger the cooling-trend sentences.
    for dt, high, low in (("hot", 92, 70), ("cool", 52, 40), ("cold", 35, 25)):
        for fan in _FANS:
            for occ in ("home", "away"):
                out.append(
                    Case(
                        f"{dt}-{high}-{low}-default-{fan}-{occ}",
                        dt,
                        high,
                        low,
                        "default",
                        fan,
                        occ,
                        False,
                        tomorrow_high=high - 12,
                    )
                )
    return out


CASES = _cases()


@functools.cache
def _render(case: Case) -> tuple[str, str, str]:
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
        time(6, 30),
        time(22, 30),
        fan_mode=case.fan,
        occupancy_mode=case.occupancy,
    )
    hvac_row = next(line for line in text.splitlines() if "HVAC Mode:" in line)
    windows_row = next(line for line in text.splitlines() if line.strip().startswith("Windows:"))
    return text, hvac_row, windows_row


def _violations(case: Case) -> list[str]:
    text, hvac_row, windows_row = _render(case)
    low = text.lower()
    heat, cool, set_heat, set_cool = _CONFIGS[case.config]
    away = case.occupancy in ("away", "vacation")
    expect_windows = case.windows_day and not away
    problems: list[str] = []

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
        allowed = {
            round(v)
            for v in (
                heat,
                cool,
                set_heat,
                set_cool,
                case.high,
                case.tomorrow_high if case.tomorrow_high is not None else case.high,
                heat - DEFAULT_SETBACK_DEPTH_F,
                cool + DEFAULT_SETBACK_DEPTH_COOL_F,
            )
        }
        for num in re.findall(r"(\d+)\s*°F", text):
            if int(num) not in allowed:
                problems.append(f"quoted temperature {num}°F is not a configured or forecast value")
    for phrase in _PROMISE_PHRASES:
        if phrase in low:
            problems.append(f"promises an action no code performs: {phrase!r}")
    if case.day_type == "cold" and "bump to" in low:
        problems.append("cold-day pre-heat promise")
    return problems


def _known_bad(case: Case) -> bool:
    """True while the tree still renders this case wrongly. Before the fix commits: every case does
    (each carries "HVAC is off / no energy impact" or an unbacked promise, and the window cases also
    follow a classifier that always recommends mild days)."""
    return True


def _params():
    for case in CASES:
        marks = (
            [pytest.mark.xfail(strict=True, reason="Issue #1211: fixed in the briefing commit")]
            if _known_bad(case)
            else []
        )
        yield pytest.param(case, id=case.id, marks=marks)


@pytest.mark.parametrize("case", _params())
def test_briefing_only_talks_about_windows_when_they_help(case: Case) -> None:
    assert _violations(case) == []
