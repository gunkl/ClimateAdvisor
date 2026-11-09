"""Issue #1492: the "How each model has scored" table is computed live over the last 30 days.

Occupant view: the table on the Prediction accuracy page used to change only when a new version was deployed (it was
copied from the deploy-time backtest). It now follows the live predictions: a better or worse v4 shows up within the
next page load, a month-old miss drops out, heating and summer days are scored apart by what the HVAC actually did,
a row without enough points says "not enough data yet", and v3 is compared at 1/12/24 h from its own new archive.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import hvac_season
from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.const import (
    ENTRY_SCOPED_FILES,
    SCORECARD_MIN_N,
    V3_ARCHIVE_FILE,
    V3_ARCHIVE_LEADS,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.prediction_engines import ENGINES
from custom_components.climate_advisor.storage_paths import resolve_entry_scoped_path
from custom_components.climate_advisor.v4_archive import V3Archive, V4Archive, slot_key

_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
_LEADS = ("1h", "4h", "12h", "24h")


def _indoor(ts: datetime) -> float:
    return round(68.0 + 2.0 * ((ts.hour + ts.minute / 60) / 24.0 - 0.5), 2)


def _rows(days: float = 40.0, *, hvac_of=None, pred_offset: float | None = 1.0) -> list[dict]:
    """Chart rows every 30 min; ``hvac_of(ts)`` gives the row's hvac action (default: heating 06-08 every day)."""
    hvac_of = hvac_of or (lambda ts: "heating" if 6 <= ts.hour < 8 else "idle")
    out = []
    for k in range(int(days * 48), -1, -1):
        ts = _NOW - timedelta(minutes=30 * k)
        row = {"ts": ts.isoformat(), "hvac": hvac_of(ts), "indoor": _indoor(ts), "outdoor": 40.0}
        if pred_offset is not None:
            row["pred_indoor"] = round(_indoor(ts) + pred_offset, 2)
        out.append(row)
    return out


def _v4(tmp_path: Path, *, days: float = 40.0, bias=0.2, leads=_LEADS) -> V4Archive:
    """v4 said the true value plus ``bias`` (a number, or a function of the slot time) for every slot."""
    arch = V4Archive(tmp_path, entry_id="z")
    for k in range(int(days * 48), 0, -1):
        ts = _NOW - timedelta(minutes=30 * k)
        b = bias(ts) if callable(bias) else bias
        for lead in leads:
            arch.put("_ca", slot_key(ts), lead, round(_indoor(ts) + b, 2))
    return arch


def _v3(tmp_path: Path, *, days: float = 40.0, bias: float = 1.0, leads=V3_ARCHIVE_LEADS) -> V3Archive:
    arch = V3Archive(tmp_path, entry_id="z")
    for k in range(int(days * 48), 0, -1):
        ts = _NOW - timedelta(minutes=30 * k)
        for lead in leads:
            arch.put("_ca", slot_key(ts), lead, round(_indoor(ts) + bias, 2))
    return arch


def _card(tmp_path: Path, **over):
    kw = dict(zone="z", now=_NOW, unit="fahrenheit", chart_rows=_rows(), archive=_v4(tmp_path), v3_archive=None)
    kw.update(over)
    rows, summary = pa.live_scorecard(**kw)
    return {(r["season"], r["lead"]): r for r in rows}, summary


# ---------------------------------------------------------------------------
# Rows follow the live archive
# ---------------------------------------------------------------------------


def test_rows_change_when_the_archived_points_change(tmp_path: Path) -> None:
    good, _ = _card(tmp_path, v3_archive=_v3(tmp_path / "a"))
    (tmp_path / "b").mkdir()
    bad, _ = _card(tmp_path / "b", archive=_v4(tmp_path / "b", bias=1.5), v3_archive=_v3(tmp_path / "a"))
    g, b = good[("heating", "4h")], bad[("heating", "4h")]
    assert g["v4"] == pytest.approx(0.2, abs=1e-6) and b["v4"] == pytest.approx(1.5, abs=1e-6)
    assert g["pass"] is True and g["beats_v3"] is True and g["beats_flat"] is True
    assert b["pass"] is False and b["beats_v3"] is False and "v4 not better than v3" in b["reasons"]
    assert b["bias_ok"] is False


def test_the_backtest_gate_no_longer_feeds_the_table(tmp_path: Path) -> None:
    backtest = {"gate": [{"season": "heating", "lead": "1h", "v4": 9.9, "v3": 9.9, "flat": 9.9, "bias": 0.0,
                          "pass": True, "scored": True}],
                "gate_summary": {"ready": True, "passed": 9, "scored": 9, "total": 9}}  # fmt: skip
    out = pa.build_prediction_accuracy(
        zone="z", sensor="_ca", lead="4h", range_str="24h", before=None, now=_NOW, unit="fahrenheit",
        chart_rows=_rows(), archive=_v4(tmp_path), backtest=backtest, model=None, sensors_meta=[],
    )  # fmt: skip
    assert out["gate_summary"]["live"] is True and out["gate_summary"]["total"] == 8
    assert not any(g["v4"] == 9.9 for g in out["gate"])


