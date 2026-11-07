"""GET /api/climate_advisor/prediction_accuracy (Issue #1230 R1): shape, ranges, downsampling, units, merge, auth.

Occupant view: the comparison page shows what v4 said N hours ago next to what the house then did and what today's
prediction said. The numbers must be right, in the occupant's unit, and reading them must change nothing in the house.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.api import API_VIEWS, ClimateAdvisorPredictionAccuracyView
from custom_components.climate_advisor.const import API_PREDICTION_ACCURACY, DOMAIN, V4_BACKTEST_FILE
from custom_components.climate_advisor.storage_paths import resolve_entry_scoped_path
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key

_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
_CLIMATE = "climate.hall"
_FINE = "sensor.hall_temperature"


def _rows(days: float = 40.0, *, with_sensor: bool = True) -> list[dict]:
    """Chart rows every 30 min: indoor drifts daily 66..70 F, v3's pred_indoor is 1 F too warm."""
    out = []
    steps = int(days * 48)
    for k in range(steps, -1, -1):
        ts = _NOW - timedelta(minutes=30 * k)
        indoor = 68.0 + 2.0 * ((ts.hour + ts.minute / 60) / 24.0 - 0.5)
        row = {
            "ts": ts.isoformat(),
            "hvac": "heating" if 6 <= ts.hour < 8 else "idle",
            "indoor": round(indoor, 2),
            "pred_indoor": round(indoor + 1.0, 2),
            "outdoor": 40.0,
        }
        if with_sensor:
            row["indoor_sensors"] = {_CLIMATE: round(indoor), _FINE: round(indoor + 0.1, 2)}
        out.append(row)
    return out


def _indoor_at(ts: datetime) -> float:
    return round(68.0 + 2.0 * ((ts.hour + ts.minute / 60) / 24.0 - 0.5), 2)


def _archive(
    tmp_path: Path, *, days: float = 40.0, bias: float = 0.5, leads=("4h",), sensors=("_ca", _FINE)
) -> V4Archive:
    """v4 says the true indoor value plus ``bias`` for every slot."""
    arch = V4Archive(tmp_path, entry_id="zoneA")
    for k in range(int(days * 48), 0, -1):
        ts = _NOW - timedelta(minutes=30 * k)
        epoch = slot_key(ts)
        for sensor in sensors:
            extra = 0.1 if sensor == _FINE else 0.0
            for lead in leads:
                arch.put(sensor, epoch, lead, round(_indoor_at(ts) + extra + bias, 2))
    return arch


def _build(tmp_path: Path, **over):
    kwargs = dict(
        zone="zoneA",
        sensor="_ca",
        lead="4h",
        range_str="24h",
        before=None,
        now=_NOW,
        unit="fahrenheit",
        chart_rows=_rows(),
        archive=_archive(tmp_path),
        backtest=None,
        model={"has_model": True, "confidence": "ok", "fitted_at": "2026-11-06T03:15:00+00:00", "span_days": 30.0},
        sensors_meta=[
            {"id": "_ca", "name": "Climate Advisor reading", "role": "the reading Climate Advisor uses"},
            {"id": _FINE, "name": "Hall", "role": "thermostat's finer sensor"},
        ],
    )
    kwargs.update(over)
    return pa.build_prediction_accuracy(**kwargs)


def test_response_shape_matches_the_contract(tmp_path: Path) -> None:
    out = _build(tmp_path)
    assert set(out) >= {
        "zone", "unit", "sensors", "lead", "range", "start", "end", "downsample",
        "points", "heat_runs", "stats", "model", "gate",
    }  # fmt: skip
    assert out["zone"] == "zoneA" and out["unit"] == "°F" and out["lead"] == "4h" and out["range"] == "24h"
    assert out["downsample"] == "raw"
    assert out["sensors"][0] == {
        "id": "_ca",
        "name": "Climate Advisor reading",
        "role": "the reading Climate Advisor uses",
    }
    assert set(out["stats"]) == {"n", "n_v3", "n_flat", "v4_mae", "v3_mae", "flat_mae", "v4_bias", "v3_bias"}
    assert out["model"] == {"confidence": "ok", "fitted_at": "2026-11-06T03:15:00+00:00", "span_days": 30.0}
    assert out["gate"] == []
    p = out["points"][-1]
    assert set(p) == {"t", "actual", "v4", "v3", "flat"}
    datetime.fromisoformat(out["start"]), datetime.fromisoformat(out["end"]), datetime.fromisoformat(p["t"])
    json.dumps(out)  # serialisable


