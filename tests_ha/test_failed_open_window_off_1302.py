"""Real-HA tests for Issue #1302: a failed or unconfirmed open-window Off is re-sent within ~30 s.

Occupant story: a window is open past its delay, so Climate Advisor turns the AC off. The thermostat refuses that
command (a cloud hiccup) or does not answer in time. Climate Advisor used to record the pause anyway and do nothing
more, so the AC kept cooling into the open window until the next 30-minute cycle re-forced Off. Now the pause is kept
(the window IS open, and closing it must restore the mode) and the Off is re-sent while the thermostat still reads an
active mode: rule 3 holds within ~30 s (G1), the re-sends stop after three (G2), and the failure shows as a WARNING
plus one Activity row (G5).

Whole-house fan site: ``_suppress_hvac_for_whf`` keeps ownership on a failed Off on purpose -- the #1288 rule-2 state
backstop re-sends it as soon as the thermostat runs beside the fan (pinned below, no code change).

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

# The #1132 house: warm morning, AC on; the forecast-peak guard rules nat-vent out, so the window means "pause".
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
    "ca_config": {"sensor_debounce_seconds": 30, "override_confirm_seconds": 60},
}


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


def _fail_off(house, monkeypatch, *, for_s: float | None) -> list[int]:
    """Make the thermostat refuse ``set_hvac_mode off`` for ``for_s`` seconds after the first refusal (a cloud blip;
    None = for ever). Returns a refusal counter."""
    sim = _sim(house)
    real = sim.async_set_hvac_mode
    refused = [0]
    first: list = []

    async def _set(hvac_mode):
        if str(hvac_mode) == "off":
            now = dt_util.utcnow()
            if not first:
                first.append(now)
            if for_s is None or (now - first[0]).total_seconds() < for_s:
                refused[0] += 1
                raise HomeAssistantError("thermostat cloud unreachable")
        await real(hvac_mode)

    monkeypatch.setattr(sim, "async_set_hvac_mode", _set)
    return refused


def _unconfirmed_off(house, monkeypatch, *, device_obeys: bool, for_s: float = 0.0) -> list[int]:
    """Offs get no answer in time (TIMEOUT -> UNCONFIRMED): the first one, and every one within ``for_s`` seconds of
    it (an unresponsive cloud). The device obeyed them or did not."""
    engine = house.coordinator.automation_engine
    real = engine._call_climate
    timed_out = [0]
    first: list = []

    async def _call(service, data, label):
        if service == "set_hvac_mode" and data.get("hvac_mode") == "off":
            now = dt_util.utcnow()
            if not first:
                first.append(now)
            if timed_out[0] == 0 or (now - first[0]).total_seconds() < for_s:
                timed_out[0] += 1
                if device_obeys:
                    await real(service, data, label)
                return ClimateCallResult(ClimateCallOutcome.TIMEOUT)
        return await real(service, data, label)

    monkeypatch.setattr(engine, "_call_climate", _call)
    return timed_out


def _tee_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    events: list = []

    def _tee(event_type: str, data: dict) -> None:
        events.append((event_type, dict(data)))
        if forward:
            forward(event_type, data)

    engine._emit_event_callback = _tee
    return events


def _retry_rows(events) -> list:
    return [d for t, d in events if t == "hvac_mode_not_applied" and d.get("kind") == "open_window_retry"]


def _ca_off_calls(calls) -> list:
    return [
        c
        for c in calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_hvac_mode"
        and c.data.get("hvac_mode") == "off"
    ]


async def _open_until_paused(house, limit_s: int = 240) -> int:
    """Open the window and step 1 s until the pause is recorded. Returns the seconds taken."""
    engine = house.coordinator.automation_engine
    await house.open("window")
    for t in range(limit_s):
        if engine._paused_by_door:
            return t
        await house.advance(seconds=1)
    raise AssertionError("precondition: the open window never paused the HVAC")


async def _seconds_until_off(house, limit_s: int) -> int | None:
    for t in range(limit_s):
        if house.thermostat.mode == "off":
            return t
        await house.advance(seconds=1)
    return None


async def test_failed_pause_off_is_resent_within_30s(make_house, monkeypatch, caplog) -> None:
    """A 13 s cloud blip: the pause's Off and the force-offs of the refreshes that follow it within seconds (the
    failure episode's status refresh, ...) are all refused. Nothing else re-asks before the 30-minute cycle -- the G1
    gap."""
    house = await make_house(**_PAUSE_HOUSE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.thermostat.mode == "cool", "precondition: the AC is on"
    events = _tee_events(house)
    refused = _fail_off(house, monkeypatch, for_s=13)

    await _open_until_paused(house)
    assert engine._pre_pause_mode == "cool", "the pause keeps its restore target"
    took = await _seconds_until_off(house, 30)
    assert refused[0] >= 1, "precondition: the pause's Off was refused"
    assert took is not None, "rule 3: the AC kept cooling into the open window after a failed Off (G1)"
    assert engine._paused_by_door
    rows = _retry_rows(events)
    assert len(rows) == 1, "G5: one Activity row for the re-send"
    label, _ = _render_hvac_mode_not_applied(rows[0], "fahrenheit")
    assert label == "Thermostat still cool with a window open -- re-sending off"
    assert "re-sending Off" in caplog.text

    await house.close_contact("window")
    await house.advance(minutes=3)
    assert not engine._paused_by_door
    assert house.thermostat.mode == "cool", "closing the window restores the AC (nothing stuck, G2/G3)"


async def test_persistent_failure_resends_a_bounded_number_of_times(make_house, monkeypatch, caplog) -> None:
    # No min-runtime fan cycle: a fan session taking the HVAC hands the job to the #1288 backstop (pinned below).
    ca_config = {**_PAUSE_HOUSE["ca_config"], "fan_min_runtime_per_hour": 0}
    house = await make_house(**{**_PAUSE_HOUSE, "ca_config": ca_config})
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    events = _tee_events(house)
    _fail_off(house, monkeypatch, for_s=None)

    await _open_until_paused(house)
    await house.advance(minutes=8)  # 15 + 60 + 300 s chain, then quiet

    def _resends() -> int:
        return sum(1 for r in caplog.records if "re-sending Off" in r.getMessage())

    assert _resends() == 3, f"expected exactly three re-sends, got {_resends()}"
    assert "still not applied after 3 re-sends" in caplog.text
    await house.advance(minutes=6)
    assert _resends() == 3, "the re-send chain must end (G2) -- the 30-minute cycle is the long backstop"
    assert len(_retry_rows(events)) == 1, "one Activity row per chain, not one per attempt"
    assert engine._paused_by_door, "the pause is kept: the window is open"

    monkeypatch.undo()  # the thermostat recovers
    await house.close_contact("window")
    await house.advance(minutes=3)
    assert not engine._paused_by_door
    assert house.thermostat.mode == "cool"
    assert engine._open_window_off_retry_cancel is None


async def test_unconfirmed_pause_off_not_obeyed_is_resent(make_house, monkeypatch) -> None:
    house = await make_house(**_PAUSE_HOUSE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    timed_out = _unconfirmed_off(house, monkeypatch, device_obeys=False, for_s=13)

    await _open_until_paused(house)
    assert timed_out[0] >= 1, "precondition: the pause's Off went unconfirmed"
    took = await _seconds_until_off(house, 30)
    assert took is not None, "rule 3: an unconfirmed Off the thermostat never took left the AC on (G1)"
    assert engine._paused_by_door


async def test_unconfirmed_pause_off_that_landed_keeps_the_pause_and_sends_nothing_more(
    make_house, monkeypatch
) -> None:
    """UNCONFIRMED is "state unknown", usually applied: the pause belief is kept and no extra Off/row is produced."""
    house = await make_house(**_PAUSE_HOUSE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    events = _tee_events(house)
    _unconfirmed_off(house, monkeypatch, device_obeys=True)

    calls_before = len(house.calls)
    await _open_until_paused(house)
    await house.advance(seconds=60)
    assert house.thermostat.mode == "off"
    assert engine._paused_by_door and engine._pre_pause_mode == "cool"
    assert len(_ca_off_calls(house.calls[calls_before:])) == 1, "no re-send when the thermostat already reads off"
    assert not _retry_rows(events)


# --- nat-vent exit with the window still open (_hold_thermostat_off_for_open_window) ---------------------------


async def test_failed_nat_vent_exit_hold_is_resent_within_30s(make_house, monkeypatch) -> None:
    # A long window delay: the door/window handler stays out of it, only the exit hold's Off is in play.
    house = await make_house(**{**_PAUSE_HOUSE, "ca_config": {"sensor_debounce_seconds": 1800}})
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    assert house.thermostat.mode == "cool"
    await house.open("window")
    await house.advance(seconds=5)
    # The call shape _exit_nat_vent() makes once the session has ended with the window open: hold, then the pause.
    refused = _fail_off(house, monkeypatch, for_s=13)
    held = await engine._hold_thermostat_off_for_open_window("outdoor warmer than indoor")
    assert held is True and refused[0] >= 1, "precondition: the hold's Off was refused"
    engine._pre_pause_mode = None
    engine._set_door_window_pause_fields(entity_label="window", hvac_already_off=True)
    assert house.thermostat.mode == "cool"

    took = await _seconds_until_off(house, 30)
    assert took is not None, "rule 3: the AC kept running into the open window after the exit hold failed (G1)"
    assert engine._paused_by_door


# --- whole-house fan: ownership kept on a failed Off, the #1288 backstop re-sends (no code change) ----------------


async def test_failed_whf_suppression_is_resent_by_the_rule2_backstop(make_house, monkeypatch) -> None:
    # A long window delay keeps the door/window pause out of it: only the fan session's Off is in play.
    house = await make_house(**{**_PAUSE_HOUSE, "ca_config": {"sensor_debounce_seconds": 1800}})
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    assert house.thermostat.mode == "cool" and house.thermostat.action == "cooling", "precondition: AC cooling"
    refused = _fail_off(house, monkeypatch, for_s=13)
    await house.open("window")
    await house.advance(seconds=5)
    await house.occupant_presses_fan(True)
    await house.advance(seconds=1)
    assert refused[0] == 1, "precondition: the suppression Off was refused"
    assert engine._whf_owns_hvac(), "ownership is kept on a failed Off (the backstop re-sends it)"
    took = await _seconds_until_off(house, 45)
    assert took is not None and took <= 30, f"rule 2: AC kept cooling beside the fan after a failed Off ({took})"
    assert engine._whf_owns_hvac()
