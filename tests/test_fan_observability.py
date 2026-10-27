"""Issue #1104: Activity-Report observability for the HVAC fan + self-reported grace churn.

Drive the REAL coordinator/engine through the headless harness. Each test states the occupant-facing
outcome: the exported Activity Report must show what CA did (or deliberately did not do) with the fan,
and a repeating grace must be reported once as an incident instead of staying silent.
"""

from __future__ import annotations

import logging

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()


def _thermo(indoor, fan_attr, action, state="heat"):
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


def _build(fan_mode, *, fan_attr="auto", action="idle", indoor=72.0):
    co, fh, sch, ev = build_headless_coordinator(
        config={"fan_mode": fan_mode, "comfort_heat": 70, "comfort_cool": 78}, skip_startup_coalesce=True
    )
    co._read_live_remote_timer_provenance = lambda: None
    co._resolved_sensors = []
    ce = co.config["climate_entity"]
    fh.states.set(ce, _thermo(indoor, fan_attr, action))
    return co, fh, sch, ev, ce


def _edge(fh, sch, ce, action, minutes=1, fan_attr="auto"):
    with sch.installed():
        sch.advance_by(60 * minutes)
        fh.states.async_set(ce, "heat", dict(_thermo(72.0, fan_attr, action).attributes))
        sch.advance_to(sch.now())


def _types(ev):
    return [e[0] for e in ev]


def _churn(ev):
    return [e[1] for e in ev if e[0] == "incident_detected" and e[1].get("incident_class") == "grace_churn"]


def test_observed_start_stop_disabled_once_per_edge():
    """Disabled install, Ecobee schedule blower: the report shows started then stopped exactly once each."""
    co, fh, sch, ev, ce = _build("disabled")
    _edge(fh, sch, ce, "fan")
    _edge(fh, sch, ce, "fan")  # same state reported again: no new edge
    _edge(fh, sch, ce, "idle", minutes=8)
    t = [x for x in _types(ev) if x.startswith("fan_")]
    assert t == ["fan_observed_started", "fan_observed_stopped"], t
    started = next(e for e in ev if e[0] == "fan_observed_started")[1]
    stopped = next(e for e in ev if e[0] == "fan_observed_stopped")[1]
    assert started["origin"] == "thermostat_schedule" and started["fan_mode_config"] == "disabled"
    assert started["thermostat_fan_mode"] == "auto" and started["hvac_action"] == "fan"
    assert stopped["duration_minutes"] >= 8


def test_observed_post_heat_tail_origin_hvac_fan():
    """hvac_fan: the blower tail after a heating cycle is labelled post_heat_tail, still never adopted."""
    co, fh, sch, ev, ce = _build("hvac_fan")
    _edge(fh, sch, ce, "heating")
    _edge(fh, sch, ce, "fan")
    _edge(fh, sch, ce, "idle", minutes=3)
    started = [e[1] for e in ev if e[0] == "fan_observed_started"]
    assert len(started) == 1 and started[0]["origin"] == "post_heat_tail"
    assert _types(ev).count("fan_observed_stopped") == 1
    assert co.automation_engine._fan_active is False


def test_ca_owned_fan_emits_no_observed_events():
    """A fan CA commanded itself is activity-reported as fan_activated, never as 'observed'."""
    co, fh, sch, ev, ce = _build("hvac_fan")
    with sch.installed():
        run_coro(co.automation_engine._activate_fan(reason="test"))
        sch.advance_to(sch.now())
    _edge(fh, sch, ce, "fan", fan_attr="on")
    assert "fan_observed_started" not in _types(ev)


def test_renderers_registered_and_render_text():
    from custom_components.climate_advisor import ai_skills_context as c

    for name in ("fan_observed_started", "fan_observed_stopped", "fan_ownership_self_healed", "fan_orphan_released"):
        assert name in c.EVENT_RENDERERS, name
    label, settings = c.EVENT_RENDERERS["fan_observed_started"](
        {
            "origin": "post_heat_tail",
            "hvac_action": "fan",
            "thermostat_fan_mode": "auto",
            "fan_mode_config": "disabled",
        },
        "F",
    )
    assert "observed" in label and "post heat tail" in settings