def test_the_table_ends_now_whatever_window_the_chart_shows(tmp_path: Path) -> None:
    """Scrolling the chart back (``before``) must not move the 30-day scorecard."""
    kw = dict(
        zone="z", sensor="_ca", lead="4h", range_str="24h", now=_NOW, unit="fahrenheit",
        chart_rows=_rows(), archive=_v4(tmp_path), backtest=None, model=None, sensors_meta=[],
    )  # fmt: skip
    a = pa.build_prediction_accuracy(before=None, **kw)
    b = pa.build_prediction_accuracy(before=_NOW - timedelta(days=10), **kw)
    assert a["gate"] == b["gate"] and a["gate_summary"] == b["gate_summary"]


# ---------------------------------------------------------------------------
# The 30-day window
# ---------------------------------------------------------------------------


def test_points_older_than_30_days_are_dropped(tmp_path: Path) -> None:
    # v4 was 5 F off more than 30 days ago, 0.2 F since: only the recent month counts.
    bias = lambda ts: 5.0 if ts < _NOW - timedelta(days=30) else 0.2  # noqa: E731
    card, summary = _card(tmp_path, archive=_v4(tmp_path, bias=bias))
    row = card[("heating", "1h")]
    assert row["v4"] == pytest.approx(0.2, abs=1e-6)
    assert row["n"] == 30 * 48  # one point per 30-minute slot in the window, nothing older
    assert datetime.fromisoformat(summary["end"]) == _NOW
    assert datetime.fromisoformat(summary["start"]) == _NOW - timedelta(days=30)
    assert summary["window_days"] == 30 and summary["live"] is True


# ---------------------------------------------------------------------------
# Seasons: what the HVAC did that day, shared with the backtest
# ---------------------------------------------------------------------------


def _season_split_hvac(ts: datetime) -> str:
    """Even days of the month heat (06-08), odd days cool (02-04: before noon, so today counts too)."""
    if ts.day % 2 == 0:
        return "heating" if 6 <= ts.hour < 8 else "idle"
    return "cooling" if 2 <= ts.hour < 4 else "idle"


def test_heating_and_summer_days_are_scored_apart(tmp_path: Path) -> None:
    # v4 is good on heating days and poor on cooling days: the split must show it.
    bias = lambda ts: 0.1 if (ts - timedelta(hours=1)).day % 2 == 0 else 2.0  # noqa: E731 - by the 1 h anchor's day
    card, _ = _card(tmp_path, chart_rows=_rows(hvac_of=_season_split_hvac), archive=_v4(tmp_path, bias=bias))
    heat, summer = card[("heating", "1h")], card[("summer", "1h")]
    assert heat["n"] > SCORECARD_MIN_N and summer["n"] > SCORECARD_MIN_N
    assert heat["n"] + summer["n"] == 30 * 48
    assert heat["v4"] == pytest.approx(0.1, abs=1e-6) and summer["v4"] == pytest.approx(2.0, abs=1e-6)


def test_a_point_takes_the_season_of_the_day_its_prediction_was_made(tmp_path: Path) -> None:
    """24 h ahead: a slot on a cooling day predicted from a heating day is a heating point (the backtest's rule)."""
    # v4 is 0.1 F off on slots of heating (even) days, 2 F off on slots of cooling (odd) days.
    bias = lambda ts: 0.1 if ts.day % 2 == 0 else 2.0  # noqa: E731
    card, _ = _card(tmp_path, chart_rows=_rows(hvac_of=_season_split_hvac), archive=_v4(tmp_path, bias=bias))
    for lead in ("1h", "24h"):
        assert card[("heating", lead)]["n"] + card[("summer", lead)]["n"] == 30 * 48
    assert card[("heating", "1h")]["v4"] < 0.5  # 1 h: the anchor is (nearly always) the slot's own heating day
    # 24 h: a heating-day anchor predicts the next (cooling) day's slots, so the heating row carries the 2 F misses
    assert card[("heating", "24h")]["v4"] > 1.5 and card[("summer", "24h")]["v4"] < 0.6


def test_shoulder_days_are_not_scored(tmp_path: Path) -> None:
    card, summary = _card(tmp_path, chart_rows=_rows(hvac_of=lambda ts: "idle"))
    assert all(r["n"] == 0 and not r["scored"] for r in card.values())
    assert summary["scored"] == 0 and summary["total"] == 8


