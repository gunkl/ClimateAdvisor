"""A dropped UNCONFIRMED occupant change is announced (generated day 12010000245).

Occupant first: you set cool 75 and turn the whole-house fan on in the same minute on a sealed house. The fan press
replaces your thermostat change (owner principle, #1133), the sealed-house rule stops the fan, and Climate Advisor
goes back to its own plan. Production said nothing about the dropped (still pending) change, so with Manual Change
Pause = 0 the household-promise journal kept it open for ever and reported every later bedtime / nat-vent write as
"changed your thermostat setting while your manual change was still in force".
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from tests.test_protect_manual_change import (  # noqa: F401 (fixtures)
    _engine,
    _occupant_overrides,
    _run,
    timers,  # noqa: F401  pytest fixture re-export
)
from tools.sim_harness.generator import run_day


def _pending_change(*, grace_s: int):
    events: list[tuple[str, dict]] = []
    engine = _engine(
        "heat", indoor=60.0, day="cold", outdoor=50.0, manual_grace_seconds=grace_s, override_confirm_seconds=600
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    engine._emit_event_callback = lambda name, payload: events.append((name, dict(payload)))
    engine.hass.async_create_task = lambda coro: coro.close()
    _occupant_overrides(engine, source="normal", new_mode="heat")
    assert engine._override_confirm_pending and not engine._manual_override_active
    return engine, events


def _cleared(events):
    return [p for n, p in events if n == "override_cleared" and p.get("was_mode") is not None]


@pytest.mark.parametrize("grace_s", [0, 1800])
def test_a_whf_press_announces_the_pending_change_it_replaces(timers, grace_s):  # noqa: F811
    engine, events = _pending_change(grace_s=grace_s)
    engine.handle_fan_manual_override(fan_before="off", fan_after="on")
    cleared = _cleared(events)
    assert len(cleared) == 1, events
    assert cleared[0]["reason"] == "new_override_during_grace"
    assert cleared[0]["pending"] is True and cleared[0]["was_mode"] == "heat"


def test_mirror_dashboard_cancel_announces_a_pending_change(timers):  # noqa: F811
    engine, events = _pending_change(grace_s=0)
    assert engine.cancel_override("user_cancel") is True
    cleared = _cleared(events)
    assert [c["reason"] for c in cleared] == ["user_cancel"]


def test_a_confirmed_override_is_announced_once_not_twice(timers):  # noqa: F811
    engine, events = _pending_change(grace_s=0)
    engine._override_confirm_pending = False
    engine._manual_override_active = True
    engine._manual_override_mode = "heat"
    engine.handle_fan_manual_override(fan_before="off", fan_after="on")
    assert len(_cleared(events)) == 1
    assert "pending" not in _cleared(events)[0]


def test_generated_day_12010000245_keeps_override_respected() -> None:
    assert not [(h.ts, h.detail) for h in run_day(12010000245).report.hard if h.promise == "override_respected"]
