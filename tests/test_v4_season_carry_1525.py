"""Issue #1525: a season without heating (cooling) no longer replaces what v4 learned about it.

Occupant view: on a v4-primary zone, after a summer with no heating, autumn pre-heat and recovery timing ran on a
heating rate derived from the cooling rate (``q_h = q_c / ratio``), not the rate the house learned last winter; spring
had the mirror. During every single-mode season v4 also read "low: heating or cooling not yet observed" and the unseen
mode's thermostat start/stop offsets went back to the defaults. The fit keeps its 30-day window (R1b); what the previous
model learned about a mode the window does not show is now carried, and the mode's new runs displace it (D35's rule:
fade only on new evidence, never on time alone).

The synthetic house's heat/cool gains (14 / 8 F/h) are far from the production ratio (8.8 / 8), so a ratio-derived gain
is wrong by about half: every carry assertion fails on the pre-fix code.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import night_replay, v4_shadow
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4

from .test_predictor_v4 import HALL, TZ, Plant, outdoor_fn

HOUSE = {
    "a_o": 0.05,
    "a_w": 0.15,
    "a_f": 0.5,
    "c_hm": 0.8,
    "c_mh": 0.3,
    "s": 0.6,
    "phase": 1.0,
    "q_h": 14.0,
    "q_c": 8.0,
}
T0 = datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
GAIN = {"heat": "q_h", "cool": "q_c"}
OTHER = {"heat": "cool", "cool": "heat"}
FIRST_END = 16  # the mode's first season: days 0-16
RETURN = 51  # it comes back on day 51 (days 16-51: only the other mode)


def _on_off(model: p4.V4Model, mode: str) -> tuple[float, float]:
    th = model.thermostat
    return (th.on_below_f, th.off_above_f) if mode == "heat" else (th.on_above_f, th.off_below_f)


def _season(mode: str, day: float) -> str:
    return mode if day < FIRST_END or day >= RETURN else OTHER[mode]


def _history(mode: str, days: int = 60, *, fan_days: tuple[int, int] | None = None) -> list[p4.V4Sample]:
    """``mode`` for days 0-16, the other mode only for days 16-51, ``mode`` again from day 51."""
    rng = random.Random(5)
    plant = Plant(HOUSE, None)
    plant.h = plant.m = 68.0
    out = []
    for k in range(1, days * 96 + 1):
        ts = (T0.astimezone(UTC) + timedelta(minutes=15 * k)).astimezone(TZ)
        d = k / 96.0
        m = _season(mode, d)
        o = outdoor_fn(42.0 if m == "heat" else 82.0, 9.0, ts)
        h = ts.astimezone(TZ).hour
        hsp = (64.0 if h >= 22 or h < 6 else 68.0) if m == "heat" else None
        csp = 75.0 if m == "cool" else None
        fan = 1.0 if fan_days and fan_days[0] <= d < fan_days[1] and 20 <= h < 23 else 0.0
        uh, uc = plant.step(ts, o, hsp, csp, 0.0, fan)
        out.append(
            p4.V4Sample(
                ts=ts, outdoor_f=round(o, 2), forecast_outdoor_f=None,
                hvac_action="heating" if uh > 0 else "cooling" if uc > 0 else "idle", fan_running=bool(fan),
                windows_open=False, heat_sp_f=hsp, cool_sp_f=csp,
                readings={HALL: round(plant.h + rng.gauss(0, 0.03), 2)}, heat_duty=uh, cool_duty=uc,
            )
        )  # fmt: skip
    return out


def _window(samples: list[p4.V4Sample], end_day: float, days: float = 30.0) -> list[p4.V4Sample]:
    end = T0.timestamp() + end_day * 86400
    return [s for s in samples if end - days * 86400 <= s.ts.timestamp() < end]


def _fit(samples, end_day, warm=None, **kw):
    win = _window(samples, end_day, kw.pop("days", 30.0))
    return p4.fit(
        win, primary=HALL, sensors=[HALL], tz=TZ, now=win[-1].ts + timedelta(minutes=1), warm_start=warm, **kw
    )


@pytest.fixture(scope="module", params=["heat", "cool"])
def chain(request):
    """The nightly chain across the season change for ``mode`` (the mode the middle season does not use)."""
    mode = request.param
    smp = _history(mode, days=63)
    first = _fit(smp, FIRST_END, days=FIRST_END)  # the mode's own season: graded
    summer = _fit(smp, 50.0, warm=first)  # 30 days of the other mode only: the mode is unseen
    cold = _fit(smp, 50.0)  # the same window with no previous model (a home that never used the mode)
    back1 = _fit(smp, RETURN + 3.0, warm=summer)  # its first days back: seen, too thin to grade
    back7 = _fit(smp, RETURN + 9.0, warm=back1)  # a week later: the window grades it; new runs displace the carry
    return {
        "mode": mode,
        "samples": smp,
        "first": first,
        "summer": summer,
        "cold": cold,
        "back1": back1,
        "back7": back7,
    }


class TestCarryThroughTheOtherSeason:
    def test_the_first_season_learned_the_mode(self, chain):
        first, mode = chain["first"], chain["mode"]
        assert first.fit["modes"][mode]["grade"] == "ok", first.fit["modes"][mode]
        assert first.house[GAIN[mode]] == pytest.approx(HOUSE[GAIN[mode]], rel=0.1)

    def test_unseen_mode_keeps_its_learned_gain_not_the_ratio(self, chain):
        """Autumn (spring) pre-heat (pre-cool) reads last season's gain, not one derived from the other mode."""
        mode, first, summer, cold = chain["mode"], chain["first"], chain["summer"], chain["cold"]
        assert summer.fit[f"{mode}_seen"] is False
        assert summer.house[GAIN[mode]] == first.house[GAIN[mode]]
        # the ratio a never-seen home still gets is about half wrong for this house
        ratio_derived = cold.house[GAIN[mode]]
        assert abs(ratio_derived - HOUSE[GAIN[mode]]) > 0.4 * HOUSE[GAIN[mode]]
        assert f"h/{GAIN[mode]}" in summer.fit["carried_params"]
        assert f"h/{GAIN[mode]}" not in summer.fit["frozen"]

    def test_unseen_mode_keeps_its_thermostat_offsets(self, chain):
        mode, first, summer = chain["mode"], chain["first"], chain["summer"]
        assert _on_off(summer, mode) == _on_off(first, mode)
        assert summer.fit["thermostat_defaults_used"] is False

    def test_confidence_is_not_low_for_a_learned_mode(self, chain):
        mode, summer, cold = chain["mode"], chain["summer"], chain["cold"]
        assert cold.confidence == "low" and cold.fit["reason"] == "heating or cooling not yet observed"
        assert summer.confidence == "ok", summer.fit["reason"]
        g = summer.fit["modes"][mode]
        assert g["grade"] == chain["first"].fit["modes"][mode]["grade"] == "ok"
        assert g["reason"] == p4.CARRY_REASON and g["carried"] is True and g["keep"] == 1.0
        assert summer.fit["carried"][mode]["keep"] == 1.0

    def test_decision_rates_carry_the_learned_gain(self, chain):
        """What pre-heat / pre-cool reads (``control_rates``) is last season's rate, at the ok -> medium grade."""
        mode, first, summer = chain["mode"], chain["first"], chain["summer"]
        key = "heating_rate_f_per_hour" if mode == "heat" else "cooling_rate_f_per_hour"
        assert p4.control_rates(summer)[key] == pytest.approx(p4.control_rates(first)[key], abs=0.02)
        assert p4.control_rates(summer)["confidence"] == "medium"

    def test_a_thin_return_does_not_replace_the_carried_values(self, chain):
        """The season's first runs are too few to grade the mode: what the house learned still stands."""
        mode, first, back1 = chain["mode"], chain["first"], chain["back1"]
        assert back1.fit[f"{mode}_seen"] is True
        assert back1.house[GAIN[mode]] == first.house[GAIN[mode]]
        assert _on_off(back1, mode) == _on_off(first, mode)
        assert back1.fit["carried"][mode]["keep"] == 1.0
        assert back1.fit["modes"][mode]["grade"] == "ok" and back1.fit["modes"][mode]["reason"] == p4.CARRY_REASON

    def test_new_runs_displace_the_carry_gradually(self, chain):
        mode, back1, back7 = chain["mode"], chain["back1"], chain["back7"]
        rec = back7.fit["carried"][mode]
        n_new = p4.runs_after(_window(chain["samples"], RETURN + 9.0), heat=mode == "heat", after_epoch=None)
        assert n_new > 0
        assert rec["keep"] == pytest.approx(p4.displaced_keep(1.0, rec["new_runs"], rec["contradicted"]), abs=1e-6)
        assert p4.CARRY_MIN_KEEP <= rec["keep"] < 1.0
        assert rec["q"] == back1.house[GAIN[mode]]  # the carried value itself never moves; its share does


