"""Issue #1717: the promise probe judges a command by the automation state when it was SENT, not when it was polled.

Occupant: switching automation off puts the thermostat back on the plain setpoint while automation is still on, and
only then goes silent. The probe used to read the automation state after the whole event had finished, so it called
that restore "a command sent while automation was off" (a false hard ``observe_only_silence`` hit). The mirror: a
command sent while still in observe-only, inside the event that turns automation back on, was read as "on" and P3
missed it. Both directions are pinned here, plus the fallback for a command that never passed the send hook.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from tools.sim_harness.promise_probe import FakeHassPromiseSource  # noqa: E402

_NOW = datetime(2026, 7, 12, 14, 40, tzinfo=UTC)
_CLIMATE = "climate.thermostat"


def _source() -> tuple[FakeHassPromiseSource, SimpleNamespace]:
    engine = SimpleNamespace(dry_run=False, _mode_change_reason=None, _command_observer=None)
    session = SimpleNamespace(
        config={"climate_entity": _CLIMATE},
        climate_entity=_CLIMATE,
        engine=engine,
        coordinator=None,
        fake_hass=SimpleNamespace(states=SimpleNamespace(add_observer=lambda _cb: None), action_log=[]),
        scheduler=SimpleNamespace(now=lambda: _NOW),
        event_log=[],
    )
    return FakeHassPromiseSource(session), engine


def _send(engine: SimpleNamespace) -> dict:
    data = {"entity_id": _CLIMATE, "temperature": 76.0}
    engine._command_observer("climate", "set_temperature", data, "setpoint")
    return {"domain": "climate", "service": "set_temperature", "data": data, "ts": _NOW}


def test_a_command_sent_while_on_keeps_on_when_automation_turns_off_before_the_poll() -> None:
    """The restore written while automation is still on is not an observe-only command (no false P3 hit)."""
    source, engine = _source()
    entry = _send(engine)  # sent with automation on
    engine.dry_run = True  # then the switch-off flips the flag, inside the same event
    cmd = source._command(entry, _NOW, None)
    assert cmd is not None and cmd.automation_enabled is True


def test_a_command_sent_while_off_stays_off_when_automation_turns_on_before_the_poll() -> None:
    """Mirror: a command sent in observe-only is still caught when the same event turns automation on."""
    source, engine = _source()
    engine.dry_run = True
    entry = _send(engine)  # sent in observe-only
    engine.dry_run = False  # the event then turns automation on
    cmd = source._command(entry, _NOW, None)
    assert cmd is not None and cmd.automation_enabled is False


def test_a_command_sent_after_the_switch_off_is_still_stamped_off() -> None:
    """The real failure P3 exists for still breaks it: a command sent once automation is off."""
    source, engine = _source()
    engine.dry_run = True
    cmd = source._command(_send(engine), _NOW, None)
    assert cmd is not None and cmd.automation_enabled is False


def test_a_command_that_bypassed_the_send_hook_falls_back_to_the_poll_time_state() -> None:
    source, engine = _source()
    engine.dry_run = True
    entry = {"domain": "climate", "service": "set_temperature", "data": {"entity_id": _CLIMATE}, "ts": _NOW}
    cmd = source._command(entry, _NOW, None)
    assert cmd is not None and cmd.automation_enabled is False
