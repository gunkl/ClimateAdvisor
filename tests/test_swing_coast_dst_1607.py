"""Issue #1607 (the swing.py mirror of #1244): a coast window lasts 30 real minutes across a daylight-saving change.

What the occupant gets: Climate Advisor learns how far the house keeps warming (or cooling) after the heat (or AC)
stops -- the "coast" -- and uses it to stop earlier, so the room lands on the setpoint instead of overshooting. The
coast window opened at a stop is bounded at ``THERMAL_COAST_MAX_S`` (30 minutes). Its age was the raw subtraction of
two local ``dt_util.now()`` values, which ignores the fold:

* fall-back: a heat stop at 01:50 PDT, 35 real minutes later is 01:25 PST, read as -25 minutes. The window stayed
  open past its bound and readings an hour late (weather, not coast) were learned as coast;
* spring-forward: a stop at 01:50 PST, 15 real minutes later is 03:05 PDT, read as 75 minutes. The window was closed
  as timed out at its first reading and the real coast was lost (or recorded with the wrong ending).

``swing.py`` is loaded standalone by tools/, so it uses its own stdlib UTC subtraction (no ``time_utils`` import).
The persisted ``ts`` (the off time's local ISO string) is unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import swing
from custom_components.climate_advisor.const import THERMAL_COAST_MAX_S

_LA = ZoneInfo("America/Los_Angeles")


def _la(y, mo, d, h, mi) -> datetime:
    """The local reading of a UTC instant, exactly what the coordinator's ``dt_util.now()`` passes in."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC).astimezone(_LA)


_FB_OFF, _FB_35 = _la(2026, 11, 1, 8, 50), _la(2026, 11, 1, 9, 25)  # 01:50 PDT -> 01:25 PST, 35 real min
_FB_20 = _la(2026, 11, 1, 9, 10)  # 01:10 PST, 20 real min after the stop
_SF_OFF, _SF_15 = _la(2026, 3, 8, 9, 50), _la(2026, 3, 8, 10, 5)  # 01:50 PST -> 03:05 PDT, 15 real min


def _windows(off: datetime) -> dict:
    return {
        "heat": {
            "ts": off.isoformat(),
            "off_dt": off,
            "off_indoor_f": 70.0,
            "extreme_f": 70.0,
            "setpoint_f": 70.0,
            "n_post": 1,
        }
    }


def test_fixture_pairs_are_wall_clock_wrong():
    assert (_FB_35 - _FB_OFF).total_seconds() == -25 * 60
    assert (_SF_15 - _SF_OFF).total_seconds() == 75 * 60


def test_fall_back_window_times_out_after_thirty_real_minutes():
    windows = _windows(_FB_OFF)
    closed = swing.coast_feed(windows, _FB_35, 71.0)
    assert [(m, ended) for m, ended, _v in closed] == [("heat", swing.COAST_ENDED_TIMEOUT)]
    assert closed[0][2].sample["window_s"] == THERMAL_COAST_MAX_S
    assert closed[0][2].sample["ts"] == _FB_OFF.isoformat(), "the persisted ts format is unchanged"
    assert not windows, "the window is closed"


def test_fall_back_window_inside_its_bound_keeps_folding_readings():
    windows = _windows(_FB_OFF)
    assert swing.coast_feed(windows, _FB_20, 71.0) == []
    assert windows["heat"]["n_post"] == 2 and windows["heat"]["extreme_f"] == 71.0


def test_spring_forward_window_is_still_open_fifteen_real_minutes_later():
    windows = _windows(_SF_OFF)
    assert swing.coast_feed(windows, _SF_15, 71.0) == []
    assert windows["heat"]["n_post"] == 2


@pytest.mark.parametrize(
    ("off", "now", "real_s"),
    [pytest.param(_FB_OFF, _FB_20, 1200.0, id="fall_back"), pytest.param(_SF_OFF, _SF_15, 900.0, id="spring_forward")],
)
def test_next_on_closes_with_the_real_window_length(off, now, real_s):
    closed = swing.coast_thermostat_event(
        _windows(off),
        now=now,
        reading=71.0,
        changed=False,
        old_dir=None,
        new_dir="heat",
        seconds_since_setpoint_change=None,
        off_setpoint_f=None,
    )
    assert [(m, ended) for m, ended, _v in closed] == [("heat", swing.COAST_ENDED_NEXT_ON)]
    assert closed[0][2].sample["window_s"] == real_s


def test_naive_clocks_subtract_as_before():
    naive_off = datetime(2026, 11, 1, 1, 50)
    windows = _windows(naive_off)
    assert swing.coast_feed(windows, datetime(2026, 11, 1, 2, 30), 71.0)[0][1] == swing.COAST_ENDED_TIMEOUT


def test_tools_still_load_swing_standalone():
    """tools/thermal_replay.py and tools/learning_db.py load swing.py by path, without Home Assistant."""
    path = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "swing.py"
    spec = importlib.util.spec_from_file_location("_ca_swing_standalone_1607", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.coast_timed_out(module._coast_age_s(_FB_35, _FB_OFF))
    finally:
        sys.modules.pop(spec.name, None)
