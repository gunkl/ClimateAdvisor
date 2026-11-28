"""Real HA: the scenario driver does not stop the clock at the loop-lag probe's 1 s timer (Issue #1743).

Occupant view: nothing in the home changes. The probe (#1730) is a measurement-only timer that re-arms itself every
second; no decision reads it. ``RealHASession._advance`` stops the simulated clock at every live loop timer (1 s floor,
30 s cap) so Climate Advisor's own timers fire on time. Counting the probe as one of them made the driver step once per
simulated second instead of up to every 30 s: the real-HA goldens ran up to 9x slower (4.9 s -> 42.9 s for one), the CI
real-HA slices nearly reached their 8-minute cap and the nightly ``ha-lifecycle`` job passed its 60 minutes.

This test drives 30 quiet simulated minutes and counts the clock steps. With the probe ignored it is a few dozen (the
30 s cap plus Climate Advisor's own timers); with the probe counted it is ~1800. The probe must still tick: every
step fires all due timers, so it keeps sampling.
"""

from __future__ import annotations

import json
from datetime import timedelta

from custom_components.climate_advisor import self_measure
from tests_ha.realha_session import RealHASession
from tools.sim_harness import divergence
from tools.sim_harness.session import make_session

SCENARIO = "r6b_natvent_window_close_follows_primary__v3"
_SPAN = timedelta(minutes=30)
# 30 min / 30 s cap = 60 steps; Climate Advisor's own timers add stops. 1 s steps (the probe) would be ~1800.
_MAX_STEPS = 300


async def test_driver_does_not_step_at_the_lag_probe(hass, freezer, tmp_path) -> None:
    scenario = json.loads((divergence.GOLDEN_DIR / f"{SCENARIO}.json").read_text(encoding="utf-8"))
    session = make_session(scenario, engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    try:
        await session.start()
        perf = self_measure.for_hass(hass)
        assert perf.running, "precondition: the loop-lag probe runs on real HA"
        samples = len(perf.lag)

        steps = 0
        real_tick = session.freezer.tick

        def _counting_tick(delta):
            nonlocal steps
            steps += 1
            return real_tick(delta)

        session.freezer.tick = _counting_tick
        try:
            await session._advance(_SPAN)
        finally:
            session.freezer.tick = real_tick

        assert steps <= _MAX_STEPS, f"#1743: the driver stepped {steps} times in 30 min (the probe's 1 s cadence)"
        assert len(perf.lag) > samples, "the probe stopped sampling"
    finally:
        await session.close()
