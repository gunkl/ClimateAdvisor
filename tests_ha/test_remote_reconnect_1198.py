"""Real-HA tests for Issue #1198: a whole-house-fan remote event re-announced after a device reconnect.

Occupant story: it is a cold night, the whole-house fan is off and the heat is on. The RF transmitter's ESPHome
device reconnects; its event entity goes unavailable for a fraction of a second and comes back holding its last
press, hours old (the same event time). Climate Advisor used to read that as a fresh press: a 3 hour
"the occupant took manual control of the fan" hold-off and the heat switched off while the fan never ran.

Each layer of the fix has its own test, written so that reverting only that layer fails only its test:

* layer 1 (remember every timestamp, seeded from the entity at start-up): the restored identical timestamp is a
  duplicate. The age bound is widened in that test so layer 2 cannot be what rejects it.
* layer 2 (reject an old event time): a timestamp never seen before, but hours old, is not a press.
  Nothing is seeded (the entity was unavailable at start-up), so layer 1 cannot be what rejects it.
* layer 3 (the fan must report on): a press stamped "now" with the fan still off is not an override.

Positive controls: a genuine new press with the fan turning on still becomes an override, including when the fan's
state entity lags the burst flush by several seconds.

Prove a test is not vacuous with ``scripts/revert_proof.py origin/main <test>`` (or revert one layer by hand).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

REMOTE = "event.house_quietcool_remote"
FAN_POWER = "binary_sensor.house_whf_power"
_COLD = {
    "start": datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
    "initial_indoor_f": 62.0,
    "outdoor_f": 45.0,
    "high_f": 52.0,
    "low_f": 40.0,
    "initial_hvac_mode": "heat",
}
# The house clock starts at 08:00 UTC; this press is seven hours old (a real press from the evening before).
_STALE_TS = "2026-11-01T01:00:00.340+00:00"


def _ts(seconds_ago: float = 0.0) -> str:
    from datetime import timedelta

    return (dt_util.utcnow() - timedelta(seconds=seconds_ago)).isoformat(timespec="milliseconds")


def _set_remote(house, ts: str, event_type: str = "low") -> None:
    house.hass.states.async_set(REMOTE, ts, {"event_type": event_type, "event_types": ["low", "high", "off"]})


async def _house(make_house, *, remote_at_start: str | None, **extra):
    """A cold-night whole-house-fan house with a remote event entity, settled past start-up coalescing."""

    async def _before(h):
        if remote_at_start is not None:
            _set_remote(h, remote_at_start)

    return await make_house(
        fan="whf_dual",
        startup="settled",
        before_ca_setup=_before,
        ca_config={"fan_remote_entity": REMOTE, "fan_min_runtime_per_hour": 0},
        **_COLD,
        **extra,
    )


async def _reconnect(house, ts: str, event_type: str = "low") -> None:
    """The ESPHome device drops and returns: unavailable, then the same state back."""
    house.hass.states.async_set(REMOTE, "unavailable")
    await house._drain()
    _set_remote(house, ts, event_type)
    await house._drain()


def _no_false_override(house) -> None:
    engine = house.coordinator.automation_engine
    assert engine._fan_override_active is False, "a re-announced press was recorded as a fan override"
    assert engine._grace_active is False, "a re-announced press started a hold-off period"
    ca_hvac_off = [
        c
        for c in house.calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_hvac_mode"
        and c.data.get("hvac_mode") == "off"
    ]
    assert ca_hvac_off == [], f"heat was switched off over a press that never happened: {ca_hvac_off}"
    assert house.thermostat.mode == "heat"


async def test_whole_incident_reconnect_with_stale_press_changes_nothing(make_house) -> None:
    """The 2026-11-01 01:01 incident end to end: a 7 hour old press re-announced, fan off throughout."""
    stale = _STALE_TS
    house = await _house(make_house, remote_at_start=stale)
    await house.advance(minutes=1)
    await _reconnect(house, stale)
    await house.advance(minutes=1)
    _no_false_override(house)


async def test_layer1_restored_identical_timestamp_is_a_duplicate(make_house, monkeypatch, caplog) -> None:
    import custom_components.climate_advisor.coordinator as coord_mod

    # Take layer 2 out of play: only the remembered timestamp may reject this restore.
    monkeypatch.setattr(coord_mod, "REMOTE_EVENT_MAX_AGE_SECONDS", 1e12, raising=False)
    stale = _STALE_TS
    house = await _house(make_house, remote_at_start=stale)
    caplog.clear()
    with caplog.at_level("INFO"):
        await _reconnect(house, stale)
        await house.advance(minutes=1)
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Fan remote burst started" in m for m in messages), "the re-announcement opened a press burst"
    assert any("Fan remote event ignored: re-announced after reconnect" in m for m in messages), messages
    _no_false_override(house)


async def test_layer2_old_timestamp_never_seen_before_is_rejected(make_house, caplog) -> None:
    # The entity is unavailable while Climate Advisor starts, so nothing can be seeded.
    async def _unavailable_at_start(h):
        h.hass.states.async_set(REMOTE, "unavailable")

    house = await make_house(
        fan="whf_dual",
        startup="settled",
        before_ca_setup=_unavailable_at_start,
        ca_config={"fan_remote_entity": REMOTE, "fan_min_runtime_per_hour": 0},
        **_COLD,
    )
    assert house.coordinator._last_fan_remote_event_ts is None, "precondition: nothing seeded"
    caplog.clear()
    with caplog.at_level("INFO"):
        await _reconnect(house, _STALE_TS)
        await house.advance(minutes=1)
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Fan remote burst started" in m for m in messages), "an hours-old press opened a burst"
    assert any("Fan remote event ignored: stale event time" in m for m in messages), messages
    _no_false_override(house)


async def test_layer3_fresh_press_with_the_fan_still_off_is_not_an_override(make_house, caplog) -> None:
    house = await _house(make_house, remote_at_start=None)
    caplog.clear()
    with caplog.at_level("INFO"):
        _set_remote(house, _ts(), "timer_2h")  # a press stamped now; the fan never turns on
        await house._drain()
        # Flush the burst first: advance() steps 30 s at a time, and a timer armed inside a step is relative to the
        # post-tick clock, so one 60 s advance would arm the 45 s give-up at +75 s and never reach it.
        await house.advance(seconds=2)
        await house.advance(seconds=60)  # past the 45 s give-up deadline
    assert any("Fan remote press not confirmed by the fan" in r.getMessage() for r in caplog.records)
    _no_false_override(house)


async def test_positive_control_real_press_and_fan_on_is_an_override(make_house) -> None:
    house = await _house(make_house, remote_at_start=None)
    engine = house.coordinator.automation_engine
    _set_remote(house, _ts(), "timer_2h")
    await house._drain()
    await house.occupant_presses_fan(True)  # the fan comes on with the press
    await house.advance(seconds=30)
    assert engine._fan_override_active is True, "a genuine press with the fan running must still be an override"
    assert engine._grace_active is True


async def test_positive_control_fan_reporting_on_after_the_burst_flush_is_still_an_override(make_house) -> None:
    """The fan's power detector lags the RF command by 8-15 s (measured), far past the 1.5 s burst flush."""
    house = await _house(make_house, remote_at_start=None)
    engine = house.coordinator.automation_engine
    _set_remote(house, _ts(), "timer_2h")
    await house._drain()
    await house.advance(seconds=13)  # flush has happened; the detector has not reported yet
    assert engine._fan_override_active is False, "recorded before the fan reported on"
    await house.occupant_presses_fan(True)
    await house._drain()
    assert engine._fan_override_active is True, "recorded the moment the fan reported on"
    assert engine._grace_active is True
    assert engine._fan_remote_timer_hours == 2.0, "the remote's timer token is kept"
