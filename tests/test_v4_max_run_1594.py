"""Issue #1594: the emulated thermostat's longest run.

A sensor that briefly reads the warm (cold) supply air of each run ends the real run within minutes, a rise the
single-pole house cannot represent, so the emulator's runs ran long and each forecast night warm-up came out about
twice the real one. The thermostat learns its longest run from the recorded runs and the emulator ends a run there.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.predictor_v4 import V4Sample, V4Thermostat
from tests.test_predictor_v4 import TRUE_HOUSE

HALL = "sensor.hall"


def _roll(th: V4Thermostat, h0: float, *, heat: bool = True, n: int = 12, out: float = 40.0):
    sp = [68.0 if heat else 74.0] * n
    return p4.rollout(
        [TRUE_HOUSE[k] for k in p4.HOUSE_KEYS], [], h0=h0, m0=h0, b0=[], out=[out] * n, win=[0.0] * n,
        fan=[0.0] * n, hours=[2.0] * n, thermostat=th, heat_sp=sp if heat else [None] * n,
        cool_sp=[None] * n if heat else sp,
    )  # fmt: skip


class TestEmulatedLongestRun:
    def test_a_run_ends_at_the_longest_run(self):
        """A cold room (60 F, setpoint 68 F) needs many steps of heat; capped at 20 min the run ends 5 min into its
        second step, and the next step starts a new run because the reading is still below the start point."""
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0, off_above_f=0.5, min_run_heat_min=7.0, max_run_heat_min=20.0)
        r = _roll(th, 60.0)
        assert r.heat_duty[0] == pytest.approx(1.0)
        assert r.heat_duty[1] == pytest.approx(5.0 / 15.0)
        assert r.heat_duty[2] == pytest.approx(1.0)  # still cold: a new run, so a long recovery keeps its heat

    def test_without_a_longest_run_the_run_continues_to_the_stop_point(self):
        """Models fitted before the cap (and a mode with too few recorded runs) carry ``None``: no change."""
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0, off_above_f=0.5, min_run_heat_min=7.0)
        r = _roll(th, 60.0)
        assert r.heat_duty[:3] == [pytest.approx(1.0)] * 3

    def test_a_run_shorter_than_the_cap_is_unchanged(self):
        base = V4Thermostat(1.0, 1.0, 10.0, 0.0, off_above_f=0.5, min_run_heat_min=7.0)
        capped = dataclasses.replace(base, max_run_heat_min=240.0)
        assert _roll(capped, 60.0, n=24).heat_duty == _roll(base, 60.0, n=24).heat_duty

    def test_cooling_mirror(self):
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0, off_below_f=0.5, min_run_cool_min=7.0, max_run_cool_min=20.0)
        r = _roll(th, 82.0, heat=False, out=95.0)
        assert r.cool_duty[0] == pytest.approx(1.0)
        assert r.cool_duty[1] == pytest.approx(5.0 / 15.0)
        assert r.cool_duty[2] == pytest.approx(1.0)
        assert all(u == 0.0 for u in r.heat_duty)

    def test_the_cap_of_one_mode_does_not_touch_the_other(self):
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0, off_below_f=0.5, min_run_cool_min=7.0, max_run_heat_min=5.0)
        assert _roll(th, 82.0, heat=False, out=95.0).cool_duty[:3] == [pytest.approx(1.0)] * 3


def _samples_with_runs(minutes: list[float]) -> list[V4Sample]:
    """A heating day: one run of each length (in 15-min steps), each followed by an hour idle."""
    t = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    out: list[V4Sample] = []
    reading = 67.0

    def add(duty: float) -> None:
        nonlocal t
        out.append(
            V4Sample(
                ts=t,
                outdoor_f=40.0,
                forecast_outdoor_f=40.0,
                hvac_action="heating" if duty > 0 else "idle",
                fan_running=duty > 0,
                windows_open=False,
                heat_sp_f=68.0,
                cool_sp_f=None,
                readings={HALL: reading, p4.CA_NODE: reading},
                heat_duty=duty,
                cool_duty=0.0,
            )  # fmt: skip
        )
        t += timedelta(minutes=15)

    for run in minutes:
        left = run
        while left > 0:
            add(min(1.0, left / 15.0))
            left -= 15.0
        for _ in range(4):
            add(0.0)
    return out


class TestLearnedLongestRun:
    def test_it_is_a_high_quantile_of_the_recorded_runs(self):
        runs = [6.0, 7.0, 7.5, 8.0, 8.0, 8.5, 9.0, 10.0, 12.0, 30.0]
        th, _ = p4.learn_thermostat_detail(_samples_with_runs(runs), primary=HALL)
        srt = sorted(runs)
        assert th.max_run_heat_min == pytest.approx(srt[int(p4._MAX_RUN_QUANTILE * len(srt))], abs=0.1)
        assert th.min_run_heat_min is not None and th.max_run_heat_min >= th.min_run_heat_min
        assert th.max_run_cool_min is None  # no cooling runs

    def test_too_few_runs_learn_no_cap(self):
        th, _ = p4.learn_thermostat_detail(_samples_with_runs([8.0, 9.0, 10.0]), primary=HALL)
        assert th.max_run_heat_min is None


class TestStoredLongestRun:
    def _model(self) -> dict:
        from tests.test_v4_sleep_loop_1593 import _fx

        return _fx()["model"]

    def test_round_trip(self):
        d = self._model()
        d["thermostat"]["max_run_heat_min"] = 19.0
        m = p4.from_dict(d)
        assert m is not None and m.thermostat.max_run_heat_min == 19.0 and m.thermostat.max_run_cool_min is None
        assert p4.to_dict(m)["thermostat"]["max_run_heat_min"] == 19.0

    def test_a_model_stored_before_the_cap_loads_without_one(self):
        d = self._model()
        assert "max_run_heat_min" not in d["thermostat"]
        m = p4.from_dict(d)
        assert m is not None and m.thermostat.max_run_heat_min is None

    @pytest.mark.parametrize("bad", [0.0, -5.0, float("nan")])
    def test_a_non_positive_cap_is_rejected(self, bad):
        d = self._model()
        d["thermostat"]["max_run_cool_min"] = bad
        assert p4.from_dict(d) is None
