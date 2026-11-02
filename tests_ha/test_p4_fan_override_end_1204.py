"""Real-HA tests for Issue #1204 (owner decisions A2 and A3, 2026-11-01).

Occupant stories:

* Whole-house-fan house, a window open, the person presses the fan on, then sets the thermostat to Cool a few
  minutes later. Rule 2 ("the newest action wins"): Climate Advisor stops the fan for the Cool. The fan is off, so
  the fan card must stop saying "off (manual override)" at once (it used to say it for 3 h or more with Manual
  Change Pause = 0, and about 25 min past the fan run's own deadline with a pause). Both pause lengths are covered.
* hvac_fan house, cooling running. The person lowers the setpoint, then a minute later switches the thermostat's
  blower On. Climate Advisor keeps the setpoint (it is protected until the blower's pause ends), so the household
  promise checker (P4) must not report "ended your manual thermostat change 14 min early".

Controls: in the hvac_fan house a Cool press next to the person's blower is not a rule-2 conflict (no stand-down,
the blower override stays); in the whole-house-fan house a fan press after a thermostat change still ends that
change by rule 2 without a P4 report.

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, check_p4_override_respected

pytestmark = pytest.mark.house

_WARM = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",  # indoor stays put: no comfort exit, the only exits are the ones the test causes
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 80.0,
    "high_f": 90.0,
    "low_f": 65.0,
    "initial_hvac_mode": "off",
}
_HOT = {
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 78.0,
    "outdoor_f": 96.0,
    "high_f": 98.0,
    "low_f": 78.0,
    "initial_hvac_mode": "cool",
}
_CONFIRM_S = 60


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


def _p4_hits(house) -> list:
    from homeassistant.util import dt as dt_util

    window = house.coordinator._promise_journal.window(dt_util.now())
    return check_p4_override_respected(window, PromiseConfig())


async def _whf_fan_then_cool(make_house, caplog, grace_s: int):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
            "manual_grace_seconds": grace_s,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_WARM,
    )
    await _steps(house, 360)  # past the start-up window that suppresses override detection
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 120)
    await house.coordinator.async_request_refresh()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(True)
    await _steps(house, 60)
    assert house.fan.is_on and engine._fan_override_active, "precondition: the occupant's fan override runs"
    await _steps(house, 240)
    events = _record_events(house)
    await house.occupant_sets_thermostat(mode="cool")
    await _steps(house, 30)
    return house, engine, events


@pytest.mark.parametrize("grace_s", [0, 900])
async def test_whf_rule2_stop_ends_the_older_fan_override(make_house, caplog, grace_s) -> None:
    house, engine, events = await _whf_fan_then_cool(make_house, caplog, grace_s)
    coordinator = house.coordinator

    assert house.fan.is_on is False, "rule 2: the occupant's newer Cool stops the older fan"
    assert house.thermostat.mode == "cool", "the occupant's Cool is kept"
    assert engine._fan_override_active is False, "the fan is off: its override must end with the stop"
    assert "manual override" not in coordinator._compute_fan_status()
    assert "manual override" not in coordinator._compute_whf_status()
    cleared = [d for t, d in events if t == "fan_override_cleared"]
    assert [d.get("reason") for d in cleared] == ["whf_safety_stop"], cleared

    # the occupant's Cool is confirmed and protected; nothing strands, no stuck-grace self-heal fires
    await _steps(house, _CONFIRM_S + 30)
    assert engine._manual_override_active and engine._manual_override_mode == "cool"
    if grace_s:
        assert engine._grace_active and engine._last_grace_trigger == "override_confirmed"
    await _steps(house, max(grace_s, 600), step=30)
    assert engine._fan_override_active is False
    assert not [t for t, _ in events if t == "stuck_grace_recovered"], "the kept grace must never be orphaned"
    assert "Stuck grace detected" not in caplog.text
    assert _p4_hits(house) == [], "the rule-2 stop is a listed ending (whf_safety_stop), not a P4 miss"
    await house.close()


async def test_hvac_fan_blower_press_keeps_the_setpoint_protected_until_the_blower_pause_ends(make_house, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="hvac_fan",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 120,
            "manual_grace_seconds": 900,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_HOT,
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    setpoint_before = house.thermostat.setpoint_f
    await house.occupant_sets_thermostat(setpoint_f=setpoint_before - 3.0)
    await _steps(house, _CONFIRM_S + 30)
    assert engine._manual_override_active and engine._grace_active, "precondition: the setpoint change is confirmed"
    await house.occupant_sets_fan_mode("on")
    await _steps(house, 60)
    assert engine._fan_override_active and engine._last_grace_trigger == "fan_manual_override"
    assert engine._manual_override_active, "production keeps the setpoint protected past the blower press"
    cancelled = [d for t, d in events if t == "grace_cancelled" and d.get("reason") == "superseded"]
    assert cancelled and cancelled[-1].get("continues") is True, cancelled

    await _steps(house, 825, step=15)  # past the setpoint's own pause end (~870 s), inside the blower's (900 s)
    from homeassistant.util import dt as dt_util

    assert dt_util.now() > dt_util.parse_datetime(cancelled[-1]["grace_end_time"]), "past the setpoint's own end"
    assert engine._grace_active and engine._manual_override_active, "inside the blower's pause"
    assert house.thermostat.setpoint_f == pytest.approx(setpoint_before - 3.0, abs=0.6)
    assert _p4_hits(house) == [], "the setpoint is protected until the blower's pause ends"
    await _steps(house, 240, step=30)  # past the blower's pause end
    assert engine._manual_override_active is False
    assert _p4_hits(house) == []
    await house.close()


async def test_hvac_fan_cool_press_next_to_the_blower_is_not_a_rule2_conflict(make_house, caplog):
    """Control (symmetry): the thermostat's blower and cooling coexist, so no stand-down and the blower stays."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="hvac_fan",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 120, "manual_grace_seconds": 900},
        **{**_HOT, "initial_hvac_mode": "off"},
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.occupant_sets_fan_mode("on")
    await _steps(house, 60)
    assert engine._fan_override_active, "precondition: blower override"
    await house.occupant_sets_thermostat(mode="cool")
    await _steps(house, 30)
    assert engine._fan_override_active, "the person's blower is not stopped by their own Cool"
    assert not [t for t, _ in events if t in ("nat_vent_manual_override_exit", "fan_override_cleared")]
    await house.close()


