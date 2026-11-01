"""The promises are never stricter than production's own allowance (Issue #1128, Step 3).

Walks the legal configuration grid (clamp ranges read from ``const.py``, in degrees F and in
degrees C steps) and proves, by running the REAL promise check on constructed histories, that
a furnace/AC start at or beyond production's hold bound is never flagged and that the flagged
zone always sits strictly inside it. If someone later changes a default or a clamp so a promise
becomes stricter than production, this fails first.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor import const  # noqa: E402
from custom_components.climate_advisor.invariant_watchdog import NUMBERS, PromiseConfig  # noqa: E402
from custom_components.climate_advisor.promise_journal import Command, PromiseInput, Reading  # noqa: E402
from custom_components.climate_advisor.temperature import convert_delta  # noqa: E402
from tests.test_promise_checks import hits_for  # noqa: E402

DAY_TYPES = ("hot", "warm", "mild", "cool", "cold")
T0 = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


def _clamp(day: str) -> tuple[float, float]:
    return (
        getattr(const, f"COMFORT_DEADBAND_{day.upper()}_MIN_F"),
        getattr(const, f"COMFORT_DEADBAND_{day.upper()}_MAX_F"),
    )


def _grid_f(lo: float, hi: float) -> list[float]:
    out, v = [], lo
    while v <= hi + 1e-9:
        out.append(round(v, 4))
        v += 0.5
    return out


def _grid_c_as_f(lo: float, hi: float) -> list[float]:
    """Celsius users pick in 0.5 C steps; the value is stored as an F delta, clamped to the legal range."""
    out = []
    c = 0.5
    while c < 20:
        f = c * 1.8
        if lo - 1e-9 <= f <= hi + 1e-9:
            out.append(round(f, 4))
        c += 0.5
    return out


def _tolerance_grids():
    for unit, fn in (("fahrenheit", _grid_f), ("celsius", _grid_c_as_f)):
        for day in DAY_TYPES:
            lo, hi = _clamp(day)
            for tol in fn(lo, hi):
                yield unit, day, tol


def _cfg(tolerances: dict[str, float], window: float = 120.0, unit: str = "fahrenheit") -> PromiseConfig:
    config = {
        f"comfort_deadband_{d}_f": tolerances.get(d, getattr(const, f"DEFAULT_COMFORT_DEADBAND_{d.upper()}_F"))
        for d in DAY_TYPES
    }
    config["comfort_family_recency_window_min"] = window
    config["temp_unit"] = unit
    return PromiseConfig.from_config(config)


def _heat_start(dip: float, *, since_cooling_min: float = 50.0) -> PromiseInput:
    """AC cooled, then the furnace started ``dip`` F under the 68 F floor on a mild afternoon."""
    t_heat = T0 + timedelta(minutes=since_cooling_min + 20)
    rs = (
        Reading(T0, "cool", "cooling", 75.0, 80.0, 82.0, 74.0, False),
        Reading(T0 + timedelta(minutes=20), "cool", "idle", 72.0, 80.0, 82.0, 74.0, False),
        Reading(t_heat, "heat", "heating", 68.0 - dip, 80.0, 82.0, 68.0, False),
    )
    cs = (Command(ts=t_heat - timedelta(seconds=2), target="thermostat", service="set_hvac_mode", mode="heat"),)
    return PromiseInput(t_heat, rs[-1], rs, cs, (), ())


class TestP1NeverStricterThanProductionTolerance:
    @pytest.mark.parametrize("unit,day,tol", list(_tolerance_grids()))
    def test_flag_zone_is_strictly_inside_the_tolerance_for_every_legal_setting(self, unit, day, tol):
        """Production holds the opposite mode up to ``tol`` past the edge; the promise flags only less."""
        cfg = _cfg({day: tol}, unit=unit)
        limit = cfg.p1_dip_limit_f()
        smallest = min(cfg.tolerances_f)
        assert limit <= smallest - NUMBERS.READING_SLACK_F + 1e-9
        assert limit <= tol  # also true for the tolerance being varied

    @pytest.mark.parametrize("unit,day,tol", list(_tolerance_grids()))
    def test_a_start_at_or_beyond_the_production_hold_bound_is_never_flagged(self, unit, day, tol):
        cfg = _cfg({d: tol for d in DAY_TYPES}, unit=unit)  # every day type at this tolerance
        for dip in (tol, tol + 0.1, tol + 1.0):
            assert hits_for("settling_flip", _heat_start(dip), cfg) == [], (unit, day, tol, dip)

    @pytest.mark.parametrize("tol", sorted({t for _, _, t in _tolerance_grids() if t >= 1.5}))
    def test_a_start_well_inside_the_hold_zone_is_still_caught(self, tol):
        """Not vacuous: where the promise has room (tolerance > slack), a start in its flag zone IS caught.

        The zone is capped at the default 2 F tolerance minus slack even when the user allows more.
        """
        cfg = _cfg({d: tol for d in DAY_TYPES})
        assert len(hits_for("settling_flip", _heat_start(0.0), cfg)) == 1
        assert len(hits_for("settling_flip", _heat_start(min(tol, 2.0) - 0.6), cfg)) == 1

    def test_the_smallest_legal_tolerance_leaves_a_narrow_but_nonempty_flag_zone(self):
        cfg = _cfg({d: 1.0 for d in DAY_TYPES})
        assert cfg.p1_dip_limit_f() == pytest.approx(0.5)
        assert len(hits_for("settling_flip", _heat_start(0.0), cfg)) == 1
        assert hits_for("settling_flip", _heat_start(0.6), cfg) == []

    def test_the_tightest_day_type_governs_when_tolerances_differ(self):
        cfg = _cfg({"hot": 8.0, "warm": 1.0, "mild": 5.0, "cool": 5.0, "cold": 8.0})
        assert cfg.p1_dip_limit_f() == pytest.approx(0.5)


class TestP1NeverLongerThanProductionWindow:
    @pytest.mark.parametrize(
        "window", _grid_f(const.COMFORT_FAMILY_RECENCY_WINDOW_MIN_MIN, const.COMFORT_FAMILY_RECENCY_WINDOW_MIN_MAX)[::6]
    )
    def test_lookback_is_shorter_than_the_configured_window(self, window):
        cfg = _cfg({}, window=window)
        assert cfg.p1_window_min() <= window - NUMBERS.TIME_SLACK_MIN + 1e-9
        assert cfg.p1_window_min() <= NUMBERS.P1_WINDOW_MIN

    @pytest.mark.parametrize("window", [15.0, 30.0, 60.0, 120.0, 240.0, 360.0])
    def test_cooling_just_outside_the_users_window_never_counts(self, window):
        """Production has already stopped holding; a promise that still flagged would be stricter."""
        cfg = _cfg({}, window=window)
        # AC reading `window + 1` minutes before the furnace start, nothing in between but idle
        gap = window + 1.0
        t_heat = T0 + timedelta(minutes=gap + 5)
        rs = (
            Reading(T0, "cool", "cooling", 75.0, 80.0, 82.0, 74.0, False),
            Reading(T0 + timedelta(minutes=1), "cool", "idle", 72.0, 80.0, 82.0, 74.0, False),
            Reading(t_heat, "heat", "heating", 67.5, 80.0, 82.0, 68.0, False),
        )
        cs = (Command(ts=t_heat - timedelta(seconds=2), target="thermostat", service="set_hvac_mode", mode="heat"),)
        assert hits_for("settling_flip", PromiseInput(t_heat, rs[-1], rs, cs, (), ()), cfg) == []

    def test_default_window_flags_cooling_inside_it(self):
        assert len(hits_for("settling_flip", _heat_start(0.5, since_cooling_min=60.0))) == 1

    def test_tiny_window_disables_the_promise_instead_of_going_negative(self):
        cfg = _cfg({}, window=const.COMFORT_FAMILY_RECENCY_WINDOW_MIN_MIN)
        assert cfg.p1_window_min() == pytest.approx(10.0)


class TestOtherPromiseNumbersAreLooserThanProduction:
    def test_blind_fan_allowance_exceeds_production_auto_stop(self):
        assert NUMBERS.P5_BLIND_CONTINUE_MIN * 60 > const.INDOOR_BLIND_FAN_STOP_S

    def test_p4_setpoint_reversal_is_looser_than_the_override_adoption_tolerance(self):
        assert NUMBERS.P4_REVERSAL_SETPOINT_F > const.OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F

    def test_p4_rule_2_window_covers_production_overlap_tolerance(self):
        """Issue #1144: the HVAC-off that follows an occupant fan press lands within production's 30 s bound."""
        assert NUMBERS.P4_RULE2_SUPPRESS_S >= const.AC_WHF_OVERLAP_TOLERANCE_S

    def test_occupant_echo_window_covers_production_own_command_echo_window(self):
        """Issue #1144: the promise layer never calls a CA fan start 'the occupant' later than production would."""
        from custom_components.climate_advisor.whf_safety_decision import WHF_OWN_COMMAND_ECHO_S

        assert NUMBERS.OCCUPANT_ECHO_S >= WHF_OWN_COMMAND_ECHO_S

    def test_p7_overshoot_allowance_covers_the_largest_default_settling_tolerance(self):
        defaults = [getattr(const, f"DEFAULT_COMFORT_DEADBAND_{d.upper()}_F") for d in DAY_TYPES]
        assert max(defaults) <= NUMBERS.P7_OVERSHOOT_EXPLAINED_F

    def test_p7_overshoot_window_covers_the_default_recency_window(self):
        assert NUMBERS.P7_OVERSHOOT_WINDOW_MIN >= const.DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN

    def test_p1_defaults_equal_production_defaults(self):
        assert (
            min(getattr(const, f"DEFAULT_COMFORT_DEADBAND_{d.upper()}_F") for d in ("warm", "mild", "cool"))
            == NUMBERS.P1_SHALLOW_F
        )
        assert NUMBERS.P1_WINDOW_MIN == const.DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN

    @pytest.mark.parametrize("tol_f", [1.0, 2.0, 5.0])
    def test_celsius_deltas_round_trip_without_changing_the_bound(self, tol_f):
        """The promise decides in F; only the occupant text is converted."""
        assert convert_delta(tol_f, "celsius") == pytest.approx(tol_f / 1.8)
