"""Issue #1722: the four worker-process jobs rebuild their callables from JSON and equal the in-process call.

Occupant view: the long fits (v4 fit, display fit, night replay, cycle-model refit) run in a worker process so Home
Assistant stays responsive. They must learn exactly what they learned in the process: the same model, the same
night-strategy evidence, the same cycle rate. Each test builds the payload with the real builder, pushes it through
``json.dumps`` / ``json.loads`` (the pipe to the worker) and runs the real job; the expected value is the real sync
function called directly with the callables. Nothing here re-implements the code under test.
"""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import cycle_model as cm
from custom_components.climate_advisor import night_replay as nr
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow as vs
from tests.test_cycle_model_1512 import T0, synth

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=TZ)
PRIMARY = "climate.a"
SENSORS = ["climate.a", "sensor.b"]
DAYS = 4  # small, and still a usable ("low") model
WRAP = ("22:30", "06:30")  # wraps midnight
AFTER_MIDNIGHT = ("01:00", "07:00")  # does not wrap (Issue #1376)


def gen_rows(days: int = DAYS, step_min: int = 30, sensors=("climate.a", "sensor.b", "sensor.c"), seed: int = 1):
    """Synthetic chart rows: a thermostat-driven house, a colder second room, a third sensor."""
    rnd = random.Random(seed)
    n = int(days * 24 * 60 / step_min)
    start = NOW - timedelta(minutes=step_min * n)
    ind, heating, out = 68.0, False, []
    for i in range(n):
        ts = start + timedelta(minutes=step_min * i)
        hr = ts.hour + ts.minute / 60
        outd = 35 + 10 * math.sin((hr - 9) / 24 * 2 * math.pi) + 6 * math.sin(ts.timetuple().tm_yday / 5.0)
        outd += rnd.gauss(0, 0.4)
        sp = 66.0 if (hr >= 22.5 or hr < 6.5) else 70.0
        if ind < sp - 1.0:
            heating = True
        elif ind > sp + 0.5:
            heating = False
        ind += step_min / 60 * (0.06 * (outd - ind) + (3.2 if heating else 0.0)) + rnd.gauss(0, 0.05)
        out.append(
            {
                "ts": ts.isoformat(),
                "hvac": "heating" if heating else "idle",
                "fan": False,
                "indoor": round(ind, 2),
                "outdoor": round(outd, 1),
                "windows_open": False,
                "windows_recommended": False,
                "pred_outdoor": round(outd + rnd.gauss(0, 1.5), 1),
                "pred_indoor": None,
                "setpoint": sp,
                "indoor_sensors": {
                    s: round(ind - (0.0 if k == 0 else 1.2 * k) + rnd.gauss(0, 0.05), 2) for k, s in enumerate(sensors)
                },
            }
        )
    return out


def gen_archive(bias: float = 8.0) -> list[dict]:
    """Forecast snapshots every 6 h, each 48 hourly values ``bias`` F off a smooth outdoor curve."""
    snaps = []
    t = NOW - timedelta(days=DAYS + 1)
    while t <= NOW:
        hours = []
        for h in range(48):
            ht = t + timedelta(hours=h)
            val = 35 + 10 * math.sin((ht.hour - 9) / 24 * 2 * math.pi) + bias
            hours.append([ht.isoformat(), round(val, 1)])
        snaps.append({"issued": t.isoformat(), "hours": hours})
        t += timedelta(hours=6)
    return snaps


ROWS = gen_rows()
ARCHIVE = gen_archive()


def archive_fn(snaps: list[dict]):
    """A forecast archive's ``get_snapshots``: honours the window it is asked for and records it."""
    calls: list[tuple[datetime, datetime]] = []

    def get(lo: datetime, hi: datetime) -> list[dict]:
        calls.append((lo, hi))
        return [s for s in snaps if lo <= datetime.fromisoformat(s["issued"]) <= hi]

    get.calls = calls  # type: ignore[attr-defined]
    return get


def wire(payload: dict) -> dict:
    """What the pipe to the worker does to a payload."""
    return json.loads(json.dumps(payload))


def canon(value: Any, drop: tuple[str, ...] = ()) -> str:
    """A comparable JSON text of a result (NaN-safe: equal texts mean equal values)."""
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in drop}
    return json.dumps(value, sort_keys=True)


