"""Issue #1291: a simulated HA restart's start-up coalescing window ends after 300 s, as in production.

Occupant view: after a real restart Climate Advisor ignores the occupant's thermostat changes for
the first 300 s only (Issue #321), then re-applies the plan (owner decision #1235: a change made
inside the window is overwritten) and from then on treats every change as the occupant's. The
harness's ``simulate_restart`` used to open the window and never close it, so every generated day
with a restart tested a house where the occupant's changes went unseen until midnight.
"""

from __future__ import annotations

import logging

from tools.sim_harness.session import drive_scenario, make_session

_DAY = "2026-06-10T"


def _ev(t: str, **kw):
    return {"time": _DAY + t, **kw}


def _run(events):
    scenario = {
        "name": "restart_coalesce_ends_1291",
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "config": {"comfort_heat": 68, "comfort_cool": 74, "setback_heat": 60, "setback_cool": 80},
        "events": [
            _ev("14:00:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
            _ev("14:01:00", type="classification", day_type="hot", today_high=90, hvac_mode="cool"),
            _ev("16:00:00", type="simulate_restart"),
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
    """Occupant sets heat 60 s after the restart: not an override; the plan (cool) is re-applied at 16:05."""
    log, acts, active = _run(
        [
            _ev("16:01:00", type="thermostat_state_changed", hvac_mode="heat"),
            _ev("16:04:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
            _ev("16:06:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
        ]
    )
    assert _times(log, "override_detected") == [], log
    assert _times(log, "startup_coalesced") == ["16:05:00"], log
    assert active is False
    recool = [a for a in acts if a[0] == "set_hvac_mode" and a[1] == "cool" and a[2].strftime("%H:%M:%S") == "16:05:00"]
    assert recool, f"the plan was not re-applied at the end of the window: {acts}"


def test_change_after_window_is_detected_as_override():
    """Occupant sets heat 400 s after the restart: the window is over, so it is the occupant's override."""
    log, _acts, active = _run(
        [
            _ev("16:06:40", type="thermostat_state_changed", hvac_mode="heat"),
            _ev("16:20:00", type="temp_update", indoor_f=76.0, outdoor_f=88.0),
        ]
    )
    assert active is False
    assert _times(log, "startup_coalesced") == ["16:05:00"], log
    assert "16:06:40" in _times(log, "override_detected"), log
