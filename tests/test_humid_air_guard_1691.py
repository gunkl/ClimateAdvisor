"""Issue #1691 (plan #1507 H1): the humid-air predicate and its option.

Occupant view: on a muggy night the whole-house fan must not pull wetter air into the house (it stays sticky
because short AC runs barely dry it), while a dry night still gets its free cooling. Every test here fails if the
rule it names is deleted from ``humidity_facts.humid_air_verdict`` or from the option plumbing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.const import (
    CONF_HUMID_AIR_GUARD_CEILING_F,
    CONF_HUMID_AIR_GUARD_ENABLED,
    CONFIG_METADATA,
    DEFAULT_HUMID_AIR_GUARD_CEILING_F,
    DEFAULT_HUMID_AIR_GUARD_ENABLED,
)
from custom_components.climate_advisor.humidity_facts import (
    HUMID_AIR_CEILING_LATCH,
    HUMID_AIR_REASON_CEILING,
    HUMID_AIR_REASON_WETTER,
    HUMID_AIR_WETTER_LATCH,
    HVAC_FAN_RESTRICT_HUMID_LATCH,
    LATCH_SPECS,
    SIGNAL_INDOOR,
    SIGNAL_OUTDOOR,
    humid_air_blocks,
    humid_air_ceiling_from_config,
    humid_air_latch_specs,
    humid_air_verdict,
    humid_air_verdict_from_facts,
    step_humid_latches,
)

CEIL = 65.0


def _v(indoor, outdoor, *, ceiling=CEIL, latched=()):
    return humid_air_verdict(indoor, outdoor, ceiling_f=ceiling, latched=latched)


class TestRelativeClause:
    def test_wetter_than_the_house_blocks(self):
        v = _v(55.0, 58.0)  # plan H1 test: 58 outside vs 55 inside
        assert v.blocks and v.reason == HUMID_AIR_REASON_WETTER
        assert v.latches == frozenset({HUMID_AIR_WETTER_LATCH})

    def test_drier_than_the_house_starts(self):
        assert not _v(61.0, 58.0).blocks  # plan H1 test: 58 outside vs 61 inside

    def test_dry_night_starts_even_when_wetter_than_a_dry_house(self):
        assert not _v(45.0, 50.0).blocks  # dry control: 50 F outdoor DP is never humid air

    def test_gap_and_floor_edges(self):
        assert _v(56.0, 58.0).blocks  # exactly 2 F wetter
        assert not _v(56.1, 58.0).blocks  # 1.9 F wetter
        assert _v(50.0, 55.0).blocks  # humid floor reached
        assert not _v(50.0, 54.9).blocks  # below the floor

    def test_hysteresis_releases_one_degree_below(self):
        on = frozenset({HUMID_AIR_WETTER_LATCH})
        assert _v(56.5, 58.0, latched=on).blocks  # gap 1.5 F: still latched
        assert not _v(56.5, 58.0).blocks  # same air, not latched: does not engage
        assert not _v(57.0, 58.0, latched=on).blocks  # gap 1.0 F: released
        assert _v(40.0, 54.5, latched=on).blocks  # outdoor 54.5 F: still above the 54 F release
        assert not _v(40.0, 54.0, latched=on).blocks  # released at the floor minus 1 F


class TestCeiling:
    def test_ceiling_blocks_even_when_the_house_is_wetter(self):
        v = _v(68.0, 65.0)
        assert v.blocks and v.reason == HUMID_AIR_REASON_CEILING
        assert not _v(68.0, 64.9).blocks

    def test_ceiling_wins_the_reason_when_both_hold(self):
        v = _v(50.0, 66.0)
        assert v.reason == HUMID_AIR_REASON_CEILING
        assert v.latches == frozenset({HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH})

    def test_ceiling_hysteresis(self):
        on = frozenset({HUMID_AIR_CEILING_LATCH})
        assert _v(70.0, 64.5, latched=on).blocks
        assert not _v(70.0, 64.5).blocks
        assert not _v(70.0, 64.0, latched=on).blocks

    def test_configured_ceiling_is_used(self):
        assert _v(70.0, 60.0, ceiling=60.0).blocks
        assert not _v(70.0, 60.0, ceiling=CEIL).blocks

    def test_off_never_blocks(self):
        v = _v(50.0, 72.0, ceiling=None, latched={HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH})
        assert not v.blocks and v.reason is None and v.latches == frozenset()


class TestMissingInputs:
    """The plan's missing-input table: the ceiling needs only outdoor DP, the relative clause both; unknown never
    blocks (today's behaviour)."""

    def test_indoor_unknown_ceiling_still_blocks(self):
        assert _v(None, 66.0).blocks
        assert _v(None, 66.0).reason == HUMID_AIR_REASON_CEILING

    def test_indoor_unknown_relative_clause_off(self):
        assert not _v(None, 60.0).blocks
        assert not _v(None, 60.0, latched={HUMID_AIR_WETTER_LATCH}).blocks

    def test_outdoor_unknown_nothing_blocks(self):
        assert not _v(50.0, None).blocks
        assert not _v(50.0, None, latched={HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH}).blocks

    def test_both_unknown_nothing_blocks(self):
        assert not _v(None, None).blocks

    def test_both_known(self):
        assert _v(55.0, 58.0).blocks and _v(55.0, 66.0).blocks


class TestLatchStep:
    def test_tick_and_verdict_share_one_step(self):
        state = step_humid_latches({SIGNAL_INDOOR: 55.0, SIGNAL_OUTDOOR: 66.0}, {}, ceiling_f=CEIL)
        # Issue #1693 (H2) steps its indoor latch in the same step: indoor 55 F engages it
        assert state == {
            HUMID_AIR_WETTER_LATCH: True,
            HUMID_AIR_CEILING_LATCH: True,
            HVAC_FAN_RESTRICT_HUMID_LATCH: True,
            "overcool_humid": False,  # Issue #1694 (H3): 55 F is below its 60 F trigger
        }
        state = step_humid_latches({SIGNAL_INDOOR: 55.0, SIGNAL_OUTDOOR: 64.5}, state, ceiling_f=CEIL)
        assert state[HUMID_AIR_CEILING_LATCH] is True  # inside the band
        latched = {k for k, on in state.items() if on}
        assert humid_air_verdict(55.0, 64.5, ceiling_f=CEIL, latched=latched).reason == HUMID_AIR_REASON_CEILING

    def test_specs(self):
        assert [s.name for s in LATCH_SPECS] == [
            HUMID_AIR_WETTER_LATCH,
            HVAC_FAN_RESTRICT_HUMID_LATCH,
            "overcool_humid",  # + #1693 H2, #1694 H3
        ]
        (spec,) = humid_air_latch_specs(60.0)
        assert (spec.name, spec.signal, spec.on, spec.off) == (HUMID_AIR_CEILING_LATCH, SIGNAL_OUTDOOR, 60.0, 59.0)
        assert humid_air_latch_specs(None) == ()

    def test_from_facts_reads_dew_points_and_latches(self):
        facts = SimpleNamespace(
            indoor_dew_point_f=56.5, outdoor_dew_point_f=58.0, humid_latches=frozenset({HUMID_AIR_WETTER_LATCH})
        )
        assert humid_air_verdict_from_facts(facts, CEIL).blocks
        assert not humid_air_verdict_from_facts(SimpleNamespace(), CEIL).blocks  # a facts stub without humidity
        assert humid_air_blocks(55.0, 58.0, ceiling_f=CEIL) is True


class TestOption:
    def test_defaults_on_at_65(self):
        assert DEFAULT_HUMID_AIR_GUARD_ENABLED is True and DEFAULT_HUMID_AIR_GUARD_CEILING_F == 65.0
        assert humid_air_ceiling_from_config({}) == 65.0
        assert humid_air_ceiling_from_config(None) == 65.0
        assert CONFIG_METADATA[CONF_HUMID_AIR_GUARD_ENABLED]["default"] is True
        assert CONFIG_METADATA[CONF_HUMID_AIR_GUARD_CEILING_F]["default"] == 65.0
        assert CONFIG_METADATA[CONF_HUMID_AIR_GUARD_CEILING_F]["display_transform"] == "temp_abs"

    def test_off_and_clamped(self):
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_ENABLED: False}) is None
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_CEILING_F: 58}) == 58.0
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_CEILING_F: 80}) == 70.0
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_CEILING_F: 40}) == 55.0
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_CEILING_F: "x"}) == 65.0
        assert humid_air_ceiling_from_config({CONF_HUMID_AIR_GUARD_CEILING_F: float("nan")}) == 65.0


