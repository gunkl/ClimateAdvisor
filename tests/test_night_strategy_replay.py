"""The night-strategy replay tool (Issue #1230 R4): classify recorded nights and score the v4 model against them.

A synthetic house with known physics generates the "recorded" history (no HVAC, windows open on some nights, the fan
on others), so a model fitted before each week must predict the later nights closely; the tool must file each night
under the right option class.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS.parent))
sys.path.insert(0, str(_TOOLS))
import night_strategy_replay as nsr  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
HOUSE = {
    "a_o": 0.05,
    "a_w": 0.15,
    "a_f": 0.5,
    "c_hm": 0.4,
    "c_mh": 0.2,
    "s": 0.5,
    "phase": 3.0,
    "q_h": 9.0,
    "q_c": 10.0,
}


def _night_mode(day: int) -> tuple[bool, bool]:
    """(windows, fan) for the night starting on synthetic day ``day``."""
    return {0: (False, False), 1: (True, False), 2: (True, True)}[day % 3]


def _history(days: int = 28) -> list[p4.V4Sample]:
    start = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
    n = days * 96
    steps = [start + timedelta(minutes=15 * k) for k in range(n)]
    house = [HOUSE[k] for k in p4.HOUSE_KEYS]
    out = [64 + 12 * math.sin((s.hour + s.minute / 60 - 9) / 24 * 2 * math.pi) for s in steps]
    win: list[float] = []
    fan: list[float] = []
    for s in steps:
        night = s.hour >= 20 or s.hour < 7
        d = (s - timedelta(hours=12)).date().toordinal()
        w, f = _night_mode(d) if night else (False, False)
        win.append(1.0 if w else 0.0)
        fan.append(1.0 if f else 0.0)
    roll = p4.rollout(
        house, [], h0=72.0, m0=72.0, b0=[], out=out, win=win, fan=fan,
        hours=[s.hour + s.minute / 60 for s in steps], heat_u=[0.0] * n, cool_u=[0.0] * n,
    )  # fmt: skip
    return [
        p4.V4Sample(
            ts=s,
            outdoor_f=out[k],
            forecast_outdoor_f=out[k],
            hvac_action="idle",
            fan_running=bool(fan[k]),
            windows_open=bool(win[k]),
            heat_sp_f=None,
            cool_sp_f=None,
            readings={"_ca": roll.primary[k]},
            heat_duty=0.0,
            cool_duty=0.0,
        )  # fmt: skip
        for k, s in enumerate(steps)
    ]


def test_classify_files_nights_under_the_right_class() -> None:
    samples = _history(6)
    nights = nsr.find_nights(samples, TZ, (20, 30), (6, 30))
    assert len(nights) >= 4
    seen = {}
    for start, _end, rows in nights:
        cls, sub, _stats = nsr.classify(rows)
        seen[start.date()] = (cls, sub)
    classes = {c for c, _ in seen.values()}
    assert classes == {ns.CLS_CLOSED, ns.CLS_WINDOWS, ns.CLS_FAN}
    assert all(sub.startswith(cls) for cls, sub in seen.values())


def test_nights_with_a_gap_are_dropped() -> None:
    samples = _history(4)
    gap = [
        s
        for s in samples
        if not (datetime(2026, 6, 2, 23, 0, tzinfo=TZ) <= s.ts < datetime(2026, 6, 3, 2, 0, tzinfo=TZ))
    ]
    full = nsr.find_nights(samples, TZ, (20, 30), (6, 30))
    cut = nsr.find_nights(gap, TZ, (20, 30), (6, 30))
    assert len(cut) == len(full) - 1


def test_replay_scores_fan_and_window_nights_against_known_physics() -> None:
    rep = nsr.run(_history(28), TZ, log=lambda *_a: None)
    classes = rep["classes"]
    assert set(classes) == {ns.CLS_CLOSED, ns.CLS_WINDOWS, ns.CLS_FAN}
    for cls, v in classes.items():
        assert v["n_nights"] >= 3, cls
        assert v["mae_f"] < 1.0, (cls, v)  # the model is fitted on the same physics
        assert v["flat_mae_f"] is not None and v["mae_f"] < v["flat_mae_f"], (cls, v)
    assert rep["provisional"] is True
    # the result is exactly what the gate reads
    val = ns.NightValidation.from_dict(rep)
    assert val is not None and ns.validation_status(val, ns.CLS_FAN)[0] == "medium"


def test_class_with_a_frozen_rate_is_excluded_not_scored() -> None:
    # fan never ran before the first fan night -> the fit has a_f frozen -> the night is excluded, not scored badly
    samples = [s for s in _history(14) if not s.fan_running or s.ts >= datetime(2026, 6, 10, tzinfo=TZ)]
    rep = nsr.run(samples, TZ, log=lambda *_a: None)
    assert rep["excluded"]["fan_rate_frozen"] >= 1
    assert ns.CLS_FAN not in rep["classes"]  # no scored fan night: no evidence, so the gate hides the fan options
    assert rep["n_nights_found"] >= 10
