"""Tests for Issue #1045: a manually-turned-on whole-house fan survives an HA restart.

Confirmed live 2026-10-09: the occupant turned the WHF on (no RF-remote timer token), an
HA restart a few minutes later cleared CA's override/grace (clean slate), and the startup
reconcile read the still-running fan as unwarranted and turned it off.

These drive the REAL production engine through the headless coordinator harness
(``restore_state()`` of a persisted snapshot, then ``_do_startup_coalesce()``) — nothing
here re-implements the logic under test.
"""

from __future__ import annotations

from datetime import timedelta

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()


def _build(*, fan_running: bool = True, window_open: bool = True, config: dict | None = None):
    coordinator, fake_hass, scheduler, event_log = build_headless_coordinator(config=config, skip_startup_coalesce=True)
    climate_entity = coordinator.config["climate_entity"]
    fake_hass.states.set(
        climate_entity,
        FakeState(
            state="off",
            attributes={
                "fan_mode": "on" if fan_running else "off",
                "hvac_action": "fan" if fan_running else "idle",
                "hvac_modes": ["off", "heat", "cool"],
            },
        ),
    )
    coordinator._resolved_sensors = ["binary_sensor.window"] if window_open else []
    if window_open:
        fake_hass.states.set("binary_sensor.window", FakeState(state="on", attributes={}))
    coordinator._read_live_remote_timer_provenance = lambda: None
    return coordinator, fake_hass, scheduler, event_log


def _persisted(scheduler, *, remaining_s: float, trigger: str = "fan_manual_override") -> dict:
    end = scheduler.now() + timedelta(seconds=remaining_s)
    return {
        "last_resume_source": "manual",
        "last_grace_trigger": trigger,
        "grace_end_time": end.isoformat(),
        "grace_duration_seconds": 10800,
    }


def _restart(coordinator, scheduler, state: dict) -> None:
    """The real restart path: engine.restore_state(persisted) then the startup coalesce."""
    with scheduler.installed():
        coordinator.automation_engine.restore_state(state)
        run_coro(coordinator._do_startup_coalesce())
        scheduler.advance_to(scheduler.now())


def _fan_off_calls(fake_hass) -> list[dict]:
    return [c for c in fake_hass.action_log if "fan" in str(c).lower() and "turn_off" in str(c).lower()]


class TestManualOverrideSurvivesRestart:
    def test_running_fan_with_persisted_manual_grace_is_rearmed_not_turned_off(self) -> None:
        coordinator, fake_hass, scheduler, event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))

        ae = coordinator.automation_engine
        assert ae._fan_override_active is True
        assert ae._grace_active is True
        assert ae._grace_protects_override is True
        assert ae._last_grace_trigger == "fan_manual_override"
        assert ae._reconcile_rearmed_override is True
        assert ae._pending_override_rearm_end is None  # consumed
        assert _fan_off_calls(fake_hass) == []
        # Remaining time honored (~2 h), not reset to the configured 3 h default.
        assert 7100 <= ae._grace_duration_seconds <= 7200

    def test_restored_override_is_tagged_in_activity_event(self) -> None:
        coordinator, _fake_hass, scheduler, event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=3600))

        payloads = [p for (t, p, _ts) in event_log if t == "fan_manual_override"]
        assert payloads and payloads[-1].get("restored_from") == "persisted"

    def test_fresh_press_event_has_no_restored_tag(self) -> None:
        coordinator, _fake_hass, scheduler, event_log = _build()
        with scheduler.installed():
            coordinator.automation_engine.handle_fan_manual_override(fan_before="off", fan_after="on")
        payloads = [p for (t, p, _ts) in event_log if t == "fan_manual_override"]
        assert payloads and "restored_from" not in payloads[-1]

    def test_shadow_fsm_tracker_fed_for_persisted_rearm(self) -> None:
        from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState

        coordinator, _fake_hass, scheduler, _event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=3600))
        assert coordinator._override_grace_fsm_state == (
            OverrideConfirmState.IDLE,
            GraceState.ACTIVE_PROTECTING_OVERRIDE,
        )

    def test_orphaned_grace_watchdog_leaves_rearmed_grace_alone(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=3600))
        with scheduler.installed():
            coordinator._check_orphaned_grace()
        ae = coordinator.automation_engine
        assert ae._grace_active is True
        assert ae._fan_override_active is True


