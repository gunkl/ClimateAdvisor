"""Household-promise counters are visible on the Debug tab (Issue #1128 I8, Observability Requirements).

Occupant first: a promise that only logs is invisible to the person who runs the house.  The
counters (how many times each promise was found broken, and when last) must reach the Debug tab
through the real status/debug API, as one line in the existing card -- no new card, no raw
evidence, and nothing at all when no promise has ever been missed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.climate_advisor.api import ClimateAdvisorAutomationStateView
from custom_components.climate_advisor.engine_inputs import TouAvWindow  # noqa: E402

from .test_api import _make_view_request, _simulate_status_get
from .test_nat_vent_dashboard_target import _make_nat_vent_coord_stub

_INDEX_HTML = (
    Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html"
).read_text(encoding="utf-8")


def _coord(stats):
    coord = _make_nat_vent_coord_stub(config={})
    coord._promise_stats = stats
    coord.automation_engine._current_thermostat_family = MagicMock(return_value=None)
    return coord


def _stats():
    """Issue #1490: day-bucketed counters dated relative to today, so the summary's 30-day prune keeps them."""
    from datetime import timedelta  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    now = dt_util.now()
    yday = now - timedelta(days=1)
    return {
        "observe_only_silence": {"days": {now.date().isoformat(): 2}, "last_ts": now.isoformat()},
        "settling_flip": {"days": {yday.date().isoformat(): 1}, "last_ts": yday.isoformat()},
    }


class TestPromiseSummary:
    def test_nothing_missed_is_null(self):
        assert _coord({})._promise_summary() is None
        assert _coord(None)._promise_summary() is None
        assert _coord({"settling_flip": {"hits": 0, "first_ts": None, "last_ts": None}})._promise_summary() is None
        assert _coord({"settling_flip": {"days": {"2020-01-01": 4}, "hits": 4}})._promise_summary() is None

    def test_garbage_and_unknown_keys_are_ignored(self):
        stats = {"not_a_promise": {"hits": 9}, "settling_flip": "bad", "observe_only_silence": {"hits": True}}
        assert _coord(stats)._promise_summary() is None

    def test_counts_per_promise_and_last_hit_only(self):
        stats = _stats()
        summary = _coord(stats)._promise_summary()
        assert summary["total_hits"] == 3
        assert summary["window_days"] == 30  # Issue #1490: the counts are the last 30 days
        rows = {r["promise"]: r for r in summary["promises"]}
        assert rows["observe_only_silence"]["hits"] == 2
        assert rows["observe_only_silence"]["last_hit"] == stats["observe_only_silence"]["last_ts"]
        assert rows["settling_flip"]["hits"] == 1
        # compact: counts, a label and the last-hit time -- no evidence, no first-seen time, no entity ids
        assert all(set(r) == {"promise", "label", "hits", "last_hit"} for r in summary["promises"])
        json.dumps(summary)  # JSON-safe


class TestPromiseSummaryThroughTheRealApi:
    def test_debug_view_returns_the_summary(self):
        coord = _coord(_stats())
        request = _make_view_request(coord)
        body = asyncio.run(ClimateAdvisorAutomationStateView().get(request)).json_data
        assert body["promise_summary"]["total_hits"] == 3

    def test_debug_view_returns_null_when_nothing_missed(self):
        request = _make_view_request(_coord({}))
        assert asyncio.run(ClimateAdvisorAutomationStateView().get(request)).json_data["promise_summary"] is None

    @staticmethod
    def _status(data):
        coord = MagicMock()
        coord.config = {"comfort_heat": 68.0, "comfort_cool": 74.0}
        coord.data = data
        coord._get_indoor_temp.return_value = 70.0
        coord._last_outdoor_temp = None
        coord.automation_enabled = True
        coord._occupancy_mode = "home"
        coord.current_classification = None
        ae = MagicMock()
        ae._active_tou_av_window.return_value = TouAvWindow()
        ae._manual_override_active = False
        ae._override_confirm_pending = False
        ae._fan_override_active = False
        ae._pre_condition_achieved = False
        ae.is_paused_by_door = False
        coord.automation_engine = ae
        coord._compute_contact_details.return_value = []
        return _simulate_status_get(coord, None)

    def test_status_api_carries_the_coordinator_data_summary(self):
        summary = _coord(_stats())._promise_summary()
        assert self._status({"promise_summary": summary})["promise_summary"] == summary

    def test_status_api_is_null_without_a_summary(self):
        assert self._status({})["promise_summary"] is None


class TestDebugTabLine:
    def test_one_value_line_in_the_existing_card_shown_only_when_present(self):
        assert _INDEX_HTML.count("Household Promises Missed") == 1
        i = _INDEX_HTML.index("Household Promises Missed")
        block = _INDEX_HTML[_INDEX_HTML.rindex("${(() => {", 0, i) : i]
        assert "data.promise_summary" in block
        assert "return '';" in block  # nothing rendered when null/empty
        # content is escaped before it reaches innerHTML
        assert "escapeHtml(String(p.label" in _INDEX_HTML and "escapeHtml(String(p.hits))" in _INDEX_HTML
        # inside the first Debug-tab card (before the Door/Window Sensors card), not a new card
        card_start = _INDEX_HTML.rindex('<div class="debug-section">', 0, i)
        assert _INDEX_HTML.index("Automation Enabled") > card_start
        assert "<h3>" not in _INDEX_HTML[card_start:i].split("Automation Enabled")[0]

    def test_coordinator_data_dict_exposes_the_summary(self):
        src = (
            Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"
        ).read_text(encoding="utf-8")
        assert '"promise_summary": self._promise_summary()' in src
