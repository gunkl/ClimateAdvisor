"""Tests for Issue #608 (Block 5 Phase 2): pure nat-vent exit-chain decision.

Direct unit coverage of ``decide_nat_vent_exit()`` — all 5 exit reasons, the
priority order between them (first match wins, matching
``check_natural_vent_conditions()``'s real ``if/elif``-equivalent chain), and
boundary conditions. The swap-in itself (replacing the inline chain in
``automation.py`` with a call to this function) is verified separately by the
existing golden/pending scenario suite, several of which assert on these exact
exit events with exact timing (e.g. ``nat-vent-comfort-floor-exit-restores-heat.json``,
``nat-vent-outdoor-rises-above-indoor-exit.json``) — a behavior change in the
swap would fail those assertions directly.
"""

from __future__ import annotations

from custom_components.climate_advisor.nat_vent_exit import (
    NatVentExitInputs,
    NatVentExitReason,
    decide_nat_vent_exit,
)


def _inputs(
    *,
    indoor: float | None = 72.0,
    outdoor: float | None = 65.0,
    comfort_heat_raw: float = 70.0,
    sleep_heat: float = 70.0,
    in_sleep_window: bool = False,
    hysteresis: float = 0.0,
    comfort_cool: float = 76.0,
    nat_vent_delta: float = 3.0,
    occupancy_mode: str = "home",
    thermal_confidence: str = "none",
    k_passive: float | None = None,
) -> NatVentExitInputs:
    return NatVentExitInputs(
        indoor=indoor,
        outdoor=outdoor,
        comfort_heat_raw=comfort_heat_raw,
        sleep_heat=sleep_heat,
        in_sleep_window=in_sleep_window,
        hysteresis=hysteresis,
        comfort_cool=comfort_cool,
        nat_vent_delta=nat_vent_delta,
        occupancy_mode=occupancy_mode,
        thermal_confidence=thermal_confidence,
        k_passive=k_passive,
    )


class TestNoExit:
    def test_stays_active_when_nothing_triggers(self) -> None:
        # indoor well above floor, home occupancy, no thermal model, outdoor
        # below indoor and below threshold.
        decision = decide_nat_vent_exit(_inputs(indoor=72.0, outdoor=65.0, comfort_cool=76.0, nat_vent_delta=3.0))
        assert decision.reason == NatVentExitReason.NONE


class TestComfortFloorExit:
    def test_fires_when_indoor_at_or_below_floor(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=70.0, comfort_heat_raw=70.0, in_sleep_window=False))
        assert decision.reason == NatVentExitReason.COMFORT_FLOOR
        assert decision.vent_floor == 70.0

    def test_no_fire_just_above_floor(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=70.1, comfort_heat_raw=70.0, in_sleep_window=False))
        assert decision.reason == NatVentExitReason.NONE

    def test_sleep_window_uses_sleep_heat_minus_hysteresis(self) -> None:
        # sleep_heat=68, hysteresis=1 -> floor=67; indoor=67 should exit.
        decision = decide_nat_vent_exit(_inputs(indoor=67.0, sleep_heat=68.0, hysteresis=1.0, in_sleep_window=True))
        assert decision.reason == NatVentExitReason.COMFORT_FLOOR
        assert decision.vent_floor == 67.0

    def test_takes_priority_over_away_ceiling(self) -> None:
        # Both conditions technically satisfiable — floor must win (checked first).
        decision = decide_nat_vent_exit(
            _inputs(indoor=70.0, comfort_heat_raw=70.0, comfort_cool=70.0, occupancy_mode="away")
        )
        assert decision.reason == NatVentExitReason.COMFORT_FLOOR


class TestAwayCeilingExit:
    def test_fires_when_away_and_indoor_at_ceiling(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=76.0, comfort_cool=76.0, occupancy_mode="away", comfort_heat_raw=60.0)
        )
        assert decision.reason == NatVentExitReason.AWAY_CEILING

    def test_no_fire_when_home_even_at_ceiling(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=76.0, comfort_cool=76.0, occupancy_mode="home", comfort_heat_raw=60.0, outdoor=65.0)
        )
        assert decision.reason == NatVentExitReason.NONE

    def test_no_fire_when_away_but_below_ceiling(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=75.9, comfort_cool=76.0, occupancy_mode="away", comfort_heat_raw=60.0, outdoor=65.0)
        )
        assert decision.reason == NatVentExitReason.NONE