def test_stats_are_the_errors_of_each_prediction_against_the_actual(tmp_path: Path) -> None:
    out = _build(tmp_path, range_str="24h")
    s = out["stats"]
    assert s["n"] == 47 or s["n"] == 48
    assert s["v4_mae"] == pytest.approx(0.5, abs=0.01) and s["v4_bias"] == pytest.approx(0.5, abs=0.01)
    assert s["v3_mae"] == pytest.approx(1.0, abs=0.01) and s["v3_bias"] == pytest.approx(1.0, abs=0.01)
    # persistence: indoor 4 h earlier vs now - the daily swing makes it wrong by a known non-zero amount
    assert s["flat_mae"] is not None and 0.0 < s["flat_mae"] < 1.0


def test_v3_only_for_the_ca_reading_at_four_hours(tmp_path: Path) -> None:
    arch = _archive(tmp_path, leads=("1h", "4h", "12h", "24h"))
    assert "v3" in _build(tmp_path, archive=arch, lead="4h", sensor="_ca")["points"][-1]
    for lead in ("1h", "12h", "24h"):
        out = _build(tmp_path, archive=arch, lead=lead, sensor="_ca")
        assert out["points"] and all("v3" not in p for p in out["points"]) and out["stats"]["v3_mae"] is None
    other = _build(tmp_path, archive=arch, lead="4h", sensor=_FINE)
    assert other["points"] and all("v3" not in p for p in other["points"]) and other["stats"]["v3_bias"] is None


def test_each_sensor_is_scored_against_its_own_actual(tmp_path: Path) -> None:
    out = _build(tmp_path, sensor=_FINE)
    # archive says fine-sensor truth + 0.5; the chart row's fine sensor reads truth + 0.1 (rounded to 2 dp)
    assert out["stats"]["v4_bias"] == pytest.approx(0.5, abs=0.02)
    assert out["points"][-1]["actual"] == pytest.approx(_indoor_at(_NOW - timedelta(minutes=30)) + 0.1, abs=0.06)


@pytest.mark.parametrize(
    ("range_str", "downsample", "max_points"),
    [
        ("6h", "raw", 13),
        ("12h", "raw", 25),
        ("24h", "raw", 49),
        ("3d", "raw", 145),
        ("7d", "hourly", 169),
        ("30d", "hourly", 721),
        ("1y", "daily", 366),
    ],
)
def test_ranges_and_downsampling_like_the_chart_log(
    tmp_path: Path, range_str: str, downsample: str, max_points: int
) -> None:
    out = _build(tmp_path, range_str=range_str)
    assert out["downsample"] == downsample
    assert 0 < len(out["points"]) <= max_points
    start, end = datetime.fromisoformat(out["start"]), datetime.fromisoformat(out["end"])
    assert end == _NOW and (end - start) == timedelta(days=pa.RANGES[range_str])
    if downsample == "hourly":
        assert all(datetime.fromisoformat(p["t"]).minute == 0 for p in out["points"])
    if downsample == "daily":
        assert all(datetime.fromisoformat(p["t"]).hour == 0 for p in out["points"])
        assert out["heat_runs"] == []  # unreadable at one point per day


def test_hourly_buckets_average_their_members(tmp_path: Path) -> None:
    out = _build(tmp_path, range_str="7d")
    # hour 09:00 holds the 09:00 and 09:30 slots; v4 is always actual + 0.5 so each bucket keeps that gap
    for p in out["points"]:
        assert p["v4"] - p["actual"] == pytest.approx(0.5, abs=0.11)


