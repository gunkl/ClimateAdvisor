"""Issue #1102: reload/unload lifecycle — release, idempotent shutdown, latch, boot hooks.

Occupant experience being guarded: after a settings save (config-entry reload) the
person turns the whole-house fan off, and the replaced ("dead") Climate Advisor
instance must not turn it back on, notify, or arm anything; the fan-off grace they
just started must survive the save. T1 lives in test_reload_teardown.py.

- T2: after an HA-style unload, nothing CA registered is still live (timers or
  listeners), named by nobody — so a future leak fails here.
- T3/T6 (N4): reload during a fan-off grace keeps its end time; HA's second
  async_shutdown (on_unload) is a no-op, so state is saved once; shutdown of a
  coordinator that never finished setup does not raise.
- T5: every command / notify / arm chokepoint refuses on a shut-down instance with
  its pinned no-op result, and issues no service call and arms no timer.
- T8: F6 (STARTED hook owned by the entry) and F7 (reload scheduled, not awaited).

Scope: FakeScheduler sees timers registered through the patched automation,
coordinator and api helpers; FakeHass sees state and bus listeners.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import (
    async_unload_entry_like_ha,
    build_headless_multi_zone,
    setup_entry_like_ha,
)

_FAN_ENTITY = "fan.whole_house"
_THERMOSTAT = "climate.zone_a_thermostat"
_T0 = datetime(2024, 1, 15, 8, 0, tzinfo=UTC)  # build_headless_multi_zone's default clock start
_WEATHER = "weather.test_forecast"  # build_headless_multi_zone's default weather entity


def _zone_config(**extra: Any) -> dict[str, Any]:
    return {
        "climate_entity": _THERMOSTAT,
        "zone_title": "Zone A",
        "fan_mode": "both",
        "fan_entity": _FAN_ENTITY,
        **extra,
    }


def _build(**extra: Any):
    zones, fake_hass, scheduler = build_headless_multi_zone(configs=[_zone_config(**extra)])
    zone = zones["zone_0"]
    return zone["coordinator"], zone["entry"], fake_hass, scheduler


# ---------------------------------------------------------------------------
# T2 — nothing left after unload
# ---------------------------------------------------------------------------


def test_unload_leaves_no_live_timer_or_listener():
    """Every timer and listener the instance registered is released by an HA-style unload.

    Exercises the leaked paths fixed by #1102 without naming them: the startup
    coalesce timer and weather retry (pending at +172 s, as in the incident),
    occupancy toggle listeners (home + vacation toggles configured), the api.py
    cancel-override follow-up, and the STARTED hook (HA not yet running).
    """
    from custom_components.climate_advisor.api import _schedule_reclassify_after_cancel

    coord, entry, fake_hass, scheduler = _build(
        home_toggle_entity="input_boolean.home",
        vacation_toggle_entity="input_boolean.vacation",
    )
    assert fake_hass.listener_count() > 0  # setup really registered listeners

    with scheduler.installed():
        scheduler.advance_by(172)
        _schedule_reclassify_after_cancel(fake_hass, coord, coord.automation_engine)
        run_coro(async_unload_entry_like_ha(fake_hass, entry))

    leftover_timers = [getattr(e.callback, "__qualname__", repr(e.callback)) for e in scheduler.live_callbacks()]
    assert leftover_timers == [], f"timers still live after unload: {leftover_timers}"
    assert fake_hass.listener_count() == 0, (
        f"listeners still live after unload: state={fake_hass._state_listeners} bus={fake_hass.bus._listeners}"
    )


# ---------------------------------------------------------------------------
# T3 / T6 — N4: idempotent shutdown, fan-off grace survives a reload
# ---------------------------------------------------------------------------


def test_reload_during_fan_off_grace_keeps_grace_and_saves_once():
    """Occupant turns the fan off (manual fan_off grace), then saves settings.

    HA unloads (async_unload_entry -> async_shutdown, then on_unload -> async_shutdown
    again) and sets the entry up again. On 1df0246 the second shutdown re-saved after
    cleanup() had wiped the grace, so the new instance came up with no grace and was
    free to turn the fan back on. Now: saved once, grace re-armed with its end time.
    """
    coord, entry, fake_hass, scheduler = _build()
    ae = coord.automation_engine
    # HA's DataUpdateCoordinator registered async_shutdown on the entry (ContextVar path).
    assert coord.async_shutdown in entry._on_unload

    saved_grace_end: list[Any] = []
    real_save = coord._async_save_state

    async def _spy_save() -> None:
        saved_grace_end.append(ae.get_serializable_state().get("grace_end_time"))
        await real_save()

    coord._async_save_state = _spy_save

    with scheduler.installed():
        ae._fan_active = True
        ae.on_fan_turned_off(fan_before="on", fan_after="off")
        assert ae._last_grace_trigger == "fan_off"
        grace_end = ae._grace_end_time
        assert grace_end

        scheduler.advance_by(60)
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        assert saved_grace_end == [grace_end], f"expected exactly one save carrying the grace: {saved_grace_end}"

        new = setup_entry_like_ha(fake_hass, scheduler, entry)

    assert new is not coord
    new_ae = new.automation_engine
    assert new_ae._grace_active is True
    assert new_ae._last_grace_trigger == "fan_off"
    assert new_ae._grace_end_time == grace_end


def test_failed_save_still_releases_everything():
    """A save that raises during shutdown (disk full, permissions) must not strand timers.

    Occupant view: without the try/finally the old instance would stay armed and could
    keep driving the fan after the reload, exactly the #1102 failure.
    """
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    coord, _entry, fake_hass, scheduler = _build(
        home_toggle_entity="input_boolean.home",
        vacation_toggle_entity="input_boolean.vacation",
    )

    async def _save_fails() -> None:
        raise OSError("disk full")

    coord._async_save_state = _save_fails
    with scheduler.installed():
        scheduler.advance_by(172)
        assert scheduler.live_callbacks()  # startup timer + retry are pending
        with pytest.raises(OSError, match="disk full"):
            run_coro(coord.async_shutdown())

    leftover = [getattr(e.callback, "__qualname__", repr(e.callback)) for e in scheduler.live_callbacks()]
    assert leftover == [], f"timers still live after a failed save: {leftover}"
    assert all(not v for v in fake_hass._state_listeners.values())
    # Only the entry-owned STARTED hook remains; HA releases it via the entry's on_unload.
    assert {k for k, v in fake_hass.bus._listeners.items() if v} <= {EVENT_HOMEASSISTANT_STARTED}
    assert coord.automation_engine._shut_down is True


def test_shutdown_twice_is_noop_and_partial_coordinator_is_safe():
    """A coordinator that never ran async_setup (HA shuts it down after a failed setup)."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord, _entry, fake_hass, scheduler = _build()
    partial = ClimateAdvisorCoordinator(fake_hass, dict(coord.config), entry_id="partial")

    saves: list[int] = []
    real_save = partial._async_save_state

    async def _spy_save() -> None:
        saves.append(1)
        await real_save()

    partial._async_save_state = _spy_save
    with scheduler.installed():
        run_coro(partial.async_shutdown())
        run_coro(partial.async_shutdown())

    assert saves == [1]
    assert partial._shut_down is True
    assert partial.automation_engine._shut_down is True
    assert partial._shutdown_requested is True  # HA base shutdown ran (no further refreshes)


