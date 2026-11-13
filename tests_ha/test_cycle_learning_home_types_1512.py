"""Real-HA learning across home types (Issue #1512 reopened, owner 2026-11-12).

Occupant story: whatever the size of the furnace or air conditioner, whatever the thermostat's sensor reports, and
whether or not the blower keeps running after a run, the heating and cooling rate Climate Advisor learns is either
right (within the expectations table of docs/05-LEARNING-ENGINE-DESIGN.md, "Real-HA check") with an honest grade, or
it says plainly that it cannot learn it (a whole/half-degree sensor) and which rate it uses instead.

The REAL coordinator, recorder, store and cycle model run against the dev thermostat sim with an HVAC profile
(``make_sim(profile=SimProfile(...))``); automation is off so the thermostat keeps the setpoints the test gives it.
The sim's house leaks fast (k_passive -0.15 /h) and steps its physics in 30 s ticks that apply a tick's heat before
the edge is reported (the edges read ~30 s late), as a cloud-polled thermostat can; the fit models that offset
(a negative lag). Matrix and tolerances: docs/05 "Real-HA check (tests_ha/test_cycle_learning_home_types_1512.py)".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests_ha.house.sim_profile import COMPANION_ENTITY, SimProfile

pytestmark = pytest.mark.house

HEAT = {
    "contacts": [],
    "startup": "settled",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 70.0,
    # a flat forecast: Climate Advisor's outdoor reading (weather / forecast) is then the sim's own outdoor air
    "outdoor_f": 50.0,
    "high_f": 50.0,
    "low_f": 50.0,
    "initial_hvac_mode": "heat",
}
MILD = {**HEAT, "outdoor_f": 64.0, "high_f": 64.0, "low_f": 64.0}  # an undersized furnace keeps up only in mild air
COOL = {
    "contacts": [],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
    "initial_indoor_f": 75.0,
    "outdoor_f": 95.0,
    "high_f": 95.0,
    "low_f": 95.0,
    "initial_hvac_mode": "cool",
}
# a 1 F maintenance swing (start 1 F short of the setpoint, stop at it), as the expectations table assumes
SWING_1F = {
    "min_run_seconds": 120,
    "min_off_seconds": 120,
    "deadband_heat_f": 1.0,
    "off_offset_heat_f": 0.0,
    "deadband_cool_f": 1.0,
    "off_offset_cool_f": 0.0,
}
RATE = {"undersized": 2.0, "extreme_weather": 5.0, "normal": 12.0, "oversized": 18.0}


async def _house(make_sim, env, profile: SimProfile, *, learned_k: float | None = None):
    house = await make_sim(profile=profile, sim_config=SWING_1F, ca_config={"primary_engine": "v3"}, **env)
    await house.set_automation(False)  # the thermostat keeps the setpoints the test gives it
    if learned_k is not None:  # a home whose closed-house leak rate is already learned (passive windows)
        state = house.coordinator.learning._state
        if not isinstance(state.thermal_model_cache, dict):
            state.thermal_model_cache = {}
        cache = state.thermal_model_cache
        cache["k_passive"] = learned_k
        cache["observation_count_passive"] = 60
    return house


def _fit(house, mode: str) -> dict:
    return house.coordinator.learning._state.thermal_model_cache.get(f"cycle_fit_{mode}") or {}


def _err(fit: dict, gross: float) -> float:
    return abs(abs(fit["q"]) - gross) / gross


def _no_phantom(house, mode: str) -> list[dict]:
    """Every stored cycle is a real run: none is a blower period on its own (2 min, rejected too short)."""
    cycles = house.coordinator._cycle_store.cycles(mode)
    assert cycles
    for c in cycles:
        if c["t_off"] is not None and c["cut"] is None:
            assert c["t_off"] - c["t_on"] >= 180, ("a blower period was recorded as a run", c["t_off"] - c["t_on"])
    rej = house.coordinator._rejection_log or {}
    flat = [r for rows in rej.values() if isinstance(rows, list) for r in rows if isinstance(r, dict)]
    assert not [r for r in flat if r.get("reason_code") == "cycle_too_short"], flat
    return cycles


def _notice(house, mode: str) -> dict | None:
    rows = [
        r for r in house.coordinator.decision_rates_summary() if r.get("source") == "cycle" and r.get("mode") == mode
    ]
    return rows[0].get("coarse_sensor") if rows else None


# ---------------------------------------------------------------------------------------------------------------
# fine sensor (tenths, the thermostat's companion sensor): maintenance cycles learn the rate
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "env", "size", "lag", "overrun", "learned_k", "tol"),
    [
        # tolerances: docs/05 "Real-HA check" (lag 0: within 5 %; a 2-min sensor lag on top of the sim's 30 s edge
        # offset: within 10 %, 12 % on the oversized system's 4-min runs)
        pytest.param("heat", HEAT, "normal", 0.0, 0.0, None, 0.05, id="heat-normal12-tenth-lag0-overrun0"),
        pytest.param("heat", HEAT, "normal", 2.0, 2.0, None, 0.10, id="heat-normal12-tenth-lag2-overrun2"),
        pytest.param("cool", COOL, "normal", 0.0, 2.0, None, 0.05, id="cool-normal12-tenth-lag0-overrun2"),
        pytest.param("heat", HEAT, "oversized", 2.0, 0.0, None, 0.12, id="heat-oversized18-tenth-lag2"),
        pytest.param("heat", MILD, "undersized", 0.0, 0.0, None, 0.10, id="heat-undersized2-tenth-mild"),
        pytest.param("heat", HEAT, "normal", 2.0, 2.0, -0.15, 0.10, id="heat-normal12-tenth-lag2-overrun2-learned-k"),
    ],
)
async def test_fine_sensor_maintenance_learns_the_rate(make_sim, mode, env, size, lag, overrun, learned_k, tol) -> None:
    profile = SimProfile(
        heat_rate=size if mode == "heat" else None,
        cool_rate=size if mode == "cool" else None,
        companion_sensor=True,
        sensor_lag_min=lag,
        **({f"blower_overrun_{mode}_min": overrun} if overrun else {}),
    )
    house = await _house(make_sim, env, profile, learned_k=learned_k)
    await house.advance(hours=8)
    cycles = _no_phantom(house, mode)
    fit = _fit(house, mode)
    gross = RATE[size]
    assert fit["entity"] == COMPANION_ENTITY and fit["sensor_class"] == "fine"
    assert fit["ne"] >= 3, fit
    assert _err(fit, gross) <= tol, (fit["q"], gross)
    assert fit["grade"] in ("medium", "high"), fit
    assert _notice(house, mode) is None
    if overrun:
        # the blower period is inside the cycle it follows: readings recorded through it and after it
        assert all(any(s[0] > c["t_off"] + overrun * 60 for s in c["samples"]) for c in cycles if c["cut"] is None)


# ---------------------------------------------------------------------------------------------------------------
# whole / half-degree climate sensor (no companion): maintenance cycles never learn, and the occupant is told
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("precision", "size", "cls"),
    [
        pytest.param("whole", "normal", "whole", id="whole-normal12"),
        pytest.param("half", "extreme_weather", "half", id="half-extreme5"),
    ],
)
async def test_coarse_sensor_maintenance_never_learns_and_says_so(make_sim, precision, size, cls) -> None:
    house = await _house(make_sim, HEAT, SimProfile(heat_rate=size, precision=precision))
    await house.advance(hours=8)
    fit = _fit(house, "heat")
    assert fit["sensor_class"] == cls and fit["ne"] == 0 and fit["grade"] == "none"
    assert fit["q"] == pytest.approx(8.0)  # the typical-house prior is kept unchanged
    notice = _notice(house, "heat")
    assert notice and notice["sensor_class"] == cls and notice["maintenance_cycles_learn"] is False
    assert notice["rate_in_use"] == "default" and "finer sensor" in notice["text"]


@pytest.mark.parametrize(
    ("learned_k", "grade", "tol"),
    [
        # k only the typical value (-0.03) in a house leaking at -0.15: q comes out low (docs/05: "k pinned at the
        # typical -0.03 leaves q 18-58 % low"), and the grade says so
        pytest.param(None, ("low",), 0.35, id="whole-recovery-k-typical"),
        pytest.param(-0.15, ("low", "medium"), 0.10, id="whole-recovery-k-learned"),
    ],
)
async def test_whole_degree_setback_recovery_learns(make_sim, learned_k, grade, tol) -> None:
    """Whole degrees, normal 12 F/h furnace: three 5 F setback recoveries (setpoint 65 -> 70) teach the rate, which
    normal cycles on this sensor never can; never better than medium on a whole-degree sensor."""
    house = await _house(make_sim, HEAT, SimProfile(heat_rate="normal", precision="whole"), learned_k=learned_k)
    for _ in range(3):
        await house.occupant_sets_thermostat(setpoint_f=65.0)
        await house.advance(hours=2)
        await house.occupant_sets_thermostat(setpoint_f=70.0)
        await house.advance(hours=1)
    fit = _fit(house, "heat")
    assert fit["sensor_class"] == "whole" and fit["ne"] >= 2, fit
    assert fit["grade"] in grade, fit
    if learned_k is None:
        assert "k_typical_prior" in fit["grade_caps"], fit
        assert fit["q"] < 12.0  # the typical leak rate under-reads a leaky house: the bias is one-sided
    assert _err(fit, 12.0) <= tol, fit["q"]