async def test_whf_fan_press_after_a_thermostat_change_is_still_a_rule2_ending(make_house, caplog):
    """Control (item 1 must not spill into the whole-house fan): the newer fan press ends the Heat by rule 2."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
            "manual_grace_seconds": 900,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_WARM,
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.occupant_sets_thermostat(mode="heat")  # the day wants cooling, so Heat is the occupant's override
    await _steps(house, _CONFIRM_S + 30)
    assert engine._manual_override_active, "precondition: Heat confirmed"
    await house.open("window")
    await _steps(house, 20)
    await house.occupant_presses_fan(True)
    await _steps(house, 90)
    assert house.fan.is_on and house.thermostat.mode == "off", "rule 2: the newer fan press switches Heat off"
    cancelled = [d for t, d in events if t == "grace_cancelled" and d.get("reason") == "superseded"]
    assert all(d.get("continues") is not True for d in cancelled), cancelled
    assert _p4_hits(house) == []
    await house.close()


@pytest.mark.parametrize("initial", ["off", "cool"])
async def test_whf_rule2_stop_then_the_cool_self_resolves_leaves_no_orphaned_grace(make_house, caplog, initial) -> None:
    """Edge of decision A3's kept grace: the Cool matches what the hot day wants, so it self-resolves.

    The kept fan grace then protects nothing. It must end on purpose (``grace_cancelled``), not be reaped by the
    orphaned-grace watchdog with its "Stuck grace detected" ERROR.
    """
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 600,  # inside the debounce: the Cool is a normal (not pause) change
            "manual_grace_seconds": 900,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **{**_HOT, "initial_hvac_mode": initial},
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.open("window")
    await _steps(house, 10)
    await house.occupant_presses_fan(True)
    await _steps(house, 40)
    assert house.fan.is_on and engine._fan_override_active and engine._grace_active, "precondition: fan override"
    await house.occupant_sets_thermostat(mode="cool")
    await _steps(house, 30)
    assert house.fan.is_on is False and engine._fan_override_active is False
    assert engine._grace_active and engine._override_confirm_pending, "the kept grace covers the pending Cool"
    await _steps(house, _CONFIRM_S + 30)
    await house.coordinator.async_request_refresh()
    await _steps(house, 30)
    assert [t for t, _ in events if t == "override_self_resolved"], "precondition: the Cool self-resolved"
    assert engine._grace_active is False
    assert [d.get("reason") for t, d in events if t == "grace_cancelled"] == ["override_self_resolved"]
    assert "Stuck grace detected" not in caplog.text
    assert not [t for t, _ in events if t == "stuck_grace_recovered"]
    assert house.thermostat.mode == "cool"
    assert _p4_hits(house) == []
    await house.close()


async def _whf_override_rearmed_after_restart(make_house, caplog):
    """Window open, the person presses the whole-house fan on, HA restarts; the start-up reconcile re-arms it."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
            "manual_grace_seconds": 900,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_WARM,
    )
    await _steps(house, 360)
    await house.open("window")
    await _steps(house, 120)
    await house.coordinator.async_request_refresh()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(True)
    await _steps(house, 60)
    await house.restart(settle=False)
    events = _record_events(house)
    await _steps(house, 310)  # the 300 s start-up window ends with the reconcile
    engine = house.coordinator.automation_engine
    assert house.fan.is_on and engine._fan_override_active and engine._grace_active, "precondition: re-armed"
    assert engine._last_grace_trigger == "fan_manual_override"
    return house, engine, events