# ---------------------------------------------------------------------------
# T5 — every chokepoint refuses on a shut-down instance
# ---------------------------------------------------------------------------


def _seed_live_device_states(fake_hass) -> None:
    """Make each refused call one that WOULD go out on a live instance."""
    from tools.sim_harness.fake_hass import FakeState

    fake_hass.states.set(
        _THERMOSTAT,
        FakeState(
            state="cool",
            attributes={
                "fan_mode": "on",
                "fan_modes": ["auto", "on"],
                "hvac_modes": ["off", "heat", "cool", "heat_cool"],
                "supported_features": 2,
            },
        ),
    )
    fake_hass.states.set(_FAN_ENTITY, FakeState(state="on"))
    # A normal daily forecast, so the briefing case below would really generate and
    # send on a live instance (reachable headless since #1117's HA-faithful as_local).
    fake_hass.states.set(
        _WEATHER,
        FakeState(state="sunny", attributes={"temperature": 55, "forecast": _daily_forecast()}),
    )


def _daily_forecast() -> list[dict]:
    return [{"datetime": (_T0 + timedelta(days=i)).isoformat(), "temperature": 60 + i, "templow": 45} for i in range(5)]


def _refusal_cases():
    from custom_components.climate_advisor.automation import (
        ClimateCallOutcome,
        FanCommandResult,
        HvacCommandResult,
    )

    def _is(expected):
        return lambda r: r is expected

    def _activate(c, ae):
        ae._fan_active = False  # so activation would be a real transition
        return ae._activate_fan(reason="t")

    climate_data = {"entity_id": _THERMOSTAT, "hvac_mode": "cool"}
    hvac_failed = _is(HvacCommandResult.FAILED)
    fan_disabled = _is(FanCommandResult.DISABLED)
    return [
        (
            "engine._call_climate",
            lambda c, ae: ae._call_climate("set_hvac_mode", climate_data, "t"),
            lambda r: r.outcome is ClimateCallOutcome.REJECTED,
        ),
        ("engine._set_hvac_mode(cool)", lambda c, ae: ae._set_hvac_mode("cool", reason="t"), hvac_failed),
        # off path carries the blower off-assert climate.set_fan_mode (blower call 1/3)
        ("engine._set_hvac_mode(off)+blower", lambda c, ae: ae._set_hvac_mode("off", reason="t"), hvac_failed),
        ("engine._set_temperature", lambda c, ae: ae._set_temperature(74.0, reason="t", mode="cool"), hvac_failed),
        # blower call 2/3 lives in _activate_fan, 3/3 in _deactivate_fan
        ("engine._activate_fan+blower", _activate, fan_disabled),
        ("engine._deactivate_fan+blower", lambda c, ae: ae._deactivate_fan(reason="t"), fan_disabled),
        (
            "engine._call_fan_service_with_context",
            lambda c, ae: ae._call_fan_service_with_context("fan", "turn_on", _FAN_ENTITY),
            _is(None),
        ),
        ("engine._release_orphaned_hvac_fan", lambda c, ae: ae._release_orphaned_hvac_fan(), _is(None)),
        ("engine._notify", lambda c, ae: ae._notify("m", "t", "briefing"), _is(None)),
        (
            "engine._start_grace_period_action",
            lambda c, ae: ae._start_grace_period_action("manual", "fan_off"),
            _is(False),
        ),
        ("engine._start_fan_thermo_backstop", lambda c, ae: ae._start_fan_thermo_backstop(), _is(None)),
        ("coordinator._async_command_fan_entity", lambda c, ae: c._async_command_fan_entity(on=True), _is(False)),
        # Issue #1117: reachable headless now (was the "as_local harness gap"); the
        # positive control is test_live_instance_sends_briefing below.
        ("coordinator._async_send_briefing", lambda c, ae: c._async_send_briefing(_T0), _is(None)),
    ]


