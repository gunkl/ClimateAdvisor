"""Real-HA tests for Issue #1212: an abandoned fan start/stop must leave no stale toggle-floor deferral behind.

Occupant story: Climate Advisor defers a fan start or stop that arrives less than five minutes after the last fan
toggle (the 5-minute floor). When that start/stop is then abandoned -- the window closes again, or the occupant
switches the fan off -- the whole-house-fan card still says "(on pending -- 5-min floor, applies at ...)" /
"(off pending ...)" for the rest of the floor, and a later UNRELATED start or stop (an hour later) logs
"5-minute floor expired -- applying deferred activation/exit", as if the old request were still alive.

Both supported fan modes (``fan_mode: both`` is unsupported, #1175)..
The hvac_fan house has no status-card text, so the card test is whole_house_fan only; the log tests run in both.

Routes (no manual state pokes, everything through the real coordinator, thermostat, fan and clock):
* F3 -- activation deferred, then the window closes: the window opens (CA starts the fan), stays open past the toggle
  floor, closes (CA stops), re-opens within 60 s (the start is deferred by the floor), closes again.
* F4 -- stop deferred, then the occupant switches the fan off: the window opens (CA starts the fan), the indoor
  temperature drops under the comfort floor (CA's nat-vent exit stop is deferred by the floor), the person switches
  the fan off before the floor ends.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_GRACE_S = 120
_FLOOR_S = 300  # FAN_MIN_TOGGLE_INTERVAL_S
_MATCHED_LOG = "holding fan off (fan_off grace); closes session from earlier manual fan-on"
_PLAIN_LOG = "Fan turned off by occupant — holding fan off (fan_off grace): fan="
_FAN_OFF_GRACE_LOG = "starting fan_off grace period"


class _OccupantOutcomeViolated(AssertionError):
    """The occupant's off of the running session was ignored (the #1205 symptom)."""


_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",  # indoor stays 72F: no comfort-floor exit, the only exits are the ones the test causes
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 65.0,
    "high_f": 70.0,
    "low_f": 55.0,
    "initial_hvac_mode": "cool",
}


async def _house(make_house, fan: str, caplog, ca_extra: dict | None = None, **overrides):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    ca_config = {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "manual_grace_seconds": _GRACE_S}
    ca_config.update(ca_extra or {})
    house = await make_house(
        fan=fan,
        ca_config=ca_config,
        **{**_HOUSE, **overrides},
    )
    await _steps(house, 360)  # past the 300 s start-up window
    _record_events(house)
    return house


def _record_events(house) -> None:
    """Tee the engine's Activity Record events (fan_cancel label, fan_activated, ...) into ``house.ca_events``."""
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    house.ca_events = []

    def _tee(event_type: str, data: dict) -> None:
        house.ca_events.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee


def _last_cancel(house) -> dict:
    return [d for n, d in house.ca_events if n == "fan_cancel"][-1]


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _fan_on(house, fan: str) -> bool:
    return house.fan.is_on if fan == "whf" else house.thermostat.fan_mode == "on"


def _ca_starts(house, fan: str) -> list:
    """Climate Advisor's own fan-on commands (ground truth from the service-call bus)."""
    if fan == "whf":
        return [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_on"]
    return [
        c
        for c in house.calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_fan_mode"
        and c.data.get("fan_mode") == "on"
    ]


async def _occupant_off(house, fan: str) -> None:
    if fan == "whf":
        await house.occupant_presses_fan(False)
    else:
        await house.occupant_sets_fan_mode("auto")


async def _ordinary_ca_start(house, fan: str) -> None:
    """The window opens (past the debounce) and Climate Advisor starts nat-vent: one new CA fan-on command."""
    starts_before = len(_ca_starts(house, fan))
    await house.open("window")
    await _steps(house, 90)
    assert len(_ca_starts(house, fan)) == starts_before + 1, "precondition: an ordinary CA nat-vent start must run"
    assert _fan_on(house, fan)
    engine = house.coordinator.automation_engine
    assert engine._fan_active and engine._natural_vent_active


async def _wait_until_start_age(house, fan: str, seconds: float) -> None:
    """Advance until the last CA start is ``seconds`` old (the occupant's off must not look like an echo)."""
    started = _ca_starts(house, fan)[-1].at
    while (dt_util.utcnow() - started).total_seconds() < seconds:
        await house.advance(seconds=10)


async def _occupant_turns_off_running_fan(house, fan: str, caplog) -> int:
    """The occupant stops the running fan; returns the caplog mark taken just before, for evidence."""
    await _wait_until_start_age(house, fan, 140)
    mark = len(caplog.records)
    await _occupant_off(house, fan)
    await _steps(house, 10)
    return mark


def _record(engine) -> tuple:
    return (engine._fan_rate_limited_direction, engine._fan_rate_limited_until)


async def _f3_activation_deferred_then_window_closed(house, fan: str) -> None:
    """Leaves an "activate" deferral recorded with the window closed again and the fan never restarted."""
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 350)  # CA starts, and the 300 s floor passes
    assert len(_ca_starts(house, fan)) == 1 and _fan_on(house, fan), "precondition: CA started the fan"
    await house.close_contact("window")
    await _steps(house, 40)  # past the 30 s debounce: CA stops the fan
    assert not _fan_on(house, fan), "precondition: CA stopped the fan"
    await house.open("window")
    await _steps(house, 40)  # past the debounce, inside the new 300 s floor: the start is deferred
    assert engine._fan_rate_limited_direction == "activate", "precondition: the window-open start was deferred"
    assert engine._fan_manual_origin_pending is None, "precondition: no #1205 pending fact on this route"
    assert len(_ca_starts(house, fan)) == 1 and not _fan_on(house, fan)
    await house.close_contact("window")
    await _steps(house, 50)  # the deferred start is abandoned (still inside the floor)
    assert not _fan_on(house, fan) and len(_ca_starts(house, fan)) == 1


