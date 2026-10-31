"""Issue #707 (ported after Issue #1152): restart-resume with a surviving RF fan timer re-arms the override.

The original file asserted the coordinator's shadow tracker (deleted in #1152 as dead diagnostics). What
matters to the occupant -- and what was always true of production -- is the engine's own override/grace
state after ``_do_startup_coalesce()``: a valid live RF timer plus a running fan must come back as a
protecting manual grace; no timer, or a fan that is already off, must not invent one.
"""

from __future__ import annotations

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState  # noqa: E402

_IDLE_NONE = (OverrideConfirmState.IDLE, GraceState.NONE)
_PROTECTING = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)


def _coalesce(coordinator, scheduler) -> None:
    with scheduler.installed():
        run_coro(coordinator._do_startup_coalesce())
        scheduler.advance_to(scheduler.now())


def _build(*, fan_running: bool, provenance, window_open: bool):
    coordinator, fake_hass, scheduler, _log = build_headless_coordinator(skip_startup_coalesce=True)
    fake_hass.states.set(
        coordinator.config["climate_entity"],
        FakeState(
            state="off",
            attributes={
                "fan_mode": "on" if fan_running else "off",
                "hvac_action": "fan" if fan_running else "off",
                "hvac_modes": ["off", "heat", "cool"],
            },
        ),
    )
    coordinator._resolved_sensors = ["binary_sensor.window"] if window_open else []
    if window_open:
        fake_hass.states.set("binary_sensor.window", FakeState(state="on", attributes={}))
    coordinator._read_live_remote_timer_provenance = lambda: provenance
    return coordinator, scheduler


class TestRestartResumeRfTimer:
    def test_live_rf_timer_and_running_fan_come_back_as_a_protecting_manual_grace(self) -> None:
        coordinator, scheduler = _build(fan_running=True, provenance=(25200.0, 8.0), window_open=True)
        ae = coordinator.automation_engine
        assert ae.override_grace_lifecycle_state == _IDLE_NONE

        _coalesce(coordinator, scheduler)

        assert ae._fan_override_active is True
        assert ae._grace_active is True
        assert ae._grace_protects_override is True
        assert ae.override_grace_lifecycle_state == _PROTECTING

    def test_control_no_timer_no_fan_invents_no_grace(self) -> None:
        coordinator, scheduler = _build(fan_running=False, provenance=None, window_open=False)

        _coalesce(coordinator, scheduler)

        ae = coordinator.automation_engine
        assert ae._grace_active is False and ae._fan_override_active is False
        assert ae.override_grace_lifecycle_state == _IDLE_NONE

    def test_control_timer_present_but_fan_already_off_invents_no_grace(self) -> None:
        coordinator, scheduler = _build(fan_running=False, provenance=(25200.0, 8.0), window_open=False)

        _coalesce(coordinator, scheduler)

        ae = coordinator.automation_engine
        assert ae._grace_active is False and ae._fan_override_active is False
        assert ae.override_grace_lifecycle_state == _IDLE_NONE
