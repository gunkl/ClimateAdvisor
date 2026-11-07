"""The timeline endpoint and the coordinator readers of the timeline (Issue #1230 R3b).

Occupant view: the dashboard asks `GET /api/climate_advisor/timeline` for the plan and the Analysis tab shows tomorrow's
plan; both must be the one timeline the cards read, in the occupant's temperature unit, and an install with
several zones
must never answer for the wrong house.
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.api import API_VIEWS, ClimateAdvisorTimelineView
from custom_components.climate_advisor.const import API_TIMELINE, DOMAIN
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.operation_timeline import build_timeline, detail_dict, to_dict
from custom_components.climate_advisor.temperature import display_temp
from custom_components.climate_advisor.timeline_surfaces import timeline_api_payload
from tests.timeline_fixtures import TZ, make_snapshot

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=TZ)


def _timeline(**kw):
    return build_timeline(make_snapshot("mild", now=NOW, tomorrow="warm", **kw))


def _request(coordinators: dict, query: dict | None = None):
    hass = MagicMock()
    hass.data = {DOMAIN: coordinators}
    hass.config_entries.async_entries = MagicMock(return_value=[])
    req = MagicMock()
    req.app = {"hass": hass}
    req.method, req.path, req.remote = "GET", API_TIMELINE, "127.0.0.1"
    req.rel_url.query = query or {}
    return req


def _coord(payload):
    c = MagicMock()
    c.zone_label = "Zone"
    c.timeline_payload.return_value = payload
    return c


def test_the_view_is_registered_and_authenticated() -> None:
    assert ClimateAdvisorTimelineView in API_VIEWS
    assert ClimateAdvisorTimelineView.requires_auth is True
    assert ClimateAdvisorTimelineView.url == "/api/climate_advisor/timeline"


def test_the_view_returns_the_coordinators_timeline_payload() -> None:
    tl = _timeline()
    payload = timeline_api_payload(to_dict(tl), detail_dict(tl), "celsius", display_temp)
    resp = asyncio.run(ClimateAdvisorTimelineView().get(_request({"zoneA": _coord(payload)}, {"zone": "zoneA"})))
    body = json.loads(json.dumps(resp.json_data))
    assert resp.status == 200 and body["unit"] == "celsius" and body["steps"]


def test_the_view_says_503_before_the_first_timeline_and_when_unloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = asyncio.run(ClimateAdvisorTimelineView().get(_request({"zoneA": _coord(None)}, {"zone": "zoneA"})))
    assert resp.status == 503
    resp = asyncio.run(ClimateAdvisorTimelineView().get(_request({})))
    assert resp.status == 503


def test_an_ambiguous_zone_is_a_400_not_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.zone_registry.ir.async_create_issue", MagicMock())
    monkeypatch.setattr("custom_components.climate_advisor.api.zone_registry.list_zones", lambda _h: [])
    a, b = _coord({"x": 1}), _coord({"x": 2})
    resp = asyncio.run(ClimateAdvisorTimelineView().get(_request({"zoneA": a, "zoneB": b})))
    assert resp.status == 400 and resp.json_data["zone_selection_required"] is True
    assert not a.timeline_payload.called and not b.timeline_payload.called
    resp = asyncio.run(ClimateAdvisorTimelineView().get(_request({"zoneA": a, "zoneB": b}, {"zone": "zoneB"})))
    assert resp.status == 200 and b.timeline_payload.called and not a.timeline_payload.called


def test_the_payload_converts_temperatures_to_the_display_unit_and_keeps_the_contract() -> None:
    tl = _timeline()
    raw = to_dict(tl)
    f = timeline_api_payload(raw, detail_dict(tl), "fahrenheit", display_temp)
    c = timeline_api_payload(raw, detail_dict(tl), "celsius", display_temp)
    assert f["unit"] == "fahrenheit" and c["unit"] == "celsius"
    assert [s["kind"] for s in f["steps"]] == [s["kind"] for s in c["steps"]] == [s.kind.value for s in tl.steps]
    band_step = next(s for s in raw["steps"] if s["target_f"] is not None)
    i = raw["steps"].index(band_step)
    assert f["steps"][i]["target_f"] == band_step["target_f"]
    assert c["steps"][i]["target_f"] == display_temp(band_step["target_f"], "celsius")
    assert f["path"][0]["temp_f"] != c["path"][0]["temp_f"] or raw["path"] == []
    assert set(raw) <= set(f), "the frozen contract keys are all still there"


def _plan_coord(tl):
    coord = MagicMock()
    base = {
        "date": "2026-11-05",
        "day_type": "warm",
        "trend_direction": "stable",
        "hvac_mode": "off",
        "windows_recommended": True,
        "window_open_time": "06:00:00",
        "window_close_time": "10:00:00",
        "pre_condition": False,
        "expected_high": 80,
        "expected_low": 58,
    }
    coord._tomorrow_classification_plan = lambda: dict(base)
    coord._surface_timeline = lambda: tl
    return coord


def test_tomorrows_plan_reads_the_timeline_for_its_window_times(monkeypatch: pytest.MonkeyPatch) -> None:
    tl = _timeline()
    monkeypatch.setattr("custom_components.climate_advisor.coordinator.dt_util.now", lambda: NOW)
    out = ClimateAdvisorCoordinator.tomorrow_plan.fget(_plan_coord(tl))
    tomorrow = (NOW + timedelta(days=1)).date()
    steps = [s for s in tl.steps if s.actor == "you" and s.ts.date() == tomorrow]
    assert steps, "fixture: the timeline holds tomorrow's window plan"
    assert out["windows_recommended"] is True
    assert out["window_open_time"] == steps[0].ts.time().isoformat()
    assert out["window_close_time"] == steps[-1].ts.time().isoformat()
    assert out["day_type"] == "warm" and out["expected_high"] == 80.0 or out["expected_high"] == 80


def test_tomorrows_plan_shows_no_windows_when_the_timeline_holds_none(monkeypatch: pytest.MonkeyPatch) -> None:
    tl = build_timeline(make_snapshot("mild", now=NOW, tomorrow="cold"))
    monkeypatch.setattr("custom_components.climate_advisor.coordinator.dt_util.now", lambda: NOW)
    coord = _plan_coord(tl)
    out = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)
    assert out["windows_recommended"] is False and out["window_open_time"] is None and out["window_close_time"] is None


def test_without_a_timeline_tomorrows_plan_is_the_classification_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    coord = _plan_coord(None)
    out = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)
    assert out["window_open_time"] == "06:00:00" and out["windows_recommended"] is True
    assert functools  # (import kept for parity with test_daily_record's binding helper)
