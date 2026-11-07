"""0.9.6 Lane F: ``v4.past_span`` lets the Status chart say "none in this view" instead of "none yet".

Occupant view: scrolled back past the oldest v4 prediction (or zoomed onto a stretch without one), the chart's key
said "v4 has no past predictions to show yet" although v4 had days of them a scroll away. The chart payload's ``past``
only carries the fetched window, so the page could not tell "none here" from "none at all". ``past_span`` is the span
of every past v4 prediction (live archive and backtest, any look-ahead), independent of the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.const import V4_ARCHIVE_LEADS
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=TZ)
MODEL = {"has_model": True, "confidence": "ok", "fitted_at": "2026-11-07T03:15:00-08:00", "primary": "_ca"}
FINER = "sensor.hallway_finer"


def _archive(tmp_path: Path, first_h: float, last_h: float) -> V4Archive:
    """_ca predictions every 30 min from ``first_h`` to ``last_h`` hours before NOW."""
    arch = V4Archive(tmp_path, entry_id="zone1")
    k = int(first_h * 2)
    while k >= int(last_h * 2):
        for lead in V4_ARCHIVE_LEADS:
            arch.put("_ca", slot_key(NOW - timedelta(minutes=30 * k)), lead, 68.5)
        k -= 1
    return arch


def _backtest(first_h: float, last_h: float) -> dict:
    """A published backtest with hourly rows for the finer sensor between those hours before NOW."""
    rows = [{"t": (NOW - timedelta(hours=h)).isoformat(), "v4": 69.0} for h in range(int(first_h), int(last_h) - 1, -1)]
    return {"points": {FINER: {lead: rows for lead in V4_ARCHIVE_LEADS}}}


def _chart(*, start_h: float, end_h: float, archive=None, backtest=None, historical=False) -> dict:
    return pa.build_chart_v4(
        now=NOW,
        start=(NOW - timedelta(hours=start_h)).timestamp(),
        end=(NOW - timedelta(hours=end_h)).timestamp(),
        range_str="24h",
        unit="fahrenheit",
        historical=historical,
        archive=archive,
        backtest=backtest,
        model=MODEL,
        last_future=None,
        sensor_ids=[FINER],
    )


def _h(iso: str) -> float:
    return round((NOW - datetime.fromisoformat(iso)).total_seconds() / 3600, 2)


def test_a_window_with_past_points_reports_the_whole_span(tmp_path):
    v4 = _chart(start_h=24, end_h=0, archive=_archive(tmp_path, 72, 1), backtest=_backtest(96, 2))
    assert v4["past"]["_ca"]["4h"] and v4["past"][FINER]["4h"]
    # the span covers every prediction, not just the 24 h window: backtest from 96 h ago, archive up to 1 h ago
    assert (_h(v4["past_span"]["first"]), _h(v4["past_span"]["last"])) == (96.0, 1.0)
    assert v4["past_span"]["sensors"] == ["_ca", FINER]


def test_a_sensor_with_past_only_outside_the_window_is_still_listed(tmp_path):
    """The backtest ended 8 h ago: a 6 h window has no past for the finer sensor, but its chip must stay."""
    v4 = _chart(start_h=6, end_h=0, backtest=_backtest(96, 8))
    assert FINER not in v4["past"]
    assert v4["past_span"]["sensors"] == [FINER] and _h(v4["past_span"]["last"]) == 8.0


def test_the_owners_case_a_scrolled_window_with_none_still_knows_v4_has_some(tmp_path):
    """Scrolled back 10 days: the window has no v4 predictions, but v4 has them (the last 3 days)."""
    v4 = _chart(start_h=10 * 24 + 24, end_h=10 * 24, archive=_archive(tmp_path, 72, 1), historical=True)
    assert v4["past"] == {}
    assert v4["past_span"] is not None
    assert (_h(v4["past_span"]["first"]), _h(v4["past_span"]["last"])) == (72.0, 1.0)


def test_nothing_anywhere_is_none(tmp_path):
    v4 = _chart(start_h=24, end_h=0, archive=V4Archive(tmp_path, entry_id="zone1"))
    assert v4["past"] == {} and v4["past_span"] is None
    assert _chart(start_h=24, end_h=0)["past_span"] is None  # no archive, no backtest


def test_the_window_filter_is_unchanged(tmp_path):
    """The span rides the same loop; the window's own past lines still keep only start <= t < end."""
    v4 = _chart(start_h=6, end_h=0, archive=_archive(tmp_path, 72, 1))
    pts = v4["past"]["_ca"]["1h"]
    assert pts and all(0 < _h(p["ts"]) <= 6 for p in pts)
    assert len(pts) == 11  # 6.0 h .. 1.0 h ago every 30 min
