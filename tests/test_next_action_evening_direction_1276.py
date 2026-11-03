"""Issue #1276: the evening windows-day card said "Open windows" while the house sat at its comfort floor.

Occupant framing: on a mild evening the house is at 68F (the floor), the heat is on, outdoor is 60.6F. The
Next User Action card told the occupant to open windows, which lets cold air into a house the thermostat is
trying to warm (and once the window is open past its delay, rule 3 pauses the heat). These tests drive the REAL
``_compute_next_action`` and ``_apply_outdoor_windows_gate`` with the reported state: ONE direction rule
(``_outdoor_direction_ok``: the band rule plus the thermostat's swing at an edge it holds) for every branch.
"""

from __future__ import annotations

import datetime
import importlib
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.temperature import free_cooling_direction_ok, outdoor_direction_ok

_COORD_MOD = "custom_components.climate_advisor.coordinator"

_CFG = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,  # DEFAULT_COMFORT_COOL
    "sleep_heat": 64.0,
    "sleep_cool": 72.0,
    "sleep_time": "22:30",
    "wake_time": "06:30",
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "temp_unit": "fahrenheit",
}

FAN_MODES = ["whole_house_fan", "hvac_fan", "disabled"]


def _cls() -> DayClassification:
    """The persisted zone state at 18:11 on 2026-11-02 (issue evidence)."""
    c = object.__new__(DayClassification)
    c.__dict__.update(
        day_type="mild",
        trend_direction="stable",
        trend_magnitude=0,
        today_high=66.92,
        today_low=52,
        tomorrow_high=66,
        tomorrow_low=52,
        hvac_mode="heat",
        pre_condition=False,
        pre_condition_target=None,
        windows_recommended=True,
        window_open_time=datetime.time(10, 0),
        window_close_time=datetime.time(17, 0),
        setback_modifier=0.0,
        window_opportunity_morning=False,
        window_opportunity_evening=False,
        window_opportunity_morning_start=datetime.time(6, 0),
        window_opportunity_morning_end=datetime.time(9, 0),
        window_opportunity_evening_start=datetime.time(17, 0),
        window_opportunity_evening_end=datetime.time(0, 0),
    )
    return c


def _coord(fan_mode: str, *, mode: str | None = "heat", paused: bool = False, pre_pause: str | None = None):
    """A coordinator whose thermostat reads ``mode`` (the heat is on in the reported case); no swing learned."""
    coord_cls = importlib.import_module(_COORD_MOD).ClimateAdvisorCoordinator
    coord = object.__new__(coord_cls)
    coord.config = {**_CFG, "fan_mode": fan_mode}
    coord._occupancy_mode = "home"
    coord.automation_engine = MagicMock(_paused_by_door=paused, _pre_pause_mode=pre_pause)
    coord._thermostat_hvac_mode = lambda: mode
    coord._learned_swings = lambda: (None, None)  # default 1.5F tolerance
    return coord_cls, coord


def _card(
    fan_mode: str,
    *,
    indoor: float,
    outdoor: float,
    windows_open: bool = False,
    mode: str | None = "heat",
    paused: bool = False,
    pre_pause: str | None = None,
) -> str:
    coord_cls, coord = _coord(fan_mode, mode=mode, paused=paused, pre_pause=pre_pause)
    now = datetime.datetime(2026, 11, 2, 18, 17)
    with patch(f"{_COORD_MOD}.dt_util.now", return_value=now):
        return coord_cls._compute_next_action(
            coord, _cls(), indoor_temp=indoor, outdoor_temp=outdoor, windows_physically_open=windows_open, ae=None
        )


# --- direction rules, computed independently -------------------------------------------------------------