def test_before_scrolls_the_window_back(tmp_path: Path) -> None:
    before = _NOW - timedelta(days=3)
    out = _build(tmp_path, range_str="24h", before=before)
    assert datetime.fromisoformat(out["end"]) == before
    assert datetime.fromisoformat(out["start"]) == before - timedelta(days=1)
    assert out["points"] and all(
        before - timedelta(days=1) <= datetime.fromisoformat(p["t"]) < before for p in out["points"]
    )
    # a window before the archive began is empty, not an error
    old = _build(tmp_path, range_str="24h", before=_NOW - timedelta(days=200))
    assert old["points"] == [] and old["stats"]["n"] == 0 and old["stats"]["v4_mae"] is None


def test_values_are_converted_to_the_display_unit(tmp_path: Path) -> None:
    f = _build(tmp_path, unit="fahrenheit")
    c = _build(tmp_path, unit="celsius")
    assert c["unit"] == "°C"
    pf, pc = f["points"][-1], c["points"][-1]
    assert pc["actual"] == pytest.approx((pf["actual"] - 32) * 5 / 9, abs=0.06)
    assert pc["v4"] == pytest.approx((pf["v4"] - 32) * 5 / 9, abs=0.06)
    # errors are deltas: scale only, no 32 offset
    assert c["stats"]["v4_mae"] == pytest.approx(f["stats"]["v4_mae"] / 1.8, abs=0.01)
    assert c["stats"]["v3_bias"] == pytest.approx(f["stats"]["v3_bias"] / 1.8, abs=0.01)


def test_heat_runs_follow_the_chart_log(tmp_path: Path) -> None:
    out = _build(tmp_path, range_str="24h")
    assert out["heat_runs"], "06:00-08:00 heating rows should produce a run"
    for t0, t1 in out["heat_runs"]:
        a, b = datetime.fromisoformat(t0), datetime.fromisoformat(t1)
        assert a.hour == 6 and a.minute == 0 and (b - a) >= timedelta(hours=1, minutes=30)


def test_backtest_fills_history_and_live_is_preferred(tmp_path: Path) -> None:
    live = _archive(tmp_path, days=1.0, bias=0.5)  # live archive covers only the last day
    old_slot = _NOW - timedelta(days=5)
    live_slot = _NOW - timedelta(hours=6)
    backtest = {
        "points": {
            "_ca": {
                "4h": [
                    {"t": old_slot.isoformat(), "v4": 66.5, "v3": 67.0, "actual": 66.0},
                    # a backtest point inside the live range: the live value (actual + 0.5) must win
                    {"t": live_slot.isoformat(), "v4": 99.0, "v3": 99.0, "actual": 99.0},
                    {"t": "garbage", "v4": 1},
                    {"t": old_slot.isoformat(), "v4": "x"},
                ]
            }
        },
        "gate": [{"zone": "zoneA", "season": "heating", "lead": "1h", "v4": 0.5, "v3": 1.8, "flat": 0.9,
                  "bias": 0.18, "pass": True, "scored": True}],
    }  # fmt: skip
    out = _build(tmp_path, archive=live, backtest=backtest, range_str="30d", unit="fahrenheit")
    by_t = {p["t"]: p for p in out["points"]}
    hist = [p for p in out["points"] if datetime.fromisoformat(p["t"]).date() == old_slot.date()]
    assert hist and any(
        p.get("v4") == 66.5 for p in hist
    )  # backtest value (its own actual 66.0 used: no chart row there? chart has rows)
    assert not any(p.get("v4", 0) >= 90 for p in out["points"]), "a backtest point overrode the live archive"
    assert len(by_t) == len(out["points"])
    # the gate is passed through, errors converted
    assert out["gate"][0]["season"] == "heating" and out["gate"][0]["pass"] is True and out["gate"][0]["v4"] == 0.5
    cel = _build(tmp_path, archive=live, backtest=backtest, range_str="30d", unit="celsius")
    assert cel["gate"][0]["v4"] == pytest.approx(0.28, abs=0.01)  # 0.5 F error in C


