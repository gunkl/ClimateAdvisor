"""Tests for tools/predict_backtest.py (Issue #1230 R1, DP-8): the walk-forward v4 vs v3 vs no-change backtest.

The v4 predictor (lane A) is replaced by a stand-in implementing the contract in
/home/dev/ca-group-briefs/1230-r1-v4-contract.md: it learns a mean-by-local-hour profile from the
samples it is fitted on, so what it knows is exactly what the fit was handed.
"""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(_TOOLS))
_spec = importlib.util.spec_from_file_location("predict_backtest", _TOOLS / "predict_backtest.py")
pb = importlib.util.module_from_spec(_spec)
sys.modules["predict_backtest"] = pb
_spec.loader.exec_module(pb)

START = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday


# ---------------------------------------------------------------------------
# Stand-in predictor_v4 + synthetic house
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeInput:
    ts: datetime
    outdoor_f: float
    heat_sp_f: float | None
    cool_sp_f: float | None
    windows_open: bool = False
    fan_running: bool = False


class FakePredictor:
    """Contract stand-in: hour-of-day mean profile fitted on whatever samples it is given."""

    V4Input = FakeInput

    def __init__(self, confidence: str = "ok"):
        self.confidence = confidence
        self.resample_calls: list[tuple[datetime, datetime, object]] = []
        self.fit_calls: list[dict] = []
        self.predict_calls: list[dict] = []

    def samples_from_chart_log(self, entries, *, tz, archive_snapshots=None):
        out = []
        for e in entries:
            ts = datetime.fromisoformat(e["ts"])
            readings = dict(e.get("indoor_sensors") or {})
            readings["_ca"] = e.get("indoor")
            out.append(SimpleNamespace(ts=ts, readings=readings))
        if out:
            self.resample_calls.append((out[0].ts, out[-1].ts, archive_snapshots))
        return out

    def fit(self, samples, *, primary, sensors, tz, now, horizon_h=12, max_iterations=60):
        by_hour: dict[int, list[float]] = {}
        for s in samples:
            v = s.readings.get(primary)
            if v is not None:
                by_hour.setdefault(s.ts.astimezone(tz).hour, []).append(v)
        profile = {h: sum(v) / len(v) for h, v in by_hour.items()}
        self.fit_calls.append({"now": now, "max_ts": max(s.ts for s in samples), "n": len(samples), "profile": profile})
        span = (samples[-1].ts - samples[0].ts).total_seconds() / 86400
        return SimpleNamespace(
            confidence=self.confidence,
            fitted_at=now.isoformat(),
            fit={"span_days": round(span, 1)},
            sensors=tuple(sensors),
            primary=primary,
            profile=profile,
        )

    def step_mass(self, model, *, mass_f, readings, sample):
        return (mass_f or 0.0) + 1.0

    def predict(self, model, *, now, readings, mass_f, inputs, tz):
        self.predict_calls.append({"now": now, "readings": dict(readings), "inputs": list(inputs), "mass": mass_f})
        if model.confidence == "none":
            return None
        return {
            s: [
                (i.ts, model.profile.get(i.ts.astimezone(tz).hour, 70.0) + (0.0 if s == model.primary else -1.0))
                for i in inputs
            ]
            for s in model.sensors
        }


@pytest.fixture
def fake():
    old = pb._PREDICTOR
    pb._PREDICTOR = FakePredictor()
    yield pb._PREDICTOR
    pb._PREDICTOR = old


