"""The timeline replay tool (Issue #1230 R3a) on a synthetic recorded day.

Occupant view: before the timeline is trusted it is compared with what Climate Advisor actually did. These tests make a
tiny known history (a heating day: wake and bedtime setpoint changes at known minutes, an occupant bump, a nat-vent
session) and check that the diff says exactly what happened: what matched and by how many minutes, what the timeline
had that CA did not do, and what CA did that no step explains.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import timeline_replay as tr


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Los_Angeles")


def _entry(ts: datetime, **kw) -> dict:
    base = {
        "ts": ts.isoformat(),
        "hvac": "idle",
        "fan": False,
        "indoor": 69.0,
        "outdoor": 50.0,
        "windows_open": False,
        "windows_recommended": False,
        "pred_outdoor": 50.0,
        "setpoint": 68.0,
        "fan_running": False,
        "nat_vent_active": False,
        "lower": 68.0,
        "upper": 74.0,
    }
    base.update(kw)
    return base


def _day_rows(day: datetime, *, bump: bool = False) -> list[dict]:
    """30-minute rows for one heating day: floor 68 awake / 66 asleep, wake 06:30, bedtime 22:30."""
    rows = []
    for k in range(48):
        ts = day + timedelta(minutes=30 * k, seconds=20)
        h = ts.hour + ts.minute / 60
        asleep = h < 6.5 or h >= 22.5
        sp = 66.0 if asleep else 68.0
        if bump and 7.0 <= h < 9.0:
            sp = 69.0
        rows.append(
            _entry(
                ts,
                setpoint=sp,
                lower=66.0 if asleep else 68.0,
                upper=72.0 if asleep else 74.0,
                outdoor=45.0 + 10 * (1 - abs(h - 15) / 15),
                pred_outdoor=45.0 + 10 * (1 - abs(h - 15) / 15),
            )
        )
    return rows


@pytest.fixture
def zone_files(tmp_path: Path):
    tz = _tz()
    first = datetime(2026, 11, 1, 0, 0, tzinfo=tz)
    entries = []
    records = []
    for i in range(5):
        day = first + timedelta(days=i)
        entries += _day_rows(day, bump=(i == 2))
        records.append(
            {
                "date": day.date().isoformat(),
                "day_type": "cool",
                "hvac_mode_recommended": "heat",
                "windows_recommended": False,
                "forecast_high_f": 55.0,
                "forecast_low_f": 40.0,
                "occupancy_mode": "home",
                "hvac_runtime_minutes": 60.0,
            }
        )
    chart = tmp_path / "climate_advisor_chart_log_zz.json"
    learn = tmp_path / "climate_advisor_learning_zz.json"
    chart.write_text(json.dumps({"entries": entries}))
    learn.write_text(
        json.dumps(
            {
                "records": records,
                "thermal_model_cache": {
                    "k_passive": -0.03,
                    "k_active_heat": 5.5,
                    "k_active_cool": -6.0,
                    "observation_count_heat": 80,
                    "observation_count_cool": 80,
                },
            }
        )
    )
    return chart, learn


def test_config_is_inferred_from_the_logged_band_ramp_positions(zone_files) -> None:
    chart, learn = zone_files
    zone = tr.load_zone(chart, learn, _tz())
    cfg = tr.infer_config(zone)
    assert (cfg["comfort_heat"], cfg["comfort_cool"], cfg["sleep_heat"], cfg["sleep_cool"]) == (68.0, 74.0, 66.0, 72.0)
    # no ramps in the synthetic rows: the documented fallback times, and an explicit override wins
    assert tr.infer_config(zone, "06:45", "21:15")["wake_time"] == "06:45"


def test_replay_matches_the_wake_and_bedtime_steps_with_their_timing(zone_files) -> None:
    chart, learn = zone_files
    zone = tr.load_zone(chart, learn, _tz())
    cfg = tr.infer_config(zone, "06:30", "22:30")
    result = tr.replay_zone(zone, cfg, days=None, with_v3=False)
    summary = result["summary"]
    assert summary["days"] == 3  # five days, first and last are partial
    assert summary["by_kind"]["wake_band"]["matched"] == 3 and summary["by_kind"]["sleep_band"]["matched"] == 3
    assert summary["band_timing_min"]["max_abs"] <= 1.0  # rows are 20 s after the minute
    assert summary["match_rate"] == 1.0


def test_an_occupant_bump_is_a_ca_only_adjustment_and_its_restore_is_named(zone_files) -> None:
    chart, learn = zone_files
    zone = tr.load_zone(chart, learn, _tz())
    result = tr.replay_zone(zone, tr.infer_config(zone, "06:30", "22:30"), days=None, with_v3=False)
    classes = result["summary"]["ca_only"]
    assert classes.get("occupant_change_expired_ca_restores") == 1  # 69 -> 68 when the bump ended
    assert classes.get("occupant_adjustment") == 1  # 68 -> 69


def test_a_step_the_house_did_not_act_on_is_classed_not_lost(zone_files, tmp_path: Path) -> None:
    chart, learn = zone_files
    data = json.loads(chart.read_text())
    for e in data["entries"]:
        e["setpoint"] = None  # thermostat Off all week: nothing to write
    chart.write_text(json.dumps(data))
    zone = tr.load_zone(chart, learn, _tz())
    result = tr.replay_zone(zone, tr.infer_config(zone, "06:30", "22:30"), days=None, with_v3=False)
    assert result["summary"]["timeline_only"].get("wake_band:thermostat_off_no_setpoint") == 3
    assert result["summary"]["match_rate"] == 0.0


def test_the_cli_writes_json_and_prints_the_report(zone_files, tmp_path: Path, capsys) -> None:
    chart, learn = zone_files
    out = tmp_path / "out.json"
    code = tr.main(
        [
            "--chart-log",
            str(chart),
            "--learning-db",
            str(learn),
            "--wake",
            "06:30",
            "--sleep",
            "22:30",
            "--no-v3",
            "--json",
            str(out),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "match rate" in printed and "CA-only classes" in printed
    assert json.loads(out.read_text())[0]["summary"]["days"] == 3


def test_a_day_without_a_record_or_enough_forecast_is_skipped_quietly(zone_files) -> None:
    chart, learn = zone_files
    data = json.loads(learn.read_text())
    data["records"] = data["records"][:2]
    learn.write_text(json.dumps(data))
    zone = tr.load_zone(chart, learn, _tz())
    result = tr.replay_zone(zone, tr.infer_config(zone, "06:30", "22:30"), days=None, with_v3=False)
    assert result["summary"]["days"] <= 1
