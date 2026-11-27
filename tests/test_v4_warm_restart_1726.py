"""Issue #1726: a warm-started fit that converged near its start skips the cold restart."""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from tests.test_predictor_v4 import HALL, T0, TZ, simulate


@pytest.fixture(scope="module")
def data():
    samples, _t, _ = simulate(T0, 20, base=44.0, amp=7.0, noise=0.03)
    first = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts)
    return samples, first


def _fit(samples, **kw):
    return p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts, **kw)


def test_constant():
    assert p4.WARM_RESTART_MIN_GAIN == 0.02


def test_converged_warm_fit_near_its_optimum_skips_the_restart(data):
    samples, first = data
    again = _fit(samples, warm_start=first)
    assert again.fit["converged"] is True
    assert again.fit["restarts"] == 0
    assert again.fit["cost"] <= first.fit["cost"] * 1.02


def test_warm_start_far_from_the_optimum_still_restarts(data):
    samples, first = data
    bad = dataclasses.replace(first, house={**first.house, "q_h": 39.0, "a_o": 0.45})
    again = _fit(samples, warm_start=bad)
    assert again.fit["restarts"] == 1
    assert again.fit["cost"] <= first.fit["cost"] * 1.1


def _nudged(first, factor):
    return dataclasses.replace(first, house={**first.house, "q_h": first.house["q_h"] * factor})


def test_a_warm_start_that_cut_its_cost_by_a_few_percent_restarts(data):
    # q_h 1 % off moves the LM by more than 2 % of its start cost (and far less than half of it): the threshold binds
    samples, first = data
    again = _fit(samples, warm_start=_nudged(first, 1.01))
    assert again.fit["converged"] is True
    assert again.fit["restarts"] == 1


def test_unconverged_warm_fit_still_restarts(data):
    samples, first = data
    near = _nudged(first, 1.002)
    # converged, this start gains under 2 % and skips the restart; so a capped run restarts only for not converging
    assert _fit(samples, warm_start=near).fit["restarts"] == 0
    capped = _fit(samples, warm_start=near, max_iterations=1)
    assert capped.fit["converged"] is False
    assert capped.fit["restarts"] == 1


def test_cold_fit_is_unchanged(data):
    samples, first = data
    assert first.fit["restarts"] == (0 if first.fit["converged"] else 1)
    capped = _fit(samples, max_iterations=3)
    assert capped.fit["converged"] is False and capped.fit["restarts"] == 1


def test_backtest_warm_start_is_opt_in_and_excludes_no_carry():
    from tests.test_predict_backtest import pb

    parser = pb.build_parser()
    assert parser.parse_args([]).warm_start is False
    assert parser.parse_args(["--warm-start"]).warm_start is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--warm-start", "--no-carry"])  # the warm start would carry the season anyway
    model = object()
    assert pb._warm_kw(p4.fit, None) == {}
    assert pb._warm_kw(p4.fit, model) == {"warm_start": model}
    assert pb._warm_kw(p4.choose_control_model, model) == {"warm_start": model}
    assert pb._warm_kw(lambda rows: rows, model) == {}  # an older predictor without the argument