class TestReleaseAndContradiction:
    @staticmethod
    def _with_record(model: p4.V4Model, mode: str, **changes) -> p4.V4Model:
        fit = dict(model.fit)
        carried = {k: dict(v) for k, v in fit["carried"].items()}
        carried[mode].update(changes)
        fit["carried"] = carried
        return dataclasses.replace(model, fit=fit)

    def test_the_carry_is_released_once_new_runs_hold_nearly_all_the_evidence(self, chain):
        mode, back7 = chain["mode"], chain["back7"]
        prev = self._with_record(back7, mode, keep=p4.CARRY_MIN_KEEP * 1.01)
        nxt = _fit(chain["samples"], RETURN + 11.0, warm=prev)
        assert mode not in nxt.fit["carried"]
        assert not nxt.fit["modes"][mode].get("carried")

    def test_contradicting_runs_displace_it_faster(self, chain):
        """A carried gain the new season's own fit disagrees with (by more than 25 %) halves every 5 runs, not 20."""
        mode, back7 = chain["mode"], chain["back7"]
        wrong = 2.0 * back7.house[GAIN[mode]]
        prev = self._with_record(back7, mode, q=wrong, keep=1.0)
        nxt = _fit(chain["samples"], RETURN + 11.0, warm=prev)
        rec = nxt.fit["carried"][mode]
        assert rec["contradicted"] is True and rec["new_runs"] > 0
        assert rec["keep"] == pytest.approx(0.5 ** (rec["new_runs"] / p4.CARRY_HALF_RUNS_CONTRADICT), abs=1e-6)
        assert nxt.house[GAIN[mode]] < wrong

    def test_release_waits_until_the_last_step_is_negligible(self, chain):
        """Below CARRY_MIN_KEEP the blend goes on while dropping the carried share would still move the gain."""
        mode, back7 = chain["mode"], chain["back7"]
        far = 1.8 * back7.house[GAIN[mode]]
        prev = self._with_record(back7, mode, q=far, keep=p4.CARRY_MIN_KEEP * 1.01)
        nxt = _fit(chain["samples"], RETURN + 11.0, warm=prev)
        rec = nxt.fit["carried"][mode]
        assert rec["keep"] < p4.CARRY_MIN_KEEP
        assert rec["keep"] * abs(far - nxt.house[GAIN[mode]]) > 0  # still blending: no step at release