def fit_kwargs(**over) -> dict:
    base = {
        "chart_entries": ROWS,
        "snapshots_fn": None,
        "tz": TZ,
        "now": NOW,
        "primary": PRIMARY,
        "sensors": SENSORS,
        "train_days": DAYS,
    }
    base.update(over)
    return base


def direct_fit(**kw) -> tuple[dict, float, str]:
    """``run_fit`` called as the coordinator does today: with the callables."""
    sw = kw.pop("sleep_window", None)
    in_sleep = (lambda dt: _coordinator_sleep(dt, sw)) if sw else None
    return vs.run_fit(**{k: v for k, v in kw.items()}, in_sleep=in_sleep)


def _coordinator_sleep(dt: datetime, sw) -> bool:
    """The coordinator's own rule (``automation._in_sleep_window``), the production side of the equivalence."""
    from custom_components.climate_advisor.automation import _in_sleep_window

    return _in_sleep_window(dt, {"sleep_time": sw[0], "wake_time": sw[1]})


# ------------------------------------------------------------------------------------------------ the v4 fit
@pytest.fixture(scope="module")
def cold_fit() -> dict:
    """One usable cold fit, reused as a warm start and as the display-fit and night-replay input."""
    model, _s, _l = vs.run_fit(**fit_kwargs())
    assert model["confidence"] != "none"
    return model


def test_fit_job_equals_run_fit_with_archive_sleep_sensor_and_tz_key() -> None:
    """Non-empty snapshots, a wrapping sleep window with a separate sleep sensor, an IANA tz key."""
    fn = archive_fn(ARCHIVE)
    kw = fit_kwargs(snapshots_fn=fn, sleep_window=list(WRAP), sleep_sensor="sensor.b", climate_entity="climate.a")
    payload = vs.fit_job_payload(**kw)
    assert payload["tz"] == {"key": "America/Chicago"}
    assert payload["snapshots"], "the archive must reach the worker as data"
    got = vs.run_fit_job(wire(payload))
    want = json.loads(json.dumps(direct_fit(**kw)))
    assert got[0]["confidence"] != "none"
    assert canon(got[0]) == canon(want[0])
    assert got[2] == want[2]
    assert isinstance(got[1], float)
    # the archive is not decoration: without it the same fit is a different model
    bare = vs.run_fit(**fit_kwargs())[0]
    assert canon(bare) != canon(want[0])


def test_fit_job_warm_start_fixed_offset_and_after_midnight_sleep(cold_fit) -> None:
    """A warm start from the stored model, a fixed-offset timezone, a sleep window that does not wrap."""
    tz = timezone(timedelta(hours=-6))
    kw = fit_kwargs(
        tz=tz,
        now=NOW.astimezone(tz),
        previous_model=cold_fit,
        sleep_window=list(AFTER_MIDNIGHT),
        sleep_sensor="sensor.b",
        primary_aliases=["climate.a"],
    )
    payload = vs.fit_job_payload(**kw)
    assert payload["tz"] == {"offset_s": -6 * 3600}
    assert payload["previous_model"] == cold_fit
    got = vs.run_fit_job(wire(payload))
    want = json.loads(json.dumps(direct_fit(**kw)))
    assert canon(got[0]) == canon(want[0])
    assert got[2] == want[2]