@pytest.mark.parametrize(("name", "call", "check"), _refusal_cases(), ids=[c[0] for c in _refusal_cases()])
def test_shut_down_instance_refuses(name, call, check):
    import inspect

    coord, entry, fake_hass, scheduler = _build()
    ae = coord.automation_engine
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        _seed_live_device_states(fake_hass)
        ae._fan_active = True  # so deactivate would be a real transition
        actions_before = len(fake_hass.action_log)
        armed_before = len(scheduler.armed_log)

        result = call(coord, ae)
        if inspect.isawaitable(result):
            result = run_coro(result)
        scheduler.advance_by(1)

    assert check(result), f"{name}: unexpected result {result!r}"
    assert fake_hass.action_log[actions_before:] == [], f"{name}: service call after shutdown"
    assert scheduler.armed_log[armed_before:] == [], f"{name}: timer armed after shutdown"


def test_live_instance_sends_briefing():
    """Positive control for the briefing refusal case: a LIVE instance does notify."""
    coord, _entry, fake_hass, scheduler = _build()
    with scheduler.installed():
        _seed_live_device_states(fake_hass)
        run_coro(coord._async_send_briefing(scheduler.now()))
        scheduler.advance_by(1)
    assert [a for a in fake_hass.action_log if a["domain"] == "notify"], "live briefing sent nothing"


