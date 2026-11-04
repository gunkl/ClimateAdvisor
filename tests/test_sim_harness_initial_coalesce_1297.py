"""Issue #1297: the harness's own start-up ends start-up coalescing after 300 s, as in production.

Occupant view: when Climate Advisor starts it ignores the occupant's thermostat changes for the first
300 s only (Issue #321), then re-applies the plan (owner decision #1235: a change made inside the
window is overwritten) and from then on treats every change as the occupant's. A coordinator scenario
that keeps the window open (``skip_startup_coalesce`` false) used to keep ignoring the occupant for the
whole run unless it dispatched an explicit ``startup_coalesce`` event. This is the initial-start mirror
of Issue #1291 (``simulate_restart``).

The coordinator starts one second before the first event (``scenario_start_time``), so its window ends
at 14:04:59 for a first event at 14:00:00.
"""

from __future__ import annotations

import logging

from tools.sim_harness.session import drive_scenario, make_session

_DAY = "2026-06-10T"


def _ev(t: str, **kw):
    return {"time": _DAY + t, **kw}


def _run(events):
    scenario = {
        "name": "initial_coalesce_ends_1297",
        "use_coordinator": True,
        "config": {"comfort_heat": 68, "comfort_cool": 74, "setback_heat": 60, "setback_cool": 80},
        "events": [
            _ev("14:00:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
            _ev("14:00:00", type="classification", day_type="hot", today_high=90, hvac_mode="cool"),
            *events,
        ],
    }
    logging.disable(logging.CRITICAL)
    try:
        session = make_session(scenario, engine="fakehass", use_coordinator=True)
        try:
            drive_scenario(session, scenario)
            log = [(e[0], e[2]) for e in session.event_log]
            acts = [(a["service"], a["data"].get("hvac_mode"), a["ts"]) for a in session.fake_hass.action_log]
            coalesce_active = session.coordinator._startup_coalesce_active
        finally:
            session.close()
    finally:
        logging.disable(logging.NOTSET)
    return log, acts, coalesce_active


def _times(log, kind):
    return [ts.strftime("%H:%M:%S") for k, ts in log if k == kind]


def test_change_inside_window_is_ignored_then_overwritten_at_300s():
    """Occupant sets heat 60 s after start-up: not an override; the plan (cool) is re-applied at +300 s."""
    log, acts, active = _run(
        [
            _ev("14:01:00", type="thermostat_state_changed", hvac_mode="heat"),
            _ev("14:06:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
        ]
    )
    assert _times(log, "override_detected") == [], log
    assert _times(log, "startup_coalesced") == ["14:04:59"], log
    assert active is False
    recool = [a for a in acts if a[0] == "set_hvac_mode" and a[1] == "cool" and a[2].strftime("%H:%M:%S") == "14:04:59"]
    assert recool, f"the plan was not re-applied at the end of the window: {acts}"


def test_change_after_window_is_detected_as_override():
    """Occupant sets heat 400 s after start-up: the window is over, so it is the occupant's override."""
    log, _acts, active = _run(
        [
            _ev("14:06:40", type="thermostat_state_changed", hvac_mode="heat"),
            _ev("14:20:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
        ]
    )
    assert active is False
    assert _times(log, "startup_coalesced") == ["14:04:59"], log
    assert "14:06:40" in _times(log, "override_detected"), log


def test_explicit_coalesce_at_the_timer_instant_runs_once():
    """A scenario's startup_coalesce at the very instant the window ends is the same refresh: coalesce once."""
    log, _acts, active = _run([_ev("14:04:59", type="startup_coalesce"), _ev("14:10:00", type="startup_coalesce")])
    assert active is False
    assert _times(log, "startup_coalesced") == ["14:04:59"], log


def test_explicit_coalesce_before_300s_ends_the_window_and_the_timer_does_nothing():
    """An explicit startup_coalesce inside the window ends it; the +300 s timer then finds the gate false."""
    log, _acts, active = _run(
        [_ev("14:04:00", type="startup_coalesce"), _ev("14:06:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0)]
    )
    assert active is False
    assert _times(log, "startup_coalesced") == ["14:04:00"], log