async def _f4_stop_deferred_then_occupant_off(house, fan: str) -> None:
    """Leaves a "deactivate" deferral recorded after the occupant switched the (still running) fan off."""
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 50)
    assert len(_ca_starts(house, fan)) == 1 and _fan_on(house, fan), "precondition: CA started the fan"
    house.set_indoor(60.0)  # under the comfort floor: CA wants nat-vent over
    for _ in range(6):
        await _steps(house, 20)
        await house.coordinator.async_refresh()
        await house.advance(seconds=0)
    assert engine._fan_rate_limited_direction == "deactivate", "precondition: CA's stop was deferred by the floor"
    assert _fan_on(house, fan), "precondition: the fan still runs"
    assert engine._fan_manual_origin_pending is None
    await _wait_until_start_age(house, fan, 140)
    assert engine._fan_rate_limited_direction == "deactivate" and _fan_on(house, fan), "still inside the floor"
    await _occupant_off(house, fan)
    await _steps(house, 10)
    assert not _fan_on(house, fan)


@pytest.mark.parametrize("fan", _FANS)
async def test_f3_abandoned_activation_is_not_applied_an_hour_later(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    await _f3_activation_deferred_then_window_closed(house, fan)
    await _steps(house, 3600)
    caplog.clear()

    await house.open("window")
    await _steps(house, 90)  # an ordinary, unrelated CA start
    assert len(_ca_starts(house, fan)) == 2 and _fan_on(house, fan), "precondition: the unrelated start ran"

    assert "applying deferred activation" not in caplog.text, "an hour-old abandoned deferral was 'applied'"


@pytest.mark.parametrize("fan", _FANS)
async def test_f4_abandoned_stop_is_not_applied_an_hour_later(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    await _f4_stop_deferred_then_occupant_off(house, fan)
    await _steps(house, 3600)
    house.set_indoor(72.0)  # the house is comfortable again, so an ordinary nat-vent start is warranted
    await house.close_contact("window")
    await _steps(house, 60)
    caplog.clear()
    await house.open("window")
    await _steps(house, 60)  # an ordinary, unrelated CA start
    assert len(_ca_starts(house, fan)) == 2 and _fan_on(house, fan), "precondition: the unrelated start ran"
    await _steps(house, 300)
    await house.close_contact("window")
    await _steps(house, 90)  # and an ordinary, unrelated CA stop
    assert not _fan_on(house, fan), "precondition: the unrelated stop ran"

    assert "applying deferred exit" not in caplog.text, "an hour-old abandoned deferral was 'applied'"


@pytest.mark.parametrize("fan", _FANS)
async def test_f3_record_cleared_after_window_closed(make_house, fan, caplog) -> None:
    """The record itself, right after the abandoned window-open start (both modes)."""
    house = await _house(make_house, fan, caplog)
    await _f3_activation_deferred_then_window_closed(house, fan)
    f3 = _record(house.coordinator.automation_engine)

    assert f3 == (None, None), f"F3: stale record after the window closed: {f3!r}"


@pytest.mark.parametrize("fan", _FANS)
async def test_f4_record_cleared_after_occupant_off(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    await _f4_stop_deferred_then_occupant_off(house, fan)

    rec = _record(house.coordinator.automation_engine)

    assert rec == (None, None), f"F4: stale record after the occupant turned the fan off: {rec!r}"


async def test_whf_card_has_no_pending_suffix_after_an_abandoned_activation(make_house, caplog) -> None:
    """whole_house_fan only: the WHF status card (still inside the 5-minute floor) must not say '(on pending ...)'."""
    house = await _house(make_house, "whf", caplog)
    await _f3_activation_deferred_then_window_closed(house, "whf")

    status = house.coordinator._compute_whf_status()

    assert "pending" not in status, f"stale pending suffix on the WHF status card: {status!r}"


async def test_whf_card_has_no_pending_suffix_after_an_abandoned_stop(make_house, caplog) -> None:
    house = await _house(make_house, "whf", caplog)
    await _f4_stop_deferred_then_occupant_off(house, "whf")

    status = house.coordinator._compute_whf_status()

    assert "pending" not in status, f"stale pending suffix on the WHF status card: {status!r}"