class TestPersistedOverrideDropped:
    def test_expired_while_down_is_clean_slate(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=-60))
        ae = coordinator.automation_engine
        assert ae._fan_override_active is False
        assert ae._reconcile_rearmed_override is False
        assert ae._pending_override_rearm_end is None

    def test_fan_off_while_down_does_not_rearm(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build(fan_running=False)
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))
        ae = coordinator.automation_engine
        assert ae._fan_override_active is False
        assert ae._grace_active is False
        assert ae._reconcile_rearmed_override is False
        assert ae._pending_override_rearm_end is None

    def test_sealed_house_does_not_rearm_and_does_not_feed_shadow_fsm(self) -> None:
        from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState

        coordinator, _fake_hass, scheduler, _event_log = _build(window_open=False)
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))
        ae = coordinator.automation_engine
        assert ae._fan_override_active is False
        assert ae._reconcile_rearmed_override is False
        # #882 sealed-house deactivate path must not claim an override re-arm to the tracker.
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

    def test_other_trigger_is_not_stashed(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build()
        with scheduler.installed():
            coordinator.automation_engine.restore_state(
                _persisted(scheduler, remaining_s=7200, trigger="override_confirmed")
            )
        assert coordinator.automation_engine._pending_override_rearm_end is None

    def test_automation_sourced_grace_is_not_stashed(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build()
        state = _persisted(scheduler, remaining_s=200)
        state["last_resume_source"] = "automation"
        with scheduler.installed():
            coordinator.automation_engine.restore_state(state)
        assert coordinator.automation_engine._pending_override_rearm_end is None


class TestRfTimerPrecedence:
    def test_live_rf_token_wins_over_persisted_stash(self) -> None:
        coordinator, _fake_hass, scheduler, event_log = _build()
        coordinator._read_live_remote_timer_provenance = lambda: (3000.0, 2.0)
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=9000))
        ae = coordinator.automation_engine
        assert ae._fan_remote_timer_hours == 2.0
        assert ae._pending_override_rearm_end is None
        payloads = [p for (t, p, _ts) in event_log if t == "fan_manual_override"]
        assert payloads[-1].get("restored_from") == "rf_timer"
        assert 2900 <= ae._grace_duration_seconds <= 3000


class TestStatusShowsRestoredGraceEnd:
    def test_status_text_reports_grace_end_time(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build()
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))
        with scheduler.installed():
            text = coordinator._format_grace_remaining(coordinator.automation_engine)
        # "— 2h (ends H:MM AM/PM)": end time is present and the duration is the remaining time.
        assert "ends" in text
        assert "2h" in text


_WHF_CONFIG = {
    "fan_mode": "whole_house_fan",
    "fan_entity": "fan.test_whf",
    "fan_state_entity": "input_boolean.test_whf_detectpower",
    "fan_state_feedback": True,
}


def _build_whf(*, physically_on: bool):
    """Whole-house-fan install shaped like the live one: a command entity that stays 'off' plus a
    separate physical power-detect entity — the exact 2026-10-09 17:32 shape. The thermostat's own
    fan attributes deliberately say the fan is idle, so only the WHF physical read can say 'running'."""
    coordinator, fake_hass, scheduler, event_log = _build(fan_running=False, config=_WHF_CONFIG)
    fake_hass.states.set("fan.test_whf", FakeState(state="off", attributes={}))
    fake_hass.states.set(
        "input_boolean.test_whf_detectpower", FakeState(state="on" if physically_on else "off", attributes={})
    )
    return coordinator, fake_hass, scheduler, event_log


class TestWholeHouseFanInstall:
    def test_physically_running_whf_is_rearmed_not_turned_off(self) -> None:
        coordinator, fake_hass, scheduler, event_log = _build_whf(physically_on=True)
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))

        ae = coordinator.automation_engine
        assert ae._fan_override_active is True
        assert ae._grace_active is True
        assert ae._reconcile_rearmed_override is True
        fan_off = [
            c
            for c in fake_hass.action_log
            if c.get("service") == "turn_off" and "fan.test_whf" in str(c.get("data", c))
        ]
        assert fan_off == []
        payloads = [p for (t, p, _ts) in event_log if t == "fan_manual_override"]
        assert payloads[-1].get("restored_from") == "persisted"

    def test_physically_off_whf_is_not_rearmed(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = _build_whf(physically_on=False)
        _restart(coordinator, scheduler, _persisted(scheduler, remaining_s=7200))

        ae = coordinator.automation_engine
        assert ae._fan_override_active is False
        assert ae._reconcile_rearmed_override is False
        assert ae._pending_override_rearm_end is None