def test_the_backtest_tool_uses_the_integrations_season_rule() -> None:
    path = Path(__file__).resolve().parents[1] / "tools" / "predict_backtest.py"
    spec = importlib.util.spec_from_file_location("_pb_season_check", path)
    pb = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pb
    spec.loader.exec_module(pb)
    assert pb.day_seasons.__code__.co_filename.endswith("hvac_season.py")
    assert pb.season_of_day.__code__.co_filename.endswith("hvac_season.py")
    rows = _rows(days=3, hvac_of=_season_split_hvac)
    assert pb.day_seasons(rows, UTC) == hvac_season.day_seasons(rows, UTC)


def test_whole_house_fan_day_is_summer() -> None:
    rows = [{"ts": "2026-07-01T20:00:00+00:00", "hvac": "idle", "fan_running": True, "nat_vent_active": True}]
    assert hvac_season.day_seasons(rows, UTC) == {datetime(2026, 7, 1).date(): "summer"}
    assert hvac_season.season_of_day(True, True) == "heating"
    assert hvac_season.season_of_day(False, False) == "shoulder"


# ---------------------------------------------------------------------------
# Too little data
# ---------------------------------------------------------------------------


def test_a_row_with_fewer_than_72_points_says_not_enough_data_yet(tmp_path: Path) -> None:
    # 1 day of rows: ~48 points per lead, under the 72 needed.
    card, summary = _card(tmp_path, chart_rows=_rows(days=1.0), v3_archive=_v3(tmp_path / "v3", days=1.0))
    row = card[("heating", "1h")]
    assert 0 < row["n"] < SCORECARD_MIN_N
    assert row["scored"] is False and row["pass"] is False
    assert row["reasons"][0] == f"not enough data yet (n={row['n']}, need {SCORECARD_MIN_N})"
    assert row["v4"] is not None  # the numbers are still shown, just not judged
    assert summary["scored"] == 0


# ---------------------------------------------------------------------------
# v3 at more look-aheads
# ---------------------------------------------------------------------------


def test_v3_cells_read_collecting_until_its_archive_has_points(tmp_path: Path) -> None:
    card, _ = _card(tmp_path)  # chart log has pred_indoor (4 h) only; no v3 archive
    assert card[("heating", "4h")]["v3_state"] == "ok" and card[("heating", "4h")]["v3"] == pytest.approx(1.0)
    for lead in ("1h", "12h", "24h"):
        row = card[("heating", lead)]
        assert row["v3_state"] == "collecting" and row["v3"] is None and row["scored"] is False
        assert row["beats_v3"] is None and row["beats_flat"] is not None  # v4 vs "no change" still shown
        assert "v3 predictions at this look-ahead are still being collected" in row["reasons"]
    filled, _ = _card(tmp_path, v3_archive=_v3(tmp_path / "v3"))
    for lead in ("1h", "12h", "24h"):
        row = filled[("heating", lead)]
        assert row["v3_state"] == "ok" and row["v3"] == pytest.approx(1.0) and row["scored"] is True


def test_v3_reads_off_when_it_is_not_running(tmp_path: Path) -> None:
    card, _ = _card(tmp_path, chart_rows=_rows(pred_offset=None), v3_running=False)
    assert all(r["v3_state"] == "off" for r in card.values())


def test_scorecard_values_are_in_the_occupants_unit(tmp_path: Path) -> None:
    f, _ = _card(tmp_path, v3_archive=_v3(tmp_path / "v3"))
    c, _ = _card(tmp_path, unit="celsius", v3_archive=_v3(tmp_path / "v3"))
    key = ("heating", "12h")
    assert c[key]["v4"] == pytest.approx(f[key]["v4"] / 1.8, abs=0.01)
    assert c[key]["v3"] == pytest.approx(f[key]["v3"] / 1.8, abs=0.01)
    assert c[key]["pass"] == f[key]["pass"]  # judged in F, shown in C


