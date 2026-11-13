"""Issue #1540: an open window with free cooling available takes over from a calling compressor.

Occupant view: a window opened while the AC was cooling (76 F in, 68 F out). The whole-house fan's start was deferred
by the anti-cycling limit (#935), and the retry was then skipped because "the compressor is calling" (#244/#402), so
the AC kept cooling into the open window and the fan never started. Household rule 3 (#1142): no heat or cool with a
window open past its debounce, except the occupant's own override. The idle-open re-check now runs while the
compressor calls; the start decision still refuses for an occupant override and below the comfort floor.
"""

from __future__ import annotations

import asyncio

from tests.test_nat_vent_fsm_phase2b_wiring import _make_engine


def _open_window(engine, *, mode: str, action: str, outdoor: float) -> None:
    engine._paused_by_door = False
    engine._natural_vent_active = False
    engine._nat_vent_soft_start = False
    engine._grace_active = False
    engine._sensor_check_callback = lambda: True
    engine._last_outdoor_temp = outdoor
    st = engine.hass.states.get.return_value
    st.state = mode
    st.attributes = {**st.attributes, "hvac_action": action}


def test_ac_cooling_into_an_open_window_hands_over_to_free_cooling() -> None:
    engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=76.0)
    _open_window(engine, mode="cool", action="cooling", outdoor=68.0)
    asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is True


def test_idle_ac_still_hands_over_as_before() -> None:
    engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=76.0)
    _open_window(engine, mode="cool", action="idle", outdoor=68.0)
    asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is True


def test_occupant_override_keeps_the_ac_running() -> None:
    """The occupant's own override is rule 3's one exception: the start decision refuses."""
    engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=76.0)
    _open_window(engine, mode="cool", action="cooling", outdoor=68.0)
    engine._manual_override_active = True
    asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is False


def test_heat_mirror_furnace_below_the_floor_is_not_cooled_by_the_fan() -> None:
    """A furnace calling with the room below the comfort floor: the re-check runs, the floor refuses free cooling."""
    engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=66.0)
    _open_window(engine, mode="heat", action="heating", outdoor=55.0)
    asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is False


def test_heat_mirror_furnace_running_into_an_open_window_above_the_floor_hands_over() -> None:
    """A furnace still running into an open window with the room above the floor and cooler air outside: as for the
    AC, the furnace no longer blocks the free-cooling start (rule 3: no heat into an open window)."""
    engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0)
    _open_window(engine, mode="heat", action="heating", outdoor=62.0)
    asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is True
