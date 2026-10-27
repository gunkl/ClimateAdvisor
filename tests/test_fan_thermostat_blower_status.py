"""Issue #1103/#1104: one story for a thermostat-schedule blower ("running (thermostat)"), no untracked event.

A thermostat's own circulation (Ecobee fan-min-on-time, post-heat tail) is observed, not an anomaly: the
occupant's Activity Report shows fan_observed_started/stopped only. A fan that is on for any OTHER unowned
reason (fan_mode attribute reads "on" with no CA command; a WHF entity on with no CA record) must STILL be
surfaced as "running (untracked)" with its event and reconcile.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.climate_advisor.fan_status import is_ca_fan_running
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()


def _thermo(fan_attr, action, indoor=72.0, state="heat"):
    return FakeState(
        state=state,
        attributes={
            "current_temperature": indoor,
            "fan_mode": fan_attr,
            "hvac_action": action,
            "fan_modes": ["auto", "on"],
            "hvac_modes": ["off", "heat", "cool"],
            "temperature": 70.0,
        },
    )


def _build(fan_mode, fan_attr, action, **extra):
    cfg = {"fan_mode": fan_mode, "comfort_heat": 70, "comfort_cool": 78}
    cfg.update(extra)
    co, fh, sch, ev = build_headless_coordinator(config=cfg, skip_startup_coalesce=True)
    co._read_live_remote_timer_provenance = lambda: None
    co._resolved_sensors = []
    fh.states.set(co.config["climate_entity"], _thermo(fan_attr, action))
    return co, fh, sch, ev


def _refresh(co, sch):
    with sch.installed():
        run_coro(co._async_update_data())
        sch.advance_to(sch.now())


def _names(ev):
    return [e[0] for e in ev]


def test_thermostat_schedule_blower_is_not_untracked_and_not_reconciled():
    """Ecobee circulation in hvac_fan: status is non-anomalous, no untracked event, no backstop reconcile."""
    for mode in ("hvac_fan", "both"):
        co, fh, sch, ev = _build(mode, "auto", "fan")
        co.automation_engine.reconcile_fan_on_startup = AsyncMock()
        assert co._compute_fan_status() == "running (thermostat)", mode
        assert co._compute_hvac_fan_status() == "running (thermostat)", mode
        _refresh(co, sch)
        assert "fan_running_untracked" not in _names(ev), mode
        assert not co.automation_engine.reconcile_fan_on_startup.await_args_list, mode


def test_thermostat_status_counts_as_running_not_ca_owned():
    co, fh, sch, ev = _build("hvac_fan", "auto", "fan")
    status = co._compute_fan_status()
    assert co._fan_is_running(status) and co._fan_physically_running(status)
    assert is_ca_fan_running(status)  # keeps the contradiction-warning/chart behaviour of the old untracked label
    assert co.automation_engine._fan_active is False


def test_manual_fan_on_missed_by_detector_is_still_untracked_with_event_and_reconcile():
    """fan_mode attribute reads 'on' and CA has no command record: still 'running (untracked)' + event + reconcile."""
    co, fh, sch, ev = _build("hvac_fan", "on", "fan")
    co.automation_engine.reconcile_fan_on_startup = AsyncMock()
    assert co._compute_fan_status() == "running (untracked)"
    assert co._compute_hvac_fan_status() == "running (untracked)"
    _refresh(co, sch)
    assert "fan_running_untracked" in _names(ev)
    assert co.automation_engine.reconcile_fan_on_startup.await_args_list
    assert co.automation_engine.reconcile_fan_on_startup.await_args_list[0].kwargs["trigger"] == "backstop_30min"


def test_manual_on_with_idle_action_is_untracked_too():
    co, fh, sch, ev = _build("hvac_fan", "on", "idle")
    assert co._compute_fan_status() == "running (untracked)"


def test_whf_untracked_unchanged():
    """A whole-house-fan entity that is on with no CA record stays 'running (untracked)'."""
    co, fh, sch, ev = _build("whole_house_fan", "auto", "idle", fan_entity="fan.whf", fan_state_feedback=True)
    co._get_fan_physical_state = lambda: True
    assert co._compute_fan_status() == "running (untracked)"
    assert co._compute_whf_status() == "running (untracked)"


def test_disabled_mode_unchanged():
    co, fh, sch, ev = _build("disabled", "auto", "fan")
    assert co._compute_fan_status() == "disabled"
    _refresh(co, sch)
    assert "fan_running_untracked" not in _names(ev)


def test_ca_owned_fan_still_active_not_thermostat():
    co, fh, sch, ev = _build("hvac_fan", "auto", "fan")
    with sch.installed():
        run_coro(co.automation_engine._activate_fan(reason="test"))
        sch.advance_to(sch.now())
    assert co._compute_fan_status() in ("active", "active (unconfirmed)")


def test_classifier_requires_no_ca_record():
    """The shared classifier is false whenever CA has a command/session/override record."""
    co, fh, sch, ev = _build("hvac_fan", "auto", "fan")
    ae = co.automation_engine
    assert ae.is_thermostat_schedule_blower() is True
    ae._fan_override_active = True
    assert ae.is_thermostat_schedule_blower() is False
    ae._fan_override_active = False
    ae._natural_vent_active = True
    assert ae.is_thermostat_schedule_blower() is False
    ae._natural_vent_active = False
    ae._fan_command_pending = True
    assert ae.is_thermostat_schedule_blower() is False