def test_direction_rules_on_reported_state():
    # The raw rule and the plain band rule both say "go ahead" for 60.6 vs 68.0 at the 68 floor ...
    assert free_cooling_direction_ok(60.6, 68.0) is True
    assert outdoor_direction_ok(60.6, 68.0, 68, 74) is True  # at the floor counts as "in band" -> no veto
    assert outdoor_direction_ok(60.6, 67.9, 68, 74) is False  # 0.1F lower and it vetoes
    # ... the held-edge margin (Issue #1276) vetoes cooler air within one heat swing above the floor.
    assert outdoor_direction_ok(60.6, 68.0, 68, 74, heat_margin=1.5) is False
    assert outdoor_direction_ok(60.6, 69.6, 68, 74, heat_margin=1.5) is True  # past the swing: in band again
    assert outdoor_direction_ok(70.0, 68.0, 68, 74, heat_margin=1.5) is True  # warmer air near the floor helps
    # Mirror: warmer air within one cool swing below the ceiling.
    assert outdoor_direction_ok(78.0, 74.0, 68, 74, cool_margin=1.5) is False
    assert outdoor_direction_ok(70.0, 74.0, 68, 74, cool_margin=1.5) is True


# --- the card ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_reported_case_at_floor_cooler_outdoor_is_not_open_windows(fan_mode):
    text = _card(fan_mode, indoor=68.0, outdoor=60.6)
    assert not text.startswith("Open windows"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_control_a_above_ceiling_cooler_outdoor_opens_windows(fan_mode):
    text = _card(fan_mode, indoor=76.0, outdoor=60.6)
    assert text.startswith("Open windows"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_control_b_at_ceiling_warmer_outdoor_is_not_open_windows(fan_mode):
    text = _card(fan_mode, indoor=74.0, outdoor=78.0)
    assert not text.startswith("Open windows"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_control_c_windows_open_at_floor_with_heat_says_close(fan_mode):
    text = _card(fan_mode, indoor=68.0, outdoor=60.6, windows_open=True)
    assert text.startswith("Close windows to hold heat"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_reported_case_says_keep_closed_to_hold_heat(fan_mode):
    text = _card(fan_mode, indoor=68.0, outdoor=60.6)
    assert text.startswith("Keep windows closed to hold heat"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_door_pause_holds_off_on_cas_behalf_the_interrupted_heat_still_counts(fan_mode):
    """A door/window pause holds the thermostat Off for CA: the heat it interrupted is what the house is on."""
    text = _card(fan_mode, indoor=68.0, outdoor=60.6, windows_open=True, mode="off", paused=True, pre_pause="heat")
    assert text.startswith("Close windows to hold heat"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_thermostat_off_does_not_hold_the_floor(fan_mode):
    """Owner rule (#1258): Off does not hold the room by hysteresis, so at-the-floor stays plain "in band"."""
    text = _card(fan_mode, indoor=68.0, outdoor=60.6, mode="off")
    assert text.startswith("Open windows"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_past_the_heat_swing_cooler_air_is_fine_again(fan_mode):
    text = _card(fan_mode, indoor=69.6, outdoor=60.6)
    assert text.startswith("Open windows"), text


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_mirror_windows_open_at_ceiling_with_cool_and_warmer_air_says_close(fan_mode):
    text = _card(fan_mode, indoor=74.0, outdoor=78.0, windows_open=True, mode="cool")
    assert text.startswith("Close windows"), text


# --- the live gate ----------------------------------------------------------------------------------------


def _gate(indoor: float, outdoor: float, mode: str | None = "heat") -> bool:
    coord_cls, coord = _coord("whole_house_fan", mode=mode)
    coord._current_classification = _cls()
    coord._last_outdoor_temp = outdoor
    coord._get_indoor_temp = lambda: indoor
    coord._schedule_planned_window_exit_check = MagicMock()
    coord_cls._apply_outdoor_windows_gate(coord)
    return coord._current_classification.windows_recommended


def test_gate_keeps_its_plain_band_rule_at_the_floor():
    """The gate drives the planned-window period (control), so it keeps the plain band rule (owner's call)."""
    assert _gate(68.0, 60.6) is True


def test_gate_below_floor_clears_flag_control():
    assert _gate(67.9, 60.6) is False


def test_gate_in_band_past_the_swing_keeps_flag():
    assert _gate(70.0, 60.6) is True


def test_gate_at_floor_thermostat_off_keeps_flag():
    assert _gate(68.0, 60.6, mode="off") is True
