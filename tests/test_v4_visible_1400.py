"""Issue #1400: v4 makes Zone 1's decisions, so what it says must be visible (chart line, archive, accuracy page).

Occupant view: on 0.9.4 the live v4 model is ``_ca``-only (``primary "_ca"``, ``sensors ["_ca"]``: no entity sensor
had enough history yet). The Status chart drew no v4 line, the archive stored nothing (``archived: 0`` every cycle)
and the Prediction accuracy page was empty for BOTH engines, so the owner could not see or judge the engine running
the house. These tests pin, per item of the issue:

1. ``build_chart_v4`` draws v4's ``_ca`` line (future, past, baseline), with break times at every hallway<->bedroom swap
   so no segment crosses one; per-sensor lines still draw when the model has them.
2. ``V4Archive.record`` stores ``_ca`` for a ``_ca``-only model (a live-shaped cycle archives > 0), inside the M4 limit.
3. The accuracy endpoint scores each engine on its own: v3 and "no change" show with an empty v4 archive.
5. The accuracy page's sensor list has ``_ca`` once.
"""

from __future__ import annotations

import tracemalloc
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.const import V4_ARCHIVE_LEADS
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key
from tests.v4_fake_predictor import install

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=TZ)
HALL = "climate.hallway"
BED = "sensor.bedroom"
CA_ONLY_MODEL = {"has_model": True, "confidence": "ok", "fitted_at": "2026-11-07T03:15:00-08:00", "primary": "_ca"}


def _row(ts: datetime, indoor: float, band: str | None = None, **kw) -> dict:
    row = {"ts": ts.isoformat(), "hvac": "idle", "indoor": indoor, "pred_indoor": indoor + 1.0, "outdoor": 50.0}
    if band:
        row["band_sensor"] = band
    row.update(kw)
    return row


def _night_rows(hours: float = 30.0) -> list[dict]:
    """Rows every 30 min up to NOW; CA reads the bedroom 22:30-07:00 (66 F) and the hallway otherwise (70 F)."""
    out = []
    for k in range(int(hours * 2), -1, -1):
        ts = NOW - timedelta(minutes=30 * k)
        local = ts.astimezone(TZ)
        asleep = local.hour * 60 + local.minute >= 22 * 60 + 30 or local.hour < 7
        out.append(_row(ts, 66.0 if asleep else 70.0, BED if asleep else HALL))
    return out


def _ca_archive(tmp_path: Path, hours: float = 24.0) -> V4Archive:
    arch = V4Archive(tmp_path, entry_id="zone1")
    for k in range(int(hours * 2), 0, -1):
        epoch = slot_key(NOW - timedelta(minutes=30 * k))
        for lead in V4_ARCHIVE_LEADS:
            arch.put("_ca", epoch, lead, 68.5)
    return arch


def _ca_future() -> dict:
    pts = [(NOW + timedelta(minutes=15 * (k + 1)), 69.0 + 0.01 * k) for k in range(96)]
    return {"at": NOW.isoformat(), "predictions": {"_ca": pts}, "corrected": [], "baseline": {"_ca": pts}}


def _chart(tmp_path: Path, **over) -> dict:
    kw = dict(
        now=NOW,
        start=(NOW - timedelta(hours=24)).timestamp(),
        end=NOW.timestamp(),
        range_str="24h",
        unit="fahrenheit",
        historical=False,
        archive=_ca_archive(tmp_path),
        backtest=None,
        model=CA_ONLY_MODEL,
        last_future=_ca_future(),
        sensor_ids=["_ca"],
    )
    kw.update(over)
    return pa.build_chart_v4(**kw)


