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


def _build_paused_by_door_coordinator(*, outdoor: float, indoor: float, hvac_already_off: bool):
    """Builds a coordinator already paused by an open door/window (PAUSED_IDLE), for
    exercising ``check_natural_vent_conditions()`` (Issue #668).

    ``hvac_already_off`` selects which of the two structurally-distinct reactivation
    code paths inside ``check_natural_vent_conditions()`` is reachable:
      - ``True`` (``_paused_with_hvac_already_off=True``, matching the actual live
        incident — HVAC was already off when the door was noticed open) makes
        ``_actively_paused`` False, which routes into the "idle_open" re-evaluation
        branch (Issue #244/#402/#504, automation.py ~3459-3546) — that branch never
        calls ``_resolve_door_window_pause_flags()`` at all (by design: nat-vent can
        legitimately re-engage through an open door without clearing the pause) and
        unconditionally ``return``s afterward, so the paused-by-door block below it
        (~3830) is never reached this call.
      - ``False`` makes ``_actively_paused`` True, skipping the idle_open branch
        entirely and reaching the paused-by-door reactivation block (~3830-3934) —
        the 2 real call sites this fix's new event emit was added to.
    """
    config = {
        "door_window_sensors": ["binary_sensor.front_door"],
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
        "fan_mode": "whole_house_fan",
    }
    coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
        config=config,
        climate_state="off",
        climate_attributes={"current_temperature": indoor},
        skip_startup_coalesce=True,
    )
    ae = coordinator.automation_engine

    fake_hass.states.set_simple("binary_sensor.front_door", "on")
    coordinator._resolved_sensors = ["binary_sensor.front_door"]
    ae.update_outdoor_temp(outdoor)
    ae._natural_vent_active = False
    ae._nat_vent_soft_start = False
    ae._paused_by_door = True
    ae._paused_with_hvac_already_off = hvac_already_off
    ae._paused_entity = "monitored door/window"

    from custom_components.climate_advisor.door_window_lifecycle import (
        DoorWindowLifecycleState as _DWLState,
    )

    coordinator._door_window_fsm_state = _DWLState.PAUSED_IDLE

    return coordinator, fake_hass, scheduler, event_log, ae


