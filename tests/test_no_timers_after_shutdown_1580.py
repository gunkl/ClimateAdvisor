"""Issue #1580: no timer is armed after an instance shut down.

Occupant view: nothing changes in the house (a leftover timer is refused when it fires), but a write that was
awaiting the thermostat when the zone unloaded used to arm its 10-second setpoint check on the dead instance; the
timer outlived the unload (nightly 2026-11-14, ``Lingering timer ... _set_temperature_impl._schedule_check``).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import automation, coordinator
from tools.sim_harness.build_engine import build_headless_engine

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"


def _check_timers(sched) -> list[str]:
    return [getattr(c.callback, "__name__", repr(c.callback)) for c in sched.live_callbacks()]


@pytest.mark.parametrize("latch_mid_write", [False, True])
def test_a_write_resuming_after_shutdown_arms_no_setpoint_check(latch_mid_write):
    """The race itself: the unload lands while ``climate.set_temperature`` is in flight."""
    engine, fake_hass, sched, _events = build_headless_engine(climate_state="heat")

    async def _service(call):
        if latch_mid_write:
            engine.latch_shutdown()  # the entry unloads while the write awaits the thermostat

    fake_hass.services.async_register("climate", "set_temperature", _service)
    with sched.installed():
        before = set(_check_timers(sched))
        asyncio.run(engine._set_temperature(70.0, reason="test", mode="heat"))
        armed = [n for n in _check_timers(sched) if n not in before]
    if latch_mid_write:
        assert "_schedule_check" not in armed, armed
    else:  # control: an engine that is still running does arm its setpoint check
        assert "_schedule_check" in armed, armed


@pytest.mark.parametrize("module", [automation, coordinator])
def test_a_latched_instance_arms_nothing_and_gets_a_safe_handle(module, monkeypatch):
    armed = []
    monkeypatch.setattr(module, "async_call_later", lambda hass, d, fn: armed.append(fn) or (lambda: None))
    live = MagicMock()
    live._shut_down = False
    module._call_later(live, 10, print)
    assert armed == [print]
    dead = MagicMock()
    dead._shut_down = True
    handle = module._call_later(dead, 10, print)
    assert armed == [print]  # nothing new armed
    assert handle() is None  # the no-op cancel is always safe to call


def test_a_latched_coordinator_arms_no_point_in_time_timer(monkeypatch):
    armed = []
    monkeypatch.setattr(coordinator, "async_track_point_in_time", lambda hass, fn, when: armed.append(fn))
    dead = MagicMock()
    dead._ha_stopping = True  # Issue #1183: HA stopping latches too
    coordinator._track_point_in_time(dead, print, None)
    assert armed == []


@pytest.mark.parametrize("path", ["automation.py", "coordinator.py"])
def test_every_timer_goes_through_the_latch_guard(path):
    """A new ``async_call_later`` / ``async_track_point_in_time`` call outside the guard would bring the bug back."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    guards = {"_call_later", "_track_point_in_time"}
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in guards:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("async_call_later", "async_track_point_in_time")
            ):
                bad.append((fn.name, node.lineno))
    assert bad == []