def make_rows(days: int, *, start: datetime = START, sensors: bool = False, ramp: bool = False, step=None):
    rows = []
    for i in range(days * 48):
        ts = start + timedelta(minutes=30 * i)
        h = ts.hour + ts.minute / 60
        indoor = 60 + 0.01 * i if ramp else 70 + 2 * math.sin(2 * math.pi * (h - 15) / 24)
        if step and ts >= step[0]:
            indoor += step[1]
        outdoor = 55 + 10 * math.sin(2 * math.pi * (h - 15) / 24)
        row = {
            "ts": ts.isoformat(),
            "hvac": "off",
            "fan": False,
            "fan_running": False,
            "indoor": indoor,
            "outdoor": outdoor,
            "windows_open": False,
            "windows_recommended": False,
            "pred_outdoor": outdoor + 1.0,
            "pred_indoor": indoor + 0.5,
            "lower": 68.0,
            "upper": 75.0,
            "setpoint": 68.0,
        }
        if sensors:
            row["indoor_sensors"] = {"climate.t": indoor, "sensor.bed": indoor - 1.0}
        rows.append(row)
    return rows


def args_for(*extra: str):
    return pb.build_parser().parse_args(["--no-v3", "--train-days", "30", *extra])


def run(rows, *extra, learning_db=None, snaps=None, zone="z1"):
    return pb.run_backtest(rows, args_for(*extra), tz=UTC, zone=zone, learning_db=learning_db, archive_snapshots=snaps)


# ---------------------------------------------------------------------------
# Walk-forward: no leakage
# ---------------------------------------------------------------------------


def test_fit_never_sees_the_test_week(fake):
    rows = make_rows(28)
    rep = run(rows, "--no-diagnostic")
    assert fake.fit_calls, "expected at least one fit"
    for call in fake.fit_calls:
        assert call["max_ts"] < call["now"], "fit was handed a sample at/after the test-week start"
    # every predict is for an anchor inside a test week, and every input is in the future of its anchor
    for call in fake.predict_calls:
        assert all(i.ts > call["now"] for i in call["inputs"])
    assert rep["weeks"][0]["week_start"].startswith("2026-03-09")


def test_step_change_in_test_week_is_not_foreseen(fake):
    """A +10 F step on Mon Mar 23: a model that peeked would learn it; the walk-forward one must not."""
    rows = make_rows(28, step=(datetime(2026, 3, 23, tzinfo=UTC), 10.0))
    rep = run(rows, "--max-weeks", "1", "--no-diagnostic")
    assert rep["weeks"][-1]["week_start"].startswith("2026-03-23")
    profile = fake.fit_calls[-1]["profile"]
    assert max(profile.values()) < 73.5, "fit profile includes post-step (test week) data"
    st = rep["stats"]["_ca"]["1h"]["heating"]
    assert st["v4_bias"] < -8.0  # under-predicts by ~10 F: the step was a surprise
    assert st["flat_mae"] < st["v4_mae"]  # and persistence wins that week, as it should


def test_leaky_resampler_output_is_dropped_and_counted(fake):
    real = fake.samples_from_chart_log

    def leaky(entries, *, tz, archive_snapshots=None):
        out = real(entries, tz=tz, archive_snapshots=archive_snapshots)
        if archive_snapshots is None and entries and out:
            pass
        late = SimpleNamespace(ts=out[-1].ts + timedelta(hours=3), readings={"_ca": 99.0})
        return [*out, late]

    fake.samples_from_chart_log = leaky
    rep = run(make_rows(28), "--max-weeks", "1", "--no-diagnostic")
    assert rep["weeks"][-1]["dropped_future_samples"] >= 1
    assert all(c["max_ts"] < c["now"] for c in fake.fit_calls)
    assert all(v < 90 for v in fake.fit_calls[-1]["profile"].values())


def test_assert_no_leak_raises():
    cutoff = datetime(2026, 3, 9, tzinfo=UTC)
    pb.assert_no_leak([cutoff - timedelta(seconds=1)], cutoff, "ok")
    with pytest.raises(pb.LeakageError):
        pb.assert_no_leak([cutoff], cutoff, "rows")