def _v3_curve(now: datetime, hours: float = 47.0) -> list[dict]:
    """v3's curve as the coordinator caches it: points on :00/:30 strictly after now (hourly forecast + midpoints)."""
    first = datetime.fromtimestamp((int(now.timestamp()) // 1800 + 1) * 1800, UTC)
    return [
        {"ts": (first + timedelta(minutes=30 * i)).isoformat(), "temp": 70.0 + i * 0.01} for i in range(int(hours * 2))
    ]


def test_v3_archive_records_every_lead_its_curve_reaches(tmp_path: Path) -> None:
    arch = V3Archive(tmp_path, entry_id="z")
    for k in range(4):  # four cycles, 30 min apart
        now = _NOW + timedelta(minutes=30 * k)
        curve = [(datetime.fromisoformat(p["ts"]), p["temp"]) for p in _v3_curve(now)]
        arch.record(now, {"_ca": curve}, "_ca")
    for lead in ("1h", "4h", "12h", "24h"):
        got = arch.series("_ca", lead, 0, float("inf"))
        assert len(got) == 4, lead
    # A curve that ends at 10 h archives 1/4 h only: nothing is invented past it.
    short = V3Archive(tmp_path / "s", entry_id="z")
    curve = [(datetime.fromisoformat(p["ts"]), p["temp"]) for p in _v3_curve(_NOW, hours=10)]
    short.record(_NOW, {"_ca": curve}, "_ca")
    assert short.series("_ca", "4h", 0, float("inf")) and not short.series("_ca", "12h", 0, float("inf"))


def test_v3_archive_persists_in_its_own_entry_scoped_file(tmp_path: Path) -> None:
    arch = V3Archive(tmp_path, entry_id="z")
    arch.put("_ca", slot_key(_NOW), "12h", 69.5)
    assert arch.save()
    path = resolve_entry_scoped_path(tmp_path, V3_ARCHIVE_FILE, "z")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["leads"] == list(V3_ARCHIVE_LEADS)
    back = V3Archive(tmp_path, entry_id="z")
    back.load()
    assert back.series("_ca", "12h", 0, float("inf")) == {slot_key(_NOW): 69.5}
    assert V3_ARCHIVE_FILE in ENTRY_SCOPED_FILES  # a removed zone leaves nothing behind
    assert not resolve_entry_scoped_path(tmp_path, "climate_advisor_v4_archive.json", "z").exists()


def test_coordinator_records_v3_curve_into_its_archive(tmp_path: Path) -> None:
    """The production recording method, on a partly built coordinator: 1/4/12/24 h written, saved at most hourly."""
    coord = ClimateAdvisorCoordinator.__new__(ClimateAdvisorCoordinator)
    coord._v3_archive = V3Archive(tmp_path, entry_id="z")
    coord._v3_last_archive_save = None
    coord._last_predicted_indoor = _v3_curve(_NOW)
    saves: list[str] = []

    async def _executor_job(fn, *a):
        saves.append(getattr(fn, "__name__", "?"))
        return fn(*a)

    coord._executor_job = _executor_job
    asyncio.run(coord._record_v3_archive(_NOW))
    for lead in ("1h", "4h", "12h", "24h"):
        assert coord._v3_archive.series("_ca", lead, 0, float("inf")), lead
    assert saves == ["save"] and resolve_entry_scoped_path(tmp_path, V3_ARCHIVE_FILE, "z").exists()
    coord._last_predicted_indoor = _v3_curve(_NOW + timedelta(minutes=30))
    asyncio.run(coord._record_v3_archive(_NOW + timedelta(minutes=30)))
    assert saves == ["save"]  # within the hour: kept in memory
    coord._last_predicted_indoor = []  # v3 not running: nothing recorded, nothing raised
    asyncio.run(coord._record_v3_archive(_NOW + timedelta(hours=1)))


def test_the_cycle_records_v3_right_after_building_its_curve() -> None:
    """Wiring: the 30-minute cycle that fills v3's 4 h ``_pred_archive`` also calls the look-ahead recorder."""
    import ast  # noqa: PLC0415

    src = Path(__file__).resolve().parents[1] / "custom_components" / "climate_advisor" / "coordinator.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        if "_last_predicted_indoor" in names and "_pred_archive" in names and "setdefault" in names:
            hits.append((fn.name, "_record_v3_archive" in names))
    assert hits and all(called for _, called in hits), hits


def test_v3_engine_past_reads_the_new_archive(tmp_path: Path) -> None:
    arch = _v3(tmp_path, days=1.0)
    src = SimpleNamespace(_pred_archive={}, _v3_archive=arch)
    v3 = ENGINES["v3"]
    assert v3.past(src, "_ca", "12h", 0, float("inf"))
    assert v3.past(src, "sensor.other", "12h", 0, float("inf")) == {}


def test_endpoint_shows_v3_at_12h_from_its_archive(tmp_path: Path) -> None:
    out = pa.build_prediction_accuracy(
        zone="z", sensor="_ca", lead="12h", range_str="24h", before=None, now=_NOW, unit="fahrenheit",
        chart_rows=_rows(), archive=_v4(tmp_path), backtest=None, model=None, sensors_meta=[],
        v3_archive=_v3(tmp_path / "v3"),
    )  # fmt: skip
    assert out["stats"]["n_v3"] > 0 and out["stats"]["v3_mae"] == pytest.approx(1.0, abs=0.01)
