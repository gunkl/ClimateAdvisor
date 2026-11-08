"""Real-HA tests for Issue #1433: the away setback waits for the 15-minute timer; after coming home every periodic
cycle keeps the home band.

Occupant story (production, 2026-11-07, Zone 1): you leave at 11:18:46; at 11:23:12, 4.5 minutes later, the 30-minute
cycle logged "Away mode -- reapplying setback instead of comfort temps". The windows were open that day, so nothing
was sent, but on a heating day the thermostat is set to the away heat (60-63 F) inside the 15-minute grace Climate
Advisor promises, and a short errand or a phone dropping off the network sets the heat back. Meanwhile the chart draws
the home band for that timer (Issue #1425), so the chart and the thermostat disagree. The reported 12:23 and 14:15
chart rows (Away band 63-79 while home) are the Issue #1425 case, fixed in 0.9.7; they are pinned again here.

Real Home Assistant, the real occupancy toggle and its real away timer, the real 30-minute cycle. Both fan modes.
The two pending Tier A scenarios run here too, through ``RealHASession``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tests_ha.test_away_band_chart_1425 import _BASE, _drawn, _rows, _set_home
from tools.sim_harness.outcomes import check_assertion

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_PENDING = Path(__file__).resolve().parent.parent / "tools" / "simulations" / "pending"
_SCENARIOS = ["issue_1433_away_grace_periodic_cycle_holds_home", "issue_1433_away_grace_tou_bank_holds_home"]
_VACATION = "input_boolean.vacation"
_GUEST = "input_boolean.guest"

# A heating day: the setback is a lower heat setpoint, so a wrong setback write is visible.
_COLD = {**_BASE, "outdoor_f": 40.0, "high_f": 50.0, "low_f": 35.0}


def _heat_writes(house, since=None) -> list[tuple[str, float]]:
    return [
        (f"{c.at:%H:%M:%S}", float(c.data["temperature"]))
        for c in house.calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_temperature"
        and "temperature" in c.data
        and (since is None or c.at >= since)
    ]


def _now(house):
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()


def _rows_since(house, when) -> list[dict]:
    """Chart rows written at or after ``when`` (row timestamps are ISO strings with their own offset)."""
    return [r for r in _rows(house) if datetime.fromisoformat(r["ts"]) >= when]


async def _house(make_house, fan: str, **extra):
    base = dict(_COLD)
    if extra:
        base["ca_config"] = {**base["ca_config"], **extra.get("ca_config", {})}
        base["extra_states"] = {**base["extra_states"], **extra.get("extra_states", {})}
    house = await make_house(fan=fan, **base)
    await house.advance(minutes=10)
    cfg = house.coordinator.config
    comfort, setback = float(cfg["comfort_heat"]), float(cfg["setback_heat"])
    assert house.thermostat.setpoint_f == pytest.approx(comfort, abs=0.6), "precondition: home comfort heat"
    return house, comfort, setback


@pytest.mark.parametrize("name", _SCENARIOS)
async def test_pending_scenario_on_real_ha(name, hass, freezer, tmp_path) -> None:
    scenario = json.loads((_PENDING / f"{name}.json").read_text(encoding="utf-8"))
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    failed = [
        (a["at"], a["expect"], a.get("reason", ""))
        for a in scenario["assertions"]
        if check_assertion(result, a) is False
    ]
    assert not failed, f"{name} on real Home Assistant: {failed}"


@pytest.mark.parametrize("fan", _FANS)
async def test_a_cycle_inside_the_away_timer_sends_no_setback(make_house, fan) -> None:
    house, comfort, setback = await _house(make_house, fan)
    await house.advance(minutes=14)  # the next 30-minute cycle is about 6 minutes after you leave
    left_at = _now(house)
    await _set_home(house, False)
    await house.advance(minutes=12)  # a 30-minute cycle runs inside the 15-minute timer
    inside = [r for r in _rows_since(house, left_at) if r.get("event") is None]
    assert inside, "precondition: a 30-minute cycle ran while the away timer was pending"
    assert not [v for _t, v in _heat_writes(house, left_at) if v == pytest.approx(setback)], (
        f"#1433: the away setback was sent inside the 15-minute timer: {_heat_writes(house, left_at)}"
    )
    assert house.thermostat.setpoint_f == pytest.approx(comfort, abs=0.6)

    back_at = _now(house)
    await _set_home(house, True)  # back after 12 minutes: no setback was ever due
    await house.advance(minutes=65)  # two periodic cycles at home
    assert {v for _t, v in _heat_writes(house, back_at)} <= {comfort}, _heat_writes(house, back_at)
    assert all(_drawn(r)[0] == comfort for r in _rows_since(house, back_at))


@pytest.mark.parametrize("fan", _FANS)
async def test_two_aways_every_cycle_after_each_return_is_home_and_away_is_kept(make_house, fan) -> None:
    house, comfort, setback = await _house(make_house, fan)
    for _ in range(2):
        await _set_home(house, False)
        await house.advance(minutes=16)  # the timer applies the setback
        assert house.thermostat.setpoint_f == pytest.approx(setback, abs=0.6), "the setback after the timer"
        await house.advance(minutes=35)  # leaving mirror: a periodic cycle while away keeps the setback
        assert house.thermostat.setpoint_f == pytest.approx(setback, abs=0.6), "#1433: a cycle undid Away"
        back_at = _now(house)
        await _set_home(house, True)
        await house.advance(minutes=65)  # the periodic cycles after the return
        writes = _heat_writes(house, back_at)
        assert writes and {v for _t, v in writes} <= {comfort}, f"#1433: a write after coming home: {writes}"
        rows = _rows_since(house, back_at)
        assert rows and all(r.get("occupancy") == "home" and _drawn(r)[0] == comfort for r in rows), [
            (r["ts"], r.get("event"), _drawn(r)) for r in rows
        ]


@pytest.mark.parametrize("fan", _FANS)
async def test_a_vacation_return_keeps_the_home_band(make_house, fan) -> None:
    house, comfort, _setback = await _house(
        make_house, fan, ca_config={"vacation_toggle_entity": _VACATION}, extra_states={_VACATION: "off"}
    )
    house.hass.states.async_set(_VACATION, "on")
    await house.advance(seconds=5)
    vac_heat = house.thermostat.setpoint_f
    assert vac_heat < comfort - 1, "Vacation's deep setback is applied at once"
    await house.advance(minutes=37)  # a cycle on vacation; then come home between cycles
    assert house.thermostat.setpoint_f == pytest.approx(vac_heat, abs=0.6), "a cycle on vacation keeps the setback"
    back_at = _now(house)
    house.hass.states.async_set(_VACATION, "off")
    await house.advance(seconds=5)
    await house.advance(minutes=65)
    writes = _heat_writes(house, back_at)
    assert writes and {v for _t, v in writes} <= {comfort}, f"#1433: a setback after the vacation return: {writes}"


@pytest.mark.parametrize("fan", _FANS)
async def test_guest_while_the_away_timer_runs_and_guest_to_home(make_house, fan) -> None:
    house, comfort, setback = await _house(
        make_house, fan, ca_config={"guest_toggle_entity": _GUEST}, extra_states={_GUEST: "off"}
    )
    left_at = _now(house)
    await _set_home(house, False)
    house.hass.states.async_set(_GUEST, "on")  # a guest arrives inside the away timer
    await house.advance(seconds=5)
    await house.advance(minutes=40)  # the timer would have fired; periodic cycles as a guest
    assert not [v for _t, v in _heat_writes(house, left_at) if v == pytest.approx(setback)], _heat_writes(
        house, left_at
    )
    house.hass.states.async_set(_GUEST, "off")
    await _set_home(house, True)
    back_at = _now(house)
    await house.advance(minutes=35)
    assert {v for _t, v in _heat_writes(house, back_at)} <= {comfort}
    assert house.thermostat.setpoint_f == pytest.approx(comfort, abs=0.6)