def test_archive_snapshots_after_the_week_are_not_given_to_fit(fake):
    rows = make_rows(28)
    snaps = [
        {"issued": "2026-03-10T00:00+00:00", "source": "w", "hours": [["2026-03-10T00:00+00:00", 40.0]]},
        {"issued": "2026-03-20T00:00+00:00", "source": "w", "hours": [["2026-03-20T00:00+00:00", 41.0]]},
    ]
    run(rows, "--max-weeks", "1", "--no-diagnostic", snaps=snaps)
    fit_resample = [c for c in fake.resample_calls if c[2] is not None]
    assert fit_resample
    for _, _, given in fit_resample:
        assert all(pb.parse_ts(s["issued"], UTC) < datetime(2026, 3, 23, tzinfo=UTC) for s in given)


# ---------------------------------------------------------------------------
# Baselines, seasons
# ---------------------------------------------------------------------------


def test_persistence_baseline_is_exact(fake):
    rows = make_rows(28, ramp=True)  # +0.01 F per 30-min row => a lead of L hours moves 0.02*L F
    rep = run(rows, "--no-diagnostic")
    for lead in (1, 4, 12, 24):
        st = rep["stats"]["_ca"][f"{lead}h"]["heating"]
        assert st["flat_mae"] == pytest.approx(0.02 * lead, abs=1e-3)
        assert st["flat_bias"] == pytest.approx(-0.02 * lead, abs=1e-3)  # predicted - actual
        assert st["n"] > 100


def test_season_split_heating_shoulder_summer(fake):
    rows = make_rows(120)  # Mar 2 .. Jun 29
    rep = run(rows, "--no-diagnostic", "--anchor-step-hours", "3")
    per = rep["stats"]["_ca"]["4h"]
    assert {"heating", "shoulder", "summer", "all"} <= set(per)
    assert per["all"]["n"] == sum(per[s]["n"] for s in ("heating", "shoulder", "summer"))
    assert pb.season_of(datetime(2026, 11, 1)) == pb.season_of(datetime(2026, 3, 31)) == "heating"
    assert pb.season_of(datetime(2026, 6, 1)) == pb.season_of(datetime(2026, 9, 30)) == "summer"
    assert {pb.season_of(datetime(2026, m, 1)) for m in (4, 5, 10)} == {"shoulder"}
    # the season is the ANCHOR's: a 24 h prediction made on Mar 31 evening is heating
    heat = [p for p in rep["points"]["_ca"]["24h"] if p["t"].startswith("2026-04-01")]
    assert heat  # scored target on Apr 1 (from a Mar 31 anchor) exists; counted under heating


def test_v4_beats_flat_on_a_periodic_house(fake):
    rep = run(make_rows(28), "--no-diagnostic")
    st = rep["stats"]["_ca"]["12h"]["heating"]
    assert st["v4_mae"] < 0.3 < st["flat_mae"]
    assert abs(st["v4_bias"]) < 0.2


def test_v4_none_is_counted_and_excluded_from_common_set(fake):
    fake.confidence = "none"
    rep = run(make_rows(28), "--no-diagnostic")
    st = rep["stats"]["_ca"]["4h"]["heating"]
    assert st["n"] == 0 and st["base_n"] > 0 and st["coverage"] == 0.0
    assert rep["run"]["skipped"]["v4_none"] > 0
    assert rep["gate_summary"]["ready"] is False


def test_diagnostic_variant_reported_unless_disabled(fake):
    with_diag = run(make_rows(28), "--max-weeks", "1")
    assert with_diag["stats"]["_ca"]["4h"]["heating"]["v4_actual_inputs_mae"] is not None
    no_diag = run(make_rows(28), "--max-weeks", "1", "--no-diagnostic")
    assert no_diag["stats"]["_ca"]["4h"]["heating"]["v4_actual_inputs_mae"] is None


