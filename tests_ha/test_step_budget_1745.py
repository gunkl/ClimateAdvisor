"""Real HA: the scenario driver fails a scenario that steps the frozen clock far too often (Issue #1745).

Occupant view: nothing in the home changes. Issue #1743: the real-HA tier's driver had been stopping its simulated
clock once per simulated second (at the #1730 loop-lag probe's timer) instead of up to every 30 s. Goldens ran up to 9x
slower, CI slices reached their 8-minute cap and some were cancelled - and for three merges nothing said so, because
every test still passed. ``RealHASession`` now counts its clock steps and simulated time, and ``close()`` (reached by
``finish()``, ``async_drive_scenario`` and every test that closes a session) fails a scenario over
``max(STEP_BUDGET_FLOOR, STEP_BUDGET_PER_MINUTE * simulated minutes)``. The message names the timer callbacks the driver
stopped at most often, so the next regression says which timer it is.

The measured distribution that sets the numbers is in ``docs/sim-harness-spec.md`` (realha_session row).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from custom_components.climate_advisor import self_measure
from tests_ha import realha_session
from tests_ha.realha_session import ClockStepBudgetExceeded, RealHASession
from tools.sim_harness import divergence
from tools.sim_harness.session import make_session

SCENARIO = "r6b_natvent_window_close_follows_primary__v3"
_SPAN = timedelta(minutes=30)


def _offline(tmp_path) -> RealHASession:
    """A session that never started: enough to exercise the budget arithmetic."""
    return RealHASession({"events": []}, hass=None, freezer=None, config_dir=tmp_path)  # type: ignore[arg-type]


def _scenario() -> dict:
    return json.loads((divergence.GOLDEN_DIR / f"{SCENARIO}.json").read_text(encoding="utf-8"))


# -- the arithmetic --
async def test_budget_is_the_larger_of_the_floor_and_the_per_minute_rate(tmp_path) -> None:
    s = _offline(tmp_path)
    s.simulated_seconds = 60.0  # one minute: the floor decides
    assert s.step_budget() == realha_session.STEP_BUDGET_FLOOR
    s.simulated_seconds = 600 * 60.0  # ten hours: the rate decides
    assert s.step_budget() == pytest.approx(realha_session.STEP_BUDGET_PER_MINUTE * 600)


async def test_a_very_short_scenario_is_not_failed_for_a_dense_burst(tmp_path) -> None:
    s = _offline(tmp_path)
    s.clock_steps, s.simulated_seconds = 34, 180.0  # measured: a 3-minute golden takes 34 steps (11 per minute)
    s.check_step_budget()


async def test_over_budget_fails_and_names_the_timers_stopped_at_most(tmp_path) -> None:
    s = _offline(tmp_path)
    s.step_stops.update(
        {"SelfMeasure._tick (custom_components.climate_advisor.self_measure)": 1700, "Other._cb (m)": 5}
    )
    s.clock_steps, s.simulated_seconds = 1705, 30 * 60.0
    with pytest.raises(ClockStepBudgetExceeded) as exc:
        s.check_step_budget()
    text = str(exc.value)
    assert "1705 clock steps over 30.0 simulated min" in text
    assert text.index("SelfMeasure._tick") < text.index("Other._cb")  # most frequent first
    assert "x1700" in text and "budget" in text


async def test_just_under_budget_passes(tmp_path) -> None:
    s = _offline(tmp_path)
    s.simulated_seconds = 600 * 60.0
    s.clock_steps = int(s.step_budget())
    s.check_step_budget()


# -- the real driver --
async def test_a_quiet_half_hour_is_within_budget_and_the_counters_add_up(hass, freezer, tmp_path) -> None:
    session = make_session(_scenario(), engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    await session.start()
    before_steps, before_sim = session.clock_steps, session.simulated_seconds
    await session._advance(_SPAN)
    assert session.simulated_seconds - before_sim == pytest.approx(_SPAN.total_seconds(), abs=60)
    assert session.clock_steps > before_steps
    assert sum(session.step_stops.values()) == session.clock_steps
    assert session.clock_steps <= session.step_budget()
    await session.close()  # raises if over budget


async def test_counting_the_loop_lag_probe_as_a_stop_fails_the_budget_and_names_it(
    hass, freezer, tmp_path, monkeypatch
) -> None:
    """Revert-proof for #1743: with the probe no longer skipped, the driver steps once per second and close() fails."""
    session = make_session(_scenario(), engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    await session.start()
    assert self_measure.for_hass(hass).running, "precondition: the probe runs on real HA"
    # _next_due_handle recognises the probe by `isinstance(callback.__self__, SelfMeasure)`: make that never match
    monkeypatch.setattr(self_measure, "SelfMeasure", type("NotTheProbe", (), {}))
    await session._advance(_SPAN)
    monkeypatch.undo()
    assert session.clock_steps > 1000  # ~1800: one per simulated second
    with pytest.raises(ClockStepBudgetExceeded) as exc:
        await session.close()
    assert "SelfMeasure._tick" in str(exc.value)
    assert session._closed  # the budget check comes after the teardown, which always runs


async def test_an_error_already_propagating_is_not_replaced_by_the_budget_report(hass, freezer, tmp_path) -> None:
    session = make_session(_scenario(), engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    await session.start()
    session.clock_steps, session.simulated_seconds = 10**6, 60.0  # wildly over budget
    with pytest.raises(RuntimeError, match="the real failure"):
        try:
            raise RuntimeError("the real failure")
        finally:
            await session.close()