def test_in_flight_backstop_tick_does_not_rearm_or_command():
    """A backstop tick already firing when cleanup() runs cannot re-arm or command (C3 cut)."""
    coord, entry, fake_hass, scheduler = _build()
    ae = coord.automation_engine
    with scheduler.installed():
        ae._start_fan_thermo_backstop()
        tick = next(e.callback for e in scheduler.live_callbacks() if "_thermo_tick" in e.callback.__qualname__)
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        _seed_live_device_states(fake_hass)
        ae._fan_active = True
        actions_before = len(fake_hass.action_log)
        armed_before = len(scheduler.armed_log)

        tick(scheduler.now())  # the tick that was already in flight
        scheduler.advance_by(1)  # drains the _thermo_backstop_task it enqueued

    assert fake_hass.action_log[actions_before:] == []
    assert scheduler.armed_log[armed_before:] == []


def test_dead_coordinator_update_and_wrapped_callbacks_refuse():
    """F11: the _async_update_data tripwire and the _zone_scoped wrappers refuse."""
    coord, entry, fake_hass, scheduler = _build()
    calls: list[str] = []

    async def _impl() -> dict:
        calls.append("impl")
        return {}

    async def _async_cb(*_a: Any) -> None:
        calls.append("async_cb")

    def _sync_cb(*_a: Any) -> None:
        calls.append("sync_cb")

    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        coord._async_update_data_impl = _impl
        data_before = coord.data
        assert run_coro(coord._async_update_data()) is data_before
        assert run_coro(coord._zone_scoped(_async_cb)()) is None
        assert coord._zone_scoped_sync(_sync_cb)() is None

    assert calls == []


# ---------------------------------------------------------------------------
# T8 — F6 STARTED hook, F7 scheduled reload
# ---------------------------------------------------------------------------


def test_boot_validation_hook_is_owned_by_the_entry():
    """F6: on first boot the weather check waits for STARTED, and unloading cancels it."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    coord, entry, fake_hass, scheduler = _build()
    assert len(fake_hass.bus._listeners.get(EVENT_HOMEASSISTANT_STARTED, [])) == 1

    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
    assert fake_hass.bus._listeners.get(EVENT_HOMEASSISTANT_STARTED, []) == []


def test_boot_validation_runs_on_started():
    """F6: firing STARTED runs the weather check (missing entity -> repair issue raised)."""
    from unittest.mock import patch

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    coord, entry, fake_hass, scheduler = _build()
    with patch("custom_components.climate_advisor.ir.async_create_issue") as create_issue, scheduler.installed():
        fake_hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        scheduler.advance_by(0)  # run the validation task the STARTED hook started
    issue_ids = [c.args[2] for c in create_issue.call_args_list]
    assert f"weather_entity_not_found_{entry.entry_id}" in issue_ids


def test_reload_path_schedules_reload_instead_of_awaiting_it():
    """F7: when HA is running and the weather entity auto-resolves, the reload is scheduled.

    Awaiting async_reload from inside async_setup_entry waits on this entry's own setup
    lock (self-deadlock); HA documents async_schedule_reload for integrations.
    """
    from unittest.mock import AsyncMock

    from tools.sim_harness.fake_hass import FakeState

    coord, entry, fake_hass, scheduler = _build()
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        fake_hass.is_running = True
        # Exactly one weather entity -> auto-resolve. Available with a normal daily
        # forecast: the new instance's first refresh runs the full regular cycle
        # (reachable headless since #1117's HA-faithful as_local).
        fake_hass.states.set(
            "weather.home",
            FakeState(state="sunny", attributes={"temperature": 55, "forecast": _daily_forecast()}),
        )
        fake_hass.config_entries.async_reload = AsyncMock()
        setup_entry_like_ha(fake_hass, scheduler, entry)

    assert entry.data["weather_entity"] == "weather.home"
    assert fake_hass.config_entries.scheduled_reloads == [entry.entry_id]
    fake_hass.config_entries.async_reload.assert_not_awaited()