def test_per_sensor_scoring_and_primary_selection(fake):
    rows = make_rows(28, sensors=True)
    primary, nodes = pb.select_sensors(rows, None)
    assert primary == "climate.t" and nodes[0] == "climate.t" and "sensor.bed" in nodes
    assert pb.select_sensors(make_rows(3), None) == ("_ca", ["_ca"])
    rep = run(rows, "--no-diagnostic", "--max-weeks", "1")
    assert {"_ca", "climate.t", "sensor.bed"} <= set(rep["stats"])
    assert rep["stats"]["climate.t"]["4h"]["heating"]["v3_mae"] is None  # v3 predicts one line only
    ids = [s["id"] for s in rep["sensors"]]
    assert ids[0] == "_ca" and "climate.t" in ids


# ---------------------------------------------------------------------------
# Inputs: forecast, setpoints, windows
# ---------------------------------------------------------------------------


def test_archive_snapshot_preferred_and_future_snapshot_ignored(fake):
    rows = make_rows(28)
    snaps = []
    for h in range(0, 28 * 24):
        issued = START + timedelta(hours=h)
        snaps.append(
            {
                "issued": issued.isoformat(timespec="minutes"),
                "source": "w",
                "hours": [[(issued + timedelta(hours=k)).isoformat(timespec="minutes"), 33.0] for k in range(0, 73)],
            }
        )
    rep = run(rows, "--max-weeks", "1", "--no-diagnostic", snaps=snaps)
    assert rep["run"]["forecast_source_anchors"]["archive"] > 100
    assert rep["run"]["forecast_note"] == "forecast archive used"
    assert all(abs(i.outdoor_f - 33.0) < 1e-6 for c in fake.predict_calls for i in c["inputs"])
    # without the archive: chart_log pred_outdoor (outdoor + 1)
    fake.predict_calls.clear()
    rep2 = run(rows, "--max-weeks", "1", "--no-diagnostic")
    assert rep2["run"]["forecast_source_anchors"]["archive"] == 0
    assert "lead-0" in rep2["run"]["forecast_note"]
    assert any(abs(i.outdoor_f - 33.0) > 5 for c in fake.predict_calls for i in c["inputs"])


def test_forecast_index_ignores_snapshot_issued_after_anchor():
    idx = pb.ChartIndex(make_rows(3), UTC, [])
    snaps = [{"issued": "2026-03-03T12:00+00:00", "source": "w", "hours": [["2026-03-03T13:00+00:00", 11.0]]}]
    fc = pb.ForecastIndex(snaps, idx)
    early = datetime(2026, 3, 3, 6, tzinfo=UTC)
    assert fc.snapshot(early.timestamp()) is None
    late = datetime(2026, 3, 3, 13, tzinfo=UTC)
    assert fc.snapshot(late.timestamp()) is not None


def test_decode_archive_matches_the_on_disk_schema():
    data = {
        "snapshots": [
            {
                "i": "2026-11-06T14:05-07:00",
                "s": "weather.home",
                "h0": "2026-11-06T14:00-07:00",
                "t": [54.3, 53.1, None, 51.8],
            }
        ]
    }
    out = pb.decode_archive(data)
    assert out[0]["issued"] == "2026-11-06T14:05-07:00"
    assert [h[0] for h in out[0]["hours"]] == [
        "2026-11-06T14:00-07:00",
        "2026-11-06T15:00-07:00",
        "2026-11-06T17:00-07:00",
    ]
    assert pb.decode_archive({"snapshots": "bad"}) == [] and pb.decode_archive(None) == []


def test_setpoints_scheduled_vs_hold(fake):
    rows = make_rows(28)
    for r in rows:  # band widens at 18:00 every day
        if datetime.fromisoformat(r["ts"]).hour >= 18:
            r["lower"], r["upper"] = 60.0, 80.0
    run(rows, "--max-weeks", "1", "--no-diagnostic")
    sched = [c for c in fake.predict_calls if c["now"].hour == 12][0]
    assert {i.heat_sp_f for i in sched["inputs"]} == {68.0, 60.0}
    fake.predict_calls.clear()
    run(rows, "--max-weeks", "1", "--no-diagnostic", "--setpoints", "hold")
    held = [c for c in fake.predict_calls if c["now"].hour == 12][0]
    assert {i.heat_sp_f for i in held["inputs"]} == {68.0}