# ------------------------------------------------------------------------------------------- 1. the chart _ca line
class TestChartCaLine:
    def test_a_ca_only_model_draws_its_ca_line_past_future_and_baseline(self, tmp_path):
        v4 = _chart(tmp_path)
        assert v4["ready"] is True and v4["sensors"] == ["_ca"]
        assert v4["future"]["_ca"] and v4["baseline"]["_ca"]
        assert set(v4["past"]["_ca"]) == set(V4_ARCHIVE_LEADS) and len(v4["past"]["_ca"]["4h"]) == 48

    def test_the_ca_line_is_broken_at_every_bedtime_and_wake_up(self, tmp_path):
        swaps = pa.ca_swap_epochs(
            _night_rows(),
            (NOW - timedelta(hours=24)).timestamp(),
            (NOW + timedelta(hours=25)).timestamp(),
            tz=TZ,
            sleep_sensor=BED,
            sleep_time="22:30",
            wake_time="07:00",
        )
        local = [datetime.fromtimestamp(s, TZ).strftime("%m-%d %H:%M") for s in swaps]
        # recorded: last night's bedtime and this morning's wake-up; future: tonight's bedtime and tomorrow's wake-up
        assert local == ["11-06 22:30", "11-07 07:00", "11-07 22:30", "11-08 07:00"]
        v4 = _chart(tmp_path, ca_swaps=swaps)
        assert [datetime.fromisoformat(t).timestamp() for t in v4["breaks"]["_ca"]] == swaps

    def test_an_unplanned_fall_back_to_the_hallway_is_a_swap_too(self):
        rows = _night_rows()
        # the bedroom sensor went stale at 02:00 and CA read the hallway until wake-up
        for r in rows:
            local = datetime.fromisoformat(r["ts"]).astimezone(TZ)
            if local.date() == NOW.date() and 2 <= local.hour < 7:
                r["band_sensor"] = HALL
        swaps = pa.ca_swap_epochs(
            rows, datetime.fromisoformat(rows[0]["ts"]).timestamp(), NOW.timestamp(),
            tz=TZ, sleep_sensor=BED, sleep_time="22:30", wake_time="07:00",
        )  # fmt: skip
        local = [datetime.fromtimestamp(s, TZ).strftime("%m-%d %H:%M") for s in swaps]
        assert "11-07 02:00" in local and "11-07 07:00" not in local  # no change at 07:00: it already read the hallway

    def test_no_sleep_sensor_means_no_swap_and_no_break(self, tmp_path):
        rows = [_row(NOW - timedelta(minutes=30 * k), 70.0, HALL) for k in range(48, -1, -1)]
        swaps = pa.ca_swap_epochs(
            rows, (NOW - timedelta(hours=24)).timestamp(), (NOW + timedelta(hours=25)).timestamp(),
            tz=TZ, sleep_sensor=None, sleep_time="22:30", wake_time="07:00",
        )  # fmt: skip
        assert swaps == []
        assert _chart(tmp_path, ca_swaps=swaps)["breaks"]["_ca"] == []

    def test_rows_from_before_band_sensor_use_the_sleep_window(self):
        rows = [_row(NOW - timedelta(minutes=30 * k), 70.0) for k in range(48, -1, -1)]  # no band_sensor field
        swaps = pa.ca_swap_epochs(
            rows, (NOW - timedelta(hours=24)).timestamp(), NOW.timestamp(),
            tz=TZ, sleep_sensor=BED, sleep_time="22:30", wake_time="07:00",
        )  # fmt: skip
        assert [datetime.fromtimestamp(s, TZ).strftime("%H:%M") for s in swaps] == ["22:30", "07:00"]

    def test_daily_means_get_no_breaks(self, tmp_path):
        v4 = _chart(tmp_path, range_str="1y", start=(NOW - timedelta(days=365)).timestamp(), ca_swaps=[NOW.timestamp()])
        assert "_ca" not in v4["breaks"]  # a daily mean is no one sensor's reading: nothing to break

    def test_per_sensor_lines_still_draw_and_ca_comes_from_the_primary_without_a_sleep_node(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        epoch = slot_key(NOW - timedelta(hours=1))
        arch.put(HALL, epoch, "4h", 70.2)
        arch.put(BED, epoch, "4h", 66.2)
        arch.set_ca_source(HALL)
        pts = [(NOW + timedelta(minutes=15 * (k + 1)), 70.0) for k in range(8)]
        fut = {"at": NOW.isoformat(), "predictions": {HALL: pts, BED: pts}, "corrected": []}
        model = {**CA_ONLY_MODEL, "primary": HALL}
        v4 = _chart(tmp_path, archive=arch, model=model, last_future=fut, sensor_ids=[HALL, BED])
        assert v4["sensors"] == ["_ca", HALL, BED]
        assert v4["past"]["_ca"]["4h"][0]["temp"] == 70.2 and v4["future"]["_ca"][0]["temp"] == 70.0

    def test_the_chart_path_computes_the_swaps_from_the_zone_config(self, tmp_path, monkeypatch):
        from tests.test_chart_v4_r2 import _get_coordinator_class  # noqa: PLC0415

        cls = _get_coordinator_class()
        coord = types.SimpleNamespace(
            _v4_store=types.SimpleNamespace(v4_model={"primary": "_ca", "sensors": ["_ca"], "confidence": "ok"}),
            _v4_archive=_ca_archive(tmp_path),
            _v4_last_future=_ca_future(),
            _entry_id="zone1",
            hass=types.SimpleNamespace(config=types.SimpleNamespace(config_dir=str(tmp_path))),
            config={"sleep_indoor_temp_entity": BED, "sleep_time": "22:30", "wake_time": "07:00"},
            engine_running=lambda _e: True,
            v4_model_summary=lambda: CA_ONLY_MODEL,
        )
        coord._chart_ca_swaps = types.MethodType(cls._chart_ca_swaps, coord)
        out = cls.v4_chart_series(
            coord,
            now=NOW,
            start=(NOW - timedelta(hours=24)).timestamp(),
            end=NOW.timestamp(),
            range_str="24h",
            unit="fahrenheit",
            historical=False,
            sensor_ids=[],
            raw_log=_night_rows(),
        )
        assert out["sensors"] == ["_ca"] and out["future"]["_ca"]
        # the 24 h window plus the chart's look-ahead and a day (8 d, Issue #1483): two swaps a day over 9 days
        assert 16 <= len(out["breaks"]["_ca"]) <= 20


# ----------------------------------------------------------------------------------------------- 2. the archive
class TestArchiveCaOnly:
    def test_a_ca_only_model_archives_its_predictions(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        pts = [(NOW + timedelta(minutes=15 * (k + 1)), 69.0) for k in range(96)]
        assert arch.record(NOW, {"_ca": pts}, "_ca") == 4  # 1/4/12/24 h
        assert arch.sensor_ids() == ["_ca"] and arch.slot_count() == {"_ca": 4}
        assert arch.series("_ca", "4h", 0, 1e12) == {slot_key(NOW + timedelta(hours=4)): 69.0}
        arch.save()
        again = V4Archive(tmp_path, entry_id="zone1")
        again.load()
        assert again.series("_ca", "4h", 0, 1e12) == {slot_key(NOW + timedelta(hours=4)): 69.0}

    def test_a_node_model_still_derives_ca_and_stores_no_duplicate(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        pts = [(NOW + timedelta(minutes=15 * (k + 1)), 69.0) for k in range(96)]
        arch.record(NOW, {HALL: pts, "_ca": pts}, HALL)
        assert "_ca" not in arch._lines and arch.series("_ca", "4h", 0, 1e12) == arch.series(HALL, "4h", 0, 1e12)

    def test_gaining_a_node_keeps_the_older_ca_only_history(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        for k in range(10):  # 5 h of a _ca-only model
            arch.put("_ca", slot_key(NOW + timedelta(minutes=30 * k)), "4h", 68.0)
        later = NOW + timedelta(hours=3)
        for k in range(10):  # then the node model: its slots overlap the last 4 of the _ca line
            arch.put(HALL, slot_key(later + timedelta(minutes=30 * k)), "4h", 70.0)
        arch.set_ca_source(HALL)
        got = arch.series("_ca", "4h", 0, 1e12)
        assert len(got) == 16  # 6 older _ca-only slots kept + 10 derived from the node
        assert got[slot_key(NOW)] == 68.0 and got[slot_key(later)] == 70.0
        assert len(arch._lines["_ca"].slots()) == 6  # the overlapping duplicates were dropped

    def test_m4_memory_limit_with_a_stored_ca_line_beside_three_nodes(self, tmp_path):
        n_slots = 400 * 48
        base = int(NOW.timestamp()) // 1800 * 1800 - n_slots * 1800
        arch = V4Archive(tmp_path, entry_id="zone1")
        tracemalloc.start()
        before, _ = tracemalloc.get_traced_memory()
        for sensor in ("_ca", HALL, BED, "sensor.b"):  # worst case: a full year of _ca-only AND node lines
            for k in range(n_slots):
                for lead in V4_ARCHIVE_LEADS:
                    arch.put(sensor, base + k * 1800, lead, 68.0)
        after, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        held = after - before
        assert held < 5 * 1024 * 1024, f"{held / 1e6:.1f} MB for 4 lines x 400 days x 4 leads"
        assert arch.memory_bytes() < 5 * 1024 * 1024

    def test_a_live_shaped_cycle_archives_more_than_zero(self, monkeypatch):
        """The 0.9.4 shape: primary "_ca", sensors ["_ca"] -> the cycle's ``archived`` is no longer 0."""
        fake = install(monkeypatch)
        from tests.test_v4_shadow_wiring import _build, _cycle, _fake_model_dict, _seed_cycle_inputs  # noqa: PLC0415

        coord, _hass, scheduler = _build()
        coord._v4_store.v4_model = _fake_model_dict(coord, primary="_ca", sensors=["_ca"])
        _seed_cycle_inputs(coord, scheduler)
        _cycle(coord, scheduler)
        assert fake.predict_calls and coord._v4_last_cycle["predicted"] is True
        # the seeded forecast reaches about 60 h: 1/4/12/24 h and, since Issue #1483, 48 h (not 72 h / 5 d)
        assert coord._v4_last_cycle["archived"] == 5
        assert coord._v4_archive.slot_count() == {"_ca": 5}
        assert coord._v4_last_future["predictions"]["_ca"]


# --------------------------------------------------------------------------------- 3. + 5. the accuracy endpoint
def _accuracy(tmp_path: Path, **over) -> dict:
    rows = []
    for k in range(48 * 3, -1, -1):
        ts = NOW - timedelta(minutes=30 * k)
        rows.append(_row(ts, 68.0 + (k % 8) * 0.25))
    kw = dict(
        zone="zone1",
        sensor="_ca",
        lead="4h",
        range_str="24h",
        before=None,
        now=NOW,
        unit="fahrenheit",
        chart_rows=rows,
        archive=V4Archive(tmp_path, entry_id="zone1"),  # live: empty
        backtest=None,  # live: no backtest file
        model=CA_ONLY_MODEL,
        sensors_meta=[
            {"id": "_ca", "name": "Climate Advisor reading", "role": "the reading Climate Advisor uses"},
            {"id": "_ca", "name": "_ca", "role": "room sensor"},  # what a _ca-only model's sensor list added
        ],
    )
    kw.update(over)
    return pa.build_prediction_accuracy(**kw)


class TestAccuracyEachEngineOnItsOwn:
    def test_empty_v4_archive_still_scores_v3_and_no_change(self, tmp_path):
        out = _accuracy(tmp_path)
        st = out["stats"]
        assert st["n"] == 0 and st["v4_mae"] is None
        assert st["n_v3"] == 48 and st["v3_mae"] == pytest.approx(1.0) and st["v3_bias"] == pytest.approx(1.0)
        assert st["n_flat"] == 48 and st["flat_mae"] is not None
        assert out["points"] and all("v4" not in p for p in out["points"])
        assert all("actual" in p and "v3" in p and "flat" in p for p in out["points"])

    def test_other_leads_and_sensors_still_get_the_no_change_baseline(self, tmp_path):
        out = _accuracy(tmp_path, lead="12h")
        assert out["stats"]["v3_mae"] is None and out["stats"]["n_flat"] == 48 and out["stats"]["flat_mae"] is not None

    def test_v4_joins_alongside_once_it_has_points(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        for k in range(1, 11):
            arch.put("_ca", slot_key(NOW - timedelta(minutes=30 * k)), "4h", 70.0)
        st = _accuracy(tmp_path, archive=arch)["stats"]
        assert st["n"] == 10 and st["v4_mae"] is not None and st["n_v3"] == 48  # v3 keeps its own 48 points

    def test_the_sensor_list_has_ca_once(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="zone1")
        arch.put("_ca", slot_key(NOW - timedelta(hours=1)), "4h", 70.0)  # the archive lists "_ca" too
        ids = [s["id"] for s in _accuracy(tmp_path, archive=arch)["sensors"]]
        assert ids == ["_ca"]

    def test_the_coordinator_does_not_list_ca_twice_for_a_ca_only_model(self, tmp_path):
        from tests.test_chart_v4_r2 import _get_coordinator_class  # noqa: PLC0415

        cls = _get_coordinator_class()
        coord = types.SimpleNamespace(
            _v4_store=types.SimpleNamespace(v4_model={"primary": "_ca", "sensors": ["_ca"]}),
            _read_chart_indoor_sensors=lambda: [HALL],
            v4_indoor_sensors_meta=lambda ids: [{"id": i, "name": i, "role": "room sensor"} for i in ids],
            config={},
            _entry_id="zone1",
            hass=types.SimpleNamespace(config=types.SimpleNamespace(config_dir=str(tmp_path))),
            _chart_log=types.SimpleNamespace(raw_entries=lambda: []),
            _v4_archive=None,
            v4_model_summary=lambda: {},
            primary_engine_id=lambda: "v4",
            shadow_engine_id=lambda: "none",
        )
        meta = cls.v4_accuracy_inputs(coord)["sensors_meta"]
        assert [m["id"] for m in meta] == ["_ca", HALL]


def test_swap_helper_handles_a_bedtime_after_midnight_and_bad_times():
    lo, hi = datetime(2026, 11, 7, 0, 0, tzinfo=TZ).timestamp(), datetime(2026, 11, 7, 23, 59, tzinfo=TZ).timestamp()
    got = pa.ca_swap_epochs([], lo, hi, tz=TZ, sleep_sensor=BED, sleep_time="01:00", wake_time="07:00")
    assert [datetime.fromtimestamp(s, TZ).strftime("%H:%M") for s in got] == ["01:00", "07:00"]
    assert pa.ca_swap_epochs([], lo, hi, tz=TZ, sleep_sensor=BED, sleep_time="bad", wake_time="07:00") == []
    assert pa.ca_swap_epochs([], lo, hi, tz=TZ, sleep_sensor=BED, sleep_time="07:00", wake_time="07:00") == []


# ------------------------------------------------------------- follow-up: swap breaks independent of the engine
class TestTopLevelCaSwaps:
    """With v4 switched off (v3 primary, no comparison) the actual CA reading and v3's line still break at swaps."""

    def _chart(self, tmp_path, primary="v3", range_str="24h"):
        from tests.test_chart_v4_r2 import _chart, _coord  # noqa: PLC0415

        coord = _coord(tmp_path)
        coord.config.update(primary_engine=primary, shadow_engine="none", sleep_time="22:30", wake_time="07:00")
        return _chart(coord, range_str)

    def test_v4_off_still_gets_the_swaps_at_the_top_level(self, tmp_path):
        out = self._chart(tmp_path)
        assert out["v4"]["reason"] == "not_running" and "breaks" not in out["v4"]
        times = sorted({datetime.fromisoformat(t).strftime("%H:%M") for t in out["ca_swaps"]})
        assert times == ["07:00", "22:30"]
        # past window plus the chart's longest look-ahead and a day (Issue #1483: 7 d + 1): two swaps a day over 9 days
        assert 16 <= len(out["ca_swaps"]) <= 20

    def test_v4_alias_agrees_with_the_top_level_key(self, tmp_path):
        from tests.test_chart_v4_r2 import _archive_with, _chart, _coord, _future, _model  # noqa: PLC0415

        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future())
        coord.config.update(sleep_time="22:30", wake_time="07:00")
        out = _chart(coord)
        alias = out["v4"]["breaks"]["_ca"]
        assert alias and set(alias) <= set(out["ca_swaps"])  # the same swaps; v4's stop at its 24 h horizon

    def test_no_swaps_at_daily_resolution(self, tmp_path):
        assert self._chart(tmp_path, range_str="1y")["ca_swaps"] == []