class TestPausedNatVentReactivatedEventCoverage:
    """Issue #668: check_natural_vent_conditions() was, until now, registered as a
    method-name-keyed, UNCONDITIONAL door/window FSM trigger (#660 Step 3) — every
    call, regardless of which internal branch it took, fired
    ``DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED``, which
    ``_transition_from_paused()`` handles unconditionally by resetting to ``NORMAL``.
    On a hot day with a door left open and no imminent nat-vent reactivation, this
    wrongly reset the shadow FSM to ``NORMAL`` every single cycle, moments after
    ``SYNC_RECONCILE`` correctly restored ``PAUSED_IDLE`` — a permanent live
    disagreement (confirmed via HA log timestamps, 2026-08-17). The feed is now
    event-driven: only fired when one of the 2 real, deeply-conditional reactivation
    branches in ``automation.py`` actually runs.
    """

    def test_no_reactivation_leaves_shadow_fsm_paused(self) -> None:
        """The exact live-incident shape: HVAC already off, door open, outdoor far
        above the reactivation threshold — the idle_open branch runs but doesn't
        reactivate, and never touches the paused-by-door block at all. Calls
        production's real ``ae.check_natural_vent_conditions()`` directly (not
        ``_mirror_to_shadow``, which replays on the *shadow* engine and can never
        reach the new event-driven feed — that's fed only from production's own
        ``_emit_event_callback``, wired to ``coordinator._emit_event``). The shadow
        FSM must stay PAUSED_IDLE, not get reset to NORMAL."""
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=85.0, indoor=73.0, hvac_already_off=True
        )

        with scheduler.installed():
            run_coro(ae.check_natural_vent_conditions())
            # Settle any fire-and-forget hass.async_create_task() calls queued by a
            # real HVAC-mode/state change, same pattern build_headless_coordinator()
            # itself uses (Issue #476) — otherwise an unawaited-coroutine warning
            # surfaces at interpreter shutdown, unrelated to this test's assertions.
            scheduler.advance_to(scheduler.now())

        assert ae._paused_by_door is True, "production's own pause flag must be unaffected — sanity check"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.PAUSED_IDLE, (
            f"shadow door/window FSM ({coordinator._door_window_fsm_state}) was wrongly reset — "
            "check_natural_vent_conditions() ran but did not actually reactivate nat-vent, so "
            "the FSM must not have received a PAUSED_NAT_VENT_REACTIVATED event (Issue #668)"
        )

    def test_real_reactivation_still_clears_shadow_fsm(self) -> None:
        """The mirror-image, correctness-preserving direction: HVAC was actively
        paused (not already off) and outdoor is genuinely cool enough to reactivate
        nat-vent while paused — reaching the paused-by-door block's real
        ``_resolve_door_window_pause_flags(kind=PAUSED_NAT_VENT_REACTIVATED, ...)``
        call site this fix's new event emit sits beside. Calls production's real
        engine directly, same reasoning as the sibling test above. The shadow FSM
        must still correctly reach NORMAL, proving the fix isn't just a blanket
        suppression."""
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=58.5, indoor=73.0, hvac_already_off=False
        )

        with scheduler.installed():
            run_coro(ae.check_natural_vent_conditions())
            # Settle any fire-and-forget hass.async_create_task() calls queued by a
            # real HVAC-mode/state change, same pattern build_headless_coordinator()
            # itself uses (Issue #476) — otherwise an unawaited-coroutine warning
            # surfaces at interpreter shutdown, unrelated to this test's assertions.
            scheduler.advance_to(scheduler.now())

        assert ae._paused_by_door is False, "production must have actually reactivated — sanity check"
        assert ae._natural_vent_active is True, "production must have actually reactivated — sanity check"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL, (
            f"shadow door/window FSM ({coordinator._door_window_fsm_state}) disagreed with "
            "production's real reactivation — the new event-driven feed must still fire when "
            "the real branch actually runs (Issue #668)"
        )


class TestPausedNatVentReactivatedPositiveControl:
    """Proves the two tests above are load-bearing: reintroducing the old
    unconditional method-name trigger must make the no-reactivation test fail."""

    def test_reintroducing_unconditional_trigger_reproduces_the_bug(self) -> None:
        import custom_components.climate_advisor.coordinator as _coordinator_mod

        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=85.0, indoor=73.0, hvac_already_off=True
        )

        # Reintroduce the Issue #660/#668 bug: method-name-keyed, unconditional trigger.
        original_kinds = dict(_coordinator_mod._DOOR_WINDOW_FSM_EVENT_KINDS)
        _coordinator_mod._DOOR_WINDOW_FSM_EVENT_KINDS["check_natural_vent_conditions"] = "paused_nat_vent_reactivated"
        try:
            with scheduler.installed():
                run_coro(coordinator._mirror_to_shadow("check_natural_vent_conditions"))
        finally:
            _coordinator_mod._DOOR_WINDOW_FSM_EVENT_KINDS.clear()
            _coordinator_mod._DOOR_WINDOW_FSM_EVENT_KINDS.update(original_kinds)

        assert ae._paused_by_door is True, "production's own flag is unaffected by the dispatch-table bug"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL, (
            "positive control FAILED: reintroducing the unconditional method-name trigger should "
            "reproduce the live bug (shadow FSM wrongly reset to NORMAL) — if this assertion "
            "fails, the tests above are not actually exercising the dispatch table they claim to"
        )