def test_windows_planned_open_only_when_recommended_at_anchor(fake):
    rows = make_rows(28)
    for r in rows:
        if datetime.fromisoformat(r["ts"]).hour == 20:
            r["windows_recommended"] = True
    run(rows, "--max-weeks", "1", "--no-diagnostic")
    open_call = next(c for c in fake.predict_calls if c["now"].hour == 20)
    flags = [i.windows_open for i in open_call["inputs"]]
    assert flags[0] and sum(flags) == 16  # 4 h of 15-min steps
    assert not any(
        i.windows_open or i.fan_running for c in fake.predict_calls if c["now"].hour == 3 for i in c["inputs"]
    )


def test_diagnostic_uses_actual_windows_and_fan(fake):
    rows = make_rows(28)
    for r in rows:
        if datetime.fromisoformat(r["ts"]).hour == 23:
            r["windows_open"], r["fan_running"] = True, True
    run(rows, "--max-weeks", "1")
    # per anchor: headline call then diagnostic call
    calls = [c for c in fake.predict_calls if c["now"].hour == 21]
    headline, diag = calls[0], calls[1]
    assert not any(i.windows_open for i in headline["inputs"])
    assert any(i.windows_open and i.fan_running for i in diag["inputs"])


def test_readings_given_to_predict_are_causal(fake):
    rows = make_rows(28, ramp=True)
    run(rows, "--max-weeks", "1", "--no-diagnostic")
    for c in fake.predict_calls:
        now_val = 60 + 0.01 * ((c["now"] - START).total_seconds() / 1800)
        assert c["readings"]["_ca"] == pytest.approx(now_val, abs=1e-6)  # never a later row


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _stats(v4=0.8, v3=1.6, flat=1.5, bias=0.1, n=200, cov=1.0):
    return {"n": n, "base_n": n, "coverage": cov, "v4_mae": v4, "v3_mae": v3, "flat_mae": flat, "v4_bias": bias}


def _full_stats(**kw):
    return {"_ca": {f"{h}h": {s: _stats(**kw) for s in ("heating", "summer")} for h in (1, 4, 12)}}


def test_gate_all_pass():
    gate, others, summary = pb.build_gate(_full_stats(), "z", v3_enabled=True, min_n=72, min_coverage=0.8)
    assert len(gate) == 6 and all(g["pass"] and g["scored"] for g in gate)
    assert summary["ready"] and summary["passed"] == 6 and others == []


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"v4": 1.7}, "v3"),
        ({"v4": 1.6, "v3": 2.0, "flat": 1.5}, "no-change"),
        ({"bias": 0.51}, "bias"),
        ({"bias": -0.51}, "bias"),
        ({"cov": 0.5}, "coverage"),
    ],
)
def test_gate_failures(kw, reason):
    gate, _, summary = pb.build_gate(_full_stats(**kw), "z", v3_enabled=True, min_n=72, min_coverage=0.8)
    assert not any(g["pass"] for g in gate) and not summary["ready"]
    assert any(reason in r for g in gate for r in g["reasons"])


def test_gate_bias_exactly_at_limit_passes_and_equal_mae_fails():
    ok = pb.gate_row("z", "_ca", "summer", 4, _stats(bias=0.5), require_v3=True, min_n=10, min_cov=0.8)
    assert ok["pass"]
    tie = pb.gate_row("z", "_ca", "summer", 4, _stats(v4=1.5, flat=1.5), require_v3=True, min_n=10, min_cov=0.8)
    assert not tie["pass"]


