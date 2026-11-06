"""P10 heat_cool_settles (Issue #1368): Climate Advisor does not keep switching between heat and cool.

Occupant first: with heat set above cool (comfort 74/72) the heat and the AC took turns all day (8-40 heat<->cool
switches per generated day against 0-5 for a normal 68/74 band) and no promise said so. P10 judges the owner's
thrashed-controls rule G2 ("it settles") for the heat/cool family: more than ``P10_MAX_SWITCHES`` switches that
Climate Advisor COMMANDED inside ``P10_WINDOW_MIN`` is a miss. Occupant presses are never counted (rule A1-A3).

Unit controls build the evidence by hand; the end-to-end control drives the real engine through a generated day
with an inverted band, so deleting the promise (or the switching behaviour it watches) fails here.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    LEVEL_LOG,
    NUMBERS,
    PROMISES,
    SCOPE_HOME,
    SCOPE_TEST,
    SEVERITY_WARNING,
    PromiseConfig,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    JOURNAL_MAX_AGE,
    Command,
    PromiseInput,
    Reading,
)

KEY = "heat_cool_settles"
T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rd(minutes: float, mode: str) -> Reading:
    return Reading(
        ts=at(minutes),
        hvac_mode=mode,
        hvac_action="idle",
        indoor_f=73.0,
        outdoor_f=60.0,
        forecast_high_f=70.0,
        setpoint_f=73.0,
        fan_on=False,
    )


def ca(minutes: float, mode: str, service: str = "set_hvac_mode") -> Command:
    return Command(ts=at(minutes), target="thermostat", service=service, mode=mode, origin="ca", indoor_f=73.0)


def make_input(readings=(), commands=()) -> PromiseInput:
    readings = tuple(sorted(readings, key=lambda r: r.ts))
    commands = tuple(sorted(commands, key=lambda c: c.ts))
    stamps = [r.ts for r in readings] + [c.ts for c in commands]
    return PromiseInput(
        now=max(stamps) if stamps else T0,
        latest=readings[-1] if readings else None,
        readings=readings,
        commands=commands,
        marks=(),
        episodes=(),
    )


def ca_flips(times_modes):
    """CA commands each mode and the thermostat reports it a few seconds later (no occupant involved)."""
    return make_input(
        [rd(m + 0.1, mode) for m, mode in times_modes],
        [ca(m, mode) for m, mode in times_modes],
    )


def hits(inp: PromiseInput, scope: str = SCOPE_TEST):
    return [h for h in run_promise_checks(inp, PromiseConfig(), scope=scope) if h.promise == KEY]


class TestRegistry:
    def test_p10_is_registered_as_a_new_log_only_warning(self):
        (spec,) = [p for p in PROMISES if p.key == KEY]
        assert (spec.number, spec.label) == (10, "10")
        assert spec.test_severity == SEVERITY_WARNING
        assert spec.home_level == LEVEL_LOG

    def test_window_fits_the_journal_so_it_judges_the_same_at_home(self):
        assert timedelta(minutes=NUMBERS.P10_WINDOW_MIN) <= JOURNAL_MAX_AGE
        assert (NUMBERS.P10_MAX_SWITCHES, NUMBERS.P10_WINDOW_MIN) == (2, 180.0)


class TestHeatCoolSettles:
    def test_negative_control_three_ca_switches_in_three_hours(self):
        # the inverted-band shape: heat, cool, heat, cool about every 50 min (seed 101000001 at 74/72)
        inp = ca_flips([(0, "heat"), (50, "cool"), (100, "heat"), (150, "cool")])
        (h,) = hits(inp)
        assert dict(h.evidence)["switches"] == 3
        assert h.ts == at(150)
        assert "3 times in the past 3 hours" in h.detail

    def test_every_further_switch_in_the_window_is_its_own_hit(self):
        inp = ca_flips([(0, "heat"), (30, "cool"), (60, "heat"), (90, "cool"), (120, "heat")])
        assert [dict(h.evidence)["switches"] for h in hits(inp)] == [3, 4]

    def test_twin_two_switches_in_three_hours_is_normal(self):
        # the generated-day maximum on a normal band
        assert hits(ca_flips([(0, "heat"), (10, "cool"), (20, "heat")])) == []

    def test_twin_switches_spread_wider_than_the_window(self):
        inp = ca_flips([(0, "heat"), (100, "cool"), (200, "heat"), (300, "cool")])
        assert hits(inp) == []

    def test_window_edge_is_inclusive(self):
        inp = ca_flips([(0, "heat"), (1, "cool"), (91, "heat"), (181, "cool")])
        assert [dict(h.evidence)["switches"] for h in hits(inp)] == [3]

    def test_set_temperature_carrying_a_mode_counts(self):
        cs = [ca(0, "heat"), ca(30, "cool", "set_temperature"), ca(60, "heat"), ca(90, "cool", "set_temperature")]
        assert len(hits(make_input([], cs))) == 1

    def test_pausing_through_off_is_not_a_switch(self):
        modes = [(0, "heat"), (20, "off"), (40, "heat"), (60, "off"), (80, "heat"), (100, "off"), (120, "heat")]
        assert hits(ca_flips(modes)) == []

    def test_heat_off_cool_is_a_switch(self):
        inp = ca_flips([(0, "heat"), (10, "off"), (20, "cool"), (40, "heat"), (60, "cool")])
        assert len(hits(inp)) == 1

    def test_occupant_flipping_the_thermostat_is_never_counted(self):
        # readings only: no Climate Advisor command anywhere near, so every change is the occupant's (A1-A3)
        rs = [rd(m, mode) for m, mode in [(0, "heat"), (10, "cool"), (20, "heat"), (30, "cool"), (40, "heat")]]
        assert hits(make_input(rs)) == []

    def test_ca_following_the_occupant_is_not_a_switch(self):
        # the occupant picks the family; CA re-affirms it a few minutes later each time
        rs, cs = [], []
        for m, mode in [(0, "heat"), (30, "cool"), (60, "heat"), (90, "cool")]:
            rs.append(rd(m, mode))
            cs.append(ca(m + 5, mode))
        assert hits(make_input(rs, cs)) == []

    def test_ca_overwriting_each_occupant_press_still_counts(self):
        # the occupant keeps choosing cool and CA keeps switching back to heat: CA is the one thrashing
        rs, cs = [rd(0, "heat")], [ca(-1, "heat")]
        for m in (20, 60, 100):
            rs.append(rd(m, "cool"))
            cs.append(ca(m + 10, "heat"))
            rs.append(rd(m + 10.1, "heat"))
        (h,) = hits(make_input(rs, cs))
        assert dict(h.evidence)["switches"] == 3

    def test_fan_commands_are_ignored(self):
        cs = [Command(ts=at(m), target="fan", service="set_fan_mode", mode=md) for m, md in enumerate("ab" * 4)]
        assert hits(make_input([], cs)) == []

    def test_runs_at_home(self):
        inp = ca_flips([(0, "heat"), (50, "cool"), (100, "heat"), (150, "cool")])
        assert len(hits(inp, scope=SCOPE_HOME)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# End to end: the real engine on a generated day with the band inverted (Issue #1368 evidence)
# ─────────────────────────────────────────────────────────────────────────────
def _band_day(seed: int, heat: float, cool: float, high: float, low: float):
    from tools.sim_harness import generator as g  # noqa: PLC0415

    p = g.build_plan(seed, "coordinator")
    p.config.update(
        comfort_heat=heat,
        comfort_cool=cool,
        sleep_heat=heat,
        sleep_cool=cool,
        setback_heat=heat - 8,
        setback_cool=cool + 6,
        temp_unit="fahrenheit",
        fan_mode="disabled",
        door_window_sensors=[],
        initial_thermostat_mode="off",
    )
    p.config.pop("aggressive_savings", None)
    p.unit = "fahrenheit"
    p.whf = None
    p.timed = [t for t in p.timed if t[1] in ("wakeup", "bedtime")]
    p.forecast = {"high": high, "low": low, "tomorrow_high": high, "tomorrow_low": low}
    p.indoor_start_f = (heat + cool) / 2
    return g.run_plan(p, probe=True)


def _p10(rec):
    return [h for h in rec.report.warnings + rec.report.hard + rec.report.waived if h.promise == KEY]


@pytest.mark.parametrize(("high", "low"), [(72.0, 48.0), (88.0, 62.0)])
def test_inverted_band_day_is_reported_and_the_normal_band_is_not(high, low):
    seed = 101000001
    inverted = _p10(_band_day(seed, 74.0, 72.0, high, low))
    assert inverted, "heat above cool made the heat and AC take turns, and P10 said nothing"
    assert all(h.severity == SEVERITY_WARNING for h in inverted)
    assert _p10(_band_day(seed, 68.0, 74.0, high, low)) == []