def test_self_heal_emits_event_and_clears_flag():
    """The 30-min invariant announces a cleaned-up stale ownership flag (no grace, no command)."""
    co, fh, sch, ev, ce = _build("hvac_fan")
    ae = co.automation_engine
    ae._fan_active = True
    ae._fan_on_since = sch.now().isoformat()
    with sch.installed():
        sch.advance_by(40 * 60)
        co._self_heal_fan_ownership()
    assert ae._fan_active is False
    healed = [e[1] for e in ev if e[0] == "fan_ownership_self_healed"]
    assert len(healed) == 1 and healed[0]["was_on_since"] and healed[0]["fan_mode_config"] == "hvac_fan"


def test_orphan_release_emits_event():
    """Fan control disabled while CA left the thermostat fan on: the report says CA released it."""
    co, fh, sch, ev, ce = _build("disabled", fan_attr="on", action="fan")
    ae = co.automation_engine
    with sch.installed():
        ae.restore_state({"fan_active": True, "fan_on_since": sch.now().isoformat()})
        run_coro(co._do_startup_coalesce())
        sch.advance_to(sch.now())
    rel = [e[1] for e in ev if e[0] == "fan_orphan_released"]
    assert len(rel) == 1 and rel[0]["old_fan_mode"] == "on" and rel[0]["command_sent"] == "auto"


def test_floor_rule_skip_logged_once_per_session(caplog):
    """A CA circulation fan below the comfort floor is kept running; the log says why, once per session."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    co, fh, sch, ev, ce = _build("hvac_fan", indoor=66.0)
    ae = co.automation_engine
    with sch.installed():
        run_coro(ae._activate_fan(reason="min_runtime_cycle"))
        sch.advance_to(sch.now())
        for _ in range(4):
            sch.advance_by(60)
            run_coro(ae.fan_thermostat_check(indoor=66.0, outdoor=58.0, trigger="tick"))
    msgs = [r.getMessage() for r in caplog.records if "circulation fan kept running" in r.getMessage()]
    assert len(msgs) == 1, msgs
    assert ae._fan_active is True


def test_grace_churn_fires_on_third_same_trigger_within_hour_and_rate_limits():
    """The same automation grace restarting every few minutes is reported once (then silent for 6 h)."""
    co, fh, sch, ev, ce = _build("disabled")
    ae = co.automation_engine
    with sch.installed():
        for _ in range(5):
            ae._start_grace_period("automation", trigger="nat_vent_exit_resume")
            sch.advance_by(60 * 5)
            sch.advance_to(sch.now())
    inc = _churn(ev)
    assert len(inc) == 1, inc
    assert inc[0]["trigger"] == "nat_vent_exit_resume" and inc[0]["count"] == 3 and inc[0]["window_minutes"] == 60
    assert inc[0]["fan_mode_config"] == "disabled" and inc[0]["first_time"] < inc[0]["last_time"]
    from custom_components.climate_advisor.ai_skills_context import _render_incident_detected

    label, settings = _render_incident_detected(dict(inc[0]), "F")
    assert "grace_churn" in label and "nat_vent_exit_resume" in settings


def test_grace_churn_not_for_normal_daily_graces():
    """A single manual-override grace, two door-close resumes hours apart, two quick exits: no incident."""
    co, fh, sch, ev, ce = _build("disabled")
    ae = co.automation_engine
    with sch.installed():
        ae.handle_fan_manual_override(fan_before="auto", fan_after="on")
        sch.advance_by(3600 * 4)
        sch.advance_to(sch.now())
        for _ in range(2):
            ae._start_grace_period("automation", trigger="sensor_closed_resume")
            sch.advance_by(3600 * 2)
            sch.advance_to(sch.now())
        for _ in range(2):  # two within an hour is below the threshold
            ae._start_grace_period("automation", trigger="nat_vent_exit_resume")
            sch.advance_by(60 * 10)
            sch.advance_to(sch.now())
    assert not _churn(ev)


def test_grace_churn_window_prunes_old_starts():
    """Starts 31 minutes apart never put 3 inside one 60-minute window."""
    co, fh, sch, ev, ce = _build("disabled")
    ae = co.automation_engine
    with sch.installed():
        for _ in range(4):
            ae._start_grace_period("automation", trigger="fan_off")
            sch.advance_by(60 * 31)
            sch.advance_to(sch.now())
    assert not _churn(ev)