async def _occupant_heat_or_cool_then_check(house, engine, events, caplog, mode: str) -> None:
    await house.occupant_sets_thermostat(mode=mode)
    for _ in range(30):
        await house.advance(seconds=1)
    assert house.fan.is_on is False, "rule 2: the occupant's newer heat/cool stops the older fan"
    assert house.thermostat.mode == mode, "the occupant's change is kept"
    assert engine._fan_override_active is False, "the fan is off: its override must end with the stop"
    assert "manual override" not in house.coordinator._compute_fan_status()
    assert "manual override" not in house.coordinator._compute_whf_status()
    assert [d.get("reason") for t, d in events if t == "fan_override_cleared"] == ["whf_safety_stop"]


@pytest.mark.parametrize("mode", ["heat", "cool"])
async def test_whf_rule2_stop_after_a_restart_rearm_ends_the_fan_override(make_house, caplog, mode) -> None:
    """Restart sequence end to end: the re-armed fan override, then the person's heat/cool (detected normally).

    Control for the mirror below: here the occupant's change is pending, so the override-detected stand-down ends
    the fan override and the grace stays for the pending change.
    """
    house, engine, events = await _whf_override_rearmed_after_restart(make_house, caplog)
    await _occupant_heat_or_cool_then_check(house, engine, events, caplog, mode)
    assert engine._grace_active, "the pending heat/cool keeps the grace"
    await _steps(house, 1200, step=30)
    assert engine._fan_override_active is False and house.fan.is_on is False
    assert "Stuck grace detected" not in caplog.text
    assert not [t for t, _ in events if t == "stuck_grace_recovered"]
    await house.close()


@pytest.mark.parametrize("mode", ["heat", "cool"])
async def test_whf_rule2_safety_stop_with_override_detection_suppressed_ends_the_fan_override(
    make_house, caplog, mode
) -> None:
    """Mirror of decision A3 on the WHF safety enforcer's rule-2 stop (``_after_whf_safety_stop``).

    Occupant story: the person's whole-house fan override is running and they set Heat or Cool while Climate
    Advisor's start-up coalescing suppresses override detection, so their change is neither pending nor confirmed.
    Rule 2 (the enforcer) stops the fan. The fan card used to keep saying "off (manual override)" until the fan's
    grace ended (hours with an RF timer). Harness note: in this harness the reconcile re-arm and the end of start-up
    coalescing coincide, so the suppressed window is held open for the occupant's change by setting the
    coordinator's own ``_startup_coalesce_active`` flag; everything after that (the enforcer's decision and stop,
    the coordinator cycle, the orphaned-grace watchdog) is the real integration.
    """
    house, engine, events = await _whf_override_rearmed_after_restart(make_house, caplog)
    house.coordinator._startup_coalesce_active = True
    await _occupant_heat_or_cool_then_check(house, engine, events, caplog, mode)
    assert not engine._override_confirm_pending and not engine._manual_override_active, "precondition: unprotected"
    assert engine._grace_active is False, "a grace protecting nothing ends on purpose"
    assert [d.get("reason") for t, d in events if t == "grace_cancelled"] == ["whf_safety_stop"]
    house.coordinator._startup_coalesce_active = False
    await _steps(house, 1200, step=30)
    assert engine._fan_override_active is False and house.fan.is_on is False
    assert "Stuck grace detected" not in caplog.text
    assert not [t for t, _ in events if t == "stuck_grace_recovered"]
    await house.close()