class TestRePauseForOpenSensorReactivationEventCoverage:
    """Issue #676: ``_re_pause_for_open_sensor()`` (the async task ``_on_grace_expired()``
    schedules when grace expires with a sensor still open) has a reactivation branch
    structurally identical to ``check_natural_vent_conditions()``'s own paused-by-door
    reactivation block (covered above by ``TestPausedNatVentReactivatedEventCoverage``) —
    but it was never updated when Issues #647/#660/#668 wired
    ``PAUSED_NAT_VENT_REACTIVATED``/``"nat_vent_reactivated_while_paused"`` into that
    sibling branch. Confirmed live, 2026-08-18: after grace expired with a sensor still
    open, production correctly reactivated nat-vent and cleared its own pause flags, but
    the shadow door/window FSM stuck at ``PAUSED_IDLE`` for 20+ minutes
    (``production=normal fsm=paused_idle``).
    """

    def test_real_reactivation_clears_shadow_fsm(self) -> None:
        """Outdoor genuinely cool enough to reactivate nat-vent while paused — reaches
        the ``_reactivates`` branch's ``_resolve_door_window_pause_flags(kind=
        PAUSED_NAT_VENT_REACTIVATED, ...)`` call this fix added. Calls production's real
        ``ae._re_pause_for_open_sensor()`` directly (not ``_mirror_to_shadow``, which
        replays on the shadow engine and never reaches the event-driven feed — that's
        fed only from production's own ``_emit_event_callback``, wired to
        ``coordinator._emit_event``, same reasoning as the sibling tests above)."""
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=58.5, indoor=73.0, hvac_already_off=False
        )

        with scheduler.installed():
            run_coro(ae._re_pause_for_open_sensor())
            scheduler.advance_to(scheduler.now())

        assert ae._paused_by_door is False, "production must have actually reactivated — sanity check"
        assert ae._natural_vent_active is True, "production must have actually reactivated — sanity check"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL, (
            f"shadow door/window FSM ({coordinator._door_window_fsm_state}) disagreed with "
            "production's real reactivation via _re_pause_for_open_sensor() — the fix's new "
            "event emit must reach the shadow FSM feed (Issue #676)"
        )

    def test_no_reactivation_leaves_shadow_fsm_paused(self) -> None:
        """Outdoor too warm to reactivate — the `_reactivates` branch is never taken, so
        the shadow FSM must stay PAUSED_IDLE, not get reset (mirrors the sibling
        no-reactivation test's correctness-preserving direction)."""
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=85.0, indoor=73.0, hvac_already_off=False
        )

        with scheduler.installed():
            run_coro(ae._re_pause_for_open_sensor())
            scheduler.advance_to(scheduler.now())

        assert ae._paused_by_door is True, "production must not have reactivated — sanity check"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.PAUSED_IDLE, (
            f"shadow door/window FSM ({coordinator._door_window_fsm_state}) was wrongly reset — "
            "_re_pause_for_open_sensor() ran but did not actually reactivate nat-vent"
        )


class TestRePauseForOpenSensorReactivationPositiveControl:
    """Proves the test above is load-bearing: filtering out just the new
    "nat_vent_reactivated_while_paused" emit (simulating the pre-#676 code, which never
    emitted it from this call site) must make the shadow FSM stick at PAUSED_IDLE even
    though production correctly reactivated."""

    def test_filtering_the_new_event_reproduces_the_bug(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log, ae = _build_paused_by_door_coordinator(
            outdoor=58.5, indoor=73.0, hvac_already_off=False
        )

        real_emit = coordinator._emit_event

        def _filtered_emit(event_type: str, data: dict | None = None) -> None:
            if event_type == "nat_vent_reactivated_while_paused":
                return
            real_emit(event_type, data)

        ae._emit_event_callback = _filtered_emit

        with scheduler.installed():
            run_coro(ae._re_pause_for_open_sensor())
            scheduler.advance_to(scheduler.now())

        assert ae._paused_by_door is False, "production's own reactivation is unaffected by the filter"
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.PAUSED_IDLE, (
            "positive control FAILED: filtering out the new event should reproduce the live bug "
            "(shadow FSM stuck at PAUSED_IDLE) — if this assertion fails, the test above is not "
            "actually exercising the new event this fix added"
        )
