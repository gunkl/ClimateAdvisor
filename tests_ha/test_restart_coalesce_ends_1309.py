"""Real-HA test-tier check for Issue #1309: the start-up window a restart opens ENDS at +300 s.

Occupant story: Home Assistant restarts on a hot afternoon. A minute later the person switches the thermostat to
heat; that change lands inside the 300 s start-up window, so Climate Advisor treats it as start-up noise and the
end-of-window coalesce puts the plan back (owner decision #1235). Later (+400 s, after the window) the person
switches to heat again; that one is theirs and must be seen as a manual override.

Harness gap this guards (test tooling): ``RealHASession`` builds the house with ``weather=False``. Production's
gate that runs ``_do_startup_coalesce()`` sits inside the refresh's ``if forecast:`` block, so in this tier the
window never ended and every later occupant change was ignored for the rest of the run. Mirror of #1291 / #1297
(Tier A). The session now completes that gate after the real refresh the real 300 s timer requests.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.util import dt as dt_util

from tests_ha.realha_session import RealHASession
from tools.sim_harness.scenario_model import parse_event_time, sorted_events

pytestmark = pytest.mark.house

_RESTART = "2026-06-10T15:00:00"
_SCENARIO = {
    "name": "issue_1309_restart_coalesce_ends",
    "use_coordinator": True,
    "skip_startup_coalesce": True,  # the initial start is not under test; the restart opens a real window
    "config": {
        "comfort_heat": 68,
        "comfort_cool": 74,
        "setback_heat": 60,
        "setback_cool": 79,
        "manual_grace_seconds": 6000,
        "wake_time": "04:00",
        "briefing_time": "04:15",
    },
    "events": [
        {"time": "2026-06-10T14:00:00", "type": "temp_update", "indoor_f": 76.0, "outdoor_f": 88.0},
        {
            "time": "2026-06-10T14:01:00",
            "type": "classification",
            "day_type": "hot",
            "today_high": 90,
            "hvac_mode": "cool",
        },
        {"time": _RESTART, "type": "simulate_restart"},
        # +60 s: inside the window -> start-up noise, overwritten by the coalesce at +300 s (#1235)
        {"time": "2026-06-10T15:01:00", "type": "thermostat_state_changed", "hvac_mode": "heat"},
        # +360 s: past the window end
        {"time": "2026-06-10T15:06:00", "type": "temp_update", "indoor_f": 76.0, "outdoor_f": 88.0},
        # +400 s: after the window -> the occupant's own change, an override
        {"time": "2026-06-10T15:06:40", "type": "thermostat_state_changed", "hvac_mode": "heat"},
        {"time": "2026-06-10T15:20:00", "type": "temp_update", "indoor_f": 76.0, "outdoor_f": 88.0},
    ],
}


def _override_events(session: RealHASession, after: datetime) -> list[str]:
    return [
        etype
        for etype, _data, ts in session.event_log
        if etype in ("override_detected", "override_confirmed") and ts is not None and ts >= after
    ]


async def test_restart_window_ends_at_300s(hass, freezer, tmp_path) -> None:
    session = RealHASession(_SCENARIO, hass=hass, freezer=freezer, config_dir=tmp_path)
    restart_at = parse_event_time(_RESTART, dt_util.utcnow())
    try:
        await session.start()
        for event in sorted_events(_SCENARIO):
            await session.advance_to(parse_event_time(event["time"], session.now()))
            await session.feed(event)
            elapsed = (session.now() - restart_at).total_seconds()
            coord = session.coordinator
            if event["time"] == _RESTART:
                assert coord._startup_coalesce_active, "precondition: the restart opened the start-up window"
            elif event["time"] == "2026-06-10T15:01:00":
                assert coord._startup_coalesce_active, f"precondition: +{elapsed:.0f} s is inside the window"
                assert session.thermostat().mode == "heat", "precondition: the occupant's +60 s change took"
            elif event["time"] == "2026-06-10T15:06:00":
                assert not coord._startup_coalesce_active, (
                    f"the start-up window is still open at +{elapsed:.0f} s (production ends it at +300 s)"
                )
                assert session.thermostat().mode == "cool", (
                    f"the +60 s change was not overwritten by the end-of-window coalesce: "
                    f"{session.thermostat().mode} (#1235)"
                )
                assert not _override_events(session, restart_at), "the +60 s change inside the window was not noise"
        detected = _override_events(session, restart_at)
        assert "override_detected" in detected, f"the +400 s change was not detected as an override: {detected}"
        assert session.engine._manual_override_active, "the +400 s change was not confirmed as a manual override"
    finally:
        await session.finish()