def test_gate_unscored_when_few_anchors_or_missing_season_or_no_v3():
    few = pb.gate_row("z", "_ca", "summer", 4, _stats(n=10), require_v3=True, min_n=72, min_cov=0.8)
    assert few["scored"] is False and few["pass"] is False
    stats = _full_stats()
    del stats["_ca"]["4h"]["summer"]
    gate, _, summary = pb.build_gate(stats, "z", v3_enabled=True, min_n=72, min_coverage=0.8)
    assert not summary["ready"] and summary["passed"] == 5 and summary["scored"] == 5
    no_v3 = pb.gate_row("z", "_ca", "summer", 4, _stats(v3=None), require_v3=True, min_n=10, min_cov=0.8)
    assert no_v3["scored"] is False
    _, _, s2 = pb.build_gate(_full_stats(), "z", v3_enabled=False, min_n=72, min_coverage=0.8)
    assert s2["ready"] is False  # without a v3 replay nothing can be proven


def test_other_sensors_gate_against_flat_only():
    stats = _full_stats()
    stats["sensor.bed"] = {f"{h}h": {s: _stats(v3=None) for s in ("heating", "summer")} for h in (1, 4, 12)}
    gate, others, summary = pb.build_gate(stats, "z", v3_enabled=True, min_n=72, min_coverage=0.8)
    assert len(others) == 6 and all(g["pass"] for g in others) and summary["other_sensors_failing"] == 0
    assert all(g["sensor"] == "_ca" for g in gate)


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


def test_publish_payload_matches_the_endpoint_contract(fake):
    rep = run(make_rows(28, sensors=True), "--max-weeks", "1")
    payload = pb.build_publish_payload(rep)
    json.dumps(payload)  # serialisable
    assert payload["zone"] == "z1" and payload["unit"] == "F" and payload["schema_version"] == 1
    pt = payload["points"]["_ca"]["4h"][0]
    assert {"t", "actual", "v4"} <= set(pt) and pt["t"].endswith("+00:00")
    assert set(payload["points"]["climate.t"]) == {"1h", "4h", "12h", "24h"}
    st = payload["stats"]["_ca"]["4h"]["heating"]
    assert {"n", "v4_mae", "v3_mae", "flat_mae", "v4_bias", "v3_bias"} <= set(st)
    g = payload["gate"][0]
    assert {"zone", "season", "lead", "v4", "v3", "flat", "bias", "pass", "scored"} <= set(g)
    assert {x["season"] for x in payload["gate"]} == {"heating", "summer"}
    assert {x["lead"] for x in payload["gate"]} == {"1h", "4h", "12h"}
    assert {"confidence", "fitted_at", "span_days"} <= set(payload["model"])
    assert "weeks" not in payload and "run" not in payload


def test_publish_remote_is_atomic_and_mode_600(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", "host", remote])
    dest = pb.publish_remote({}, "abc123", {"zone": "abc123", "points": {}})
    assert dest == "/config/climate_advisor_v4_backtest_abc123.json"
    remote = seen["cmd"][-1]
    assert remote.startswith("umask 077 && cat > ")
    assert f"{dest}.tmp" in remote and "chmod 600" in remote and remote.rstrip().endswith(dest)
    assert remote.index("chmod 600") < remote.index("mv ")
    assert json.loads(seen["input"])["zone"] == "abc123"


def test_publish_remote_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"boom"))
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", remote])
    with pytest.raises(RuntimeError, match="boom"):
        pb.publish_remote({}, "abc", {})


def test_backtest_file_name_is_entry_scoped():
    assert pb.remote_path(pb.BACKTEST_FILE, "e1") == "/config/climate_advisor_v4_backtest_e1.json"
    try:
        from custom_components.climate_advisor import const  # noqa: PLC0415

        assert const.FORECAST_ARCHIVE_FILE == pb.FORECAST_ARCHIVE_FILE
    except ImportError:  # pragma: no cover
        pass


