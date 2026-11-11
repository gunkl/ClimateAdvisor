"""Issue #1517 (owner decision 2026-11-11): keep the 240-minute TOU lead cap, and log a warning when the target looks
out of reach within it.

Occupant: on a day the equipment cannot close the gap in 4 hours, the bank still starts at the 4-hour maximum and the
house may fall short of the target when the window opens; the log now says so instead of staying silent. A lead that
only reaches the cap because of the safety margin is reachable and stays quiet, and the warning is rate-limited so the
5-minute checks and the chart walk do not repeat it.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor import scheduler, thermal_lead_time
from custom_components.climate_advisor.thermal_lead_time import compute_lead_minutes_from_rate

_LOGGER_NAME = "custom_components.climate_advisor.thermal_lead_time"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    thermal_lead_time._out_of_reach_warned.clear()
    yield
    thermal_lead_time._out_of_reach_warned.clear()


def _plain(delta_t: float, rate: float, *, label: str | None = "Time-of-use bank") -> float:
    return compute_lead_minutes_from_rate(
        delta_t,
        rate,
        min_minutes=30,
        max_minutes=240,
        safety_multiplier=1.3,
        fallback_minutes=45,
        start_temp=72.0,
        target_temp=72.0 - delta_t,
        cooling=True,
        warn_label=label,
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]


def test_a_target_beyond_the_cap_warns_once_and_still_returns_the_cap(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    assert _plain(10.0, 2.0) == 240  # 300 minutes without the margin: out of reach
    assert _plain(10.0, 2.0) == 240  # the next 5-minute check
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "Time-of-use bank" in msgs[0] and "out of reach" in msgs[0] and "62.0" in msgs[0]


def test_the_warning_repeats_after_the_rate_limit_window(caplog, monkeypatch) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    clock = [1000.0]
    monkeypatch.setattr(thermal_lead_time.time, "monotonic", lambda: clock[0])
    _plain(10.0, 2.0)
    clock[0] += thermal_lead_time.OUT_OF_REACH_WARN_INTERVAL_S - 1
    _plain(10.0, 2.0)
    clock[0] += 2
    _plain(10.0, 2.0)
    assert len(_warnings(caplog)) == 2


def test_capped_only_by_the_safety_margin_is_reachable_and_quiet(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    # 200 minutes bare (reachable), 260 with the 1.3 margin: capped, but no warning
    assert _plain(10.0, 3.0) == 240
    assert _warnings(caplog) == []


def test_without_a_label_nothing_is_logged(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    assert _plain(10.0, 2.0, label=None) == 240
    assert _warnings(caplog) == []


def test_no_rate_falls_back_quietly(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    assert (
        compute_lead_minutes_from_rate(
            4.0, None, min_minutes=30, max_minutes=240, safety_multiplier=1.3, fallback_minutes=45, warn_label="x"
        )
        == 45
    )
    assert _warnings(caplog) == []


@pytest.mark.parametrize("cooling", [True, False])
def test_leak_aware_unreachable_target_warns_both_modes(caplog, cooling: bool) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    # steady state T_out - q/k sits on the wrong side of the target: never reached
    start, target, outdoor, rate = (74.0, 68.0, 100.0, -3.0) if cooling else (66.0, 72.0, 10.0, 3.0)
    lead = compute_lead_minutes_from_rate(
        abs(start - target),
        rate,
        min_minutes=30,
        max_minutes=240,
        safety_multiplier=1.3,
        fallback_minutes=45,
        start_temp=start,
        target_temp=target,
        outdoor_temp=outdoor,
        k_passive=-0.2,
        cooling=cooling,
        confidence="high",
        warn_label="Time-of-use bank",
    )
    assert lead == 240
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and ("cooling" if cooling else "heating") in msgs[0] and "leak-aware" in msgs[0]


class _WeakStepper:
    """A house model whose equipment barely moves the house: no target 5 F away is reached in 4 hours."""

    cool_gain = 0.5
    heat_gain = 0.5

    def step(self, t, mass, out, _a, _b, _hour):
        return t, mass


class _StrongStepper(_WeakStepper):
    cool_gain = 1.4  # 5 F in ~214 min bare: reachable, but 1.15x margin caps it
    heat_gain = 1.4


@pytest.mark.parametrize(("stepper", "warns"), [(_WeakStepper(), True), (_StrongStepper(), False)])
def test_house_model_path_warns_only_when_the_bare_run_misses(caplog, stepper, warns: bool) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    lead = compute_lead_minutes_from_rate(
        5.0,
        -3.0,
        min_minutes=30,
        max_minutes=240,
        safety_multiplier=1.3,
        fallback_minutes=45,
        start_temp=73.0,
        target_temp=68.0,
        outdoor_temp=80.0,
        k_passive=-0.05,
        cooling=True,
        house_stepper=stepper,
        outdoor_at=lambda _when: 80.0,
        event_ts=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        confidence="medium",
        warn_label="Time-of-use bank",
    )
    assert lead == 240
    msgs = _warnings(caplog)
    assert (len(msgs) == 1 and "house model" in msgs[0]) if warns else msgs == []


def test_both_tou_bank_paths_and_only_they_ask_for_the_warning() -> None:
    src = inspect.getsource(scheduler)
    assert 'warn_label="Time-of-use bank"' in src
    assert 'warn_label="Time-of-use bank (away/vacation)"' in src
    for mod in ("ode_ceiling_guard", "nat_vent_plan"):
        text = inspect.getsource(__import__(f"custom_components.climate_advisor.{mod}", fromlist=["x"]))
        assert "warn_label" not in text, mod
