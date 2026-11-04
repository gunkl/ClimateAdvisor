"""Real-HA tests for Issues #1307 and #1308: a blower-ventilation Off and a restore that did not land are re-sent.

#1307 occupant story (hvac_fan install): the window is opened, the thermostat's blower starts ventilating and Climate
Advisor turns the AC off. The thermostat refuses that Off (a cloud blip). Nothing re-asked it, so the AC stayed armed
beside the open window until the 30-minute cycle. Now the Off is re-sent from device state while the blower session
is still ventilating with a window open (rule 3 within ~30 s, G1), on #1302's bounded 15/60/300 s schedule.

#1308 occupant story: the occupant closes the last open window and Climate Advisor's restore of the AC fails or goes
unconfirmed. Climate Advisor used to clear the pause and start its 5-minute automation grace anyway, so the AC did not
come back until the grace ended and a cycle ran. Owner decision 2026-11-03 ("retry, no grace"): the restore stays
pending and is re-sent on the same bounded schedule, and the automation grace starts only once it lands. An occupant
change in the meantime wins (G3); a persistent failure ends after three re-sends with nothing stuck (G2); a WARNING
and one Activity row make it visible (G5). The whole-house fan release restore is covered the same way.

The thermostat failure is made at the device (the simulated thermostat raises), the unconfirmed case at
``_call_climate`` (a 10 s blocking timeout cannot be produced under the frozen test clock).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.ai_skills_context import _render_hvac_mode_not_applied
from custom_components.climate_advisor.automation import ClimateCallOutcome, ClimateCallResult

pytestmark = pytest.mark.house

SIM_DOMAIN = "ca_dev_thermostat_sim"

# #1302's pause house: a warm morning, AC on; the forecast-peak guard rules nat-vent out, so the window means "pause".
_PAUSE_HOUSE = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "initial_indoor_f": 82.0,
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "outdoor_f": 73.5,
    "high_f": 95.0,
    "low_f": 52.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"sensor_debounce_seconds": 30, "override_confirm_seconds": 60, "fan_min_runtime_per_hour": 0},
}

# #1142's blower house: a mild evening, the open window starts blower ventilation.
_BLOWER_HOUSE = {
    "fan": "hvac_fan",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 60},
}


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


def _refuse_mode(house, monkeypatch, mode: str, *, for_s: float | None) -> list[int]:
    """The thermostat refuses ``set_hvac_mode <mode>`` for ``for_s`` seconds after the first refusal (a cloud blip;
    None = for ever). Returns a refusal counter."""
    sim = _sim(house)
    real = sim.async_set_hvac_mode
    refused = [0]
    first: list = []

    async def _set(hvac_mode):
        if str(hvac_mode) == mode:
            now = dt_util.utcnow()
            if not first:
                first.append(now)
            if for_s is None or (now - first[0]).total_seconds() < for_s:
                refused[0] += 1
                raise HomeAssistantError("thermostat cloud unreachable")
        await real(hvac_mode)

    monkeypatch.setattr(sim, "async_set_hvac_mode", _set)
    return refused


def _tee_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    events: list = []

    def _tee(event_type: str, data: dict) -> None:
        events.append((dt_util.utcnow(), event_type, dict(data)))
        if forward:
            forward(event_type, data)

    engine._emit_event_callback = _tee
    return events


def _rows(events, kind: str) -> list:
    return [d for _, t, d in events if t == "hvac_mode_not_applied" and d.get("kind") == kind]


def _graces(events, trigger: str) -> list:
    return [at for at, t, d in events if t == "grace_started" and d.get("trigger") == trigger]


def _ca_mode_calls(calls, mode: str, since) -> list:
    return [
        c
        for c in calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_hvac_mode"
        and c.data.get("hvac_mode") == mode
        and c.at >= since
    ]


async def _seconds_until_mode(house, mode: str, limit_s: int) -> int | None:
    for t in range(limit_s):
        if house.thermostat.mode == mode:
            return t
        await house.advance(seconds=1)
    return None


async def _paused_house(make_house, **overrides):
    house = await make_house(**{**_PAUSE_HOUSE, **overrides})
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.open("window")
    for _ in range(240):
        if engine._paused_by_door and house.thermostat.mode == "off":
            break
        await house.advance(seconds=1)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"
    assert engine._pre_pause_mode == "cool"
    await house.advance(minutes=2)
    return house, engine


async def _close_until_restore_attempted(house, refused: list[int], limit_s: int = 120) -> datetime:
    await house.close_contact("window")
    for _ in range(limit_s):
        if refused[0]:
            return dt_util.utcnow()
        await house.advance(seconds=1)
    raise AssertionError("precondition: closing the window never attempted the restore")


# --- #1307: blower ventilation Off -------------------------------------------------------------------------------


async def test_failed_blower_ventilation_off_is_resent_within_30s(make_house, monkeypatch, caplog) -> None:
    house = await make_house(**_BLOWER_HOUSE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    assert house.thermostat.mode == "cool", "precondition: the AC is armed"
    events = _tee_events(house)
    refused = _refuse_mode(house, monkeypatch, "off", for_s=13)

    await house.open("window")
    for _ in range(240):
        if refused[0]:
            break
        await house.advance(seconds=1)
    assert refused[0] >= 1, "precondition: the blower session's Off was refused"
    assert engine._natural_vent_active and not engine._paused_by_door, "precondition: blower ventilation, no pause"
    took = await _seconds_until_mode(house, "off", 30)
    assert took is not None, "rule 3: the AC stayed armed beside the open window after a failed blower Off (G1)"
    assert engine._natural_vent_active
    rows = _rows(events, "open_window_retry")
    assert len(rows) == 1, f"G5: one Activity row for the re-send, got {rows}"
    assert "blower ventilation" in rows[0]["reason"]
    assert "re-sending Off" in caplog.text
    await house.advance(minutes=8)
    assert engine._open_window_off_retry_cancel is None, "the chain ended once the Off landed (G2)"


# --- #1308: the restore after the last window closes --------------------------------------------------------------


async def test_failed_window_close_restore_is_resent_and_grace_waits_for_it(make_house, monkeypatch, caplog) -> None:
    house, engine = await _paused_house(make_house)
    events = _tee_events(house)
    refused = _refuse_mode(house, monkeypatch, "cool", for_s=13)

    t0 = await _close_until_restore_attempted(house, refused)
    assert not engine._paused_by_door, "the window is closed: the pause is cleared (rule 3 no longer needs it)"
    assert not _graces(events, "sensor_closed_resume"), "no automation grace for a restore that did not land"
    assert engine._pending_restore is not None, "the restore is kept pending"
    took = await _seconds_until_mode(house, "cool", 30)
    assert took is not None, "the AC did not come back within ~30 s of a failed restore (comfort)"
    await house.advance(seconds=2)
    graces = _graces(events, "sensor_closed_resume")
    assert len(graces) == 1, f"the automation grace starts once the restore lands: {graces}"
    landed_at = next(c.at for c in _ca_mode_calls(house.calls, "cool", t0) if c.at >= t0)
    assert graces[0] >= t0 and engine._grace_active
    assert landed_at <= graces[0] or house.thermostat.mode == "cool"
    assert engine._pending_restore is None and engine._restore_resend_cancel is None, "nothing pending (G2)"
    rows = _rows(events, "restore_retry")
    assert len(rows) == 1, f"G5: one Activity row: {rows}"
    label, _ = _render_hvac_mode_not_applied(rows[0], "fahrenheit")
    assert label == "Thermostat still off after Climate Advisor restored cool -- re-sending"
    assert "re-sending the restore" in caplog.text


async def test_persistent_restore_failure_is_bounded_and_leaves_nothing_stuck(make_house, monkeypatch, caplog) -> None:
    house, engine = await _paused_house(make_house)
    events = _tee_events(house)
    refused = _refuse_mode(house, monkeypatch, "cool", for_s=None)

    await _close_until_restore_attempted(house, refused)
    await house.advance(minutes=8)  # 15 + 60 + 300 s chain, then quiet

    def _resends() -> int:
        return sum(1 for r in caplog.records if "re-sending the restore" in r.getMessage())

    assert _resends() == 3, f"expected exactly three re-sends, got {_resends()}"
    assert "Restore still not applied after 3 re-sends" in caplog.text
    await house.advance(minutes=6)
    assert _resends() == 3, "the chain must end (G2) -- the 30-minute cycle is the long backstop"
    assert len(_rows(events, "restore_retry")) == 1, "one Activity row per chain"
    assert engine._pending_restore is None and engine._restore_resend_cancel is None, "nothing pending (G2)"
    assert not engine._paused_by_door, "no pause behind a closed window"
    assert not _graces(events, "sensor_closed_resume"), "no automation grace for a restore that never landed"

    monkeypatch.undo()  # the thermostat recovers: one deliberate action fixes it (G4)
    await house.occupant_sets_thermostat(mode="cool")
    await house.advance(seconds=30)
    assert house.thermostat.mode == "cool"


async def test_occupant_change_during_the_chain_stops_it(make_house, monkeypatch, caplog) -> None:
    house, engine = await _paused_house(make_house)
    events = _tee_events(house)
    refused = _refuse_mode(house, monkeypatch, "cool", for_s=None)

    await _close_until_restore_attempted(house, refused)
    assert engine._pending_restore is not None, "precondition: the restore is pending"
    await house.advance(seconds=3)
    await house.occupant_sets_thermostat(mode="heat")
    t_occ = dt_util.utcnow()
    await house.advance(minutes=8)

    assert house.thermostat.mode == "heat", "the occupant's recognised change wins (G3)"
    assert not _ca_mode_calls(house.calls, "cool", t_occ), "Climate Advisor re-sent its restore over the occupant"
    assert engine._pending_restore is None and engine._restore_resend_cancel is None, "nothing pending (G2)"
    assert not _graces(events, "sensor_closed_resume"), "no automation grace over the occupant's change"
    assert "Restore re-send stopped" in caplog.text


async def test_unconfirmed_restore_that_landed_starts_the_grace_and_sends_nothing_more(make_house, monkeypatch) -> None:
    house, engine = await _paused_house(make_house)
    events = _tee_events(house)
    real = engine._call_climate
    timed_out = [0]

    async def _call(service, data, label):
        if service in ("set_hvac_mode", "set_temperature") and data.get("hvac_mode") != "off":
            timed_out[0] += 1
            await real(service, data, label)  # the thermostat obeyed; the answer did not arrive in time
            return ClimateCallResult(ClimateCallOutcome.TIMEOUT)
        return await real(service, data, label)

    monkeypatch.setattr(engine, "_call_climate", _call)
    await house.close_contact("window")
    for _ in range(120):
        if timed_out[0]:
            break
        await house.advance(seconds=1)
    assert timed_out[0], "precondition: the restore went unconfirmed"
    await house.advance(seconds=2)
    assert house.thermostat.mode == "cool"
    assert len(_graces(events, "sensor_closed_resume")) == 1, "a restore that landed starts the grace at once"
    assert engine._pending_restore is None
    marker = len(house.calls)
    await house.advance(seconds=90)
    resent = [c for c in house.calls[marker:] if c.origin.startswith("ca:") and c.service == "set_hvac_mode"]
    assert not resent, f"no re-send of a restore that landed: {resent}"
    assert not _rows(events, "restore_retry")


# --- #1308: the whole-house fan release restore -------------------------------------------------------------------


async def test_failed_whf_release_restore_is_resent(make_house, monkeypatch) -> None:
    house = await make_house(
        fan="whf",
        contacts=["window"],
        startup="settled",
        profile="scripted",
        start=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        initial_indoor_f=72.0,
        outdoor_f=65.0,
        high_f=70.0,
        low_f=55.0,
        initial_hvac_mode="cool",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    )
    engine = house.coordinator.automation_engine
    try:
        await house.advance(minutes=6)
        await house.open("window")
        await house.advance(seconds=90)
        await house.advance(minutes=6)  # past the 5-minute fan toggle floor
        assert engine._natural_vent_active and engine._fan_active, "precondition: Climate Advisor is ventilating"
        assert engine._pre_fan_hvac_mode == "cool", "precondition: the AC is held for the fan"
        events = _tee_events(house)
        refused = _refuse_mode(house, monkeypatch, "cool", for_s=13)

        await _close_until_restore_attempted(house, refused, limit_s=180)
        assert not house.fan.is_on, "precondition: the fan stopped"
        took = await _seconds_until_mode(house, "cool", 30)
        assert took is not None, "the AC did not come back within ~30 s of a failed release restore"
        assert not engine._whf_owns_hvac()
        await house.advance(minutes=8)
        assert engine._pending_restore is None and engine._restore_resend_cancel is None, "nothing pending (G2)"
        assert len(_rows(events, "restore_retry")) <= 1
    finally:
        house.fan.close()
