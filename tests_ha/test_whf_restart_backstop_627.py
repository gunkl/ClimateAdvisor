"""Issue #1238 (real HA): a restart must not let the untracked-fan backstop stop a fan the occupant started.

Occupant story: a warm evening, a window open, outdoor air no cooler than indoor (so free cooling is not
warranted). The person starts the whole-house fan with the RF remote's 2 h timer. Home Assistant restarts.
Climate Advisor's first update cycles after a restart run inside the 300 s start-up coalescing window, while
``restore_state()``'s clean slate has wiped the in-memory fan override. If the untracked-fan backstop were not
gated on ``_startup_coalesce_active`` (``coordinator._should_run_untracked_fan_backstop``, #627) it would read
the running fan as "unwarranted" and switch it off within a minute: the person's 2 h timer would be lost, and
on the live incident (2026-08-11) the AC was then commanded to Cool into the open house.

Which test guards what:

* ``test_rf_timer_pressed_while_ha_is_down_keeps_the_fan`` is the revert-sensitive one. The press lands while
  Climate Advisor is unloaded, so no fan override was persisted; only the remote's live event token says the
  occupant asked for 2 h. Inside the window only the coalesce gate keeps the backstop away; at the end of the
  window the start-up reconcile re-arms the fan override from the token. Delete the ``not
  self._startup_coalesce_active`` line from ``_should_run_untracked_fan_backstop`` and it fails.
* ``test_wall_press_while_ha_is_down_is_reconciled_after_the_window`` is the other direction: a wall-switch press
  while down leaves no token, so the fan is kept inside the window (gate) and turned off after it.
* ``test_session_started_before_the_restart_keeps_the_fan`` is the issue's literal scenario, kept as an
  invariant. It is NOT revert-sensitive any more: the fan override persisted before the restart (#1045) is
  restored as a grace that the first reconcile consumes, so the backstop's override/grace checks also hold it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

from tests_ha.house.builder import STARTUP_WINDOW_S

pytestmark = pytest.mark.house

REMOTE = "event.house_quietcool_remote"
_WARM_NO_NATVENT = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
    "initial_indoor_f": 78.0,
    "outdoor_f": 80.0,  # outdoor >= indoor: nat-vent is NOT warranted, so the backstop would act on the fan
    "high_f": 92.0,
    "low_f": 66.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_remote_entity": REMOTE, "fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
}
_IN_WINDOW_S = 120  # well inside the 300 s coalescing window: several coordinator cycles have run


def _set_remote(house, event_type: str) -> None:
    """The RF remote's ESPHome event entity: a fresh press stamped now (pattern of test_remote_reconnect_1198)."""
    ts = dt_util.utcnow().isoformat(timespec="milliseconds")
    house.hass.states.async_set(REMOTE, ts, {"event_type": event_type, "event_types": ["timer_2h", "off"]})


def _ca_fan_offs(house, since: int) -> list:
    return [
        c for c in house.calls[since:] if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_off"
    ]


def _ca_cool(house, since: int) -> list:
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.data.get("hvac_mode") == "cool"
    ]


async def _window_open_fan_off(make_house):
    house = await make_house(**_WARM_NO_NATVENT)
    await house.open("window")
    await house.advance(minutes=2)
    assert not house.fan.is_on, "precondition: free cooling is not warranted, so CA left the fan off"
    return house


async def _rf_timer_press(house) -> None:
    _set_remote(house, "timer_2h")
    await house.occupant_presses_fan(True)


async def _wall_press(house) -> None:
    await house.occupant_presses_fan(True)


async def test_rf_timer_pressed_while_ha_is_down_keeps_the_fan(make_house) -> None:
    house = await _window_open_fan_off(make_house)
    mark = len(house.calls)
    await house.restart(settle=False, while_down=_rf_timer_press, down_for_seconds=60)
    coord = house.coordinator
    ae = coord.automation_engine
    assert coord._startup_coalesce_active, "precondition: the first cycles run inside start-up coalescing"

    await house.advance(seconds=_IN_WINDOW_S)
    assert coord._startup_coalesce_active
    assert not _ca_fan_offs(house, mark), "#627: the untracked-fan backstop stopped the fan inside the window"
    assert house.fan.is_on

    await house.advance(seconds=STARTUP_WINDOW_S)
    assert not coord._startup_coalesce_active
    assert house.fan.is_on, "the occupant's 2 h RF-remote timer was lost across the restart"
    assert ae._fan_override_active, "the start-up reconcile did not re-arm the occupant's fan override"
    assert ae._fan_remote_timer_hours == 2.0
    assert not _ca_fan_offs(house, mark)
    assert not _ca_cool(house, mark), "the AC was commanded to Cool while the whole-house fan ran"


async def test_wall_press_while_ha_is_down_is_reconciled_after_the_window(make_house) -> None:
    house = await _window_open_fan_off(make_house)
    mark = len(house.calls)
    await house.restart(settle=False, while_down=_wall_press, down_for_seconds=60)
    coord = house.coordinator

    await house.advance(seconds=_IN_WINDOW_S)
    assert coord._startup_coalesce_active
    assert not _ca_fan_offs(house, mark), "the backstop acted inside the start-up coalescing window"
    assert house.fan.is_on

    await house.advance(seconds=STARTUP_WINDOW_S)
    assert not coord._startup_coalesce_active
    assert _ca_fan_offs(house, mark), "control: after the window an unwarranted, untracked fan is reconciled off"
    assert not house.fan.is_on
    assert not _ca_cool(house, mark)


async def test_session_started_before_the_restart_keeps_the_fan(make_house) -> None:
    """Invariant (the issue's literal scenario): a remote-timer session running when HA restarts survives it."""
    house = await _window_open_fan_off(make_house)
    await _rf_timer_press(house)
    await house.advance(seconds=90)
    ae = house.coordinator.automation_engine
    assert house.fan.is_on and ae._fan_override_active, "precondition: CA took the press as an occupant override"
    assert ae._fan_remote_timer_hours == 2.0

    mark = len(house.calls)
    await house.restart(settle=False)
    coord = house.coordinator
    ae = coord.automation_engine
    assert coord._startup_coalesce_active
    await house.advance(seconds=_IN_WINDOW_S)
    assert house.fan.is_on and not _ca_fan_offs(house, mark)

    await house.advance(seconds=STARTUP_WINDOW_S)
    assert not coord._startup_coalesce_active
    assert house.fan.is_on and ae._fan_override_active
    assert not _ca_fan_offs(house, mark)
    assert not _ca_cool(house, mark), "the AC was commanded to Cool while the whole-house fan ran"
