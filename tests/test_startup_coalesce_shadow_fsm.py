"""Tests for Issue #707: restart-resume with a surviving RF fan timer never fed
the override/grace shadow FSM tracker.

Background: ``coordinator._do_startup_coalesce()`` calls
``reconcile_fan_on_startup()`` wrapped in ``_mirror_to_shadow("reconcile_fan_on_startup",
...)``. ``"reconcile_fan_on_startup"`` is not a key in
``_OVERRIDE_GRACE_FSM_EVENT_KINDS``, so the mirror dispatch never feeds the
override/grace shadow tracker for that outer call. When a live RF remote fan
timer survives an HA restart, ``AutomationEngine._reconcile_fan_on_startup_locked()``
calls ``handle_fan_manual_override()`` INTERNALLY (automation.py ~5005-5022) —
this correctly updates production's real override/grace flags, but that inner
call is invisible to the mirror dispatch (which only inspects the outer method
name), so the shadow tracker was left permanently stuck reporting
``(idle, none)`` after every such restart. Confirmed live 2026-08-20 06:54-07:11:
production=idle/active_protecting_override, fsm=idle/none, sustained 993s+.

Occupant impact: none — production's real override/grace flags are correct
either way, since ``_resolve_override_grace_fsm_state()`` runs correctly inside
``handle_fan_manual_override()`` regardless of this bug. This is purely a
diagnostic-signal gap (the live-verification comparison signal an upcoming
combined observation window depends on being trustworthy), not a
production-behavior bug.

Fix: ``_do_startup_coalesce()`` now feeds
``self._evaluate_override_grace_fsm(OverrideGraceFsmEventKind.FAN_OVERRIDE_DETECTED)``
directly, gated on the exact same condition
(``remote_timer_provenance is not None and thermostat_fan_running``)
``_reconcile_fan_on_startup_locked()`` uses to decide whether to call
``handle_fan_manual_override()`` — so it only fires when the inner override
call actually happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind  # noqa: E402
from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState  # noqa: E402

# Issue #685: build_headless_coordinator's stub environment leaves
# homeassistant.util.dt.now() returning a MagicMock — _update_shadow_engine_
# diagnostic()'s wall-clock debounce arithmetic needs a real, fixed datetime
# when called directly outside a _mirror_to_shadow()-driven cycle.
_FIXED_NOW = datetime(2026, 8, 20, 7, 0, 0, tzinfo=UTC)


def _run(coro):
    return run_coro(coro)


def _build_coord_with_fan_running(remote_timer_provenance):
    """Build a headless coordinator whose thermostat reports a running fan and
    whose live remote-timer-provenance read is stubbed to the given value."""
    coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(
        skip_startup_coalesce=True,
    )
    climate_entity = coordinator.config["climate_entity"]
    fake_hass.states.set(
        climate_entity,
        FakeState(
            state="off",
            attributes={"fan_mode": "on", "hvac_action": "fan", "hvac_modes": ["off", "heat", "cool"]},
        ),
    )
    coordinator._resolved_sensors = []  # no windows open — isolate the fan-reconcile path
    coordinator._read_live_remote_timer_provenance = lambda: remote_timer_provenance
    return coordinator, fake_hass, scheduler, event_log


class TestRestartResumeRfTimerFeedsShadowFsm:
    def test_rf_timer_survives_restart_feeds_fan_override_detected(self) -> None:
        """The exact live bug scenario: a valid live RF timer survives restart, the
        fan is still physically running — reconcile_fan_on_startup() internally
        re-arms the manual override via handle_fan_manual_override(). Without the
        fix, coordinator._override_grace_fsm_state stays stuck at (idle, none)."""
        coordinator, _fake_hass, scheduler, _event_log = _build_coord_with_fan_running(
            remote_timer_provenance=(25200.0, 8.0)
        )
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

        with scheduler.installed():
            _run(coordinator._do_startup_coalesce())
            scheduler.advance_to(scheduler.now())

        # Production's real flags were correctly set by the inner
        # handle_fan_manual_override() call regardless of this bug.
        ae = coordinator.automation_engine
        assert ae._fan_override_active is True
        assert ae._grace_active is True
        assert ae._grace_protects_override is True

        # The fix under test: the shadow tracker now reflects that real state
        # instead of staying stuck at (idle, none).
        assert coordinator._override_grace_fsm_state == (
            OverrideConfirmState.IDLE,
            GraceState.ACTIVE_PROTECTING_OVERRIDE,
        )

    def test_diagnostic_agrees_after_rf_timer_restart(self) -> None:
        """The live-verification diagnostic (what the upcoming combined observation
        window depends on) must report agreement, not a stuck disagreement.

        The diagnostic snapshot itself is only recomputed inside
        ``_mirror_to_shadow()``'s finally block (i.e. on the next coordinator
        update cycle's mirrored call) — the fix's direct FSM feed in
        ``_do_startup_coalesce()`` updates ``_override_grace_fsm_state`` immediately
        (asserted above) but the diagnostic snapshot itself lags to that next
        cycle by design, same as every other coordinator-originated FSM feed
        (``_check_orphaned_grace()``). Calling ``_update_shadow_engine_diagnostic()``
        directly here simulates that next cycle's snapshot."""
        coordinator, _fake_hass, scheduler, _event_log = _build_coord_with_fan_running(
            remote_timer_provenance=(25200.0, 8.0)
        )

        with scheduler.installed():
            _run(coordinator._do_startup_coalesce())
            scheduler.advance_to(scheduler.now())

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["override_grace_production_state"] == "idle/active_protecting_override"
        assert diag["override_grace_fsm_state"] == "idle/active_protecting_override"
        assert diag["override_grace_fsm_agrees"] is True

    def test_normal_coalesce_no_timer_no_spurious_fsm_feed(self) -> None:
        """Control case: no live remote timer provenance and no fan running — the
        plain 'nothing to reconcile' path. The FSM tracker must stay at its
        initial (idle, none) state — no spurious event feed for the ordinary,
        no-active-fan-timer restart path."""
        coordinator, fake_hass, scheduler, _event_log = build_headless_coordinator(
            skip_startup_coalesce=True,
        )
        climate_entity = coordinator.config["climate_entity"]
        fake_hass.states.set(
            climate_entity,
            FakeState(state="off", attributes={"fan_mode": "off", "hvac_modes": ["off", "heat", "cool"]}),
        )
        coordinator._resolved_sensors = []
        coordinator._read_live_remote_timer_provenance = lambda: None
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

        with scheduler.installed():
            _run(coordinator._do_startup_coalesce())
            scheduler.advance_to(scheduler.now())

        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

    def test_provenance_present_but_fan_not_running_no_spurious_feed(self) -> None:
        """Provenance is live and valid, but the fan already reads off — the inner
        RF-timer branch is deliberately skipped (thermostat_fan_running gate), so
        this must not feed FAN_OVERRIDE_DETECTED either."""
        coordinator, fake_hass, scheduler, _event_log = build_headless_coordinator(
            skip_startup_coalesce=True,
        )
        climate_entity = coordinator.config["climate_entity"]
        fake_hass.states.set(
            climate_entity,
            FakeState(state="off", attributes={"fan_mode": "off", "hvac_modes": ["off", "heat", "cool"]}),
        )
        coordinator._resolved_sensors = []
        coordinator._read_live_remote_timer_provenance = lambda: (25200.0, 8.0)

        with scheduler.installed():
            _run(coordinator._do_startup_coalesce())
            scheduler.advance_to(scheduler.now())

        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

    def test_called_with_explicit_event_kind_only_when_gate_passes(self) -> None:
        """Intercept _evaluate_override_grace_fsm directly (same technique as
        tests/test_override_grace_fsm_shadow_wiring.py) to pin the exact event kind
        fired, independent of the FSM's own transition table."""
        coordinator, _fake_hass, scheduler, _event_log = _build_coord_with_fan_running(
            remote_timer_provenance=(25200.0, 8.0)
        )
        called: list[OverrideGraceFsmEventKind] = []
        original = coordinator._evaluate_override_grace_fsm

        def _capture(event_kind):
            called.append(event_kind)
            return original(event_kind)

        coordinator._evaluate_override_grace_fsm = _capture

        with scheduler.installed():
            _run(coordinator._do_startup_coalesce())
            scheduler.advance_to(scheduler.now())

        assert OverrideGraceFsmEventKind.FAN_OVERRIDE_DETECTED in called
