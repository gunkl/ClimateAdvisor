"""Issue #1227: a door/window opened for less than its delay never changes the HVAC, even if a grace expires in it.

Real Home Assistant. An ``hvac_fan`` home, 600 s door/window delay, a 60 s open while an automation grace expires
~30 s into it. Before the fix the expiry re-paused on the raw open sensor: the thermostat went Off and the blower
started for a window that had been open for 30 s.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

DEB = 600
BASE = {
    "fan": "hvac_fan",
    "profile": "scripted",
    "contacts": ["window", "door"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": DEB, "automation_grace_seconds": 120},
}


def _spy(engine):
    sent = []
    real = engine._notify

    async def _n(message, title, notification_type=None, **kw):
        sent.append((notification_type, message[:90]))
        return await real(message, title, notification_type, **kw)

    engine._notify = _n
    return sent


def _writes(house, since):
    return [
        (c.origin, c.domain, c.service, dict(c.data))
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain in ("climate", "fan", "switch")
    ]


async def _grace_expiring_in_open(make_house):
    house = await make_house(**BASE)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    sent = _spy(engine)
    engine._start_grace_period("automation", trigger="test_1227")
    assert engine._grace_active
    await house.advance(seconds=90)
    return house, engine, sent


async def test_brief_open_while_grace_expires_changes_nothing(make_house):
    house, engine, sent = await _grace_expiring_in_open(make_house)
    since = len(house.calls)
    pre = (house.thermostat.mode, house.thermostat.fan_mode)
    await house.open("window")
    await house.advance(seconds=60)  # the grace expires ~30 s in
    assert not engine._grace_active
    assert not engine._paused_by_door and not engine._natural_vent_active
    await house.close_contact("window")
    await house.advance(minutes=12)
    assert (house.thermostat.mode, house.thermostat.fan_mode) == pre
    assert not engine._paused_by_door and not engine._natural_vent_active
    assert not [w for w in _writes(house, since) if w[2] in ("set_hvac_mode", "set_fan_mode", "turn_on", "turn_off")]
    assert not [n for n in sent if n[0] in ("hvac_paused", "door_window_pause", "grace_expired_repause")], sent


async def test_open_past_the_delay_still_starts_or_pauses(make_house):
    """Control: the same grace, but the window stays open past the delay: the normal response happens."""
    house, engine, _sent = await _grace_expiring_in_open(make_house)
    await house.open("window")
    await house.advance(seconds=DEB + 60)
    assert engine._natural_vent_active or engine._paused_by_door