class TestOptionsFlowUnits:
    """Celsius installs see and enter the ceiling in °C; it is stored as an absolute °F dew point."""

    def test_display_fahrenheit_and_celsius(self):
        from custom_components.climate_advisor.config_flow import humid_ceiling_display

        assert humid_ceiling_display(None, False) == (65.0, 55.0, 70.0, 1.0)
        default, lo, hi, step = humid_ceiling_display(None, True)
        assert (default, lo, hi, step) == (18.3, 12.8, 21.1, 0.1)
        assert humid_ceiling_display(60.8, True)[0] == pytest.approx(16.0)

    def test_celsius_entry_is_converted_as_an_absolute_temperature(self):
        from custom_components.climate_advisor.config_flow import humid_ceiling_to_fahrenheit

        out = humid_ceiling_to_fahrenheit({CONF_HUMID_AIR_GUARD_CEILING_F: 16.0}, {}, "celsius")
        assert out[CONF_HUMID_AIR_GUARD_CEILING_F] == pytest.approx(60.8)  # not 16 * 1.8 = 28.8 (a delta)

    def test_untouched_values_do_not_drift(self):
        from custom_components.climate_advisor.config_flow import humid_ceiling_to_fahrenheit

        # the fresh default left as shown (18.3 °C) stays "not set" (= 65 °F), never 64.94 °F
        assert CONF_HUMID_AIR_GUARD_CEILING_F not in humid_ceiling_to_fahrenheit(
            {CONF_HUMID_AIR_GUARD_CEILING_F: 18.3}, {}, "celsius"
        )
        stored = {CONF_HUMID_AIR_GUARD_CEILING_F: 61.0}
        shown = round((61.0 - 32) / 1.8, 4)
        out = humid_ceiling_to_fahrenheit({CONF_HUMID_AIR_GUARD_CEILING_F: shown}, stored, "celsius")
        assert out[CONF_HUMID_AIR_GUARD_CEILING_F] == 61.0
        out = humid_ceiling_to_fahrenheit({CONF_HUMID_AIR_GUARD_CEILING_F: 62.0}, {}, "fahrenheit")
        assert out[CONF_HUMID_AIR_GUARD_CEILING_F] == 62.0

    def test_options_step_saves_the_celsius_ceiling_in_fahrenheit(self):
        from tests.test_config_flow import _run_options_flow

        entry = {"temp_unit": "celsius", "climate_entity": "climate.t", "weather_entity": "weather.h"}
        typed = {CONF_HUMID_AIR_GUARD_ENABLED: True, CONF_HUMID_AIR_GUARD_CEILING_F: 16.0}
        saved = _run_options_flow(entry, [("async_step_sensors", typed)])
        assert saved[CONF_HUMID_AIR_GUARD_CEILING_F] == pytest.approx(60.8)
        assert saved[CONF_HUMID_AIR_GUARD_ENABLED] is True