def test_text_report_and_json_cli(fake, tmp_path, capsys):
    chart = tmp_path / "chart.json"
    chart.write_text(json.dumps({"entries": make_rows(28)}))
    out = tmp_path / "out.json"
    rc = pb.main(
        [
            "--chart-log",
            str(chart),
            "--no-v3",
            "--train-days",
            "30",
            "--max-weeks",
            "2",
            "--json",
            str(out),
            "--zone",
            "zz",
            "-q",
        ]
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "=== Zone zz" in text and "DP-8 gate" in text and "NOT READY" in text and "total wall time" in text
    data = json.loads(out.read_text())
    assert "zz" in data["zones"] and "points" not in data["zones"]["zz"]
    assert data["zones"]["zz"]["gate_summary"]["total"] == 6
    out2 = tmp_path / "out2.json"
    pb.main(
        [
            "--chart-log",
            str(chart),
            "--no-v3",
            "--train-days",
            "30",
            "--max-weeks",
            "1",
            "--json",
            str(out2),
            "--json-points",
            "-q",
        ]
    )
    assert "points" in json.loads(out2.read_text())["zones"]["local"]


# ---------------------------------------------------------------------------
# SSH path (mocked) and zone discovery
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, zones, rows):
        self.zones, self.rows = zones, rows
        self.writes: list[tuple[str, bytes]] = []
        self.commands: list[str] = []

    def run(self, cmd, **kw):
        remote = cmd[-1]
        self.commands.append(remote)
        ok = lambda out=b"": subprocess.CompletedProcess(cmd, 0, out, b"")  # noqa: E731
        if remote.startswith("ls "):
            listing = "".join(f"/config/climate_advisor_chart_log_{z}.json\n" for z in self.zones)
            return subprocess.CompletedProcess(cmd, 0, listing if kw.get("text") else listing.encode(), "")
        if remote.startswith("umask 077"):
            self.writes.append((remote, kw["input"]))
            return ok()
        if "chart_log" in remote:
            return ok(json.dumps({"entries": self.rows}).encode())
        if "core.config" in remote:
            return ok(json.dumps({"data": {"time_zone": "UTC"}}).encode())
        return subprocess.CompletedProcess(cmd, 1, b"", b"No such file")  # learning DB / archive absent


@pytest.fixture
def host(monkeypatch):
    h = FakeHost(["aaa", "bbb"], make_rows(28))
    monkeypatch.setattr(subprocess, "run", h.run)
    monkeypatch.setattr(pb, "load_config", lambda: {"HA_API_TOKEN": "SECRET-TOKEN-123", "HA_HOST": "h"})
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", "host", remote])
    return h


def test_zone_discovery_lists_entry_scoped_chart_logs(host):
    assert pb.discover_zones({}) == ["aaa", "bbb"]
    assert host.commands[-1] == "ls /config/climate_advisor_chart_log_*.json 2>/dev/null"


def test_all_zones_over_ssh_publishes_each_without_leaking_secrets(fake, host, capsys):
    rc = pb.main(["--all-zones", "--no-v3", "--train-days", "30", "--max-weeks", "1", "--publish", "-q"])
    assert rc == 0
    cap = capsys.readouterr()
    assert "SECRET-TOKEN-123" not in cap.out + cap.err
    assert len(host.writes) == 2
    dests = sorted(w[0].rstrip().split()[-1] for w in host.writes)
    assert dests == ["/config/climate_advisor_v4_backtest_aaa.json", "/config/climate_advisor_v4_backtest_bbb.json"]
    for _, body in host.writes:
        d = json.loads(body)
        assert d["points"] and d["gate"] and d["stats"]
    assert any("/config/climate_advisor_chart_log_aaa.json" in c for c in host.commands)
    assert any("climate_advisor_forecast_archive_aaa.json" in c for c in host.commands)


def test_multiple_zones_without_flag_is_an_error(fake, host, capsys):
    assert pb.main(["--no-v3", "-q"]) == 2
    assert "multiple zones" in capsys.readouterr().err


def test_single_zone_flag_reads_only_that_zone(fake, host):
    pb.main(["--zone", "bbb", "--no-v3", "--train-days", "30", "--max-weeks", "1", "-q"])
    assert not any("chart_log_aaa" in c for c in host.commands)
    assert any("chart_log_bbb" in c for c in host.commands)