def test_fit_job_without_sleep_window_or_archive_passes_no_callables(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run_fit(**kw):
        seen.update(kw)
        return {"confidence": "none"}, 0.5, "line"

    monkeypatch.setattr(vs, "run_fit", fake_run_fit)  # resolved at call time: a monkeypatch still holds
    out = vs.run_fit_job(wire(vs.fit_job_payload(**fit_kwargs())))
    assert out == [{"confidence": "none"}, 0.5, "line"]
    assert seen["snapshots_fn"] is None and seen["in_sleep"] is None
    assert seen["now"] == NOW and seen["tz"] == TZ
    # with data they are rebuilt: the snapshots are handed back for any window, the sleep rule answers
    payload = vs.fit_job_payload(**fit_kwargs(snapshots_fn=archive_fn(ARCHIVE), sleep_window=list(WRAP)))
    vs.run_fit_job(wire(payload))
    assert seen["snapshots_fn"](NOW, NOW) == payload["snapshots"]
    assert seen["in_sleep"](NOW.replace(hour=23, minute=0)) is True
    assert seen["in_sleep"](NOW.replace(hour=12, minute=0)) is False


def test_fit_job_payload_prefetches_exactly_the_fit_window_and_windows_rows() -> None:
    fn = archive_fn(ARCHIVE)
    old = [{**ROWS[0], "ts": (NOW - timedelta(days=60)).isoformat()}]
    payload = vs.fit_job_payload(**fit_kwargs(chart_entries=old + ROWS, snapshots_fn=fn))
    assert fn.calls == [vs.fit_snapshot_window(NOW, DAYS)]
    assert fn.calls[0] == (NOW - timedelta(days=DAYS + 1), NOW + timedelta(hours=1))
    assert len(payload["chart_entries"]) == len(ROWS)  # the 60-day-old row is windowed out in the builder
    # and run_fit itself asks for the same window
    fn2 = archive_fn(ARCHIVE)
    vs.run_fit(**fit_kwargs(snapshots_fn=fn2))
    assert fn2.calls == fn.calls
    assert vs.fit_snapshot_window(NOW, None)[0] == NOW - timedelta(days=401)


def test_fit_job_equals_run_fit_when_the_archive_raises() -> None:
    def boom(_lo, _hi):
        raise OSError("archive unreadable")

    kw = fit_kwargs(snapshots_fn=boom)
    payload = vs.fit_job_payload(**kw)
    assert payload["snapshots"] == []
    got = vs.run_fit_job(wire(payload))
    want = json.loads(json.dumps(vs.run_fit(**kw)))
    assert canon(got[0]) == canon(want[0])


# --------------------------------------------------------------------------------------------- tz helpers
def test_encode_decode_tz_key_offset_and_utc() -> None:
    assert vs.encode_tz(TZ, NOW) == {"key": "America/Chicago"}
    assert vs.decode_tz({"key": "America/Chicago"}) == TZ
    fixed = timezone(timedelta(hours=5, minutes=30))
    assert vs.encode_tz(fixed, NOW) == {"offset_s": 19800}
    assert vs.decode_tz({"offset_s": 19800}).utcoffset(None) == timedelta(hours=5, minutes=30)
    assert vs.encode_tz(UTC, NOW) == {"offset_s": 0}
    assert vs.encode_tz(None, NOW) == {"offset_s": 0}

    class Odd(tzinfo):  # a tzinfo whose offset cannot be read
        def utcoffset(self, dt):
            raise RuntimeError("no offset")

    assert vs.encode_tz(Odd(), NOW) == {"offset_s": 0}
    assert vs.decode_tz(None) is UTC
    assert vs.decode_tz({}) is UTC
    # an unknown key on this machine falls back to the offset that came with it, else UTC
    assert vs.decode_tz({"key": "Nowhere/Land", "offset_s": 3600}).utcoffset(None) == timedelta(hours=1)
    assert vs.decode_tz({"key": "Nowhere/Land"}) is UTC


def test_sleep_predicate_boundaries() -> None:
    wrap = vs.sleep_predicate(list(WRAP))
    assert wrap(NOW.replace(hour=22, minute=30)) and wrap(NOW.replace(hour=3, minute=0))
    assert not wrap(NOW.replace(hour=22, minute=29)) and not wrap(NOW.replace(hour=6, minute=30))
    after = vs.sleep_predicate(list(AFTER_MIDNIGHT))
    assert after(NOW.replace(hour=1, minute=0)) and after(NOW.replace(hour=6, minute=59))
    assert not after(NOW.replace(hour=23, minute=0)) and not after(NOW.replace(hour=12, minute=0))
    assert vs.sleep_predicate(None) is None and vs.sleep_predicate([]) is None
    assert vs.sleep_predicate(["22:00", None])(NOW) is False  # an unset time is an empty window


# ----------------------------------------------------------------------------------------------- display fit
def test_display_fit_job_equals_run_display_fit_with_an_extra_sensor(cold_fit) -> None:
    kw = {
        "chart_entries": ROWS,
        "tz": TZ,
        "now": NOW,
        "primary": PRIMARY,
        "sensors": [*SENSORS, "sensor.c"],
        "model_dict": cold_fit,
        "primary_aliases": ["climate.a"],
        "train_days": DAYS,
    }
    payload = vs.display_fit_job_payload(**kw, sleep_window=list(WRAP))
    assert payload["model_dict"] == cold_fit
    got = vs.run_display_fit_job(wire(payload))
    want = json.loads(json.dumps(vs.run_display_fit(**kw, in_sleep=lambda dt: _coordinator_sleep(dt, WRAP))))
    assert "sensor.c" in got[2], got[2]
    assert canon(got[0]) == canon(want[0])
    assert got[2] == want[2]


def test_display_fit_job_without_sleep_window_and_fixed_offset(cold_fit) -> None:
    tz = timezone(timedelta(hours=-6))
    kw = {
        "chart_entries": ROWS,
        "tz": tz,
        "now": NOW.astimezone(tz),
        "primary": PRIMARY,
        "sensors": [*SENSORS, "sensor.c"],
        "model_dict": cold_fit,
        "train_days": DAYS,
    }
    got = vs.run_display_fit_job(wire(vs.display_fit_job_payload(**kw)))
    want = json.loads(json.dumps(vs.run_display_fit(**kw)))
    assert canon(got[0]) == canon(want[0]) and got[2] == want[2]


# ------------------------------------------------------------------------------------------- night replay
NIGHT_DAYS = 6  # the replay needs a week of history before its first replayed night
ROWS_NIGHT = gen_rows(NIGHT_DAYS)
NIGHT = {"train_days": NIGHT_DAYS, "nights": 3, "bed": (22, 30), "wake": (6, 30)}


def test_nightly_job_equals_run_nightly_with_archive_previous_and_sleep_window() -> None:
    fn = archive_fn(ARCHIVE)
    kw = {
        "chart_entries": ROWS_NIGHT,
        "snapshots_fn": fn,
        "tz": TZ,
        "now": NOW,
        "primary": PRIMARY,
        "sensors": SENSORS,
        "primary_aliases": ["climate.a"],
        **NIGHT,
        "model_fitted_at": "2026-11-24T03:15:00+00:00",
    }
    first = nr.run_nightly(**kw)
    kw["previous"] = json.loads(json.dumps(first))
    payload = nr.nightly_job_payload(**kw, sleep_window=list(WRAP))
    assert payload["bed"] == [22, 30] and payload["previous"] == kw["previous"]
    assert fn.calls[-1] == nr.nightly_snapshot_window(NOW, nr.nightly_horizon_days(NIGHT_DAYS, 3))
    got = nr.run_nightly_job(wire(payload))
    want = json.loads(json.dumps(nr.run_nightly(**kw, in_sleep=lambda dt: _coordinator_sleep(dt, WRAP))))
    assert got["model_fitted_at"] == want["model_fitted_at"]
    assert canon(got, ("generated",)) == canon(want, ("generated",))
    assert got["classes"], "the replay must grade at least one night class"


def test_nightly_job_fixed_offset_no_archive_after_midnight_window() -> None:
    tz = timezone(timedelta(hours=-6))
    kw = {
        "chart_entries": ROWS_NIGHT,
        "snapshots_fn": None,
        "tz": tz,
        "now": NOW.astimezone(tz),
        "primary": PRIMARY,
        "sensors": SENSORS,
        **NIGHT,
        "bed": (1, 0),
        "wake": (7, 0),
    }
    payload = nr.nightly_job_payload(**kw, sleep_window=list(AFTER_MIDNIGHT))
    assert payload["snapshots"] is None
    got = nr.run_nightly_job(wire(payload))
    want = json.loads(json.dumps(nr.run_nightly(**kw, in_sleep=lambda dt: _coordinator_sleep(dt, AFTER_MIDNIGHT))))
    assert canon(got, ("generated",)) == canon(want, ("generated",))


def test_nightly_job_resolves_run_nightly_at_call_time_and_unreadable_archive(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(nr, "run_nightly", lambda **kw: seen.update(kw) or {"ok": True})

    def boom(_lo, _hi):
        raise OSError("gone")

    kw = {"chart_entries": ROWS, "snapshots_fn": boom, "tz": TZ, "now": NOW, "primary": PRIMARY, "sensors": SENSORS}
    payload = nr.nightly_job_payload(**kw)
    assert payload["snapshots"] == []
    assert nr.run_nightly_job(wire(payload)) == {"ok": True}
    assert seen["snapshots_fn"](NOW, NOW) == [] and seen["in_sleep"] is None
    assert seen["train_days"] == nr.V4_FIT_TRAIN_DAYS and seen["nights"] == nr.NIGHT_REPLAY_NIGHTS
    assert seen["bed"] == (22, 30) and seen["wake"] == (6, 30) and seen["previous"] is None
    # a payload without the optional keys falls back to the defaults of run_nightly
    assert nr.run_nightly_job({"tz": None, "now": NOW.isoformat(), "primary": PRIMARY}) == {"ok": True}
    assert seen["bed"] == (22, 30) and seen["sensors"] == []


# ------------------------------------------------------------------------------------------ cycle refit
def cycle_pool(mode: str, n: int = 12, seed: int = 3) -> list[dict]:
    return synth(mode, n=n, seed=seed, q=7.0 if mode == "heat" else -6.0)


def test_refit_job_equals_the_coordinator_job_body_with_other_mode_evidence() -> None:
    heat = cycle_pool("heat")
    cool = cycle_pool("cool", seed=5)
    now_ts = heat[-1]["t_off"] + 3600
    cool_fit = cm.fit_cycles(cool, "cool", cm.typical_prior("cool"), now_ts=now_ts)
    snap = {
        "rate_prior": {"k_active_heat": {"value": 5.0, "ts": T0, "n": 40, "keep": 1.0}},
        "cycle_fit_cool": json.loads(json.dumps(cool_fit)),
        "k_passive": {"value": -0.05},
    }
    payload = cm.refit_job_payload(heat, "heat", snap, now_ts)
    assert set(payload) == {"cycles", "mode", "snap", "now_ts"}
    got = cm.refit_job(wire(payload))
    prior = cm.prior_from_cache(snap, "heat", now_ts, cycles=heat)
    want = cm.fit_cycles(heat, "heat", prior, now_ts=now_ts, lag_evidence=snap["cycle_fit_cool"])
    assert got["ne"] > 0 and got["w_by_id"]
    assert canon(got) == canon(want)


def test_refit_job_cool_mode_reads_the_heat_fit_and_empty_snapshot() -> None:
    cool = cycle_pool("cool", n=8, seed=7)
    now_ts = cool[-1]["t_off"] + 3600
    got = cm.refit_job(wire(cm.refit_job_payload(cool, "cool", {}, now_ts)))
    want = cm.fit_cycles(cool, "cool", cm.prior_from_cache({}, "cool", now_ts, cycles=cool), now_ts=now_ts)
    assert canon(got) == canon(want)
    # no cycles at all: the held prior, still a plain dict
    none = cm.refit_job(wire(cm.refit_job_payload([], "heat", {}, T0)))
    assert none["ne"] == 0 and none["w_by_id"] == {}


def test_refit_result_is_json_safe_and_unchanged_by_a_round_trip() -> None:
    """What ``learning.apply_cycle_fit`` and ``CycleStore.set_weights`` read survives the pipe: no NaN or inf, no
    tuples, no non-str keys, so ``json.loads(json.dumps(x)) == x``."""
    heat = cycle_pool("heat", n=10, seed=9)
    now_ts = heat[-1]["t_off"] + 3600
    out = cm.refit_job(wire(cm.refit_job_payload(heat, "heat", {}, now_ts)))
    text = json.dumps(out, allow_nan=False)
    assert json.loads(text) == out
    assert all(isinstance(k, str) for k in out["w_by_id"])
    # the in-process result (before any JSON) is the same value
    direct = cm.fit_cycles(heat, "heat", cm.prior_from_cache({}, "heat", now_ts, cycles=heat), now_ts=now_ts)
    assert json.loads(json.dumps(direct, allow_nan=False)) == direct


def test_v4_results_survive_a_json_round_trip(cold_fit) -> None:
    """The in-process model dict equals its own JSON round-trip (tuples, int keys or NaN would break this)."""
    assert json.loads(json.dumps(cold_fit, allow_nan=False)) == cold_fit
    assert p4.from_dict(json.loads(json.dumps(cold_fit))) is not None