def test_backtest_actual_is_used_when_the_chart_log_has_no_row(tmp_path: Path) -> None:
    slot = _NOW - timedelta(days=2)
    backtest = {"points": {"_ca": {"4h": [{"t": slot.isoformat(), "v4": 66.5, "v3": 67.5, "actual": 66.0}]}}}
    out = _build(
        tmp_path, archive=V4Archive(tmp_path, entry_id="zoneA"), chart_rows=[], backtest=backtest, range_str="7d"
    )
    assert len(out["points"]) == 1
    p = out["points"][0]
    assert p["actual"] == 66.0 and p["v4"] == 66.5 and p["v3"] == 67.5
    assert out["stats"]["v4_mae"] == 0.5 and out["stats"]["v3_mae"] == 1.5


def test_no_data_at_all_is_an_empty_state_not_an_error(tmp_path: Path) -> None:
    out = _build(tmp_path, archive=V4Archive(tmp_path, entry_id="zoneA"), chart_rows=[], model=None)
    assert out["points"] == [] and out["heat_runs"] == [] and out["gate"] == []
    assert out["stats"] == {
        "n": 0, "n_v3": 0, "n_flat": 0, "v4_mae": None, "v3_mae": None, "flat_mae": None, "v4_bias": None,
        "v3_bias": None,
    }  # fmt: skip
    assert out["model"] == {"confidence": "none", "fitted_at": None, "span_days": None}


def test_unknown_sensor_in_archive_or_backtest_is_listed(tmp_path: Path) -> None:
    arch = _archive(tmp_path, sensors=("_ca", "sensor.bedroom"))
    out = _build(tmp_path, archive=arch, backtest={"points": {"sensor.other": {}}})
    ids = [s["id"] for s in out["sensors"]]
    assert ids[:2] == ["_ca", _FINE] and "sensor.bedroom" in ids and "sensor.other" in ids


def test_load_backtest_reads_the_zones_file_only(tmp_path: Path) -> None:
    assert pa.load_backtest(tmp_path, "zoneA") is None
    resolve_entry_scoped_path(tmp_path, V4_BACKTEST_FILE, "zoneA").write_text(
        json.dumps({"points": {}}), encoding="utf-8"
    )
    resolve_entry_scoped_path(tmp_path, V4_BACKTEST_FILE, "zoneB").write_text("not json", encoding="utf-8")
    assert pa.load_backtest(tmp_path, "zoneA") == {"points": {}}
    assert pa.load_backtest(tmp_path, "zoneB") is None  # corrupt: ignored, never raised
    resolve_entry_scoped_path(tmp_path, V4_BACKTEST_FILE, "zoneC").write_text("[1]", encoding="utf-8")
    assert pa.load_backtest(tmp_path, "zoneC") is None


# ---------------------------------------------------------------------------
# The view: auth, parameter handling, executor
# ---------------------------------------------------------------------------


def _request(coordinator, query: dict | None = None):
    hass = MagicMock()
    hass.data = {DOMAIN: {"zoneA": coordinator} if coordinator is not None else {}}
    hass.config_entries.async_entries = MagicMock(return_value=[])
    ran_in_executor: list = []

    async def _executor(fn, *args):
        ran_in_executor.append(fn)
        return fn(*args)

    hass.async_add_executor_job = _executor
    req = MagicMock()
    req.app = {"hass": hass}
    req.method, req.path, req.remote = "GET", API_PREDICTION_ACCURACY, "127.0.0.1"
    req.rel_url.query = query or {}
    return req, ran_in_executor


def _coordinator(tmp_path: Path):
    c = MagicMock()
    c.zone_label = "Zone A"
    c._entry_id = "zoneA"
    c.v4_accuracy_inputs.return_value = {
        "zone": "zoneA",
        "unit": "fahrenheit",
        "config_dir": tmp_path,
        "chart_rows": _rows(2.0),
        "archive": _archive(tmp_path, days=2.0),
        "model": {"has_model": False},
        "sensors_meta": [{"id": "_ca", "name": "CA", "role": ""}],
    }
    return c


def _get(coordinator, query: dict | None = None):
    view = ClimateAdvisorPredictionAccuracyView()
    req, ran = _request(coordinator, query)
    resp = asyncio.run(view.get(req))
    return resp, ran


def _payload(resp) -> dict:
    return json.loads(json.dumps(resp.json_data))  # also proves the payload is JSON-serialisable