class TestThinNightFlip:
    """Verification finding 1: a thin night between graded nights neither displaces the carry nor undoes it."""

    def test_seen_thin_seen_keeps_the_displacement(self, chain):
        mode, back1, back7 = chain["mode"], chain["back1"], chain["back7"]
        rec7 = back7.fit["carried"][mode]
        assert rec7["keep"] < 1.0
        thin = _fit(chain["samples"], RETURN + 3.0, warm=back7)  # a window that has the mode but cannot grade it
        assert thin.fit[f"{mode}_seen"] is True and thin.fit["modes"][mode]["reason"] == p4.CARRY_REASON
        rec = thin.fit["carried"][mode]
        assert rec["keep"] == rec7["keep"] and rec["q"] == rec7["q"]  # not rebased to 1.0 at the used value
        assert rec["counted_through"] == rec7["counted_through"] and rec["new_runs"] == 0
        assert thin.house[GAIN[mode]] == back7.house[GAIN[mode]]  # held at what the previous night used
        assert _on_off(thin, mode) == _on_off(back7, mode)
        graded = _fit(chain["samples"], RETURN + 11.0, warm=thin)
        direct = _fit(chain["samples"], RETURN + 11.0, warm=back7)  # the same graded night with no thin night before
        rec_g, rec_d = graded.fit["carried"][mode], direct.fit["carried"][mode]
        assert rec_g["keep"] == rec_d["keep"] < rec7["keep"]  # displacement continues as if the thin night never was
        n = p4.runs_after(
            _window(chain["samples"], RETURN + 11.0), heat=mode == "heat", after_epoch=rec7["counted_through"]
        )
        assert rec_g["new_runs"] == n > 0
        assert back1.fit["carried"][mode]["keep"] == 1.0