class TestProactiveFloorExit:
    def test_fires_when_prediction_inside_threshold(self) -> None:
        # indoor=72, comfort_heat_now=70 (awake, no sleep) -> 2F to floor.
        # k_passive=-1.0, outdoor=65 -> passive_rate = -1.0*(72-65) = -7 F/hr.
        # time_to_floor = 2/7 = 0.2857 hr < 1.0 -> fires.
        decision = decide_nat_vent_exit(
            _inputs(
                indoor=72.0,
                outdoor=65.0,
                comfort_heat_raw=70.0,
                thermal_confidence="high",
                k_passive=-1.0,
            )
        )
        assert decision.reason == NatVentExitReason.PROACTIVE_FLOOR
        assert decision.comfort_heat_now == 70.0
        assert decision.time_to_floor_hr is not None
        assert round(decision.time_to_floor_hr, 4) == round(2 / 7, 4)

    def test_no_fire_when_confidence_low(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=72.0, outdoor=65.0, comfort_heat_raw=70.0, thermal_confidence="low", k_passive=-1.0)
        )
        assert decision.reason == NatVentExitReason.NONE

    def test_no_fire_when_k_passive_positive(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=72.0, outdoor=65.0, comfort_heat_raw=70.0, thermal_confidence="high", k_passive=0.5)
        )
        assert decision.reason == NatVentExitReason.NONE

    def test_no_fire_when_time_to_floor_beyond_threshold(self) -> None:
        # indoor=90, floor=70 -> 20F to floor; k_passive=-0.5, outdoor=65 ->
        # passive_rate = -0.5*(90-65)=-12.5 F/hr; time=20/12.5=1.6hr >= 1.0 -> no fire.
        decision = decide_nat_vent_exit(
            _inputs(indoor=90.0, outdoor=65.0, comfort_heat_raw=70.0, thermal_confidence="high", k_passive=-0.5)
        )
        assert decision.reason == NatVentExitReason.NONE

    def test_no_fire_when_outdoor_not_below_indoor(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(indoor=72.0, outdoor=73.0, comfort_heat_raw=70.0, thermal_confidence="high", k_passive=-1.0)
        )
        # Falls through to outdoor-rise exit instead (outdoor > indoor).
        assert decision.reason == NatVentExitReason.OUTDOOR_RISE

    def test_takes_priority_over_outdoor_rise(self) -> None:
        decision = decide_nat_vent_exit(
            _inputs(
                indoor=72.0,
                outdoor=65.0,
                comfort_heat_raw=70.0,
                comfort_cool=76.0,
                nat_vent_delta=3.0,
                thermal_confidence="high",
                k_passive=-1.0,
            )
        )
        assert decision.reason == NatVentExitReason.PROACTIVE_FLOOR


class TestOutdoorRiseExit:
    def test_fires_when_outdoor_exceeds_indoor(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=74.0, outdoor=74.5))
        assert decision.reason == NatVentExitReason.OUTDOOR_RISE

    def test_fires_when_outdoor_equals_indoor(self) -> None:
        """Issue #690: boundary was previously strict (>), so outdoor==indoor did
        NOT fire OUTDOOR_RISE here — disagreeing with fan_thermostat_decision.py's
        Check 1, which already used non-strict (>=) for the same real-world
        condition. Now non-strict here too: at equality no cooling benefit
        remains, so the exit fires immediately instead of waiting a tick."""
        decision = decide_nat_vent_exit(_inputs(indoor=74.0, outdoor=74.0))
        assert decision.reason == NatVentExitReason.OUTDOOR_RISE

    def test_takes_priority_over_ceiling_threshold(self) -> None:
        # outdoor > indoor AND outdoor > threshold -- outdoor-rise checked first.
        # indoor=75 kept comfortably above the comfort_heat_raw=70 floor so
        # comfort-floor doesn't fire first.
        decision = decide_nat_vent_exit(
            _inputs(indoor=75.0, outdoor=80.0, comfort_heat_raw=70.0, comfort_cool=76.0, nat_vent_delta=3.0)
        )
        assert decision.reason == NatVentExitReason.OUTDOOR_RISE


class TestCeilingThresholdExit:
    def test_fires_when_outdoor_exceeds_threshold_but_not_indoor(self) -> None:
        # threshold = comfort_cool(76) + delta(3) = 79; outdoor=80 > 79, but
        # outdoor(80) > indoor(85)? No -- indoor=85 > outdoor=80, so outdoor-rise
        # doesn't fire, ceiling-threshold does.
        decision = decide_nat_vent_exit(_inputs(indoor=85.0, outdoor=80.0, comfort_cool=76.0, nat_vent_delta=3.0))
        assert decision.reason == NatVentExitReason.CEILING_THRESHOLD

    def test_no_fire_at_exactly_threshold(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=85.0, outdoor=79.0, comfort_cool=76.0, nat_vent_delta=3.0))
        assert decision.reason == NatVentExitReason.NONE

    def test_none_indoor_still_allows_ceiling_threshold(self) -> None:
        # indoor=None short-circuits comfort-floor, away-ceiling (needs indoor),
        # proactive-floor (needs indoor), and outdoor-rise (needs indoor) --
        # only ceiling-threshold (indoor-independent) can still fire.
        decision = decide_nat_vent_exit(_inputs(indoor=None, outdoor=80.0, comfort_cool=76.0, nat_vent_delta=3.0))
        assert decision.reason == NatVentExitReason.CEILING_THRESHOLD


class TestMissingData:
    def test_none_outdoor_and_indoor_no_exit(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=None, outdoor=None))
        assert decision.reason == NatVentExitReason.NONE

    def test_none_outdoor_only_comfort_floor_and_away_ceiling_still_evaluable(self) -> None:
        decision = decide_nat_vent_exit(_inputs(indoor=70.0, outdoor=None, comfort_heat_raw=70.0))
        assert decision.reason == NatVentExitReason.COMFORT_FLOOR