# ---------------------------------------------------------------------------
# v3: the production helper, imported headless
# ---------------------------------------------------------------------------


def _db(n_obs=40):
    obs = [
        {
            "date": f"2026-02-{(i % 27) + 1:02d}",
            "hvac_mode": "passive",
            "k_passive": -0.05,
            "confidence_grade": "high",
            "r_squared_passive": 0.9,
        }
        for i in range(n_obs)
    ]
    obs += [{"date": "2026-04-15", "hvac_mode": "passive", "k_passive": -0.5, "confidence_grade": "high"}]
    return {
        "thermal_observations": obs,
        "thermal_model_cache": {"k_passive": -0.05, "observation_count_passive": n_obs},
    }


def test_production_coordinator_helper_imports_headless_and_predicts():
    coord = pb.load_coordinator()
    assert callable(coord._build_predicted_indoor_future) and callable(coord._compute_day_hvac_modes)
    rows = make_rows(5)
    idx = pb.ChartIndex(rows, UTC, [])
    anchor = datetime(2026, 3, 5, 12, tzinfo=UTC)
    hourly = [50.0] * 26
    model = pb.V3Models(_db(), "asof").for_week(datetime(2026, 3, 9, tzinfo=UTC))[0]
    out = pb.v3_curve(
        coord,
        idx=idx,
        anchor=anchor,
        hourly=hourly,
        model=model,
        band_fn=lambda a, t: idx.band_at(t),
        plan_windows=False,
        plan_hours=4,
    )
    leads = {h: int(round((anchor + timedelta(hours=h)).timestamp())) for h in (1, 4, 12, 24)}
    assert all(k in out for k in leads.values()), "production curve must cover 1/4/12/24 h"
    assert all(40 < v < 90 for v in out.values())
    # the half-hour interpolation points are production's own
    assert int(anchor.timestamp()) + 1800 + 3600 in out


def test_v3_replay_runs_production_code_and_matches_the_logged_sanity_row(fake):
    rows = make_rows(40)
    args = pb.build_parser().parse_args(["--train-days", "30", "--max-weeks", "1", "--no-diagnostic"])
    rep = pb.run_backtest(rows, args, tz=UTC, zone="z", learning_db=_db())
    assert rep["run"]["v3"]["enabled"] is True
    per = rep["stats"]["_ca"]["4h"]
    st = per.get("heating") or per["shoulder"]
    assert st["v3_mae"] is not None and st["n"] > 0
    assert st["logged_pred_indoor_mae"] == pytest.approx(0.5, abs=1e-3)  # sanity row: pred_indoor = indoor + 0.5


def test_v3_model_as_of_uses_only_prior_observations():
    models = pb.V3Models(_db(), "asof")
    _, src_early = models.for_week(datetime(2026, 2, 1, tzinfo=UTC))
    assert src_early == "current"  # nothing dated before Feb 1: falls back to the current cache (reported)
    mid, src = models.for_week(datetime(2026, 3, 9, tzinfo=UTC))
    assert src == "asof(40 obs)"  # the April observation is in the future of this week
    assert mid["k_passive"] == pytest.approx(-0.05, abs=1e-4)
    later, src_l = models.for_week(datetime(2026, 4, 20, tzinfo=UTC))
    assert src_l == "asof(41 obs)" and later["k_passive"] < -0.05
    cur = pb.V3Models(_db(), "current").for_week(datetime(2026, 3, 9, tzinfo=UTC))
    assert cur[1] == "current"


def test_v3_off_without_learning_db(fake):
    args = pb.build_parser().parse_args(["--train-days", "30", "--max-weeks", "1", "--no-diagnostic"])
    rep = pb.run_backtest(make_rows(28), args, tz=UTC, zone="z", learning_db=None)
    assert rep["run"]["v3"]["enabled"] is False
    assert rep["gate_summary"]["ready"] is False