class TestNodeSwitch:
    """Verification finding 2: a switch between the stitched and the per-room model keeps what the house learned."""

    def test_the_carry_crosses_a_primary_node_change(self, chain):
        mode, first, summer = chain["mode"], chain["first"], chain["summer"]
        renamed = [
            dataclasses.replace(s, readings={p4.CA_NODE: s.readings[HALL]}) for s in _window(chain["samples"], 50.0)
        ]
        other = p4.fit(renamed, primary=p4.CA_NODE, sensors=[p4.CA_NODE], tz=TZ,
                       now=renamed[-1].ts + timedelta(minutes=1), warm_start=summer)  # fmt: skip
        assert other.primary != summer.primary
        assert other.house[GAIN[mode]] == summer.house[GAIN[mode]] == first.house[GAIN[mode]]
        assert _on_off(other, mode) == _on_off(summer, mode)
        assert other.fit["modes"][mode]["grade"] == "ok" and other.confidence == "ok"


class TestNeverSeenAndUpgrade:
    def test_a_home_that_never_used_the_mode_keeps_the_ratio(self, chain):
        mode, cold = chain["mode"], chain["cold"]
        other = cold.house[GAIN[OTHER[mode]]]
        expect = other * p4._COOL_HEAT_RATIO if mode == "cool" else other / p4._COOL_HEAT_RATIO
        assert cold.house[GAIN[mode]] == pytest.approx(min(40.0, max(0.5, expect)), rel=1e-9)
        assert not cold.fit["carried"]
        assert f"h/{GAIN[mode]}" in cold.fit["frozen"]

    def test_a_previous_model_that_never_graded_the_mode_carries_nothing(self, chain):
        """Upgrade day: a pre-fix summer model (the mode unseen, ratio-tied, no carry fields) changes nothing."""
        mode, cold = chain["mode"], chain["cold"]
        legacy_fit = {k: v for k, v in cold.fit.items() if k not in ("carried", "carried_params")}
        legacy = dataclasses.replace(cold, fit=legacy_fit)
        again = _fit(chain["samples"], 50.0, warm=legacy)
        assert not again.fit["carried"]
        assert again.house[GAIN[mode]] == pytest.approx(cold.house[GAIN[mode]], rel=0.02)
        assert again.confidence == "low" and again.fit["reason"] == "heating or cooling not yet observed"

    def test_a_pre_fix_model_that_learned_the_mode_is_carried(self, chain):
        """Upgrade continuity the other way: a model written before the fix (no carry fields) that graded the mode is
        carried, so the first fit after the upgrade keeps the value the decisions were already using."""
        mode, first = chain["mode"], chain["first"]
        legacy = dataclasses.replace(
            first, fit={k: v for k, v in first.fit.items() if k not in ("carried", "carried_params")}
        )
        summer = _fit(chain["samples"], 50.0, warm=legacy)
        assert summer.house[GAIN[mode]] == first.house[GAIN[mode]]


