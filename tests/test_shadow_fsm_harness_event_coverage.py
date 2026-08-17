"""Regression test for Issue #666: coordinator test harness silently dropped
event-driven shadow-FSM feed for every engine-originated event.

Live incident: ``Nat-vent FSM disagreement (Issue #633)`` and
``Door/window FSM disagreement (Issue #637)`` warnings fired continuously in
production starting 2026-08-15 06:30:43, surviving multiple restarts, with zero
occupant impact (production's real pause/nat-vent state was always correct — this
is a shadow-diagnostic-only bug). Root cause investigation found the true first
trigger: ``nat_vent_temperature_check()``'s hard-floor exit path, called while a
monitored sensor was open.

Investigation proved two separate things, both covered here:

1. ``door_window_fsm.py``'s pure transition logic is correct for this exact
   transition (``NAT_VENT_EXITED_SENSOR_STILL_OPEN`` from ``NORMAL`` -> ``PAUSED_IDLE``
   given real-world inputs) — already covered by ``tests/test_door_window_fsm.py``'s
   unit-level table, not re-proven here.
2. ``tools/sim_harness/build_coordinator.py`` itself broke the ability to test the
   *dispatch* path: it pointed ``automation_engine._emit_event_callback`` at a bare
   local function instead of the real ``coordinator._emit_event`` (the only place
   ``_feed_lifecycle_fsms_from_event()`` — and therefore every event-type-keyed FSM
   evaluator — is invoked in production). Every coordinator-level Tier A test,
   including ``test_shadow_engine_live.py``, was silently unable to exercise this
   path. This is why Issue #647 (merged the day before this incident, explicitly
   about closing this exact coverage gap) could ship green and still leave the bug
   class unresolved.

This test replays the real incident sequence through the REAL, now-fixed harness
wiring and asserts the shadow FSM actually reaches the same lifecycle state
production's own live flags derive — the assertion #647's own test suite could
never have made honestly before the harness fix.
"""

from __future__ import annotations

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.door_window_lifecycle import (  # noqa: E402
    DoorWindowLifecycleInputs,
    DoorWindowLifecycleState,
    derive_door_window_lifecycle_state,
)


def _build_incident_coordinator():
    """Reconstructs the live 2026-08-15 06:30:43 incident's starting conditions:
    a monitored sensor open, an active nat-vent session, HVAC mode off, indoor
    temperature at the comfort-heat hard floor."""
    config = {
        "door_window_sensors": ["binary_sensor.front_door"],
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
        "fan_mode": "whole_house_fan",
    }
    coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
        config=config,
        climate_state="off",
        skip_startup_coalesce=True,
    )
    ae = coordinator.automation_engine

    # Silent seed (set_simple, not async_set): models a sensor that was ALREADY
    # open before this coordinator instance started — no fresh "door opened"
    # state-change event ever fires, so door_window_fsm never receives a
    # SENSOR_OPENED event. This is the real incident's actual shape (matches
    # _sync_paused_by_door_with_live_sensors()'s own documented "sensor open
    # since before any event-driven path ever ran" class of gap, Issue #620) —
    # using async_set() here would dispatch a real listener and pause the
    # coordinator via the SENSOR_OPENED path before nat-vent's own exit event
    # is ever reached, which is a different (already-covered) scenario.
    fake_hass.states.set_simple("binary_sensor.front_door", "on")

    coordinator._resolved_sensors = ["binary_sensor.front_door"]
    ae._natural_vent_active = True
    ae._nat_vent_soft_start = False
    ae._pre_fan_hvac_mode = "off"
    ae._fan_active = True

    return coordinator, fake_hass, scheduler, event_log, ae


class TestHardFloorExitWithSensorOpen:
    """Replays nat_vent_temperature_check()'s real hard-floor exit path — the
    exact trigger identified as the live incident's origin."""

    def test_door_window_fsm_reaches_paused_idle(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_incident_coordinator()

        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL

        with scheduler.installed():
            run_coro(ae.nat_vent_temperature_check(67.0, outdoor=58.5))

        production_state = derive_door_window_lifecycle_state(
            DoorWindowLifecycleInputs(
                paused_by_door=bool(ae._paused_by_door),
                paused_with_hvac_already_off=bool(ae._paused_with_hvac_already_off),
                grace_active=bool(ae._grace_active),
            )
        )
        assert ae._paused_by_door is True, "production's own pause flag must be set — sanity check"
        assert production_state == DoorWindowLifecycleState.PAUSED_IDLE
        assert coordinator._door_window_fsm_state == production_state, (
            f"shadow door/window FSM ({coordinator._door_window_fsm_state}) disagreed with "
            f"production's real derived state ({production_state}) after a hard-floor nat-vent "
            "exit with the sensor open — this is the exact live incident (Issue #666)"
        )

    def test_nat_vent_fsm_reaches_inactive(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_incident_coordinator()

        with scheduler.installed():
            run_coro(ae.nat_vent_temperature_check(67.0, outdoor=58.5))

        assert ae._natural_vent_active is False
        assert coordinator._nat_vent_fsm_state.value == "inactive", (
            f"shadow nat-vent FSM ({coordinator._nat_vent_fsm_state}) disagreed with production's "
            "real _natural_vent_active=False after a hard-floor exit — matches the live "
            "'Nat-vent FSM disagreement (Issue #633)' warning"
        )


class TestHarnessEventFeedPositiveControl:
    """Proves the regression test above is load-bearing: reintroducing the exact
    harness bug found in Issue #666 must make it fail. Without this, a future
    accidental reintroduction of the same bug would pass silently — the same trap
    that let this bug ship unnoticed through Issue #647."""

    def test_bypassing_coordinator_emit_event_reproduces_the_bug(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_incident_coordinator()

        # Reintroduce the Issue #666 bug: point the engine callback at a bare
        # function that never reaches coordinator._emit_event(), exactly as
        # build_coordinator.py did before the fix.
        captured: list[tuple[str, dict]] = []
        ae._emit_event_callback = lambda event_type, data: captured.append((event_type, data))

        with scheduler.installed():
            run_coro(ae.nat_vent_temperature_check(67.0, outdoor=58.5))

        assert ae._paused_by_door is True, "production's own flag is unaffected by the callback bug"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL, (
            "positive control FAILED: bypassing coordinator._emit_event() should reproduce the "
            "live bug (shadow FSM stuck at NORMAL) — if this assertion fails, the harness fix "
            "test above is not actually exercising the code path it claims to"
        )
        assert any(evt[0] == "nat_vent_comfort_floor_exit" for evt in captured), (
            "sanity check: the event really was emitted, just not routed to the FSM feed"
        )