def test_view_is_registered_and_requires_auth() -> None:
    assert ClimateAdvisorPredictionAccuracyView in API_VIEWS
    assert ClimateAdvisorPredictionAccuracyView.requires_auth is True
    assert ClimateAdvisorPredictionAccuracyView.url == "/api/climate_advisor/prediction_accuracy"


def test_view_builds_in_the_executor_and_returns_the_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    resp, ran = _get(_coordinator(tmp_path), {"zone": "zoneA", "range": "24h", "lead": "4h", "sensor": "_ca"})
    assert resp.status == 200
    body = _payload(resp)
    assert body["zone"] == "zoneA" and body["downsample"] == "raw" and body["points"]
    assert len(ran) == 1, "the chart-log walk and backtest read must run in the executor"


def test_view_resets_bad_parameters_instead_of_rejecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    resp, _ = _get(_coordinator(tmp_path), {"range": "9 years", "lead": "7h", "before": "not a date"})
    body = _payload(resp)
    assert resp.status == 200 and body["range"] == "24h" and body["lead"] == "4h"
    assert datetime.fromisoformat(body["end"]) == _NOW  # a bad `before` is ignored


def test_view_before_and_zone_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    before = (_NOW - timedelta(hours=12)).isoformat()
    resp, _ = _get(_coordinator(tmp_path), {"entry_id": "zoneA", "before": before})
    assert datetime.fromisoformat(_payload(resp)["end"]) == _NOW - timedelta(hours=12)


def test_view_unloaded_integration_is_503(tmp_path: Path) -> None:
    resp, _ = _get(None)
    assert resp.status == 503


def test_view_with_several_zones_and_no_zone_refuses_instead_of_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scorecard silently showing another zone's numbers would mislead the owner's per-zone v4 decision."""
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    monkeypatch.setattr("custom_components.climate_advisor.zone_registry.ir.async_create_issue", MagicMock())
    monkeypatch.setattr("custom_components.climate_advisor.api.zone_registry.list_zones", lambda _h: [])
    view = ClimateAdvisorPredictionAccuracyView()
    a, b = _coordinator(tmp_path), _coordinator(tmp_path)
    req, ran = _request(a)
    req.app["hass"].data = {DOMAIN: {"zoneA": a, "zoneB": b}}
    resp = asyncio.run(view.get(req))
    assert resp.status == 400
    assert _payload(resp)["zone_selection_required"] is True
    assert ran == [] and not a.v4_accuracy_inputs.called and not b.v4_accuracy_inputs.called
    # With the zone named, the same install answers for that zone.
    req, _ = _request(a, {"zone": "zoneB"})
    req.app["hass"].data = {DOMAIN: {"zoneA": a, "zoneB": b}}
    resp = asyncio.run(view.get(req))
    assert resp.status == 200 and b.v4_accuracy_inputs.called and not a.v4_accuracy_inputs.called


def test_view_single_zone_needs_no_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    resp, _ = _get(_coordinator(tmp_path), {})
    assert resp.status == 200 and _payload(resp)["zone"] == "zoneA"


def test_page_fixture_has_exactly_the_endpoint_keys(tmp_path: Path) -> None:
    """The preview page's tests run on tests/fixtures/prediction_accuracy_sample.json: it must stay the real shape."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "prediction_accuracy_sample.json").read_text(encoding="utf-8")
    )
    out = _build(tmp_path)
    assert set(fixture) == set(out)
    assert fixture["sensors"][0]["id"] == out["sensors"][0]["id"] == "_ca"
    assert set(fixture["stats"]) == set(out["stats"]) and set(fixture["model"]) == set(out["model"])


@pytest.mark.parametrize("bad", [7, [{"t": 1}], "points", {"_ca": 3}, {"_ca": {"4h": "x"}}])
def test_malformed_backtest_points_never_break_the_page(tmp_path: Path, bad) -> None:
    """A hand-edited or truncated backtest file must degrade to "no backtest", never to a 500."""
    out = _build(tmp_path, backtest={"points": bad, "gate": "nope", "gate_summary": [1]})
    assert out["points"] and out["gate"] == [] and out["gate_summary"] is None
    json.dumps(out)