class TestPersistence:
    def test_save_and_load_keep_the_carry(self, chain):
        mode, summer = chain["mode"], chain["summer"]
        back = p4.from_dict(p4.to_dict(summer))
        assert back is not None
        assert back.fit["carried"] == summer.fit["carried"]
        again = _fit(chain["samples"], RETURN + 3.0, warm=back)
        assert again.house[GAIN[mode]] == chain["back1"].house[GAIN[mode]]
        assert again.fit["carried"][mode]["keep"] == chain["back1"].fit["carried"][mode]["keep"]

    def test_an_unusable_night_does_not_cost_the_carry(self, chain):
        """A night with too little data (confidence none) keeps the last usable model for the next fit's carry."""
        mode, summer = chain["mode"], chain["summer"]
        thin = _fit(chain["samples"], 50.0, warm=summer, days=0.1)
        assert thin.confidence == "none" and "carry_source" in thin.fit
        stored = p4.from_dict(p4.to_dict(thin))
        nxt = _fit(chain["samples"], 50.0, warm=stored)
        assert nxt.house[GAIN[mode]] == summer.house[GAIN[mode]]
        assert nxt.fit["modes"][mode]["reason"] == p4.CARRY_REASON


class TestWords:
    def test_debug_tab_says_carried(self, chain):
        mode, summer = chain["mode"], chain["summer"]
        s = v4_shadow.model_summary(p4.to_dict(summer))
        assert s["modes"][mode]["carried"] is True
        why, by_mode = v4_shadow.describe_grade(s)
        word = "heating" if mode == "heat" else "cooling"
        assert f"{word} ok: carried from last season (not seen)" in by_mode
        assert why == f"every check passes; {word} carried from last season"


