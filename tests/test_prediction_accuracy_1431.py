"""Issue #1431: the prediction-accuracy payload's sensor swaps and per-sensor "anything to draw" flags.

Occupant view: on the Prediction accuracy page the hallway and the bedroom read differently, so the lines that follow
"the reading Climate Advisor uses" must break at bedtime and wake-up (``ca_swaps``, the Status chart's rule), and a
sensor v4 has never predicted must be labelled ("no v4 data yet") or, with nothing recorded at all, disabled, instead
of opening an empty chart (``sensors[].has_v4`` / ``has_readings``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.v4_archive import V4Archive
from tests.test_v4_shadow_endpoint import _FINE, _NOW, _archive, _build, _coordinator, _get, _payload, _rows

_BED = "sensor.bedroom_temp"
_ATTIC = "sensor.attic_temp"


def _rows_with_band(days: float = 2.0) -> list[dict]:
    """``_rows`` plus ``band_sensor``: the bedroom from 22:00 to 06:00 UTC, the hall otherwise."""
    rows = _rows(days)
    for r in rows:
        h = datetime.fromisoformat(r["ts"]).hour
        r["band_sensor"] = _BED if (h >= 22 or h < 6) else _FINE
        r["indoor_sensors"][_BED] = 66.0
    return rows


def _meta() -> list[dict]:
    return [
        {"id": "_ca", "name": "Climate Advisor reading", "role": "the reading Climate Advisor uses"},
        {"id": _FINE, "name": "Hall", "role": "thermostat's finer sensor"},
        {"id": _BED, "name": "Bedroom", "role": "bedroom sensor"},
        {"id": _ATTIC, "name": "Attic", "role": "room sensor"},
    ]


def test_ca_swaps_are_the_status_charts_swap_times_in_the_window(tmp_path: Path) -> None:
    rows = _rows_with_band()
    out = _build(tmp_path, chart_rows=rows, sleep_sensor=_BED, sleep_time="22:00", wake_time="06:00")
    start, end = datetime.fromisoformat(out["start"]).timestamp(), datetime.fromisoformat(out["end"]).timestamp()
    want = pa.ca_swap_epochs(rows, start, end, tz=_NOW.tzinfo, sleep_sensor=_BED, sleep_time="22:00", wake_time="06:00")
    got = [datetime.fromisoformat(t).timestamp() for t in out["ca_swaps"]]
    assert got == want and got, "the page must get the same swap times the Status chart breaks at"
    # 24 h ending 12:00 UTC: bedtime 22:00 and wake-up 06:00 once each
    assert sorted(datetime.fromisoformat(t).astimezone(_NOW.tzinfo).hour for t in out["ca_swaps"]) == [6, 22]
    json.dumps(out)


def test_ca_swaps_from_recorded_band_sensor_without_a_sleep_sensor(tmp_path: Path) -> None:
    out = _build(tmp_path, chart_rows=_rows_with_band())
    assert len(out["ca_swaps"]) == 2  # the chart log's own band_sensor changes still count


def test_ca_swaps_empty_without_any_swap_and_at_daily_resolution(tmp_path: Path) -> None:
    assert _build(tmp_path)["ca_swaps"] == []  # no band_sensor, no sleep sensor: nothing swapped
    daily = _build(
        tmp_path,
        chart_rows=_rows_with_band(40),
        range_str="1y",
        sleep_sensor=_BED,
        sleep_time="22:00",
        wake_time="06:00",
    )
    assert daily["downsample"] == "daily" and daily["ca_swaps"] == []


def test_ca_swaps_never_break_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("bad rows")

    monkeypatch.setattr(pa, "ca_swap_epochs", boom)
    out = _build(tmp_path, chart_rows=_rows_with_band())
    assert out["ca_swaps"] == [] and out["points"]


def test_sensor_flags_say_what_each_sensor_can_draw(tmp_path: Path) -> None:
    out = _build(tmp_path, chart_rows=_rows_with_band(), sensors_meta=_meta())
    flags = {s["id"]: (s["has_v4"], s["has_readings"]) for s in out["sensors"]}
    assert flags["_ca"] == (True, True)
    assert flags[_FINE] == (True, True)
    assert flags[_BED] == (False, True)  # readings, never predicted by v4: "no v4 data yet"
    assert flags[_ATTIC] == (False, False)  # nothing at all: the button is disabled


def test_has_v4_counts_the_backtest_and_ignores_points_without_v4(tmp_path: Path) -> None:
    t = (_NOW - timedelta(days=60)).isoformat()
    backtest = {
        "points": {_BED: {"4h": [{"t": t, "actual": 66.0, "v4": 66.4}]}, _ATTIC: {"1h": [{"t": t, "actual": 60.0}]}}
    }
    out = _build(tmp_path, chart_rows=_rows(), sensors_meta=_meta(), backtest=backtest)
    flags = {s["id"]: s["has_v4"] for s in out["sensors"]}
    assert flags[_BED] is True and flags[_ATTIC] is False


def test_empty_archive_flags_every_sensor_without_v4(tmp_path: Path) -> None:
    out = _build(tmp_path, archive=V4Archive(tmp_path, entry_id="zoneA"), chart_rows=[], sensors_meta=_meta())
    assert all(s["has_v4"] is False and s["has_readings"] is False for s in out["sensors"])
    assert out["ca_swaps"] == []


def test_view_passes_the_zones_sleep_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.api.dt_util.now", lambda: _NOW, raising=False)
    c = _coordinator(tmp_path)
    c.v4_accuracy_inputs.return_value["chart_rows"] = _rows_with_band()
    c.v4_accuracy_inputs.return_value["archive"] = _archive(tmp_path, days=2.0)
    c.config = {"sleep_indoor_temp_entity": _BED, "sleep_time": "22:00", "wake_time": "06:00"}
    resp, _ = _get(c, {"zone": "zoneA", "range": "24h"})
    body = _payload(resp)
    assert resp.status == 200 and len(body["ca_swaps"]) == 2
