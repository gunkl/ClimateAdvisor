"""Issue #1102: a config-entry reload must leave no live CA instance behind.

Occupant experience being guarded: the person turns the whole-house fan off,
and a coordinator/engine that was "unloaded" by a settings save (config-entry
reload) keeps running in the background, turns the fan back on, and blames
them for overriding it. Root cause on 1df0246: ``async_setup()`` arms a 300 s
startup-coalesce timer with ``async_call_later`` and discards the handle, so
unloading inside that window leaves the timer armed; when it fires it calls
``async_request_refresh()`` on the dead coordinator, which runs a full
``_async_update_data`` cycle and lets the dead engine command devices again.

T1 (plan v5): set up one zone through the REAL ``async_setup_entry``; unload
it at t0+172 s the way HA does (the REAL ``async_unload_entry``, then the
entry's on_unload callbacks LIFO) — inside the startup window, as in the
field incident; advance the virtual clock past t0+600 s.
After unload the old instance must run no ``_async_update_data``, make no
service call, and arm no timer.

Scope: the FakeScheduler only sees timers registered through the automation
and coordinator modules' patched helpers, and service calls through FakeHass.
"""

from __future__ import annotations

from datetime import timedelta

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import async_unload_entry_like_ha, build_headless_multi_zone

_UNLOAD_AT_S = 172
_ADVANCE_PAST_S = 601


def _callback_name(cb) -> str:
    return getattr(cb, "__qualname__", None) or repr(cb)


def test_unloaded_instance_stays_dead_after_startup_window():
    # Harness default: no weather entity, so setup's first refresh also arms the
    # weather-not-ready retry chain (coordinator.py:3479, discarded handle, F2).
    # Both it and the 300 s startup-coalesce timer (coordinator.py:1430, F1)
    # are pending at +172 s; the failure message lists them so the +300 s
    # startup-timer refresh is identifiable. (Seeding a ready weather entity
    # instead trips a harness gap: FakeScheduler patches dt_util.as_local to
    # identity, so the cosine-fallback forecast mixes naive/aware datetimes.)
    zones, fake_hass, scheduler = build_headless_multi_zone(zone_count=1)
    zone = zones["zone_0"]
    old_coordinator = zone["coordinator"]
    t0 = scheduler.now()

    # Spy on the old coordinator's update cycle (instance attribute shadows the
    # method; the refresh path resolves self._async_update_data at call time).
    update_calls: list = []
    real_update = old_coordinator._async_update_data

    async def _spy_update_data():
        update_calls.append(scheduler.now())
        return await real_update()

    old_coordinator._async_update_data = _spy_update_data

    # build_headless_multi_zone exits scheduler.installed() before returning,
    # so unload + advance need their own patched context.
    with scheduler.installed():
        scheduler.advance_to(t0 + timedelta(seconds=_UNLOAD_AT_S))

        # Positive controls: the assertions below are only meaningful if (a) a refresh
        # really reaches _async_update_data on the live instance (a no-op refresh, e.g.
        # from a test module swapping the coordinator's base class, made T1 pass on
        # 1df0246) and (b) the FakeScheduler captured the 300 s startup timer, i.e.
        # the coordinator code running is the module the scheduler patched.
        update_calls.clear()
        run_coro(old_coordinator.async_request_refresh())
        assert len(update_calls) == 1, "harness refresh is a no-op: T1 would pass vacuously"
        live_before_unload = [_callback_name(e.callback) for e in scheduler.live_callbacks()]
        assert any("_on_startup_coalesce_timer" in name for name in live_before_unload), (
            f"startup timer not captured by FakeScheduler: T1 would pass vacuously ({live_before_unload})"
        )

        run_coro(async_unload_entry_like_ha(fake_hass, zone["entry"]))

        unloaded_at = scheduler.now()
        update_calls.clear()
        actions_at_unload = len(fake_hass.action_log)
        armed_at_unload = len(scheduler.armed_log)
        live_after_unload = [
            f"{_callback_name(e.callback)} @ +{(e.fire_at - t0).total_seconds():.0f}s"
            for e in scheduler.live_callbacks()
        ]

        scheduler.advance_to(t0 + timedelta(seconds=_ADVANCE_PAST_S))

    new_actions = fake_hass.action_log[actions_at_unload:]
    new_timers = [
        f"{_callback_name(cb)} armed @ +{(armed - t0).total_seconds():.0f}s"
        for armed, _fire_at, cb in scheduler.armed_log[armed_at_unload:]
    ]
    context = (
        f"unloaded at +{(unloaded_at - t0).total_seconds():.0f}s; live timers right after unload: {live_after_unload}"
    )

    # Collect all three so one run shows every way the dead instance stayed alive.
    failures = []
    if update_calls:
        failures.append(
            f"dead coordinator ran _async_update_data {len(update_calls)}x after unload at "
            f"{[f'+{(t - t0).total_seconds():.0f}s' for t in update_calls]}"
        )
    if new_actions:
        failures.append(
            f"dead engine made {len(new_actions)} service call(s) after unload: "
            f"{[(a['domain'], a['service'], a['data'].get('entity_id')) for a in new_actions]}"
        )
    if new_timers:
        failures.append(f"dead instance armed {len(new_timers)} timer(s) after unload: {new_timers}")
    assert not failures, "\n".join([context, *failures])