class TestEvidenceRule:
    def test_keep_halves_per_runs_and_faster_on_contradiction(self):
        assert p4.displaced_keep(1.0, 0, False) == 1.0  # no new run: nothing moves, however long it has been
        assert p4.displaced_keep(1.0, p4.CARRY_HALF_RUNS, False) == pytest.approx(0.5)
        assert p4.displaced_keep(1.0, p4.CARRY_HALF_RUNS_CONTRADICT, True) == pytest.approx(0.5)

    def test_numbers_are_d35s(self):
        from custom_components.climate_advisor import const

        assert p4.CARRY_HALF_RUNS == const.THERMAL_EVIDENCE_HALF_N
        assert p4.CARRY_HALF_RUNS_CONTRADICT == const.THERMAL_EVIDENCE_HALF_N_CONTRADICT

    def test_runs_after_counts_only_new_runs(self):
        smp = _history("heat", days=3)
        allr = p4.runs_after(smp, heat=True, after_epoch=None)
        half = p4.runs_after(smp, heat=True, after_epoch=smp[len(smp) // 2].ts.timestamp())
        assert allr > half > 0
        assert p4.runs_after(smp, heat=False, after_epoch=None) == 0


# --------------------------------------------------------------------------- window / fan rates (R1b unharmed)
@pytest.fixture(scope="module")
def fan_models():
    smp = _history("heat", days=50, fan_days=(0, 20))
    with_fan = _fit(smp, 20.0, days=20.0)  # the fan ran in this window: a_f learned
    no_fan = _fit(smp, 50.0, warm=with_fan)  # 30 days without the fan
    return smp, with_fan, no_fan


class TestFanRate:
    def test_fan_rate_stays_learned_through_a_window_without_the_fan(self, fan_models):
        _smp, with_fan, no_fan = fan_models
        assert "h/a_f" not in with_fan.fit["frozen"]
        assert no_fan.house["a_f"] == with_fan.house["a_f"]
        assert "h/a_f" in no_fan.fit["carried_params"] and "h/a_f" not in no_fan.fit["frozen"]
        rates = p4.control_rates(no_fan)
        assert rates["k_vent_fan"] == pytest.approx(-(no_fan.house["a_o"] + with_fan.house["a_f"]))

    def test_fan_rate_comes_only_from_the_window_when_the_fan_ran(self, fan_models):
        """#1230 R1b: older evidence never enters the fan rate of a window that has the fan in it."""
        smp, with_fan, _ = fan_models
        lo = dataclasses.replace(with_fan, house={**with_fan.house, "a_f": 0.05})
        hi = dataclasses.replace(with_fan, house={**with_fan.house, "a_f": 1.5})
        a = _fit(smp, 20.0, days=20.0, carry_from=lo)
        b = _fit(smp, 20.0, days=20.0, carry_from=hi)
        assert a.house["a_f"] == b.house["a_f"] == with_fan.house["a_f"]
        assert "h/a_f" not in a.fit["carried_params"]


# --------------------------------------------------------------------------- night replay record
def _cls(n: int, mae: float = 0.8, flat: float = 1.6) -> dict:
    return {"n_nights": n, "mae_f": mae, "flat_mae_f": flat, "bias_f": 0.1}


def _report(classes: dict) -> dict:
    return {"params": {"train_days": 30}, "classes": classes, "classes_ok_fits_only": classes, "generated": "x"}


def _previous(classes: dict, ev: ns.Evidence | None = None) -> dict:
    return night_replay.validation_record(_report(classes), ev)


class TestNightReplayCarry:
    def test_a_class_that_left_the_replay_span_keeps_its_result(self):
        prev = _previous({"fan": _cls(12), "closed": _cls(20)})
        rec = night_replay.validation_record(_report({"closed": _cls(20)}), None, previous=prev)
        assert rec["classes"]["fan"]["n_nights"] == 12 and rec["carried_classes"] == ["fan"]
        val = ns.NightValidation.from_dict(rec)
        assert ns.validation_status(val, "fan")[0] == "high"

    def test_a_couple_of_new_nights_do_not_replace_it(self):
        prev = _previous({"fan": _cls(12)})
        rec = night_replay.validation_record(_report({"fan": _cls(2, mae=0.5)}), None, previous=prev)
        assert rec["classes"]["fan"]["n_nights"] == 12

    def test_enough_new_nights_replace_it(self):
        prev = _previous({"fan": _cls(12)})
        rec = night_replay.validation_record(_report({"fan": _cls(10, mae=0.6)}), None, previous=prev)
        assert rec["classes"]["fan"] == _cls(10, mae=0.6) and rec["carried_classes"] == []

    def test_contradicting_nights_speak_at_once(self):
        prev = _previous({"fan": _cls(12)})
        rec = night_replay.validation_record(_report({"fan": _cls(3, mae=2.5)}), None, previous=prev)
        assert rec["classes"]["fan"]["mae_f"] == 2.5

    def test_evidence_hours_never_fall_with_time(self):
        prev = _previous({}, ns.Evidence(30.0, 20.0, 50.0, 0.0, {"s.bed": 700.0}))
        new = ns.Evidence(0.0, 0.0, 70.0, 10.0, {"s.bed": 300.0})
        rec = night_replay.validation_record(_report({}), new, previous=prev)
        ev = night_replay.evidence_from_record(rec)
        assert ev == ns.Evidence(30.0, 20.0, 70.0, 10.0, {"s.bed": 700.0})

    def test_provisional_is_judged_per_class(self):
        """A class final on its own (10+ nights on ok fits) is not held provisional by another class's old record."""
        prev = night_replay.validation_record(
            {**_report({"fan": _cls(12)}), "classes_ok_fits_only": {"fan": _cls(4)}}, None
        )
        assert prev["provisional"] is True and prev["ok_nights"] == {"fan": 4}
        new = {"closed": _cls(20)}
        rec = night_replay.validation_record(_report(new), None, previous=prev)
        assert rec["carried_classes"] == ["fan"] and rec["provisional"] is True  # the carried class is not final
        prev2 = night_replay.validation_record(_report({"fan": _cls(12), "closed": _cls(2)}), None)
        assert prev2["provisional"] is True  # closed has too few ok nights
        rec2 = night_replay.validation_record(_report({"closed": _cls(20)}), None, previous=prev2)
        assert rec2["carried_classes"] == ["fan"] and rec2["ok_nights"]["fan"] == 12
        assert rec2["provisional"] is False  # closed is final now, fan carries its own final count
        assert ns.validation_status(ns.NightValidation.from_dict(rec2), "fan")[0] == "high"

    def test_no_previous_record_is_unchanged(self):
        rec = night_replay.validation_record(_report({"fan": _cls(2)}), None)
        assert rec["classes"]["fan"]["n_nights"] == 2 and rec["carried_classes"] == []
